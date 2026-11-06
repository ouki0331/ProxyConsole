# Capture & Replay Recorder (`DEV-002`)

`services/recorder/` は [HTTP Capture Contract v0](../../docs/architecture/CAPTURE_CONTRACT.md) に従い、`inspector` (mitmproxy) を通過する HTTP リクエストおよび WebSocket ハンドシェイクの記録・読み戻し・意味的リプレイを提供する。

## 構成ファイル

- `addon.py`: mitmproxy addon。リクエストの原始情報（method, scheme, authority, raw_path, raw_query, 順序付き重複可能 headers/trailers）と原始 body bytes を分離保存する。
- `parser.py`: 原始記録と原始 body bytes から派生ビュー（`query_params`, `cookies_view`, `json_view`, `form_view`, `multipart_view`, `text_preview`, `hex_preview`, `parse_errors`）を生成する。解析失敗は `parse_errors` に記録され、プロキシ転送や原始 bytes の保存成功を妨げない。
- `capture_cli.py`: 保存済みレコードの一覧取得 (`list`)、整合性検証付き詳細取得 (`get`)、および意味的リプレイ (`replay`) を行う CLI。

## 保存フォーマット (`schema_version: "v0"`)

保存先ディレクトリ（コンテナ内 `/var/lib/proxyconsole`、ホスト同期時 `var/`、いずれも Git 管理外）:

- `captures/<capture_id>.json`: メタデータ、順序付き headers/trailers、body サイズ・SHA-256・`blob_ref`、`capture_status` (`complete` / `truncated` / `metadata-only` / `capture-failed`)、`replayable` フラグ、派生ビュー。
- `blobs/<capture_id>.bin`: リクエスト body の原始バイト列（UTF-8 変換を行わずそのまま保存）。

## Capture 上限と大容量転送

- `CAPTURE_MAX_BODY_BYTES`（既定 `1048576` = 1 MiB）を超えるリクエスト（例: 100 MiB アップロード）は、`requestheaders` フックで `flow.request.stream` によるストリーミング転送に切り替える。
- リクエスト全体は target まで中断なく転送され、全体のサイズ (`size`) と SHA-256 (`sha256`) を計算しつつ、保存は上限バイト数までで打ち切り `capture_status: "truncated"`・`replayable: false` とする。
- `capture_cli.py replay` は `replayable: false`（`truncated` / `metadata-only` / WebSocket）のレコードを拒否する。
