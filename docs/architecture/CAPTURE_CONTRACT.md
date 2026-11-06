# HTTP Capture Contract v0

状態: P1 検証用。G1 合格後に v1 として固定する。

## 目的

最初の記録実装が、後の閲覧、編集、意味的リプレイ、cURL/fetch 出力を妨げないための最低条件を定める。

## 原始記録

最低限、次を lossless または状態付きで保持する。

- capture ID、時刻、route ID
- method、scheme、authority/host
- raw path と raw query
- HTTP version
- request headers を順序付き key/value 列として保持する
- request trailers を順序付き key/value 列として保持する
- body の原始 bytes または body blob 参照
- body size、hash、content type、content encoding
- `complete`、`truncated`、`metadata-only`、`capture-failed`
- 実際の target、response status、duration、proxy error

## 禁止する縮約

- query を単一値 dictionary にしない。
- headers を重複不可の object にしない。
- body を UTF-8 text だけで保存しない。
- decoded URL だけを保存しない。
- truncated body を replayable と表示しない。
- 表示上の mask を原始値の削除と混同しない。

## 解析ビュー

次は原始記録から再生成可能な派生データとして扱う。

- decoded query の ordered multi-value view
- JSON tree
- URL-encoded form
- multipart field/file metadata
- cookie view
- human-readable text、hex view

解析失敗は原始 bytes の保存成功を失敗扱いにしてはならない。

## リプレイ互換性

意味的リプレイ時は原始記録を入力にし、次の transport/hop-by-hop 情報は送信時に再構築する。

- Host
- Content-Length
- Connection
- Transfer-Encoding
- Proxy-Connection
- Keep-Alive

TLS session、TCP packet、HTTP/2 frame の byte-for-byte 再現は対象外とする。

## 将来の変更ルール

自動変更機能を導入した後は、原始リクエストを base とし、変更内容を ordered mutation list として記録する。変更前後の巨大 body を無条件に二重保存しない。

## 保存制限

上限超過時は転送を中断するのではなく、capture status を `truncated` または `metadata-only` とする。完全リプレイ不可であることを UI と API で明示する。

## 保持ポリシーと容量計算範囲 (DEV-011)

- **容量計算範囲 (`calculation_scope`)**:
  - `captures_bytes`: `captures/*.json` の実ファイルサイズ合計
  - `blobs_bytes`: `blobs/*.bin` の実ファイルサイズ合計
  - `index_db_bytes`: `database/captures.sqlite3`（および `-wal`、`-shm`）の実ファイルサイズ合計
  - `total_bytes`: `captures_bytes + blobs_bytes + index_db_bytes`
  - 各 record の回収可能 byte 数は `record_bytes (captures/<id>.json) + blob_bytes (blobs/<id>.bin、存在しない場合は 0)` とする。
- **優先順位と削除順序**:
  1. 削除順序は常に `captured_at_unix_us ASC, capture_id ASC`（最古優先・同一時刻は capture ID 昇順）で決定的に評価する。
  2. `total_bytes > hard_limit_bytes` の場合、最短保持期間（既定24時間）に関わらず最古 record から削除し、`hard_limit_bytes` 以下へ戻す。
  3. `soft_limit_bytes < total_bytes <= hard_limit_bytes` の場合、最短保持期間（既定24時間）を超過した record のみを最古順で削除し、`soft_limit_bytes` 以下を目指す。24時間以内の record は保持する。
  4. `total_bytes <= soft_limit_bytes` の場合、有効な record は削除しない。
  5. 孤立 blob（対応 capture JSON がない `blobs/*.bin`）、破損 capture JSON、欠損 blob（`blob_ref` の実体がない record）、不整合 index row は cleanup 実行時に停止せず reconciliation され、結果に明示される。


