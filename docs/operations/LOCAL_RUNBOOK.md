# ProxyConsole ローカル運用ランブック（Local Operations Runbook / DEV-050）

更新日: 2026-11-15  
対象構成: `deploy/compose/docker-compose.yml`（Project: `proxyconsole-dev001`）

本ランブックは、WSL2 / Docker Desktop（macOS / Linux / Windows WSL）環境における ProxyConsole の日常運用（起動・停止、ヘルスチェック、設定検証、バックアップ、リストア、容量清掃、証明書交換、障害・インシデント確認、レディネス自己診断）の再現可能な手順をまとめたものである。

---

## 1. 起動・停止・初期化（Startup / Shutdown / Clean）

### 1.1 標準起動（証明書生成・ビルド・起動・疎通待機）

```bash
make up
# または直接実行:
./scripts/up.sh
```

- 初回実行時は `var/certificates/edge.crt` と `var/certificates/edge.key` が自動生成される。
- 起動完了後、以下のエンドポイントが利用可能になる：
  - **HTTPS プロキシ入口 (`edge`)**: `https://127.0.0.1:8443`（環境変数 `EDGE_HTTPS_PORT` で変更可能、例: `EDGE_HTTPS_PORT=443 make up`）
  - **Web 管理コンソール & Control API (`control-api`)**: `http://127.0.0.1:18480/`（`127.0.0.1` 限定バインド、環境変数 `CONTROL_API_PORT` でポート変更可能）

### 1.2 停止（ボリュームとコンテナの停止・削除）

```bash
make down
# または:
./scripts/down.sh
```

> **注意**: `./scripts/down.sh` は `docker compose down -v --remove-orphans` を実行するため、`capture_data` ボリューム内のデータは削除される。データを残したままコンテナのみ一時停止・再起動する場合は以下のコマンドを使用すること：
> ```bash
> # データを保持して一時停止
> docker compose -f deploy/compose/docker-compose.yml stop
> # データを保持して再開
> docker compose -f deploy/compose/docker-compose.yml start
> ```

### 1.3 完全クリーンアップ（ホスト側 `var/` 同期データ・証明書の消去を含む）

```bash
make clean
```

---

## 2. ヘルスチェックと稼働状態確認（Health & Status Verification）

### 2.1 コンテナ稼働状態とポートバインドの確認

```bash
docker compose -f deploy/compose/docker-compose.yml ps
```

- `control-api` が `127.0.0.1:18480->18480/tcp`、`edge` が `0.0.0.0:8443->443/tcp` であり、他の内部コンテナ（`inspector`, `router`, `echo-a`, `echo-b`）にホスト公開ポートがないことを確認する。

### 2.2 管理 API・プロキシチェーンの疎通確認

```bash
# 1. Control API ヘルスチェック
curl -fsS http://127.0.0.1:18480/api/v1/health

# 2. HTTPS Edge -> Inspector -> Router -> Echo-A の E2E 疎通確認
curl -fsS --cacert var/certificates/edge.crt \
  --resolve app-a.local.test:8443:127.0.0.1 \
  https://app-a.local.test:8443/readyz

# 3. ルート設定と Nginx 反映同期状態の確認
./scripts/capture-ctl.sh route-status
```

- `route-status` の出力で `"in_sync": true`、`"last_apply_status": "applied"` であれば、SQLite のルート定義と内部 Nginx ルーターの実設定が一致している。

---

## 3. 設定検証（Configuration Validation）

Docker Compose 構文（デフォルトポートおよび `EDGE_HTTPS_PORT=443`）と、外部 `edge` / 内部 `router` 両方の `nginx -t` 検証を実行する：

```bash
make config-test
# または:
./scripts/verify-config.sh
```

また、現在のルート定義から生成される Nginx 設定候補を本番反映前にドライラン検証する場合は以下を実行する：

```bash
curl -fsS -X POST http://127.0.0.1:18480/api/v1/routes/validate \
  -H "Content-Type: application/json" -d '{}'
```

---

## 4. バックアップ手順（Backup Procedure）

ProxyConsole の永続状態は、`capture_data` ボリューム内の `/var/lib/proxyconsole/{captures,blobs,database,router}` と、ホスト側の `var/certificates/{edge.crt,edge.key}` に集約されている。

稼働中（オンライン）に一貫性のあるバックアップを取得するため、SQLite のオンラインバックアップ API（`sqlite3.Connection.backup()`）で DB スナップショットを取得した上で `.tar.gz` アーカイブを作成する。

```bash
mkdir -p backups
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

# 1. コンテナ内永続データ（SQLite DB + Captures + Blobs + Router 設定）の一貫性バックアップ
docker compose -f deploy/compose/docker-compose.yml exec -T control-api \
  gosu mitmproxy python3 -c "
from pathlib import Path
import shutil, sqlite3, subprocess
from index_store import CaptureIndexStore

store = CaptureIndexStore()
store.retention.checkpoint_wal()
stage = Path('/tmp/pc_backup_stage')
shutil.rmtree(stage, ignore_errors=True)
(stage / 'database').mkdir(parents=True, exist_ok=True)
with sqlite3.connect(str(store.db_path)) as src, sqlite3.connect(str(stage / 'database' / 'captures.sqlite3')) as dst:
    src.backup(dst)
for sub in ('captures', 'blobs', 'router'):
    src_dir = store.storage_dir / sub
    if src_dir.is_dir():
        shutil.copytree(src_dir, stage / sub)
subprocess.run(['tar', '-C', str(stage), '-czf', '/tmp/pc_backup.tar.gz', 'captures', 'blobs', 'database', 'router'], check=True)
shutil.rmtree(stage, ignore_errors=True)
"
docker compose -f deploy/compose/docker-compose.yml exec -T control-api \
  sh -c "cat /tmp/pc_backup.tar.gz && rm -f /tmp/pc_backup.tar.gz" > "backups/proxyconsole-data-${TIMESTAMP}.tar.gz"

# 2. TLS 証明書・秘密鍵のバックアップ（必要な場合のみ。Git 管理外の安全な場所へ保管すること）
tar -C var/certificates -czf "backups/proxyconsole-certs-${TIMESTAMP}.tar.gz" edge.crt edge.key
chmod 600 "backups/proxyconsole-certs-${TIMESTAMP}.tar.gz"
```

> **重要**: `backups/` ディレクトリには生のリクエストボディや秘密鍵が含まれるため、Git にコミットしてはならない（一時確認用にホストの `var/` へコピーする場合は `./scripts/capture-ctl.sh sync` も利用可能）。

---

## 5. リストア手順（Restore Procedure）

バックアップアーカイブ（例: `BACKUP_FILE="backups/proxyconsole-data-YYYYMMDDTHHMMSSZ.tar.gz"`）から状態を復元する手順：

```bash
BACKUP_FILE="backups/proxyconsole-data-YYYYMMDDTHHMMSSZ.tar.gz"

# 1. スタックが起動していることを確認（未起動なら ./scripts/up.sh）
./scripts/up.sh

# 2. アーカイブを /var/lib/proxyconsole へ展開
docker compose -f deploy/compose/docker-compose.yml exec -T control-api \
  sh -c "rm -rf /var/lib/proxyconsole/captures/* /var/lib/proxyconsole/blobs/* /var/lib/proxyconsole/database/* /var/lib/proxyconsole/router/* && tar -C /var/lib/proxyconsole -xzf -" < "${BACKUP_FILE}"

# 3. router, inspector, control-api を再起動して復元データを読み込ませる
docker compose -f deploy/compose/docker-compose.yml restart router inspector control-api

# 4. 復元後の状態確認（ルート・キャプチャ・ルール・通知）
./scripts/capture-ctl.sh route-list
./scripts/capture-ctl.sh index-list --limit 10
./scripts/capture-ctl.sh rule-list
./scripts/capture-ctl.sh notification-status
```

---

## 6. ストレージ容量確認・Retention 清掃・インデックス修復（Storage & Cleanup）

### 6.1 現在のディスク使用量と Retention ポリシーの確認

```bash
./scripts/capture-ctl.sh retention-status
# または REST API:
curl -fsS http://127.0.0.1:18480/api/v1/retention/status
```

- `storage.total_bytes`（`captures_bytes` + `blobs_bytes` + `index_db_bytes`）および `captures_count`、`blobs_count`、`indexed_count` を確認できる。

### 6.2 ドライラン清掃（削除対象の事前確認）と即時清掃の実行

```bash
# ドライラン（実際にはファイルを削除せず、削除予定件数と解放予定バイト数を確認）
./scripts/capture-ctl.sh retention-run --dry-run

# 即時清掃の実行
./scripts/capture-ctl.sh retention-run
```

### 6.3 Retention ポリシー（保持期間・Soft/Hard 上限）の変更

```bash
# 例: 最小保持時間を 12 時間（43200秒）、Soft 上限を 5 GiB、Hard 上限を 10 GiB に変更
./scripts/capture-ctl.sh retention-set \
  --min-retention-seconds 43200 \
  --soft-limit-bytes 5368709120 \
  --hard-limit-bytes 10737418240
```

### 6.4 SQLite インデックスの再構築（破損・不整合時の修復）

生キャプチャ JSON（`/var/lib/proxyconsole/captures/*.json`）を正として、`captures_index` テーブルを再構築し、孤立・破損レコードを整合させる：

```bash
./scripts/capture-ctl.sh index-rebuild
```

---

## 7. TLS 証明書の確認と交換（Certificate Inspection & Replacement）

### 7.1 証明書の有効期限・SAN の確認

```bash
openssl x509 -in var/certificates/edge.crt -noout -subject -issuer -dates -ext subjectAltName

# 30日（2592000秒）以内に有効期限が切れないかチェック（終了コード 0 = 有効）
openssl x509 -in var/certificates/edge.crt -noout -checkend 2592000
```

### 7.2 開発用自己署名証明書のローテーション（強制再生成と反映）

```bash
# 1. 新しい証明書と秘密鍵を再生成
./scripts/generate-certs.sh --force

# 2. edge, inspector, control-api イメージを再ビルドして反映
./scripts/up.sh
```

### 7.3 カスタムドメインの追加または社内 CA 証明書への交換

1. 自己署名証明書に新しい開発ドメインを追加する場合は `deploy/certificates/openssl.cnf` の `[alt_names]` に `DNS.5 = new-app.local.test` 等を追記し、`./scripts/generate-certs.sh --force && ./scripts/up.sh` を実行する。
2. 社内 CA 発行の証明書を使用する場合は、証明書を `var/certificates/edge.crt`（`chmod 644`）、秘密鍵を `var/certificates/edge.key`（`chmod 600`）に配置し、`./scripts/up.sh` を実行する。

---

## 8. 障害・通知インシデント・ログの確認（Incident & Log Troubleshooting）

### 8.1 集約通知インシデントの確認・Acknowledge・手動 Resolve

Web コンソール（`http://127.0.0.1:18480/` の **Notifications** ワークスペース）または CLI から確認・操作できる：

```bash
# 1. 通知サブシステムのサマリ確認（未解決件数・未確認件数・最新 seq）
./scripts/capture-ctl.sh notification-status

# 2. 未解決（open）インシデントの一覧表示
./scripts/capture-ctl.sh notification-list --status open

# 3. 特定インシデントの詳細と発生タイムライン（seq 履歴）の確認
./scripts/capture-ctl.sh notification-get <incident_id>

# 4. インシデントの確認済み（Acknowledge）マーク
./scripts/capture-ctl.sh notification-ack <incident_id>

# 5. インシデントの手動解決（Resolve）
./scripts/capture-ctl.sh notification-resolve <incident_id> --reason "manual_operator_resolve"
```

### 8.2 コンテナログおよび内部 Nginx エラーログの確認

```bash
# 各サービスのコンテナログ確認
docker compose -f deploy/compose/docker-compose.yml logs --tail=100 edge inspector router control-api

# 内部 Nginx ルーターの永続エラーログ確認
docker compose -f deploy/compose/docker-compose.yml exec -T router \
  tail -n 100 /var/lib/proxyconsole/router/nginx-error.log
```

### 8.3 Docker コンテナ自動発見（Discovery）の状態確認と再スキャン

```bash
# Discovery 状態と検出ターゲット一覧の確認
curl -fsS http://127.0.0.1:18480/api/v1/discovery/status
curl -fsS -X POST http://127.0.0.1:18480/api/v1/discovery/refresh \
  -H "Content-Type: application/json" -d '{}'
```

---

## 9. 再現可能な会社環境レディネス・バックアップ／リストア自動検証コマンド

ポート境界、ネットワーク分離、Docker Socket 使用 API、証明書権限・Git 除外、依存パッケージとライセンス一覧、合成データによるフルバックアップ／リストア、およびディスク書き込み不能時の Fail-Open 回復を一括で検証するには、以下のコマンドを実行する：

```bash
# フル検証（スタッククリーン再起動 + インベントリ検証 + Backup/Restore 実証 + Disk-Full 検証）
./scripts/check-company-readiness.sh

# 稼働中スタックを止めずにインベントリ JSON のみ出力する場合
./scripts/check-company-readiness.sh --inventory-only
```
