#!/usr/bin/env python3
"""Provider-neutral, bounded Container Engine log reader (DEV-062)."""

from __future__ import annotations

from datetime import datetime, timezone
import http.client
import os
import re
import struct
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

if TYPE_CHECKING:
    from docker_discovery import DockerDiscoveryService


DEFAULT_TAIL = 200
MAX_TAIL = 1000
DEFAULT_RESPONSE_BYTE_CAP = 2 * 1024 * 1024
SEVERITIES = {"error", "warn", "info"}
_TIMESTAMP_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s(?P<text>.*)$"
)
_ERROR_RE = re.compile(r"(?:^|\W)(?:fatal|panic|critical|error)(?:$|\W)", re.I)
_WARN_RE = re.compile(r"(?:^|\W)warn(?:ing)?(?:$|\W)", re.I)


class EngineLogsError(RuntimeError):
    """Base error for Engine log reads."""


class EngineLogsUnavailableError(EngineLogsError):
    """Raised when the configured Engine cannot be queried."""


class EngineLogSourceNotFoundError(KeyError):
    """Raised when a container is not present in host-wide inventory."""


class EngineLogsValidationError(ValueError):
    """Raised when a log filter is invalid."""


def classify_severity(text: str) -> str:
    if _ERROR_RE.search(text):
        return "error"
    if _WARN_RE.search(text):
        return "warn"
    return "info"


def _split_timestamp(text: str) -> tuple[str | None, str]:
    match = _TIMESTAMP_RE.match(text)
    if not match:
        return None, text
    return match.group("timestamp"), match.group("text")


def _lines_for_chunk(chunk: bytes, stream: str) -> list[dict[str, Any]]:
    decoded = chunk.decode("utf-8", errors="replace")
    items: list[dict[str, Any]] = []
    for raw_line in decoded.splitlines():
        timestamp, text = _split_timestamp(raw_line)
        items.append(
            {
                "timestamp": timestamp,
                "stream": stream,
                "severity": classify_severity(text),
                "text": text,
            }
        )
    return items


def decode_engine_log_payload(payload: bytes) -> tuple[list[dict[str, Any]], str]:
    """Decode Docker multiplex frames, falling back to Podman/plain text."""

    if not payload:
        return [], "plain"

    offset = 0
    frames: list[tuple[str, bytes]] = []
    multiplex_prefix = (
        len(payload) >= 8
        and payload[0] in (1, 2)
        and payload[1:4] == b"\x00\x00\x00"
    )
    while offset + 8 <= len(payload):
        stream_type = payload[offset]
        if stream_type not in (1, 2) or payload[offset + 1 : offset + 4] != b"\x00\x00\x00":
            frames = []
            break
        length = struct.unpack(">I", payload[offset + 4 : offset + 8])[0]
        frame_end = offset + 8 + length
        if frame_end > len(payload):
            break
        frames.append(("stdout" if stream_type == 1 else "stderr", payload[offset + 8 : frame_end]))
        offset = frame_end

    if multiplex_prefix and (frames or offset < len(payload)):
        lines: list[dict[str, Any]] = []
        for stream, chunk in frames:
            lines.extend(_lines_for_chunk(chunk, stream))
        return lines, "multiplexed"

    return _lines_for_chunk(payload, "stdout"), "plain"


class EngineLogsService:
    def __init__(self, discovery: DockerDiscoveryService) -> None:
        self.discovery = discovery
        try:
            configured_cap = int(
                os.environ.get("ENGINE_LOG_RESPONSE_BYTE_CAP", DEFAULT_RESPONSE_BYTE_CAP)
            )
        except ValueError:
            configured_cap = DEFAULT_RESPONSE_BYTE_CAP
        self.response_byte_cap = max(1024, configured_cap)

    @staticmethod
    def _validate_since(since: str | None) -> str | None:
        if since is None:
            return None
        cleaned = since.strip()
        if not cleaned:
            return None
        try:
            datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EngineLogsValidationError("since must be an RFC3339 timestamp") from exc
        return cleaned

    @staticmethod
    def _validate_tail(tail: int | str | None) -> int:
        try:
            parsed = DEFAULT_TAIL if tail is None else int(tail)
        except (TypeError, ValueError) as exc:
            raise EngineLogsValidationError("tail must be an integer") from exc
        if parsed < 1 or parsed > MAX_TAIL:
            raise EngineLogsValidationError(f"tail must be between 1 and {MAX_TAIL}")
        return parsed

    def _engine_get_bytes(self, path: str, *, socket_path: str) -> tuple[bytes, bool]:
        if not os.path.exists(socket_path):
            raise EngineLogsUnavailableError(
                f"Container Engine socket path does not exist: {socket_path}"
            )
        connection_factory = getattr(self.discovery, "connection_factory", None)
        if connection_factory is None:
            from docker_discovery import _UnixSocketHTTPConnection

            connection_factory = _UnixSocketHTTPConnection
        conn: http.client.HTTPConnection = connection_factory(socket_path, timeout=5.0)
        try:
            conn.request(
                "GET",
                path,
                headers={"Host": "localhost", "Accept": "application/octet-stream"},
            )
            response = conn.getresponse()
            payload = response.read(self.response_byte_cap + 1)
            if response.status >= 400:
                detail = payload[:4096].decode("utf-8", errors="replace").strip()
                raise EngineLogsUnavailableError(
                    f"Container Engine logs API returned HTTP {response.status}: {detail}"
                )
            return payload[: self.response_byte_cap], len(payload) > self.response_byte_cap
        except EngineLogsUnavailableError:
            raise
        except Exception as exc:
            raise EngineLogsUnavailableError(
                f"Failed to query Container Engine logs: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_logs(
        self,
        *,
        container_id: str | None,
        tail: int | str | None = None,
        since: str | None = None,
        severity: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        clean_id = str(container_id or "").strip()
        if not clean_id:
            raise EngineLogsValidationError("container_id is required")
        parsed_tail = self._validate_tail(tail)
        parsed_since = self._validate_since(since)
        parsed_severity = str(severity or "").strip().lower() or None
        if parsed_severity not in SEVERITIES and parsed_severity is not None:
            raise EngineLogsValidationError("severity must be error, warn, or info")
        parsed_search = str(search or "").strip() or None

        scan = self.discovery._scan_docker(scope="all", persist_status=False)
        if not scan["available"]:
            raise EngineLogsUnavailableError(str(scan.get("error") or "Container Engine unavailable"))
        container = next(
            (
                item
                for item in scan["all_containers"]
                if clean_id in {str(item.get("container_id")), str(item.get("inventory_id"))}
            ),
            None,
        )
        if container is None:
            raise EngineLogSourceNotFoundError(clean_id)

        params: dict[str, str] = {
            "stdout": "1",
            "stderr": "1",
            "timestamps": "1",
            "tail": str(parsed_tail),
        }
        if parsed_since is not None:
            params["since"] = parsed_since
        path = f"/containers/{quote(str(container['container_id']), safe='')}/logs?{urlencode(params)}"
        payload, byte_truncated = self._engine_get_bytes(
            path, socket_path=str(scan["socket_path"])
        )
        lines, encoding = decode_engine_log_payload(payload)
        if parsed_severity is not None:
            lines = [line for line in lines if line["severity"] == parsed_severity]
        if parsed_search is not None:
            needle = parsed_search.casefold()
            lines = [line for line in lines if needle in line["text"].casefold()]

        metadata = {
            "inventory_id": container.get("inventory_id"),
            "container_id": container.get("container_id"),
            "container_short_id": container.get("container_short_id"),
            "container_name": container.get("container_name"),
            "service_name": container.get("service_name"),
            "project": container.get("compose_project") or "standalone",
            "role": container.get("role"),
            "state": container.get("state"),
        }
        return {
            "available": True,
            "provider": scan["provider"],
            "container": metadata,
            "requested": {
                "tail": parsed_tail,
                "since": parsed_since,
                "severity": parsed_severity,
                "search": parsed_search,
            },
            "encoding": encoding,
            "lines": lines,
            "returned": len(lines),
            "truncated": byte_truncated,
            "response_byte_cap": self.response_byte_cap,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }
