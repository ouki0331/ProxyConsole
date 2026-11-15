# 会社環境導入に向けた準備評価レポート（Company-Environment Readiness Assessment / DEV-050）

更新日: 2026-11-15  
対象タスク: `DEV-050`（Phase P6 / Product relation: `OPS-BOUNDARY-001`, `DATA-RISK-001`, `DEPLOY-READINESS-001`）  
対象コミットベースライン: `dfed526` / `b78d7a8`（P1〜P5 完了・G5 通過済みローカル版）  
自動検証コマンド: `./scripts/check-company-readiness.sh`

---

## 1. 評価の目的と前提（Function Rules 1〜8 準拠）

本レポートは、P1〜P5 で完成した **localhost 専用版の ProxyConsole** を会社環境（社内開発環境・共有検証環境等）へ導入する候補として評価するため、実コード・Docker Compose 定義・コンテナランタイム状態・生成アーティファクト・自動バックアップ／リストア検証スクリプト（`./scripts/check-company-readiness.sh`）の観測事実に基づき整理したものである。

- **推測の排除（Rule 1）**: すべて実ファイルと再現可能なコマンド出力に基づく事実のみを記載する。
- **機密値の非出力・非コミット（Rule 2）**: 評価およびバックアップ検証には合成データ（synthetic data）のみを使用し、秘密鍵や実キャプチャ本文は一切出力・コミットしない。
- **localhost 前提と外部公開要件の分離（Rule 3）**: 現在の localhost 単一開発者境界でそのまま成立する事項と、社内 LAN / 共有ホスト公開時に追加で必須となる制御を明確に分離する。
- **会社判断の明示（Rule 8）**: 社内 CA、SSO/IdP、データ保持規程、共有サーバ運用方針など会社固有ポリシーに依存する項目は `company_decision_required` として分類し、独断で仕様や認証方式を実装しない。

---

## 2. コンテナ・ネットワーク・ホスト公開ポート・マウント境界インベントリ（AC-2, AC-3）

### 2.1 コンテナおよびホスト公開ポート・マウント一覧

`docker compose -f deploy/compose/docker-compose.yml config --format json` および `docker compose ps --format json` による機械的インベントリ結果は以下のとおりである。

| サービス名 | 役割 | コンテナ実行ユーザー | ホスト公開ポート（Bind Address : Host Port -> Target Port） | 内部 Expose ポート | 接続ネットワーク | ボリューム / バインドマウント |
| --- | --- | --- | --- | --- | --- | --- |
| `edge` | 外部 HTTPS/TLS 終端 Nginx | master: `root` / worker: `nginx` | `0.0.0.0:${EDGE_HTTPS_PORT:-8443}->443/tcp` | `443/tcp` | `edge_inspector_net` | なし（証明書はイメージビルド時に `/etc/nginx/certs/` へ COPY） |
| `inspector` | mitmproxy リバースプロキシ・キャプチャ・変更ルール適用・通知観測 | `mitmproxy` | **なし（非公開）** | `8080/tcp`, `8081/tcp` | `edge_inspector_net`, `inspector_router_net` | `capture_data:/var/lib/proxyconsole` (rw) |
| `control-api` | 管理 REST API・WebSocket ライブ配信・Web コンソール配信・Docker 発見・Retention 実行 | 起動時 `chmod 0666 /var/run/docker.sock` のみ `root`、本体プロセスは `gosu mitmproxy` | **`127.0.0.1:${CONTROL_API_PORT:-18480}->18480/tcp`**（localhost 限定） | `18480/tcp` | `edge_inspector_net` | `capture_data:/var/lib/proxyconsole` (rw), `/var/run/docker.sock:/var/run/docker.sock` (rw) |
| `router` | 内部 Host ルーティング Nginx + 原子的設定反映ウォッチャー | watcher/master: `root` / worker: `nginx` | **なし（非公開）** | `8080/tcp` | `inspector_router_net`, `router_targets_net` | `capture_data:/var/lib/proxyconsole` (rw) |
| `echo-a` | 検証用 HTTP/WebSocket Echo ターゲット A | `root`（`python:3.12-alpine` 既定） | **なし（非公開）** | `8080/tcp` | `router_targets_net` | なし |
| `echo-b` | 検証用 HTTP/WebSocket Echo ターゲット B | `root`（`python:3.12-alpine` 既定） | **なし（非公開）** | `8080/tcp`, `8081/tcp` | `router_targets_net` | なし |

### 2.2 ネットワーク分離境界

| ネットワーク名 | ドライバ | `internal` 設定 | 接続サービス | セキュリティ・トラスト境界の評価 |
| --- | --- | --- | --- | --- |
| `edge_inspector_net` | `bridge` | `false` | `edge`, `inspector`, `control-api` | 外部トラフィック入口（`edge`）およびリプレイ送信元（`control-api` -> `https://edge:443`）が接続するネットワーク。`router` やターゲットコンテナには直接到達できない。 |
| `inspector_router_net` | `bridge` | **`true`（外部通信遮断）** | `inspector`, `router` | `inspector` から内部 `router` への転送専用内部ネットワーク。ホストや外部ネットワークへのゲートウェイを持たない。 |
| `router_targets_net` | `bridge` | **`true`（外部通信遮断）** | `router`, `echo-a`, `echo-b`（および将来の対象コンテナ） | 内部 `router` から各バックエンドターゲットへの転送専用内部ネットワーク。外部からターゲットへ直接到達することはできない。 |

### 2.3 管理 API / Web コンソールの Localhost 境界と外部バインド時の影響（AC-3, Rule 3）

- **現状のランタイム確認結果（Localhost 境界の成立）**:
  - `deploy/compose/docker-compose.yml` の 69 行目で `"127.0.0.1:${CONTROL_API_PORT:-18480}:18480"` と明示されており、`docker compose ps --format json` の `Publishers` でも `URL: "127.0.0.1", PublishedPort: 18480` であることが確認された。
  - これにより、同一マシン上のプロセスまたは明示的な SSH ローカルポートフォワーディング以外からは、管理 UI（`/`）、管理 API（`/api/v1/*`）、WebSocket（`/api/v1/notifications/ws`）へネットワーク到達できない。
- **外部バインド（`0.0.0.0` や社内 LAN IP への公開）を行った場合の影響とリスク**:
  1. **無認証での全機能操作リスク**: 現在の管理 API・Web コンソールはローカル専用設計（Rule 10）のため、認証・認可（ログイン、API トークン、RBAC、CSRF トークン）を持たない。外部バインドすると、到達可能な任意の端末から以下が可能になる：
     - 保存済み全 HTTP キャプチャ（`Authorization` ヘッダー、`Cookie`、個人情報や業務データを含むリクエスト本文）の閲覧・cURL/fetch エクスポート
     - ルート設定の変更・削除および内部コンテナへのトラフィック転送先変更
     - 自動変更ルール（Modification Rules）の追加による通過トラフィックのヘッダー・クエリ・Cookie・JSON/Form 本文の書き換え
     - 任意のリクエストドラフト編集およびバックエンドへの Replay 実行（SSRF 相当の内部リクエスト送信）
     - `/api/v1/discovery/config` を介した Docker ソケットパスの変更やコンテナ構成の列挙
  2. **平文 HTTP 通信**: `control-api`（`18480`）自体は HTTP（非 TLS）で待受しているため、LAN 上へ直接公開すると管理通信およびキャプチャ閲覧データが平文で流れる。
  3. **結論**: 管理ポート `18480` を `127.0.0.1` 以外にバインドすることは、認証・TLS・RBAC・監査ログを実装するまで **厳禁** である。

---

## 3. 保存データ・機密性・Retention・バックアップ対象インベントリ（AC-4, Rule 4）

### 3.1 保存データ種別一覧表

| データ種別 | 保存場所（コンテナ内 / ホスト側） | フォーマット | 保持期間（Retention） | 機密性・含まれる情報 | バックアップ対象 | 削除・初期化方法 |
| --- | --- | --- | --- | --- | --- | --- |
| **HTTP キャプチャメタデータ & 解析ビュー** | `capture_data` ボリューム: `/var/lib/proxyconsole/captures/cap-*.json`<br>（`capture-ctl.sh sync` 時はホスト `var/captures/`） | JSON（Capture Contract `v0`） | 既定: 最小保持 24 時間（`86400s`）、Soft 上限 `20 GiB`、Hard 上限 `30 GiB`、定期清掃周期 `300s` | **高（High）**: リクエストの raw path、raw query、全リクエストヘッダー（`Authorization`, `Cookie`, API キー等を含む）、解析済み JSON/Form/Multipart ビュー、変更ルール適用前後の差分（`before`/`after`）を含む。 | 対象（業務監査・障害調査で保持が必要な場合） | 自動: Retention エンジン（`/api/v1/retention/run` または定期実行）<br>全体初期化: `make clean` |
| **HTTP リクエスト生ボディ Blob** | `capture_data` ボリューム: `/var/lib/proxyconsole/blobs/cap-*.bin`<br>（`sync` 時はホスト `var/blobs/`） | 生バイナリ（`.bin`、最大 `CAPTURE_MAX_BODY_BYTES` 既定 1 MiB） | 上記キャプチャ JSON と連動して削除（孤立 Blob も Retention 清掃時に自動削除） | **高（High）**: POST/PUT/PATCH 等の生リクエストボディ（認証情報、トークン、個人情報、アップロードファイル断片等）。 | 対象（キャプチャ JSON とセットで必須） | 自動: Retention エンジン（親 JSON 削除時および孤立 Blob 検知時）<br>全体初期化: `make clean` |
| **SQLite インデックス & 管理状態 DB** | `capture_data` ボリューム: `/var/lib/proxyconsole/database/captures.sqlite3`（および `-wal`, `-shm`） | SQLite 3（WAL モード、Schema `v7`） | 永続（`captures_index` 行のみ Retention 削除に連動。他テーブルは明示削除まで保持） | **中〜高（Medium-High）**:<br>・`captures_index`: ホスト・パス・クエリ・ステータス（ヘッダー値や生ボディは非保持）<br>・`routes`, `route_apply_state`, `route_apply_runs`: ルート定義と反映履歴<br>・`request_drafts`, `replay_runs`: **編集済みヘッダー・Cookie・ボディを含むドラフト**と再送履歴<br>・`modification_rules`: **自動注入するヘッダー値・Cookie・JSON 値を含むルール定義**<br>・`notification_incidents`, `notification_events`: 集約インシデントとイベント履歴（ヘッダー値やボディは非保持）<br>・`retention_policy`, `retention_runs`: 保持設定と清掃履歴 | **必須（Core State）** | 個別: 各 REST API / CLI（`route-delete`, `draft-delete`, `rule-delete` 等）<br>インデックス再構築: `./scripts/capture-ctl.sh index-rebuild`<br>全体初期化: `make clean` |
| **Nginx ルーター稼働設定 & 状態** | `capture_data` ボリューム: `/var/lib/proxyconsole/router/`（`active.conf`, `previous.conf`, `candidates/`, `requests/`, `responses/`, `nginx-error.log`） | Nginx 設定テキスト / エラーログ | `active.conf` / `previous.conf` はルート反映ごとに更新。`nginx-error.log` はボリューム存続中追記 | **低〜中（Low-Medium）**: ドメイン名、内部 upstream ホスト/ポート、Nginx warn/error ログ行（接続失敗理由・Host 名を含む）。 | `active.conf` / `previous.conf` は推奨（`routes` テーブルから `/api/v1/routes/apply` でも再生成可能） | ルート再反映: `POST /api/v1/routes/apply`<br>ログ切り詰め: コンテナ内で `: > /var/lib/proxyconsole/router/nginx-error.log` |
| **開発用 TLS サーバ証明書 & 秘密鍵** | ホスト: `var/certificates/edge.crt` (`0644`), `var/certificates/edge.key` (`0600`)<br>コンテナ内: `edge` の `/etc/nginx/certs/`、`inspector`/`control-api` の `/etc/proxyconsole/certs/edge.crt` | PEM 形式（X.509 自己署名証明書・RSA 2048-bit 秘密鍵） | 有効期限 365 日（再生成まで保持） | **高（High / Secret）**: `edge.key` は TLS 終端の秘密鍵。`.gitignore` により Git コミット対象外。 | 対象（カスタム証明書や社内 CA 証明書を使用する場合） | 再生成: `./scripts/generate-certs.sh --force` の後に `docker compose build edge inspector control-api && ./scripts/up.sh` |

### 3.2 Capture Fidelity（完全性）と機密情報保護の分離評価（Rule 4）

P1〜P5 の設計原則により、現在の ProxyConsole は「意味的リプレイに必要な HTTP リクエスト情報を欠損なく保存する（Capture Fidelity）」ことを最優先としている。会社環境へ導入する際は、以下の 3 段階の選択肢を混同せず、会社ポリシーに応じて選択する必要がある。

| アプローチ | 仕組み | リプレイ・デバッグへの影響 | 評価分類 |
| --- | --- | --- | --- |
| **A. 表示・エクスポート時マスキング（Display/Export Masking）** | 保存データ（JSON/Blob）はそのまま保持し、Web コンソール画面表示および cURL / fetch エクスポート時に `Authorization`, `Cookie`, `X-Api-Key` 等を既定で `***` 表示にする（明示トグルで表示）。 | **影響なし**（生データが保持されるため、そのままリプレイ可能）。画面共有やスクリーンショット時の意図せぬ漏洩を防げる。 | `code_change_required`（機能を壊さない最優先の推奨改善） |
| **B. 選択的削除・パージ（Selective Purge / Redaction API）** | 特定の `capture_id`、特定の `route_id`、または期間を指定して、不要になったキャプチャ JSON・Blob・ドラフトを即座に完全削除する API / UI ボタンを提供する。 | 削除対象外のキャプチャのリプレイには影響しない。誤って機密情報を送信した直後の事後消去が可能になる。 | `code_change_required`（運用監査・データ最小化に有効） |
| **C. 保存時スクラビング（Ingest-Time Redaction）** | `addon.py` がディスクへ書き込む前に特定ヘッダーや JSON フィールドを不可逆にマスクする。 | **リプレイ機能が劣化する**（マスクされた `Authorization` や `Cookie` のままリプレイされるため、認証が必要なバックエンドへの再送が失敗する）。 | `company_decision_required`（社内規定でディスクへの生トークン保存が全面禁止されている場合のみ採用） |

---

## 4. TLS 証明書・秘密鍵の生成・保管・更新・期限確認（AC-5）

1. **生成場所とアルゴリズム**:
   - `./scripts/generate-certs.sh` が `deploy/certificates/openssl.cnf` を用いてホスト側の `var/certificates/edge.key`（RSA 2048-bit）と `var/certificates/edge.crt`（SHA-256 自己署名 X.509、有効期間 365 日）を生成する。
   - Subject Alternative Name (SAN): `DNS:app-a.local.test`, `DNS:app-b.local.test`, `DNS:*.local.test`, `DNS:localhost`, `IP Address:127.0.0.1`。
2. **ファイル権限と Git 除外の検証結果**:
   - ホスト上の権限: `edge.key` は `0600` (`-rw-------`)、`edge.crt` は `0644` (`-rw-r--r--`)。
   - `.gitignore` に `var/`、`*.key`、`*.pem`、`*.crt` が登録されており、`git check-ignore -q var/certificates/edge.key var/certificates/edge.crt` が終了コード `0`（除外済み）であることを `./scripts/check-company-readiness.sh` Step 3 で確認済み。
3. **コンテナへの配布方式と注意点**:
   - 現在、`deploy/nginx/edge/Dockerfile` が `edge.crt` と `edge.key` をイメージ内に `COPY` し、`services/recorder/Dockerfile` がリプレイ検証用に `edge.crt` を `/etc/proxyconsole/certs/edge.crt` へ `COPY` している。
   - **運用上の注意**: ホストの `var/certificates/` 上の証明書を差し替えた場合、単なるコンテナ再起動（`docker compose restart`）ではなく、**イメージの再ビルドを伴う `./scripts/up.sh`（内部で `docker compose build` と `up -d` を実行）が必要**である。また、ビルド済みイメージのレイヤ内に `edge.key` が含まれるため、**ビルドした `proxyconsole-dev001-edge` イメージを社内・外部の Docker Registry へ push してはならない**（バインドマウント方式への移行は後続タスク候補）。
4. **有効期限確認コマンド**:
   ```bash
   # 有効期限日時と SAN の確認
   openssl x509 -in var/certificates/edge.crt -noout -subject -dates -ext subjectAltName
   # 30日（2592000秒）以内に期限切れにならないかの機械的チェック（0=有効、1=期限切れ間近）
   openssl x509 -in var/certificates/edge.crt -noout -checkend 2592000
   ```
5. **証明書の交換・ローテーション手順**:
   - **開発用自己署名証明書の再生成**:
     ```bash
     ./scripts/generate-certs.sh --force
     ./scripts/up.sh
     ```
   - **社内 CA 発行証明書への差し替え**:
     1. 追加ドメインがある場合は社内 CA で SAN に該当ドメインを含めたサーバ証明書（PEM）と秘密鍵を発行する。
     2. サーバ証明書（中間 CA 証明書がある場合は結合した fullchain）を `var/certificates/edge.crt`（`chmod 644`）、秘密鍵を `var/certificates/edge.key`（`chmod 600`）に配置する（自己署名でない社内 CA の場合、`inspector`/`control-api` の `REPLAY_CACERT` には社内ルート CA 証明書またはチェーン証明書を指定する）。
     3. `./scripts/up.sh` を実行して `edge` / `inspector` / `control-api` を再ビルド・再起動する。

---

## 5. Docker Socket マウントと使用 API オペレーションのインベントリ（AC-6, Rule 5）

### 5.1 マウント状況とプロセス権限

- **マウント対象**: `control-api` コンテナのみが `/var/run/docker.sock:/var/run/docker.sock` をマウントしている（`edge`, `inspector`, `router`, `echo-a`, `echo-b` はマウントしていない）。
- **コンテナ起動時の処理**: `deploy/compose/docker-compose.yml` の 67 行目にて、`chmod 0666 /var/run/docker.sock 2>/dev/null || true; exec gosu mitmproxy python3 /app/recorder/api_server.py` を実行している。これは Docker Desktop / WSL 環境でソケット GID がコンテナ内 `mitmproxy` ユーザーと一致しない問題を回避するためであるが、ソケット自体のパーミッションを `0666` に変更する動作を伴う。

### 5.2 コード上で実際に呼び出している Docker Engine API オペレーション（機械的抽出結果）

`services/recorder/docker_discovery.py`（`_docker_get_json` メソッド、126〜155 行目）を走査した結果、ProxyConsole が使用する Docker API は以下の **2 つの読み取り専用（`GET`）エンドポイントのみ** である。

| HTTP メソッド | Docker Engine API エンドポイント | 呼び出し箇所 (`docker_discovery.py`) | 用途 | 必要な権限種別 |
| --- | --- | --- | --- | --- |
| `GET` | `/containers/json?all=1` | `_scan_docker()` (465行目) | コンテナ一覧、ラベル（`com.docker.compose.project`, `com.docker.compose.service`, `proxyconsole.role`, `proxyconsole.discoverable`）、稼働状態、ネットワーク一覧の取得 | **Read-only（Containers 読み取り）** |
| `GET` | `/containers/{id}/json` | `_scan_docker()` (489行目) | 対象スコープ内コンテナの詳細 `State.Running`、`Config.ExposedPorts`、`NetworkSettings.Ports`、`NetworkSettings.Networks`（内部 IP・DNS エイリアス）の取得 | **Read-only（Containers 読み取り）** |

- **Write / Lifecycle オペレーションの不使用証明**:
  - コンテナの作成（`POST /containers/create`）、起動・停止・再起動（`POST /containers/{id}/start|stop|restart`）、削除（`DELETE`）、コマンド実行（`POST /containers/{id}/exec`）、イメージ操作、ボリューム操作等は **一切実装・使用していない**。
- **セキュリティ評価と最小権限化オプション**:
  - Docker Engine の仕様上、生の `/var/run/docker.sock` をマウントすると、API クライアント側が `GET` しか使っていなくても、ソケット自体はホスト root 相当の権限を持つ。
  - **対策 1（設定・運用で即対応可能）**: Docker 発見機能が不要な環境（手動で `upstream_host` と `upstream_port` を指定する運用）では、`/var/run/docker.sock` マウントを外すか、`POST /api/v1/discovery/config` で無効パスを設定しても、フォールバック設計（DEV-021 AC-7）により全ルート管理・プロキシ機能が正常に動作する。
  - **対策 2（推奨後続タスク）**: `tecnativa/docker-socket-proxy` 等の読み取り専用ソケットプロキシ（`CONTAINERS=1`, `POST=0`）を挟む構成、または TCP/Unix ソケットプロキシ経由に制限することで、万一 `control-api` が侵害されてもコンテナ操作やホスト特権昇格を防止できる。

---

## 6. 依存関係・ベースイメージ・ライセンス・Version Pinning インベントリ（AC-7）

`./scripts/check-company-readiness.sh` Step 4 により実行中コンテナおよびイメージから抽出した結果は以下のとおりである。

### 6.1 ベースイメージと Version Pinning の現状

| 使用箇所（Dockerfile） | 指定タグ | 実測 OS / ランタイムバージョン | ローカル検証時の Image Digest (`RepoDigests`) | Version Pinning 評価 |
| --- | --- | --- | --- | --- |
| `deploy/nginx/edge/Dockerfile`<br>`deploy/nginx/router/Dockerfile` | `nginx:1.27-alpine` | Alpine Linux `v3.21.3` / `nginx/1.27.5` | `nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10` | マイナーバージョン（`1.27-alpine`）まで固定。パッチ更新や SHA-256 ダイジェスト固定は未実施（`config_required`）。 |
| `services/recorder/Dockerfile` (`inspector`, `control-api`) | `mitmproxy/mitmproxy:latest` | Debian GNU/Linux `13 (trixie)` / Python `3.13.7` / Mitmproxy `12.1.2` / OpenSSL `3.5.2` | `mitmproxy/mitmproxy@sha256:0d7b2a14b4a71a908cd6f31b8f50e29bbc5c8720fe6caa706acf782da558e5c7` | **`latest` タグを使用中**。再ビルド時期により破壊的変更が入る可能性があるため、社内導入時は `mitmproxy/mitmproxy:12.1.2` または `@sha256:...` への固定を推奨（`config_required`）。 |
| `tests/helpers/Dockerfile` (`echo-a`, `echo-b`) | `python:3.12-alpine` | Alpine Linux `v3.22.1` / Python `3.12.11` | `python@sha256:9b8808206f4a956130546a32cbdd8633bc973b19db2923b7298e6f90cc26db08` | マイナーバージョン（`3.12-alpine`）まで固定。 |

### 6.2 アプリケーション直接依存関係（Direct Dependencies）

- **バックエンド (`services/recorder/*.py`)**:
  - `addon.py` のみが `mitmproxy`（`from mitmproxy import http`）に直接依存。
  - その他の全モジュール（`api_server.py`, `index_store.py`, `retention.py`, `route_manager.py`, `docker_discovery.py`, `draft_replay.py`, `rule_engine.py`, `notification_engine.py`, `parser.py`, `capture_cli.py`）は **Python 標準ライブラリのみ**（`sqlite3`, `http.server`, `http.client`, `ssl`, `urllib`, `json`, `hashlib`, `fcntl`, `threading`, `select` 等）で実装されており、追加の `pip install` パッケージは **0 件** である。
- **フロントエンド (`web/console/*`)**:
  - `index.html`, `styles.css`, `app.js` は **外部 NPM パッケージ・外部 CDN・フロントエンドフレームワーク一切なし（0 件）** の Vanilla HTML/CSS/JS で実装されている。インターネット非接続（エアギャップ）環境でもフォントやスクリプトの読み込み失敗は発生しない。

### 6.3 コンテナ内 Python パッケージ（Transitive Dependencies: 全 46 パッケージ）とライセンス

`mitmproxy/mitmproxy:latest`（`12.1.2`）イメージ内にプリインストールされている全 46 パッケージのバージョンとライセンス（`importlib.metadata` による抽出結果）：

| ライセンス区分 | パッケージ名（バージョン） | 会社利用上の評価 |
| --- | --- | --- |
| **MIT / BSD / Apache-2.0 / PSF-2.0 / MPL-2.0**（計 44 パッケージ） | `mitmproxy (12.1.2, MIT)`, `mitmproxy_linux (0.12.7, MIT)`, `mitmproxy_rs (0.12.7, MIT)`, `aioquic (1.2.0, BSD-3-Clause)`, `argon2-cffi (25.1.0, MIT)`, `argon2-cffi-bindings (25.1.0, MIT)`, `asgiref (3.9.1, BSD-3-Clause)`, `attrs (25.3.0, MIT)`, `blinker (1.9.0, MIT)`, `Brotli (1.1.0, MIT)`, `certifi (2025.8.3, MPL-2.0)`, `cffi (1.17.1, MIT)`, `click (8.2.1, BSD-3-Clause)`, `cryptography (45.0.6, Apache-2.0 OR BSD-3-Clause)`, `Flask (3.1.2, BSD-3-Clause)`, `h11 (0.16.0, MIT)`, `h2 (4.3.0, MIT)`, `hpack (4.1.0, MIT)`, `hyperframe (6.1.0, MIT)`, `itsdangerous (2.2.0, BSD)`, `Jinja2 (3.1.6, BSD)`, `kaitaistruct (0.10, MIT)`, `MarkupSafe (3.0.2, BSD-3-Clause)`, `msgpack (1.1.1, Apache-2.0)`, `passlib (1.7.4, BSD)`, `pip (25.2, MIT)`, `publicsuffix2 (2.20191221, MIT and MPL-2.0)`, `pyasn1 (0.6.1, BSD-2-Clause)`, `pyasn1_modules (0.4.2, BSD)`, `pycparser (2.22, BSD-3-Clause)`, `pylsqpack (0.3.22, BSD-3-Clause)`, `pyOpenSSL (25.1.0, Apache-2.0)`, `pyparsing (3.2.3, MIT)`, `pyperclip (1.9.0, BSD)`, `ruamel.yaml (0.18.15, MIT)`, `ruamel.yaml.clib (0.2.12, MIT)`, `service-identity (24.2.0, MIT)`, `sortedcontainers (2.4.0, Apache-2.0)`, `tornado (6.5.2, Apache-2.0)`, `typing_extensions (4.14.1, PSF-2.0)`, `wcwidth (0.2.13, MIT)`, `Werkzeug (3.1.3, BSD)`, `wsproto (1.2.0, MIT)`, `zstandard (0.24.0, BSD)` | 寛容なオープンソースライセンスであり、社内ツール利用において問題なし。 |
| **LGPL 系**（計 2 パッケージ） | `ldap3 (2.9.1, LGPL-3.0)`, `urwid (2.6.16, LGPL-2.1-only)` | `mitmproxy` の TUI/LDAP 補助依存として同梱（ProxyConsole からは直接 import していない）。社内利用（非再配布）では通常問題にならないが、法務ポリシーがある場合は確認対象。 |
| **取得不能・制限事項** | OS レベルの C ライブラリ（Debian `apt` / Alpine `apk` パッケージ）の SBOM | コンテナ内で `dpkg-query -W` / `apk info -v` により一覧化可能（Runbook 参照）。 |

---

## 7. ディスク使用量・Retention 制御・SQLite 成長・Disk Exhaustion 挙動（AC-8）

### 7.1 ストレージ計測スコープと Retention 境界

- **計測スコープ (`CALCULATION_SCOPE`)**:
  - `services/recorder/retention.py` の `get_storage_usage()` は、`captures/*.json`（`captures_bytes`）+ `blobs/*.bin`（`blobs_bytes`）+ `database/captures.sqlite3*`（`index_db_bytes`）の合計を `total_bytes` として算出する。計測前に必ず `PRAGMA wal_checkpoint(TRUNCATE)` を実行するため、一時的な WAL ファイル膨張による誤判定は発生しない。
- **既定ポリシーと優先順位**:
  - `min_retention_seconds`: `86400` 秒（24 時間）
  - `soft_limit_bytes`: `20 GiB`（`21,474,836,480` bytes）— 24 時間以上経過した古いキャプチャから削除。24 時間未満のキャプチャは保護される。
  - `hard_limit_bytes`: `30 GiB`（`32,212,254,720` bytes）— **24 時間未満の保護を上書き**し、`total_bytes <= hard_limit_bytes`（可能なら `soft_limit_bytes`）に収まるまで最も古いキャプチャから強制削除する。
  - `periodic_interval_seconds`: `300` 秒（5 分ごとに `control-api` バックグラウンドタイマーが自動実行）。

### 7.2 SQLite テーブル成長と未制限領域の評価

- `captures_index` テーブルは Retention 清掃によってレコードが削除され、削除後に `checkpoint_wal()` が走るため無制限には肥大化しない（ただし SQLite ファイル自体のフリーページ回収には明示的な `VACUUM` が必要な場合がある）。
- **注意点（長期運用時の緩やかな増加対象）**:
  - `notification_incidents` および `notification_events`（1 イベントあたり約 400〜600 bytes）、`replay_runs`、`retention_runs`、`route_apply_runs`、`router/nginx-error.log` は現在 Retention 自動削除の対象外である。
  - 5 分ウィンドウ集約により通知イベント数は大幅に抑制されているが、数ヶ月〜数年の連続稼働を行う共有環境では、古い解決済みインシデント（例: 30 日以上前の `resolved` インシデントと関連イベント）および `nginx-error.log` をローテーション・パージする仕組みの追加が推奨される（`code_change_required` 候補）。

### 7.3 ディスク枯渇・書き込み不能（Disk Exhaustion / Read-Only）時の挙動と回復手順（実証結果）

`./scripts/check-company-readiness.sh` Step 7 において、`/var/lib/proxyconsole/captures` および `/var/lib/proxyconsole/blobs` を書き込み禁止（`0555`）にしてディスク枯渇・I/O 障害をシミュレートし、以下を実証した。

1. **プロキシ転送の継続（Fail-Open 保証）**:
   - `services/recorder/addon.py` の `_finalize_flow()`（354〜400 行目）は、Blob 書き込み、JSON 書き込み、SQLite インデックス更新、通知記録のすべてを `try ... except Exception` で分離している。
   - そのため、ディスク満杯や I/O エラーでキャプチャ保存が失敗しても、**クライアントからターゲット（`echo-a` 等）への HTTPS リクエスト転送とレスポンス返却は HTTP 200 で正常に完了する**。
2. **管理 API の挙動**:
   - 読み取り API（`/api/v1/health`, `/api/v1/routes`, `/api/v1/captures` 等）は継続動作する。書き込み API は HTTP 500/503 JSON エラーを返すがプロセスはクラッシュしない。
3. **回復手順**:
   1. ホストのディスク空き容量を確認（`df -h`）し、不要ファイルの削除または Retention 上限の引き下げ＋即時清掃を実行する：
      ```bash
      ./scripts/capture-ctl.sh retention-set --soft-limit-bytes 1073741824 --hard-limit-bytes 2147483648
      ./scripts/capture-ctl.sh retention-run
      ```
   2. 孤立ファイルや破損レコードがある場合は `./scripts/capture-ctl.sh index-rebuild` を実行する。
   3. 書き込み可能に戻った直後から、プロキシ再起動なしで新規キャプチャの保存（`X-Capture-Status: complete`）が自動再開されることを Step 7 で確認済み。

---

## 8. バックアップ／リストア検証結果（AC-9, Rule 6）

`./scripts/check-company-readiness.sh` の Step 5〜Step 6 において、アーカイブ作成だけでなく、**ボリュームを完全に破棄（`down.sh` で `docker compose down -v`）した新規スタックへ復元し、全エンティティを読み戻して実トラフィックを流す検証**を実施した。

### 8.1 検証した合成データセットと復元結果

| 対象エンティティ | 投入した合成データ | ボリューム完全破棄後のリストア検証結果 |
| --- | --- | --- |
| **Route & Active Nginx Config** | `route_id: "route:readiness-app"`, `domain: "app-readiness.local.test"` -> `http://echo-b:8080` | `GET /api/v1/routes/route:readiness-app` で完全一致。リストア直後の `https://app-readiness.local.test:8443/v1/readiness/post-restore` が `echo-b` へ正常ルーティングされた。 |
| **Modification Rule** | `rule_id: "rule:readiness-header"`, `X-Readiness-Rule: applied-synthetic` | `GET /api/v1/rules/rule:readiness-header` で完全一致。リストア後の実リクエストに対してヘッダー `X-Readiness-Rule: applied-synthetic` が自動付与された。 |
| **HTTP Capture & Raw Blob** | POST `/v1/readiness/check?mode=backup`（JSON ボディ 59 bytes, SHA-256 `f0d9393ff14b...`） | `GET /api/v1/captures/<id>` でメタデータ・解析ビュー（`content_kind: "json"`）・Blob SHA-256 が完全一致。 |
| **Request Draft & Replay History** | 上記キャプチャから作成した Draft（`DEV-050 Readiness Draft`）と Replay 実行履歴 1 件 | `GET /api/v1/drafts/<id>` でドラフト定義・`source_capture_id`・`replays_count == 1` が完全一致。 |
| **Notification Incident & Events** | `http_502_503_504` インシデント（`acknowledged: true`）とイベント `seq` | `GET /api/v1/notifications/incidents/<id>` でインシデント状態・Ack 状態・イベント `seq` 履歴が完全一致。 |
| **Retention Policy** | `min_retention_seconds: 43200`（12 時間） | `GET /api/v1/retention/status` で `43200` が保持されていることを確認。 |

> **技術的知見（SQLite オンラインバックアップ）**: `control-api` のバックグラウンド監視スレッドが稼働中に `/var/lib/proxyconsole/database` ディレクトリを直接 `tar` すると、WAL/SHM ファイルの更新と競合して `tar: database: file changed as we read it` が発生し得る。そのため、バックアップ時は Python の `sqlite3.Connection.backup()` API を用いて一貫性のあるスナップショットを作成してからアーカイブ化する手順を標準とし、`LOCAL_RUNBOOK.md` および `check-company-readiness.sh` に組み込んだ。

---

## 9. ログ・インシデント・キャプチャによる運用監査可能性（Auditability）の評価

| 監査対象操作 | 現在の記録先・証跡 | 監査可能性の評価と限界 |
| --- | --- | --- |
| **通過した HTTP リクエストと変更ルール適用結果** | `captures/cap-*.json`（`captured_at`, `route_id`, `request`, `forwarding`, `rule_evaluation`）および `captures_index` テーブル | **高**: いつ・どのルートへ・どのリクエストが到達し、どの変更ルールがどのフィールドを `before` -> `after` に書き換えたかがリクエスト単位で完全に追跡可能。 |
| **ルート設定の変更・Nginx 反映履歴** | `routes` テーブル（`created_at`, `updated_at`, `copied_from_route_id`）および `route_apply_runs` テーブル（`apply_id`, `ran_at`, `trigger_action`, `status`, `rolled_back`, `config_sha256`） | **中〜高**: 「いつ・どのアクション（例: `create:route:app-c`）で設定世代が上がり、どの SHA-256 の Nginx 設定が適用されたか」が永続記録される。ただし単一ローカルユーザー前提のため「誰が（User ID / クライアント IP）」は記録されない。 |
| **リクエストのリプレイ実行履歴** | `replay_runs` テーブル（`replay_id`, `draft_id`, `source_capture_id`, `result_capture_id`, `replayed_at`, `response_status`, `duration_ms`） | **高**: どの元キャプチャ・ドラフトからいつリプレイが実行され、結果としてどの新規キャプチャ（`result_capture_id`）が生成されたかが完全に紐付く。 |
| **Retention 清掃・削除履歴** | `retention_runs` テーブル（`run_id`, `ran_at`, `trigger_source`, `deleted_captures_count`, `freed_bytes`, `bytes_before`, `bytes_after`, `details_json`） | **高**: 手動または定期実行でいつ・何件・何バイト削除されたかが理由とともに永続記録される。 |
| **プロキシ障害・ターゲット停止・復旧履歴** | `notification_incidents` および `notification_events` テーブル（単調増加 `seq`、初回/集約更新/復旧/Ack の全タイムライン）、`/var/lib/proxyconsole/router/nginx-error.log` | **高**: 5xx、タイムアウト、Nginx warn/error、コンテナ停止の発生時刻・回数・復旧時刻・Ack 日時が SQLite に永続化されている。 |

---

## 10. Readiness Matrix（分類・根拠・推奨後続タスク一覧）（AC-11, Rule 7, Rule 8）

各評価項目を以下の 4 区分で判定する：
- `ready`: 現在の実装のまま利用可能
- `config_required`: コード変更なしで設定・Compose オーバーライド・運用手順のみで対応可能
- `code_change_required`: 会社環境での共有・長期運用に向けて後続タスクでのコード変更を推奨
- `company_decision_required`: 会社固有のセキュリティ・インフラ・法務ポリシーの決定が前提として必要

| # | 評価領域・項目 | 分類 | 実証根拠（Evidence） | 推奨対応 / 後続タスク案 |
| --- | --- | --- | --- | --- |
| 1 | **単一開発者端末（WSL / Docker Desktop）でのローカル利用** | **`ready`** | `control-api` は `127.0.0.1:18480` に限定バインドされ、内部ネットワーク `inspector_router_net` / `router_targets_net` は `internal: true` で隔離済み（`check-company-readiness.sh` Step 2）。 | そのまま社内開発者のローカル検証環境として利用可能。 |
| 2 | **プロキシ転送・キャプチャ・リプレイ・変更ルール・集約通知の機能信頼性と障害分離** | **`ready`** | DEV-001〜DEV-042 の全 12 統合テストスイートおよびディスク書き込み不能時の Fail-Open テスト（Step 7）がすべて PASS。 | そのまま利用可能。 |
| 3 | **設定・SQLite・Captures・Blobs・証明書のバックアップとリストア** | **`ready`** | `sqlite3.Connection.backup()` を用いた一貫性スナップショットにより、全データ種別（Route, Rule, Capture, Blob, Draft, Replay, Notification, Retention Policy）の完全復元を Step 5〜6 で実証。 | `docs/operations/LOCAL_RUNBOOK.md` の手順に従い定期または作業前バックアップを実施。 |
| 4 | **コンテナベースイメージのバージョン・ダイジェスト固定** | **`config_required`** | `services/recorder/Dockerfile` が `mitmproxy/mitmproxy:latest` を参照しており、ビルド時期により Python/mitmproxy バージョンが変動し得る（Step 4）。 | `Dockerfile` の `FROM` を `mitmproxy/mitmproxy:12.1.2@sha256:0d7b2a14...` 等へ固定する。 |
| 5 | **不要環境での Docker Socket マウント無効化** | **`config_required`** | 手動 upstream 指定（`upstream_host` + `upstream_port`）のみを使う場合、`control-api` から `/var/run/docker.sock` マウントを外しても全ルート管理・プロキシ機能が動作する（DEV-021 AC-7）。 | コンテナ自動発見が不要な環境では Compose override で `/var/run/docker.sock` マウントを除外する。 |
| 6 | **Retention 上限値（Soft / Hard / 保持時間）の環境別調整** | **`config_required`** | 既定は 24h / Soft 20 GiB / Hard 30 GiB。API（`PUT /api/v1/retention/policy`）または CLI（`capture-ctl.sh retention-set`）から無停止で変更可能。 | 導入先マシンのディスク容量に合わせて初期値を設定する。 |
| 7 | **TLS 証明書・秘密鍵のランタイムマウント化（イメージ内 COPY の廃止）** | **`code_change_required`** | 現在 `edge/Dockerfile` が `var/certificates/edge.key` をイメージに `COPY` するため、証明書更新時に再ビルドが必要であり、またビルド済みイメージに秘密鍵が含まれる。 | **推奨タスク `DEV-051`**: `edge` / `inspector` / `control-api` の証明書を Compose の read-only バインドマウント（`./var/certificates:/etc/nginx/certs:ro`）へ変更し、再ビルドなしの `nginx -s reload` による証明書ローテーションを実現する。 |
| 8 | **Docker Socket の最小権限化（Read-Only Socket Proxy 導入 & `chmod 0666` 廃止）** | **`code_change_required`** | コード上は `GET /containers/json` と `GET /containers/{id}/json` のみ使用しているが、生ソケットマウントと起動時の `chmod 0666 /var/run/docker.sock` によりコンテナ脱出時のリスクがある（Step 2）。 | **推奨タスク `DEV-052`**: Read-only Docker Socket Proxy（GET のみ許可）コンテナの挟み込み、またはホスト docker グループ GID マッピングによる `chmod 0666` の廃止。 |
| 9 | **機密ヘッダー・ボディの表示時マスキングと選択的パージ API/UI** | **`code_change_required`** | 現在は Capture Fidelity 重視のため、`Authorization` や `Cookie` が画面・エクスポートにそのまま出力され、個別キャプチャの削除ボタンがない。 | **推奨タスク `DEV-053`**: Web コンソール表示・cURL/fetch 出力時の機密ヘッダー既定マスク（明示トグル解除）と、特定キャプチャ／ルート単位の即時パージ API・UI の追加。 |
| 10 | **通知履歴・リプレイ履歴・Nginx エラーログの長期ローテーション** | **`code_change_required`** | `captures_index` と Blob は Retention で自動削除されるが、`notification_incidents`/`events` と `router/nginx-error.log` は長期稼働で緩やかに増加し続ける。 | **推奨タスク `DEV-054`**: Retention エンジンへの「解決済み古い通知インシデント（例: 30日超）および Nginx エラーログの自動切り詰め」の統合。 |
| 11 | **社内共有ホスト展開時の管理面公開可否・認証方式（SSO / OIDC / Basic / mTLS）** | **`company_decision_required`** | 現在 `control-api` は `127.0.0.1` 専用・無認証。共有サーバへ配置する場合、SSH トンネル運用にするか、リバースプロキシ等で認証を掛けるかは社内セキュリティ基準に依存する。 | **会社確認事項 D-1**: 共有環境へ配置するか、各開発者ローカルに限定するか。共有する場合は認証方式（社内 IdP/OIDC、mTLS 等）と操作監査ログ要件を決定後に **タスク `DEV-055`** を起票。 |
| 12 | **キャプチャへの本番・社内機密データ（トークン・個人情報）保存可否ポリシー** | **`company_decision_required`** | 生のリクエストヘッダーとボディをディスクに保存する仕様上、通過させるトラフィックの種別（合成データのみか、社内ステージングデータを含むか）の規程確認が必要。 | **会社確認事項 D-2**: 保存時スクラビング（Ingest Redaction）を強制するか、表示時マスク＋保持期間短縮（例: 1時間）で承認されるかを社内セキュリティ部門へ確認。 |
| 13 | **社内 CA 証明書の発行・配布ポリシーと内部プロキシ / ミラーレジストリ要件** | **`company_decision_required`** | 現在は OpenSSL 自己署名証明書と Docker Hub パブリックイメージを使用。 | **会社確認事項 D-3**: 社内 CA 証明書の利用有無、および社内ミラーレジストリ・SBOM 提出要件（`ldap3`/`urwid` の LGPL 同梱可否を含む）を確認。 |

---

## 11. 既存機能を壊さない段階的 Hardening 推奨順序

会社環境への段階的導入にあたっては、P1〜P5 で確立したプロキシ・キャプチャ・リプレイ・ルール・通知の機能を一切損なわないよう、以下の順序で進めることを推奨する。

1. **Step 1（即時・コード変更なしで利用可能）**:
   - 各開発者のローカル環境（WSL2 / Docker Desktop）にて、`control-api` を `127.0.0.1:18480` に限定したまま利用する。
   - `LOCAL_RUNBOOK.md` に従い、Retention 上限の設定と定期バックアップを運用する。
2. **Step 2（ローカル版の安全性・運用性を高める非破壊な改善: `DEV-051`〜`DEV-054`）**:
   - `DEV-051`: 証明書のランタイム read-only バインドマウント化とベースイメージ digest 固定（イメージ内からの `edge.key` 排除）。
   - `DEV-052`: Docker Socket の Read-only Proxy 化と `chmod 0666` の廃止。
   - `DEV-053`: 画面・cURL/fetch エクスポート時の機密ヘッダー既定マスキング（保存データは維持してリプレイ互換性を100%保持）＋選択的キャプチャ削除 API。
   - `DEV-054`: 解決済み通知イベントと Nginx エラーログの自動ローテーション。
3. **Step 3（会社ポリシー決定後の共有環境対応: `DEV-055`）**:
   - 会社判断（D-1〜D-3）が確定した後にのみ、管理コンソールの TLS 化・認証認可（SSO/OIDC または mTLS）・ユーザー別監査ログ・保存時スクラビング（必要な場合のみ）を実装する。
