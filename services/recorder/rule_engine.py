#!/usr/bin/env python3
"""Automatic request modification rule core engine (DEV-040).

Provides:
- Persistent rule CRUD, copy, enable/disable, priority, and deterministic ordering
  (`priority ASC, created_at ASC, rule_id ASC`).
- Save-time validation of match criteria (including regex compilation) and action schemas.
- Request-time rule matching and ordered sequential application across query, headers,
  cookies, nested JSON paths, and URL-encoded form pairs (preserving duplicate keys
  and exact pair ordering).
- Human-readable decoded action inputs with RFC 3986 wire re-encoding, hop-by-hop header
  protection, and automatic Content-Length recalculation.
- Fail-open runtime semantics: malformed JSON/form bodies, action errors, WebSocket
  handshakes, and unsupported body types never break proxy forwarding or corrupt the
  original request, while recording explicit applied/skipped/failed rule metadata.
- Dry-run / preview evaluation against either a stored capture or an inline request
  without sending traffic to any target or creating captures.
"""

from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, unquote
import uuid

from draft_replay import (
    DraftValidationError,
    apply_json_path_mutations,
    apply_pair_mutations,
    build_raw_query_from_entries,
    decode_path_human_readable,
    encode_decoded_path,
    encode_uri_component,
    entries_from_pairs,
    parse_raw_query_entries,
    sync_cookies_into_headers,
)
from parser import parse_cookies_ordered

if TYPE_CHECKING:
    from index_store import CaptureIndexStore

RULE_ID_RE = re.compile(r"^[A-Za-z0-9:._-]{1,120}$")

# Hop-by-hop headers stripped when normalizing modified requests for forwarding
HOP_BY_HOP_HEADERS = {
    "connection",
    "transfer-encoding",
    "proxy-connection",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
}

ALLOWED_MATCH_KEYS = {
    "route_id",
    "route_ids",
    "domain",
    "domains",
    "host",
    "hosts",
    "method",
    "methods",
    "path",
    "path_exact",
    "path_prefix",
    "path_regex",
    "query",
    "query_pairs",
    "query_match",
    "headers",
    "header_pairs",
    "header_match",
    "content_type",
    "content_types",
}

ALLOWED_ACTION_TARGETS = {"query", "header", "cookie", "json", "form"}


class RuleValidationError(ValueError):
    """Raised when a modification rule fails save-time schema or regex validation."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_rule",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.details,
            "error": self.code,
            "message": str(self),
        }


class RuleNotFoundError(KeyError):
    """Raised when a rule_id does not exist in modification_rules."""

    def __init__(self, rule_id: str) -> None:
        super().__init__(f"Modification rule not found: {rule_id}")
        self.rule_id = rule_id


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_rule_id(rule_id: Any) -> str:
    if not isinstance(rule_id, str):
        raise RuleValidationError(
            "rule_id must be a string", code="invalid_rule_id"
        )
    cleaned = rule_id.strip()
    if not cleaned or not RULE_ID_RE.match(cleaned):
        raise RuleValidationError(
            f"Invalid rule_id format: {rule_id!r}", code="invalid_rule_id"
        )
    return cleaned


def validate_priority(priority: Any) -> int:
    if isinstance(priority, bool) or priority is None:
        raise RuleValidationError(
            "priority must be an integer", code="invalid_priority"
        )
    try:
        val = int(priority)
    except (TypeError, ValueError) as exc:
        raise RuleValidationError(
            f"Invalid priority {priority!r}: must be an integer",
            code="invalid_priority",
        ) from exc
    if val < -1_000_000 or val > 1_000_000:
        raise RuleValidationError(
            f"priority {val} out of range (-1000000..1000000)",
            code="invalid_priority",
        )
    return val


def _normalize_str_list(val: Any, field_name: str, *, upper: bool = False, lower: bool = False) -> list[str]:
    if isinstance(val, str):
        items = [val]
    elif isinstance(val, list):
        items = val
    else:
        raise RuleValidationError(
            f"match.{field_name} must be a string or list of strings",
            code="invalid_match_schema",
        )
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise RuleValidationError(
                f"match.{field_name} entries must be non-empty strings",
                code="invalid_match_schema",
            )
        s = item.strip()
        if upper:
            s = s.upper()
        elif lower:
            s = s.lower()
        out.append(s)
    return out


def _normalize_pair_matchers(raw_list: Any, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(raw_list, list):
        raise RuleValidationError(
            f"match.{field_name} must be a list of pair matchers",
            code="invalid_match_schema",
        )
    normalized: list[dict[str, Any]] = []
    for item in raw_list:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            name = str(item[0]).strip()
            if not name:
                raise RuleValidationError(
                    f"match.{field_name} pair name cannot be empty",
                    code="invalid_match_schema",
                )
            normalized.append({"name": name, "value": str(item[1])})
        elif isinstance(item, dict):
            allowed = {"name", "key", "value", "value_regex", "index", "absent", "present"}
            unknown = set(item.keys()) - allowed
            if unknown:
                raise RuleValidationError(
                    f"Unknown keys in match.{field_name} item: {sorted(unknown)}",
                    code="invalid_match_schema",
                )
            raw_name = item.get("name", item.get("key"))
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise RuleValidationError(
                    f"match.{field_name} item requires non-empty 'name'",
                    code="invalid_match_schema",
                )
            entry: dict[str, Any] = {"name": raw_name.strip()}
            if "value" in item and item["value"] is not None:
                if not isinstance(item["value"], (str, int, float, bool)):
                    raise RuleValidationError(
                        f"match.{field_name} value must be a scalar string",
                        code="invalid_match_schema",
                    )
                entry["value"] = str(item["value"])
            if "value_regex" in item and item["value_regex"] is not None:
                pat = item["value_regex"]
                if not isinstance(pat, str) or not pat:
                    raise RuleValidationError(
                        f"match.{field_name}.value_regex must be a non-empty string",
                        code="invalid_regex",
                    )
                try:
                    re.compile(pat)
                except re.error as exc:
                    raise RuleValidationError(
                        f"Invalid regex in match.{field_name}.value_regex {pat!r}: {exc}",
                        code="invalid_regex",
                        details={"pattern": pat, "error": str(exc)},
                    ) from exc
                entry["value_regex"] = pat
            if "index" in item and item["index"] is not None:
                if isinstance(item["index"], bool):
                    raise RuleValidationError(
                        f"match.{field_name}.index must be an integer",
                        code="invalid_match_schema",
                    )
                try:
                    entry["index"] = int(item["index"])
                except (TypeError, ValueError) as exc:
                    raise RuleValidationError(
                        f"match.{field_name}.index must be an integer",
                        code="invalid_match_schema",
                    ) from exc
            if "absent" in item and item["absent"] is not None:
                entry["absent"] = bool(item["absent"])
            elif "present" in item and item["present"] is not None:
                entry["absent"] = not bool(item["present"])
            normalized.append(entry)
        else:
            raise RuleValidationError(
                f"Invalid item in match.{field_name}: {item!r}",
                code="invalid_match_schema",
            )
    return normalized


def validate_and_normalize_match(match_input: Any) -> dict[str, Any]:
    if match_input is None:
        return {}
    if not isinstance(match_input, dict):
        raise RuleValidationError(
            "match must be a JSON object", code="invalid_match_schema"
        )
    unknown_keys = set(match_input.keys()) - ALLOWED_MATCH_KEYS
    if unknown_keys:
        raise RuleValidationError(
            f"Unsupported match fields: {sorted(unknown_keys)}",
            code="invalid_match_schema",
            details={"unsupported_fields": sorted(unknown_keys)},
        )

    out: dict[str, Any] = {}

    # route_id / route_ids
    routes: list[str] = []
    if match_input.get("route_id") is not None:
        routes.extend(_normalize_str_list(match_input["route_id"], "route_id"))
    if match_input.get("route_ids") is not None:
        routes.extend(_normalize_str_list(match_input["route_ids"], "route_ids"))
    if routes:
        out["route_ids"] = list(dict.fromkeys(routes))

    # domain / domains / host / hosts
    domains: list[str] = []
    for k in ("domain", "domains", "host", "hosts"):
        if match_input.get(k) is not None:
            domains.extend(_normalize_str_list(match_input[k], k, lower=True))
    if domains:
        out["domains"] = list(dict.fromkeys(domains))

    # method / methods
    methods: list[str] = []
    for k in ("method", "methods"):
        if match_input.get(k) is not None:
            m_list = _normalize_str_list(match_input[k], k, upper=True)
            for m in m_list:
                if not m.isalpha():
                    raise RuleValidationError(
                        f"Invalid HTTP method in match: {m!r}",
                        code="invalid_match_schema",
                    )
            methods.extend(m_list)
    if methods:
        out["methods"] = list(dict.fromkeys(methods))

    # path_exact / path
    path_exact = match_input.get(
        "path_exact", match_input.get("path")
    )
    if path_exact is not None:
        if not isinstance(path_exact, str) or not path_exact.strip():
            raise RuleValidationError(
                "match.path_exact must be a non-empty string",
                code="invalid_match_schema",
            )
        p_clean = path_exact.strip()
        if not p_clean.startswith("/"):
            p_clean = "/" + p_clean
        out["path_exact"] = p_clean

    # path_prefix
    path_prefix = match_input.get("path_prefix")
    if path_prefix is not None:
        if not isinstance(path_prefix, str) or not path_prefix.strip():
            raise RuleValidationError(
                "match.path_prefix must be a non-empty string",
                code="invalid_match_schema",
            )
        pref_clean = path_prefix.strip()
        if not pref_clean.startswith("/"):
            pref_clean = "/" + pref_clean
        out["path_prefix"] = pref_clean

    # path_regex
    path_regex = match_input.get("path_regex")
    if path_regex is not None:
        if not isinstance(path_regex, str) or not path_regex:
            raise RuleValidationError(
                "match.path_regex must be a non-empty string",
                code="invalid_regex",
            )
        try:
            re.compile(path_regex)
        except re.error as exc:
            raise RuleValidationError(
                f"Invalid regular expression in match.path_regex {path_regex!r}: {exc}",
                code="invalid_regex",
                details={"pattern": path_regex, "error": str(exc)},
            ) from exc
        out["path_regex"] = path_regex

    # query_pairs
    q_matchers: list[dict[str, Any]] = []
    for k in ("query", "query_pairs", "query_match"):
        if match_input.get(k) is not None:
            q_matchers.extend(_normalize_pair_matchers(match_input[k], k))
    if q_matchers:
        out["query_pairs"] = q_matchers

    # header_pairs
    h_matchers: list[dict[str, Any]] = []
    for k in ("headers", "header_pairs", "header_match"):
        if match_input.get(k) is not None:
            h_matchers.extend(_normalize_pair_matchers(match_input[k], k))
    if h_matchers:
        out["header_pairs"] = h_matchers

    # content_type / content_types
    cts: list[str] = []
    for k in ("content_type", "content_types"):
        if match_input.get(k) is not None:
            cts.extend(_normalize_str_list(match_input[k], k, lower=True))
    if cts:
        out["content_types"] = list(dict.fromkeys(cts))

    return out


def _normalize_single_action(raw: Any, default_target: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuleValidationError(
            f"Each action must be an object, got: {raw!r}",
            code="invalid_action_schema",
        )

    allowed_keys = {
        "target",
        "type",
        "op",
        "action",
        "name",
        "key",
        "path",
        "value",
        "index",
        "occurrence",
        "all",
    }
    unknown = set(raw.keys()) - allowed_keys
    if unknown:
        raise RuleValidationError(
            f"Unknown action fields: {sorted(unknown)}",
            code="invalid_action_schema",
            details={"unsupported_fields": sorted(unknown)},
        )

    raw_target = str(raw.get("target") or raw.get("type") or default_target or "").strip().lower()
    raw_op = str(raw.get("op") or raw.get("action") or "").strip().lower()

    # Support combined shorthand like target="query_set" or op="header_add"
    for candidate in (raw_target, raw_op):
        if "_" in candidate and candidate not in ("set_at", "remove_at"):
            prefix, suffix = candidate.split("_", 1)
            if prefix in ("query", "header", "headers", "cookie", "cookies", "json", "form"):
                raw_target = prefix.rstrip("s") if prefix in ("headers", "cookies") else prefix
                raw_op = suffix
                break

    if raw_target in ("headers", "cookies"):
        raw_target = raw_target[:-1]
    elif raw_target in ("urlencoded_form", "form_urlencoded"):
        raw_target = "form"

    if raw_target not in ALLOWED_ACTION_TARGETS:
        raise RuleValidationError(
            f"Invalid action target {raw_target!r}: must be one of {sorted(ALLOWED_ACTION_TARGETS)}",
            code="invalid_action_target",
        )

    op_map = {
        "add": "add",
        "append": "add",
        "set": "set",
        "set_at": "set_at",
        "update": "set",
        "replace": "set",
        "remove": "remove",
        "remove_at": "remove_at",
        "delete": "remove",
    }
    if raw_op not in op_map:
        raise RuleValidationError(
            f"Invalid action op {raw_op!r} for target {raw_target!r}",
            code="invalid_action_op",
        )
    norm_op = op_map[raw_op]

    if raw_target == "json":
        if norm_op not in ("set", "remove"):
            raise RuleValidationError(
                f"JSON action only supports 'set' or 'remove', got {raw_op!r}",
                code="invalid_action_op",
            )
        path_val = raw.get("path", raw.get("name", raw.get("key")))
        if not isinstance(path_val, str) or not path_val.strip():
            raise RuleValidationError(
                "JSON action requires a non-empty 'path'",
                code="invalid_action_schema",
            )
        clean_path = path_val.strip()
        tokens = [t for t in clean_path.split(".") if t != ""]
        if not tokens or ".." in clean_path:
            raise RuleValidationError(
                f"Invalid JSON path syntax: {path_val!r}",
                code="invalid_json_path",
            )
        act: dict[str, Any] = {
            "target": "json",
            "op": norm_op,
            "path": clean_path,
        }
        if norm_op == "set":
            if "value" not in raw:
                raise RuleValidationError(
                    f"JSON 'set' action on path {clean_path!r} requires 'value'",
                    code="invalid_action_schema",
                )
            act["value"] = raw["value"]
        return act

    # Pair targets: query, header, cookie, form
    name_val = raw.get("name", raw.get("key"))
    idx_val = raw.get("index")
    occ_val = raw.get("occurrence")
    if idx_val is not None:
        if isinstance(idx_val, bool):
            raise RuleValidationError(
                "action.index must be an integer", code="invalid_action_schema"
            )
        try:
            idx_val = int(idx_val)
        except (TypeError, ValueError) as exc:
            raise RuleValidationError(
                f"Invalid action.index {idx_val!r}: must be an integer",
                code="invalid_action_schema",
            ) from exc
    if occ_val is not None:
        if isinstance(occ_val, bool):
            raise RuleValidationError(
                "action.occurrence must be an integer", code="invalid_action_schema"
            )
        try:
            occ_val = int(occ_val)
        except (TypeError, ValueError) as exc:
            raise RuleValidationError(
                f"Invalid action.occurrence {occ_val!r}: must be an integer",
                code="invalid_action_schema",
            ) from exc

    if name_val is not None and str(name_val).strip():
        clean_name = str(name_val).strip()
        if any(ord(c) < 32 or c in ("\r", "\n") for c in clean_name):
            raise RuleValidationError(
                f"Invalid characters in {raw_target} name {clean_name!r}",
                code="invalid_action_schema",
            )
    elif norm_op in ("remove", "remove_at", "set_at") and idx_val is not None:
        clean_name = ""
    else:
        raise RuleValidationError(
            f"{raw_target} action requires a non-empty 'name'",
            code="invalid_action_schema",
        )

    act = {
        "target": raw_target,
        "op": norm_op,
        "name": clean_name,
    }
    if occ_val is not None:
        act["occurrence"] = occ_val
    if idx_val is not None:
        if clean_name and norm_op in ("set", "remove") and occ_val is None:
            act["occurrence"] = idx_val
        else:
            act["index"] = idx_val
    if raw.get("all") is not None:
        act["all"] = bool(raw["all"])

    if norm_op in ("add", "set", "set_at"):
        if "value" not in raw or raw["value"] is None:
            raise RuleValidationError(
                f"{raw_target} '{norm_op}' action for {clean_name!r} requires 'value'",
                code="invalid_action_schema",
            )
        val_str = str(raw["value"])
        if raw_target in ("header", "cookie") and any(c in ("\r", "\n") for c in val_str):
            raise RuleValidationError(
                f"CR/LF characters are not allowed in {raw_target} value",
                code="invalid_action_schema",
            )
        act["value"] = val_str

    return act


def validate_and_normalize_actions(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    raw_actions: list[tuple[Any, str | None]] = []

    if isinstance(payload, list):
        for item in payload:
            raw_actions.append((item, None))
    elif isinstance(payload, dict):
        if "actions" in payload and payload["actions"] is not None:
            if not isinstance(payload["actions"], list):
                raise RuleValidationError(
                    "actions must be a list of action objects",
                    code="invalid_action_schema",
                )
            for item in payload["actions"]:
                raw_actions.append((item, None))

        shorthand_groups = (
            ("query_mutations", "query"),
            ("header_mutations", "header"),
            ("cookie_mutations", "cookie"),
            ("json_mutations", "json"),
            ("form_mutations", "form"),
        )
        for key, target_name in shorthand_groups:
            if key in payload and payload[key] is not None:
                grp = payload[key]
                if not isinstance(grp, list):
                    raise RuleValidationError(
                        f"{key} must be a list", code="invalid_action_schema"
                    )
                for item in grp:
                    raw_actions.append((item, target_name))
    else:
        raise RuleValidationError(
            "actions must be a list of action objects",
            code="invalid_action_schema",
        )

    if not raw_actions:
        raise RuleValidationError(
            "Rule must define at least one action", code="invalid_actions"
        )

    return [_normalize_single_action(item, target) for item, target in raw_actions]


def _match_pair_list(
    matcher: dict[str, Any],
    pairs: list[list[str]],
    *,
    case_insensitive_name: bool,
) -> bool:
    target_name = matcher["name"]
    t_cmp = target_name.lower() if case_insensitive_name else target_name

    matching_values = [
        v
        for k, v in pairs
        if (k.lower() if case_insensitive_name else k) == t_cmp
    ]

    if matcher.get("absent"):
        return len(matching_values) == 0

    if not matching_values:
        return False

    if "index" in matcher and matcher["index"] is not None:
        idx = int(matcher["index"])
        if idx < 0 or idx >= len(matching_values):
            return False
        candidate_values = [matching_values[idx]]
    else:
        candidate_values = matching_values

    if "value" in matcher and matcher["value"] is not None:
        expected_val = str(matcher["value"])
        if not any(v == expected_val for v in candidate_values):
            return False

    if "value_regex" in matcher and matcher["value_regex"] is not None:
        rx = re.compile(matcher["value_regex"])
        if not any(rx.search(v) for v in candidate_values):
            return False

    return True


def evaluate_rule_match(
    match_spec: dict[str, Any],
    *,
    route_id: str,
    method: str,
    authority: str,
    host: str,
    raw_path: str,
    decoded_path: str,
    query_params: list[list[str]],
    headers: list[list[str]],
    content_type: str | None,
) -> bool:
    if not match_spec:
        return True

    if "route_ids" in match_spec:
        rid_short = route_id.removeprefix("route:")
        if not any(
            r == route_id or r == rid_short or f"route:{r}" == route_id
            for r in match_spec["route_ids"]
        ):
            return False

    if "domains" in match_spec:
        auth_l = authority.lower()
        host_l = host.lower()
        if not any(d == host_l or d == auth_l for d in match_spec["domains"]):
            return False

    if "methods" in match_spec:
        if method.upper() not in match_spec["methods"]:
            return False

    if "path_exact" in match_spec:
        pe = match_spec["path_exact"]
        pe_decoded = unquote(pe)
        if raw_path != pe and decoded_path != pe and decoded_path != pe_decoded:
            return False

    if "path_prefix" in match_spec:
        pp = match_spec["path_prefix"]
        pp_decoded = unquote(pp)
        if not (
            raw_path.startswith(pp)
            or decoded_path.startswith(pp)
            or decoded_path.startswith(pp_decoded)
        ):
            return False

    if "path_regex" in match_spec:
        rx = re.compile(match_spec["path_regex"])
        if not (rx.search(decoded_path) or rx.search(raw_path)):
            return False

    if "query_pairs" in match_spec:
        for qm in match_spec["query_pairs"]:
            if not _match_pair_list(qm, query_params, case_insensitive_name=False):
                return False

    if "header_pairs" in match_spec:
        for hm in match_spec["header_pairs"]:
            if not _match_pair_list(hm, headers, case_insensitive_name=True):
                return False

    if "content_types" in match_spec:
        ct_l = (content_type or "").strip().lower()
        base_ct = ct_l.split(";", 1)[0].strip()
        if not any(target_ct == base_ct or target_ct in ct_l for target_ct in match_spec["content_types"]):
            return False

    return True


def _get_header_ci(headers: list[list[str]], name: str) -> str | None:
    nl = name.lower()
    for k, v in headers:
        if k.lower() == nl:
            return v
    return None


def _extract_connection_tokens(headers: list[list[str]]) -> set[str]:
    connection_tokens: set[str] = set()
    for k, v in headers:
        if k.lower() == "connection":
            for tok in v.split(","):
                tc = tok.strip().lower()
                if tc:
                    connection_tokens.add(tc)
    return connection_tokens


def _strip_hop_by_hop_and_rebuild_headers(
    headers: list[list[str]],
    *,
    authority: str,
    body_size: int,
    is_websocket: bool = False,
) -> tuple[list[list[str]], list[list[str]]]:
    if is_websocket:
        return [list(p) for p in headers], []

    connection_tokens = _extract_connection_tokens(headers)

    excluded: list[list[str]] = []
    filtered: list[list[str]] = [["Host", authority]]
    for k, v in headers:
        kl = k.lower()
        if kl in ("host", "content-length"):
            continue
        if kl in HOP_BY_HOP_HEADERS or kl in connection_tokens:
            excluded.append([k, v])
            continue
        filtered.append([k, v])

    filtered.append(["Content-Length", str(body_size)])
    return filtered, excluded


class RuleEngineManager:
    """Manages persistent modification rules, request-time evaluation/application, and dry-run previews."""

    def __init__(self, store: CaptureIndexStore) -> None:
        self.store = store

    def _row_to_rule_dict(self, row: Any) -> dict[str, Any]:
        return {
            "rule_id": row["rule_id"],
            "name": row["name"],
            "description": row["description"] or "",
            "enabled": bool(row["enabled"]),
            "priority": int(row["priority"]),
            "match": json.loads(row["match_json"] or "{}"),
            "actions": json.loads(row["actions_json"] or "[]"),
            "copied_from_rule_id": row["copied_from_rule_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_rules(self, *, enabled: bool | None = None) -> dict[str, Any]:
        self.store.ensure_schema()
        with self.store._connect() as conn:
            if enabled is None:
                rows = conn.execute(
                    """
                    SELECT * FROM modification_rules
                    ORDER BY priority ASC, created_at ASC, rule_id ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM modification_rules
                    WHERE enabled = ?
                    ORDER BY priority ASC, created_at ASC, rule_id ASC
                    """,
                    (1 if enabled else 0,),
                ).fetchall()
            all_cnt_row = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS en_cnt FROM modification_rules"
            ).fetchone()

        items = [self._row_to_rule_dict(r) for r in rows]
        return {
            "total": len(items),
            "all_rules_count": int(all_cnt_row["total"] or 0),
            "enabled_count": int(all_cnt_row["en_cnt"] or 0),
            "ordering": "priority ASC, created_at ASC, rule_id ASC",
            "items": items,
        }

    def get_rule(self, rule_id: str) -> dict[str, Any]:
        self.store.ensure_schema()
        clean_id = rule_id.strip()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM modification_rules WHERE rule_id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            raise RuleNotFoundError(clean_id)
        return self._row_to_rule_dict(row)

    def create_rule(
        self,
        payload: dict[str, Any] | None = None,
        *,
        name: str | None = None,
        match: dict[str, Any] | None = None,
        actions: list[dict[str, Any]] | None = None,
        rule_id: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
        copied_from_rule_id: str | None = None,
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        if payload is None:
            payload = {}
            if name is not None:
                payload["name"] = name
            if match is not None:
                payload["match"] = match
            if actions is not None:
                payload["actions"] = actions
            if rule_id is not None:
                payload["rule_id"] = rule_id
            if description is not None:
                payload["description"] = description
            if enabled is not None:
                payload["enabled"] = enabled
            if priority is not None:
                payload["priority"] = priority
            if copied_from_rule_id is not None:
                payload["copied_from_rule_id"] = copied_from_rule_id
        elif not isinstance(payload, dict):
            raise RuleValidationError("Rule payload must be a JSON object")

        now_iso = _now_utc_iso()
        raw_id = payload.get("rule_id")
        if raw_id is not None and str(raw_id).strip():
            rule_id = validate_rule_id(raw_id)
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            rule_id = f"rule-{ts}-{uuid.uuid4().hex[:8]}"

        name_val = payload.get("name")
        if not isinstance(name_val, str) or not name_val.strip():
            name_val = rule_id
        else:
            name_val = name_val.strip()

        desc_val = str(payload.get("description") or "").strip()
        en_val = bool(
            payload["enabled"]
            if "enabled" in payload and payload["enabled"] is not None
            else True
        )
        prio_val = validate_priority(
            payload["priority"]
            if "priority" in payload and payload["priority"] is not None
            else 100
        )
        match_norm = validate_and_normalize_match(payload.get("match"))
        actions_norm = validate_and_normalize_actions(payload)
        copied_from = payload.get("copied_from_rule_id")

        with self.store._connect() as conn:
            existing = conn.execute(
                "SELECT rule_id FROM modification_rules WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()
            if existing is not None:
                raise RuleValidationError(
                    f"Rule ID already exists: {rule_id}",
                    code="duplicate_rule_id",
                )
            conn.execute(
                """
                INSERT INTO modification_rules (
                    rule_id, name, description, enabled, priority,
                    match_json, actions_json, copied_from_rule_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    name_val,
                    desc_val,
                    1 if en_val else 0,
                    prio_val,
                    json.dumps(match_norm, ensure_ascii=False),
                    json.dumps(actions_norm, ensure_ascii=False),
                    copied_from,
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()
        self.store.retention.checkpoint_wal()
        return self.get_rule(rule_id)

    def update_rule(
        self,
        rule_id: str,
        updates: dict[str, Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
        match: dict[str, Any] | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        if updates is None:
            updates = {}
            if name is not None:
                updates["name"] = name
            if description is not None:
                updates["description"] = description
            if enabled is not None:
                updates["enabled"] = enabled
            if priority is not None:
                updates["priority"] = priority
            if match is not None:
                updates["match"] = match
            if actions is not None:
                updates["actions"] = actions
        elif not isinstance(updates, dict):
            raise RuleValidationError("Rule updates must be a JSON object")

        clean_id = rule_id.strip()
        now_iso = _now_utc_iso()

        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM modification_rules WHERE rule_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise RuleNotFoundError(clean_id)

            name = row["name"]
            if "name" in updates and updates["name"] is not None:
                if not isinstance(updates["name"], str) or not updates["name"].strip():
                    raise RuleValidationError(
                        "name must be a non-empty string", code="invalid_name"
                    )
                name = updates["name"].strip()

            description = row["description"]
            if "description" in updates and updates["description"] is not None:
                description = str(updates["description"]).strip()

            enabled = bool(row["enabled"])
            if "enabled" in updates and updates["enabled"] is not None:
                enabled = bool(updates["enabled"])

            priority = int(row["priority"])
            if "priority" in updates and updates["priority"] is not None:
                priority = validate_priority(updates["priority"])

            match_norm = json.loads(row["match_json"] or "{}")
            if "match" in updates:
                match_norm = validate_and_normalize_match(updates["match"])

            actions_norm = json.loads(row["actions_json"] or "[]")
            action_keys = (
                "actions",
                "query_mutations",
                "header_mutations",
                "cookie_mutations",
                "json_mutations",
                "form_mutations",
            )
            if any(k in updates for k in action_keys):
                actions_norm = validate_and_normalize_actions(updates)

            conn.execute(
                """
                UPDATE modification_rules
                SET name = ?,
                    description = ?,
                    enabled = ?,
                    priority = ?,
                    match_json = ?,
                    actions_json = ?,
                    updated_at = ?
                WHERE rule_id = ?
                """,
                (
                    name,
                    description,
                    1 if enabled else 0,
                    priority,
                    json.dumps(match_norm, ensure_ascii=False),
                    json.dumps(actions_norm, ensure_ascii=False),
                    now_iso,
                    clean_id,
                ),
            )
            conn.commit()
        self.store.retention.checkpoint_wal()
        return self.get_rule(clean_id)

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any]:
        return self.update_rule(rule_id, {"enabled": bool(enabled)})

    def delete_rule(self, rule_id: str) -> dict[str, Any]:
        self.store.ensure_schema()
        clean_id = rule_id.strip()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT rule_id, name FROM modification_rules WHERE rule_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise RuleNotFoundError(clean_id)
            conn.execute(
                "DELETE FROM modification_rules WHERE rule_id = ?",
                (clean_id,),
            )
            conn.commit()
        self.store.retention.checkpoint_wal()
        return {
            "deleted": True,
            "rule_id": clean_id,
            "name": row["name"],
        }

    def copy_rule(
        self,
        source_rule_id: str,
        overrides: dict[str, Any] | None = None,
        *,
        rule_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
        match: dict[str, Any] | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        src = self.get_rule(source_rule_id)
        ovr = dict(overrides or {})
        if rule_id is not None:
            ovr["rule_id"] = rule_id
        if name is not None:
            ovr["name"] = name
        if description is not None:
            ovr["description"] = description
        if enabled is not None:
            ovr["enabled"] = enabled
        if priority is not None:
            ovr["priority"] = priority
        if match is not None:
            ovr["match"] = match
        if actions is not None:
            ovr["actions"] = actions
        new_payload: dict[str, Any] = {
            "rule_id": ovr.get("new_rule_id") or ovr.get("rule_id"),
            "name": ovr.get("name") or f"Copy of {src['name']}",
            "description": (
                ovr["description"]
                if "description" in ovr and ovr["description"] is not None
                else src["description"]
            ),
            "enabled": (
                ovr["enabled"]
                if "enabled" in ovr and ovr["enabled"] is not None
                else src["enabled"]
            ),
            "priority": (
                ovr["priority"]
                if "priority" in ovr and ovr["priority"] is not None
                else src["priority"]
            ),
            "match": ovr["match"] if "match" in ovr else copy.deepcopy(src["match"]),
            "actions": (
                ovr["actions"]
                if "actions" in ovr
                else copy.deepcopy(src["actions"])
            ),
            "copied_from_rule_id": src["rule_id"],
        }
        return self.create_rule(new_payload)

    def evaluate_and_apply(
        self,
        *,
        route_id: str,
        method: str,
        scheme: str,
        authority: str,
        raw_path: str,
        raw_query: str,
        headers: list[list[str]],
        raw_body: bytes,
        is_websocket: bool = False,
        is_streamed: bool = False,
        rules_override: list[dict[str, Any]] | None = None,
        include_disabled: bool = False,
    ) -> dict[str, Any]:
        """Evaluates rules in deterministic order (`priority ASC, created_at ASC, rule_id ASC`)
        and applies matching rules with per-rule fail-open rollback on runtime parse/action errors.
        """
        if rules_override is not None:
            candidate_rules: list[dict[str, Any]] = []
            for idx, r_in in enumerate(rules_override):
                r_id = validate_rule_id(r_in.get("rule_id") or f"preview-rule-{idx + 1}")
                r_en = bool(r_in.get("enabled", True))
                if not r_en and not include_disabled:
                    continue
                candidate_rules.append(
                    {
                        "rule_id": r_id,
                        "name": str(r_in.get("name") or r_id),
                        "description": str(r_in.get("description") or ""),
                        "enabled": r_en,
                        "priority": validate_priority(r_in.get("priority", 100)),
                        "match": validate_and_normalize_match(r_in.get("match")),
                        "actions": validate_and_normalize_actions(r_in),
                        "created_at": str(r_in.get("created_at") or f"1970-01-01T00:00:{idx:02d}Z"),
                    }
                )
            candidate_rules.sort(
                key=lambda r: (r["priority"], r["created_at"], r["rule_id"])
            )
        else:
            listed = self.list_rules(enabled=None if include_disabled else True)
            candidate_rules = listed["items"]

        host = authority.split(":", 1)[0].strip().lower()
        decoded_path = decode_path_human_readable(raw_path or "/")
        orig_query_entries = parse_raw_query_entries(raw_query or "")
        orig_query_params = [[e["name"], e["value"]] for e in orig_query_entries]
        orig_headers = [[str(k), str(v)] for k, v in (headers or [])]
        orig_cookies = parse_cookies_ordered(orig_headers)
        orig_content_type = _get_header_ci(orig_headers, "Content-Type")
        orig_body_bytes = raw_body or b""
        orig_body_sha256 = hashlib.sha256(orig_body_bytes).hexdigest()

        before_summary = {
            "route_id": route_id,
            "method": method.upper(),
            "scheme": scheme,
            "authority": authority,
            "host": host,
            "raw_path": raw_path or "/",
            "decoded_path": decoded_path,
            "raw_query": raw_query or "",
            "query_params": copy.deepcopy(orig_query_params),
            "headers": copy.deepcopy(orig_headers),
            "cookies": copy.deepcopy(orig_cookies),
            "content_type": orig_content_type,
            "body_size": len(orig_body_bytes),
            "body_sha256": orig_body_sha256,
        }

        # Working state updated sequentially by each applied rule (Rule 8)
        work_query_entries = copy.deepcopy(orig_query_entries)
        work_raw_query = raw_query or ""
        work_headers = copy.deepcopy(orig_headers)
        work_cookies = copy.deepcopy(orig_cookies)
        work_body_bytes = orig_body_bytes
        work_content_type = orig_content_type
        connection_tokens = _extract_connection_tokens(orig_headers)

        matched_rule_ids: list[str] = []
        applied_rule_ids: list[str] = []
        skipped_rule_ids: list[str] = []
        failed_rule_ids: list[str] = []
        rule_reports: list[dict[str, Any]] = []
        all_changes: list[dict[str, Any]] = []
        skipped_hop_headers: list[list[str]] = []

        for rule in candidate_rules:
            rid = rule["rule_id"]
            if not rule.get("enabled", True):
                continue

            cur_query_params = [[e["name"], e["value"]] for e in work_query_entries]
            is_match = evaluate_rule_match(
                rule["match"],
                route_id=route_id,
                method=method,
                authority=authority,
                host=host,
                raw_path=raw_path or "/",
                decoded_path=decoded_path,
                query_params=cur_query_params,
                headers=work_headers,
                content_type=work_content_type,
            )
            if not is_match:
                continue

            matched_rule_ids.append(rid)

            # WebSocket handshakes are protected from automatic rewrite (AC-14)
            if is_websocket:
                skipped_rule_ids.append(rid)
                rule_reports.append(
                    {
                        "rule_id": rid,
                        "name": rule["name"],
                        "priority": rule["priority"],
                        "matched": True,
                        "outcome": "skipped",
                        "reason": "websocket_handshake",
                        "changes": [],
                        "skipped_actions": [],
                        "errors": [
                            {
                                "code": "websocket_handshake",
                                "reason": "websocket_handshake",
                                "message": "WebSocket handshake requests are protected and skipped by modification rules.",
                            }
                        ],
                    }
                )
                continue

            # Streamed requests (oversized > max_body_bytes or chunked) are passed through without modification
            if is_streamed:
                skipped_rule_ids.append(rid)
                rule_reports.append(
                    {
                        "rule_id": rid,
                        "name": rule["name"],
                        "priority": rule["priority"],
                        "matched": True,
                        "outcome": "skipped",
                        "reason": "streamed_request",
                        "changes": [],
                        "skipped_actions": [],
                        "errors": [
                            {
                                "code": "streamed_request",
                                "reason": "streamed_request",
                                "message": "Streamed requests are passed through without modification and skipped by modification rules.",
                            }
                        ],
                    }
                )
                continue

            # Snapshot working state before this rule so runtime failures roll back cleanly (fail-open)
            snap_query_entries = copy.deepcopy(work_query_entries)
            snap_raw_query = work_raw_query
            snap_headers = copy.deepcopy(work_headers)
            snap_cookies = copy.deepcopy(work_cookies)
            snap_body_bytes = work_body_bytes
            snap_content_type = work_content_type

            rule_changes: list[dict[str, Any]] = []
            rule_errors: list[dict[str, Any]] = []
            rule_skipped_reasons: list[str] = []
            rule_skipped_actions: list[dict[str, Any]] = []
            rule_failed = False

            for act in rule["actions"]:
                target = act["target"]
                op = act["op"]

                try:
                    if target == "query":
                        q_pairs = [[e["name"], e["value"]] for e in work_query_entries]
                        mutated_q = apply_pair_mutations(
                            q_pairs,
                            [act],
                            case_insensitive_name=False,
                            field_label="query",
                        )
                        work_query_entries = entries_from_pairs(mutated_q)
                        work_raw_query = build_raw_query_from_entries(work_query_entries)
                        rule_changes.append(
                            {
                                "rule_id": rid,
                                "target": "query",
                                "op": op,
                                "field": act.get("name", ""),
                                "index": act.get("index"),
                                "value": act.get("value"),
                            }
                        )

                    elif target == "header":
                        hdr_name = str(act.get("name") or "").strip()
                        if not hdr_name and act.get("index") is not None:
                            idx_candidate = int(act["index"])
                            if 0 <= idx_candidate < len(work_headers):
                                hdr_name = str(work_headers[idx_candidate][0]).strip()
                        hdr_name_l = hdr_name.lower()
                        if (
                            hdr_name_l in ("host", "content-length")
                            or hdr_name_l in HOP_BY_HOP_HEADERS
                            or hdr_name_l in connection_tokens
                        ):
                            if hdr_name_l in HOP_BY_HOP_HEADERS or hdr_name_l in connection_tokens:
                                skipped_hop_headers.append(
                                    [hdr_name or str(act.get("name") or ""), str(act.get("value") or "")]
                                )
                            rule_skipped_reasons.append("protected_header")
                            rule_skipped_actions.append(
                                {
                                    "rule_id": rid,
                                    "target": "header",
                                    "op": op,
                                    "field": hdr_name or act.get("name", ""),
                                    "index": act.get("index"),
                                    "value": act.get("value"),
                                    "reason": "protected_header",
                                    "message": f"Header {hdr_name!r} is a protected authority, Content-Length, or hop-by-hop header and cannot be modified.",
                                }
                            )
                            continue

                        work_headers = apply_pair_mutations(
                            work_headers,
                            [act],
                            case_insensitive_name=True,
                            field_label="header",
                        )
                        if act.get("name", "").lower() == "cookie":
                            work_cookies = parse_cookies_ordered(work_headers)
                        work_content_type = _get_header_ci(work_headers, "Content-Type")
                        rule_changes.append(
                            {
                                "rule_id": rid,
                                "target": "header",
                                "op": op,
                                "field": act.get("name", ""),
                                "index": act.get("index"),
                                "value": act.get("value"),
                            }
                        )

                    elif target == "cookie":
                        work_cookies = apply_pair_mutations(
                            work_cookies,
                            [act],
                            case_insensitive_name=False,
                            field_label="cookie",
                        )
                        work_headers = sync_cookies_into_headers(
                            work_headers, work_cookies
                        )
                        rule_changes.append(
                            {
                                "rule_id": rid,
                                "target": "cookie",
                                "op": op,
                                "field": act.get("name", ""),
                                "index": act.get("index"),
                                "value": act.get("value"),
                            }
                        )

                    elif target == "json":
                        if is_streamed:
                            rule_skipped_reasons.append("streamed_body")
                            continue
                        ct_l = (work_content_type or "").strip().lower()
                        if ct_l.startswith("multipart/") or ct_l.startswith(
                            "application/octet-stream"
                        ):
                            rule_failed = True
                            rule_errors.append(
                                {
                                    "target": "json",
                                    "op": op,
                                    "field": act.get("path"),
                                    "code": "unsupported_body_type",
                                    "reason": "unsupported_body_type",
                                    "message": f"JSON action skipped/failed on unsupported body type: {work_content_type!r}",
                                }
                            )
                            break
                        if "application/json" not in ct_l and not ct_l.endswith("+json"):
                            rule_failed = True
                            rule_errors.append(
                                {
                                    "target": "json",
                                    "op": op,
                                    "field": act.get("path"),
                                    "code": "content_type_mismatch",
                                    "reason": "content_type_mismatch",
                                    "message": f"JSON action requires application/json content-type, got {work_content_type!r}",
                                }
                            )
                            break
                        try:
                            decoded_json = work_body_bytes.decode("utf-8")
                            parsed_json = json.loads(decoded_json)
                        except Exception as exc:
                            rule_failed = True
                            rule_errors.append(
                                {
                                    "target": "json",
                                    "op": op,
                                    "field": act.get("path"),
                                    "code": "malformed_json_body",
                                    "reason": "malformed_json_body",
                                    "message": f"Failed to parse request JSON body: {exc}",
                                }
                            )
                            break
                        try:
                            mutated_json = apply_json_path_mutations(
                                parsed_json, [act]
                            )
                        except DraftValidationError as exc:
                            err_code = (
                                "json_index_out_of_range"
                                if "out of range" in str(exc)
                                else (exc.code or "json_action_error")
                            )
                            rule_failed = True
                            rule_errors.append(
                                {
                                    "target": "json",
                                    "op": op,
                                    "field": act.get("path"),
                                    "code": err_code,
                                    "reason": err_code,
                                    "message": str(exc),
                                }
                            )
                            break
                        work_body_bytes = json.dumps(
                            mutated_json, ensure_ascii=False
                        ).encode("utf-8")
                        rule_changes.append(
                            {
                                "rule_id": rid,
                                "target": "json",
                                "op": op,
                                "field": act.get("path", ""),
                                "value": act.get("value"),
                            }
                        )

                    elif target == "form":
                        if is_streamed:
                            rule_skipped_reasons.append("streamed_body")
                            continue
                        ct_l = (work_content_type or "").strip().lower()
                        if ct_l.startswith("multipart/") or ct_l.startswith(
                            "application/octet-stream"
                        ):
                            rule_failed = True
                            rule_errors.append(
                                {
                                    "target": "form",
                                    "op": op,
                                    "field": act.get("name"),
                                    "code": "unsupported_body_type",
                                    "reason": "unsupported_body_type",
                                    "message": f"Form action skipped/failed on unsupported body type: {work_content_type!r}",
                                }
                            )
                            break
                        if "application/x-www-form-urlencoded" not in ct_l:
                            rule_failed = True
                            rule_errors.append(
                                {
                                    "target": "form",
                                    "op": op,
                                    "field": act.get("name"),
                                    "code": "content_type_mismatch",
                                    "reason": "content_type_mismatch",
                                    "message": f"Form action requires application/x-www-form-urlencoded content-type, got {work_content_type!r}",
                                }
                            )
                            break
                        try:
                            decoded_form = work_body_bytes.decode("utf-8")
                            form_pairs = (
                                [
                                    [k, v]
                                    for k, v in parse_qsl(
                                        decoded_form,
                                        keep_blank_values=True,
                                        strict_parsing=True,
                                    )
                                ]
                                if decoded_form
                                else []
                            )
                        except Exception as exc:
                            rule_failed = True
                            rule_errors.append(
                                {
                                    "target": "form",
                                    "op": op,
                                    "field": act.get("name"),
                                    "code": "malformed_form_body",
                                    "reason": "malformed_form_body",
                                    "message": f"Failed to parse URL-encoded form body: {exc}",
                                }
                            )
                            break
                        try:
                            mutated_form = apply_pair_mutations(
                                form_pairs,
                                [act],
                                case_insensitive_name=False,
                                field_label="form",
                            )
                        except DraftValidationError as exc:
                            err_code = exc.code or "form_action_error"
                            rule_failed = True
                            rule_errors.append(
                                {
                                    "target": "form",
                                    "op": op,
                                    "field": act.get("name"),
                                    "code": err_code,
                                    "reason": err_code,
                                    "message": str(exc),
                                }
                            )
                            break
                        encoded_form_str = "&".join(
                            f"{encode_uri_component(k)}={encode_uri_component(v)}"
                            for k, v in mutated_form
                        )
                        work_body_bytes = encoded_form_str.encode("utf-8")
                        rule_changes.append(
                            {
                                "rule_id": rid,
                                "target": "form",
                                "op": op,
                                "field": act.get("name", ""),
                                "index": act.get("index"),
                                "value": act.get("value"),
                            }
                        )

                except DraftValidationError as exc:
                    err_code = exc.code or f"{target}_action_error"
                    rule_failed = True
                    rule_errors.append(
                        {
                            "target": target,
                            "op": op,
                            "field": act.get("name") or act.get("path"),
                            "code": err_code,
                            "reason": err_code,
                            "message": str(exc),
                        }
                    )
                    break
                except Exception as exc:
                    rule_failed = True
                    rule_errors.append(
                        {
                            "target": target,
                            "op": op,
                            "field": act.get("name") or act.get("path"),
                            "code": "runtime_action_error",
                            "reason": "runtime_action_error",
                            "message": str(exc),
                        }
                    )
                    break

            if rule_failed:
                # Fail-open: restore pre-rule snapshot so failed rule never corrupts the request!
                work_query_entries = snap_query_entries
                work_raw_query = snap_raw_query
                work_headers = snap_headers
                work_cookies = snap_cookies
                work_body_bytes = snap_body_bytes
                work_content_type = snap_content_type
                failed_rule_ids.append(rid)
                rule_reports.append(
                    {
                        "rule_id": rid,
                        "name": rule["name"],
                        "priority": rule["priority"],
                        "matched": True,
                        "outcome": "failed",
                        "reason": rule_errors[0]["reason"] if rule_errors else "action_failed",
                        "changes": [],
                        "skipped_actions": rule_skipped_actions,
                        "errors": rule_errors,
                    }
                )
            elif rule_changes:
                applied_rule_ids.append(rid)
                all_changes.extend(rule_changes)
                rule_reports.append(
                    {
                        "rule_id": rid,
                        "name": rule["name"],
                        "priority": rule["priority"],
                        "matched": True,
                        "outcome": "applied",
                        "reason": None,
                        "changes": rule_changes,
                        "skipped_actions": rule_skipped_actions,
                        "errors": [],
                    }
                )
            else:
                skip_reason = (
                    rule_skipped_reasons[0]
                    if rule_skipped_reasons
                    else "no_applicable_actions"
                )
                skipped_rule_ids.append(rid)
                rule_reports.append(
                    {
                        "rule_id": rid,
                        "name": rule["name"],
                        "priority": rule["priority"],
                        "matched": True,
                        "outcome": "skipped",
                        "reason": skip_reason,
                        "changes": [],
                        "skipped_actions": rule_skipped_actions,
                        "errors": [
                            {
                                "code": skip_reason,
                                "reason": skip_reason,
                                "message": (
                                    rule_skipped_actions[0]["message"]
                                    if rule_skipped_actions
                                    else f"Rule skipped: {skip_reason}"
                                ),
                            }
                        ],
                    }
                )

        modified = len(applied_rule_ids) > 0
        if modified:
            normalized_headers, stripped_hop_headers = (
                _strip_hop_by_hop_and_rebuild_headers(
                    work_headers,
                    authority=authority,
                    body_size=len(work_body_bytes),
                    is_websocket=is_websocket,
                )
            )
            excluded_hop_headers = stripped_hop_headers + skipped_hop_headers
        else:
            normalized_headers = orig_headers
            excluded_hop_headers = skipped_hop_headers

        final_raw_path = raw_path or "/"
        final_raw_target = (
            f"{final_raw_path}?{work_raw_query}"
            if work_raw_query
            else final_raw_path
        )
        final_body_sha256 = hashlib.sha256(work_body_bytes).hexdigest()
        final_query_params = [[e["name"], e["value"]] for e in work_query_entries]

        after_summary = {
            "route_id": route_id,
            "method": method.upper(),
            "scheme": scheme,
            "authority": authority,
            "host": host,
            "raw_path": final_raw_path,
            "decoded_path": decoded_path,
            "raw_query": work_raw_query,
            "raw_target": final_raw_target,
            "query_params": final_query_params,
            "headers": normalized_headers,
            "cookies": work_cookies,
            "excluded_hop_by_hop_headers": excluded_hop_headers,
            "content_type": work_content_type,
            "content_length": len(work_body_bytes),
            "body_size": len(work_body_bytes),
            "body_sha256": final_body_sha256,
        }

        has_failures = len(failed_rule_ids) > 0
        if modified:
            outcome = "applied"
        elif has_failures:
            outcome = "failed"
        elif is_websocket and skipped_rule_ids:
            outcome = "skipped_websocket"
        elif skipped_rule_ids:
            outcome = "skipped"
        else:
            outcome = "no_match"

        evaluation_metadata = {
            "evaluated": True,
            "modified": modified,
            "rule_modified": modified,
            "has_failures": has_failures,
            "rule_failed": has_failures,
            "outcome": outcome,
            "matched_rule_ids": matched_rule_ids,
            "applied_rule_ids": applied_rule_ids,
            "skipped_rule_ids": skipped_rule_ids,
            "failed_rule_ids": failed_rule_ids,
            "change_count": len(all_changes),
            "change_summary": all_changes,
            "rules": rule_reports,
            "before_summary": before_summary,
            "after_summary": after_summary,
        }

        return {
            "evaluation": evaluation_metadata,
            "modified": modified,
            "method": method.upper(),
            "scheme": scheme,
            "authority": authority,
            "raw_path": final_raw_path,
            "raw_query": work_raw_query,
            "raw_target": final_raw_target,
            "headers": normalized_headers,
            "excluded_hop_by_hop_headers": excluded_hop_headers,
            "body_bytes": work_body_bytes,
            "body_size": len(work_body_bytes),
            "body_sha256": final_body_sha256,
            "content_type": work_content_type,
        }

    def preview_rules(
        self,
        payload: dict[str, Any] | None = None,
        *,
        capture_id: str | None = None,
        request_spec: dict[str, Any] | None = None,
        rule_ids: list[str] | None = None,
        include_disabled: bool | None = None,
        candidate_rule: dict[str, Any] | None = None,
        rules: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Dry-run preview against either a stored capture (`capture_id`) or an inline `request`
        without sending traffic to any target or creating captures (Rule 3, AC-11).
        """
        self.store.ensure_schema()
        if payload is None:
            payload = {}
            if capture_id is not None:
                payload["capture_id"] = capture_id
            if request_spec is not None:
                payload["request"] = request_spec
            if rule_ids is not None:
                payload["rule_ids"] = rule_ids
            if include_disabled is not None:
                payload["include_disabled"] = include_disabled
            if candidate_rule is not None:
                payload["candidate_rule"] = candidate_rule
            if rules is not None:
                payload["rules"] = rules
        elif not isinstance(payload, dict):
            raise RuleValidationError("Preview payload must be a JSON object")

        cap_id = payload.get("capture_id")
        req_in = payload.get("request")
        is_websocket = False
        is_streamed = False

        if cap_id:
            detail = self.store.get_capture_detail(str(cap_id).strip())
            req = detail["request"]
            route_id = str(detail.get("route_id") or "")
            method = str(req.get("method") or "GET").upper()
            scheme = str(req.get("scheme") or "https").lower()
            authority = str(req.get("authority") or "app-a.local.test")
            raw_path = str(req.get("raw_path") or "/")
            raw_query = str(req.get("raw_query") or "")
            headers = [[str(k), str(v)] for k, v in (req.get("headers") or [])]
            is_websocket = bool(detail.get("is_websocket"))
            is_streamed = bool(
                not is_websocket
                and detail.get("capture_status") in ("truncated", "metadata-only")
            )
            body_meta = req.get("body") or {}
            blob_ref = body_meta.get("blob_ref")
            raw_body = b""
            if blob_ref:
                blob_path = self.store.storage_dir / blob_ref
                if blob_path.is_file():
                    raw_body = blob_path.read_bytes()
        elif isinstance(req_in, dict):
            authority = str(
                req_in.get("authority") or req_in.get("host") or "app-a.local.test"
            ).strip()
            host_only = authority.split(":", 1)[0].strip().lower()
            route_id = str(
                req_in.get("route_id")
                or self.store.routes.resolve_route_id_for_host(host_only)
            )
            method = str(req_in.get("method") or "GET").strip().upper()
            scheme = str(req_in.get("scheme") or "https").strip().lower()
            if "decoded_path" in req_in and req_in["decoded_path"] is not None:
                raw_path = encode_decoded_path(str(req_in["decoded_path"]))
            else:
                raw_path = str(req_in.get("raw_path") or req_in.get("path") or "/")
            if "?" in raw_path and "raw_query" not in req_in and "query_params" not in req_in:
                raw_path, raw_query = raw_path.split("?", 1)
            elif "query_params" in req_in and isinstance(req_in["query_params"], list):
                raw_query = build_raw_query_from_entries(
                    entries_from_pairs(req_in["query_params"])
                )
            else:
                raw_query = str(req_in.get("raw_query") or "").lstrip("?")

            headers = [
                [str(p[0]), str(p[1])]
                for p in (req_in.get("headers") or [])
                if isinstance(p, (list, tuple)) and len(p) == 2
            ]
            if "cookies" in req_in and isinstance(req_in["cookies"], list):
                c_pairs = [
                    [str(p[0]), str(p[1])]
                    for p in req_in["cookies"]
                    if isinstance(p, (list, tuple)) and len(p) == 2
                ]
                headers = sync_cookies_into_headers(headers, c_pairs)

            ct_in = req_in.get("content_type")
            if ct_in and not _get_header_ci(headers, "Content-Type"):
                headers.append(["Content-Type", str(ct_in)])

            is_websocket = bool(req_in.get("is_websocket", False))
            is_streamed = bool(req_in.get("is_streamed", False))
            if "json_value" in req_in and req_in["json_value"] is not None:
                raw_body = json.dumps(
                    req_in["json_value"], ensure_ascii=False
                ).encode("utf-8")
                if not _get_header_ci(headers, "Content-Type"):
                    headers.append(["Content-Type", "application/json"])
            elif "form_fields" in req_in and isinstance(req_in["form_fields"], list):
                raw_body = "&".join(
                    f"{encode_uri_component(str(k))}={encode_uri_component(str(v))}"
                    for k, v in req_in["form_fields"]
                ).encode("utf-8")
                if not _get_header_ci(headers, "Content-Type"):
                    headers.append(
                        ["Content-Type", "application/x-www-form-urlencoded"]
                    )
            elif "body_base64" in req_in and req_in["body_base64"] is not None:
                raw_body = base64.b64decode(str(req_in["body_base64"]))
            elif "body_text" in req_in and req_in["body_text"] is not None:
                raw_body = str(req_in["body_text"]).encode("utf-8")
            else:
                raw_body = b""
        else:
            raise RuleValidationError(
                "Preview requires either 'capture_id' or 'request' object",
                code="invalid_preview_input",
            )

        inc_dis = bool(payload.get("include_disabled", False))
        rules_override = payload.get("rules")
        filter_rule_ids = payload.get("rule_ids")
        cand_rule = payload.get("candidate_rule")

        if rules_override is None and (filter_rule_ids or cand_rule is not None):
            base_items = self.list_rules(enabled=None if inc_dis else True)["items"]
            if filter_rule_ids:
                want = {str(x).strip() for x in filter_rule_ids if str(x).strip()}
                base_items = [r for r in base_items if r["rule_id"] in want]
            if isinstance(cand_rule, dict):
                base_items.append(cand_rule)
            rules_override = base_items

        res = self.evaluate_and_apply(
            route_id=route_id,
            method=method,
            scheme=scheme,
            authority=authority,
            raw_path=raw_path,
            raw_query=raw_query,
            headers=headers,
            raw_body=raw_body,
            is_websocket=is_websocket,
            is_streamed=is_streamed,
            rules_override=rules_override,
            include_disabled=inc_dis,
        )

        body_bytes: bytes = res["body_bytes"]
        body_text: str | None = None
        json_val: Any = None
        form_val: list[list[str]] | None = None
        try:
            body_text = body_bytes.decode("utf-8")
            ct_l = (res["content_type"] or "").lower()
            if "application/json" in ct_l or ct_l.endswith("+json"):
                json_val = json.loads(body_text)
            elif "application/x-www-form-urlencoded" in ct_l and body_text:
                form_val = [
                    [k, v]
                    for k, v in parse_qsl(body_text, keep_blank_values=True)
                ]
        except Exception:
            pass

        eval_meta = res["evaluation"]
        modified_request = {
            **eval_meta["after_summary"],
            "body_text": body_text,
            "json_value": json_val,
            "form_fields": form_val,
        }
        return {
            "dry_run": True,
            "target_sent": False,
            "sent_to_target": False,
            "capture_id": cap_id,
            "evaluated": eval_meta["evaluated"],
            "modified": eval_meta["modified"],
            "rule_modified": eval_meta["rule_modified"],
            "has_failures": eval_meta["has_failures"],
            "rule_failed": eval_meta["rule_failed"],
            "outcome": eval_meta["outcome"],
            "matched_rule_ids": eval_meta["matched_rule_ids"],
            "applied_rule_ids": eval_meta["applied_rule_ids"],
            "skipped_rule_ids": eval_meta["skipped_rule_ids"],
            "failed_rule_ids": eval_meta["failed_rule_ids"],
            "change_count": eval_meta["change_count"],
            "change_summary": eval_meta["change_summary"],
            "rules": eval_meta["rules"],
            "before_summary": eval_meta["before_summary"],
            "after_summary": eval_meta["after_summary"],
            "rule_evaluation": eval_meta,
            "modified_request": modified_request,
            "modified_body_preview": {
                "content_type": res["content_type"],
                "content_length": res["body_size"],
                "body_sha256": res["body_sha256"],
                "body_text": body_text,
                "json_value": json_val,
                "form_fields": form_val,
            },
        }
