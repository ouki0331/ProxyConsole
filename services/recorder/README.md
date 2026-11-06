# Capture, Durable Index & Replay Recorder (`DEV-002` / `DEV-010`)

`services/recorder/` は [HTTP Capture Contract v0](../../docs/architecture/CAPTURE_CONTRACT.md) に従い、`inspector` (mitmproxy) を通過する HTTP リクエストおよび WebSocket ハンドシェイクの原始記録保存、SQLite 耐久インデックス検索、および意味的リプレイを提供する。

## 構成ファイル

- `addon.py`: mitmproxy addon。リクエストの原始情報（method, scheme, authority, raw_path, raw_query, 順序付き重複可能 headers/trailers）と原始 body bytes を分離保存し、fault-isolated に SQLite インデックスへ反映する。
- `parser.py`: 原始記録と原始 body bytes から派生ビュー（`query_params`, `cookies_view`, `json_view`, `form_view`, `multipart_view`, `text_preview`, `hex_preview`, `parse_errors`）を生成する単一実装。
- `index_store.py`: SQLite (`database/captures.sqlite3`) を用いた永続メタデータインデックス。初回 import、冪等 upsert、削除後の完全 rebuild、複合フィルタ検索、破損レコードの隔離記録 (`capture_import_errors`) を担当する。
- `api_server.py`: 後続の管理 UI や外部ツールが直接利用できるローカル HTTP JSON API (`127.0.0.1:${CONTROL_API_PORT:-18480}`)。
- `capture_cli.py`: 保存済みレコードの検索 (`list` / `search`)、詳細取得 (`get` / `detail`)、インデックス管理 (`import` / `rebuild` / `index-status`)、意味的リプレイ (`replay`) を行う CLI。

## データ境界と Schema / Rebuild 方針

1. **原始記録（Source of Truth）**:
   - `captures/<capture_id>.json`: リクエスト/レスポンスの原始メタデータ、順序付き headers/trailers、body サイズ・SHA-256・`blob_ref`、`capture_status`、`replayable`。
   - `blobs/<capture_id>.bin`: リクエスト body の原始バイト列（インデックス DB には重複保存しない）。
2. **派生インデックス (`migrations/001_capture_index.sql`, Schema Version `1`)**:
   - `database/captures.sqlite3`: 検索・ページネーション用の派生インデックス。
   - `schema_migrations` テーブルで適用済み migration version を管理する。
   - インデックスが破損・削除された場合や schema 更新時は、`POST /api/v1/index/rebuild` または `./scripts/capture-ctl.sh rebuild` により `captures/*.json` から完全に再構築できる。
   - 一部の `captures/*.json` が破損していても `capture_import_errors` テーブルにファイル名とエラー内容を記録し、他レコードの import/検索およびプロキシ転送を止めない。

## ローカル HTTP API エンドポイント

- `GET /api/v1/health`: ヘルスチェック
- `GET /api/v1/captures`: 検索・一覧取得
  - クエリパラメータ: `limit`, `offset`, `sort` (`desc` / `asc`), `from_time`, `to_time`, `host`, `authority`, `method`, `path`, `path_prefix`, `capture_status`, `response_status`
- `GET /api/v1/captures/<capture_id>`: 単一レコードの詳細・順序付き headers/query・body metadata・派生ビュー取得
- `GET /api/v1/index/status`: インデックス件数、migration 状態、破損レコード一覧 (`import_errors`) の取得
- `POST /api/v1/index/import`: `captures/*.json` からの冪等インポート
- `POST /api/v1/index/rebuild`: インデックス DB の削除と原始レコードからの完全再構築
