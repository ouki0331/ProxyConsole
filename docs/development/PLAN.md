# ProxyConsole 開発計画

更新日: 2026-11-24
計画状態: Active
現在フェーズ: P7 — Personal local dashboard & observability
実装状態: G5 passed / P6 deferred / DEV-060 review remediation planned

## 1. バージョン目標

個人用ローカル版の終了条件は、次の一連の操作を利用者が確認できることである。

1. WSL/Linux 上の Docker または Podman で、一つの HTTP/HTTPS 入口へ複数ドメインでアクセスできる。
2. 各ドメインが指定されたコンテナまたは手動指定先へ転送される。
3. 選択したルートについて、将来の意味的リプレイに必要な HTTP リクエスト情報が欠損なく保存される。
4. 管理画面からルートの作成、複製、無効化、Docker ターゲット選択ができる。
5. 保存したリクエストを検索、閲覧し、後続版では編集、リプレイ、cURL/fetch 出力ができる。
6. エラー条件を集約し、同一障害を連続通知しないローカル通知ができる。
7. 英語 / 日本語を切り替え、フォームのない Overview から全体状態を確認して keyboard で各 workspace へ移動できる。
8. Docker Compose / Podman Compose project、container、ローカル DB 状態、関連ログを個別 CLI や container path を探さず確認できる。

## 2. 現在の前提

- リポジトリは空の状態から開始する。
- 開発は `main` 一本で順次行い、並列作業はしない。
- 製品は単一利用者の localhost 開発ツールとして継続し、会社共有環境への導入は現在のロードマップから延期する。
- Nginx を TLS 終端およびホストルーティングの基盤として維持する。
- リプレイ UI は初期フェーズに含めないが、記録形式は後の編集・リプレイを妨げてはならない。
- 管理 UI は login / multi-user を持たず、localhost 以外へ公開しない。
- container runtime は Docker と Podman の両方を正式対応対象とする。開発・回帰検証は hosted CI に依存せず、runtime を明示した local CI command で実行する。

### 個人用ローカル版の優先順位

P1 から P5 の proxy / capture / replay / rule / notification を維持し、P7 では個人が日常的に状態を把握する console experience と local observability を優先する。情報漏洩フィルター、権限分離、公開環境向け hardening は localhost 前提を変更しない限り blocking gate にしない。ただし、データ破壊、誤配送、既存機能停止など機能結果に直結する問題はレビュー対象に含める。

## 3. 明示的な初期除外

- リクエストを対話的に一時停止するライブインターセプト
- WebSocket フレーム編集とリプレイ
- gRPC メッセージ編集
- マルチユーザー認証と権限管理
- 公開インターネット上の管理画面
- 会社環境への自動展開
- 会社共有 host、SSO、RBAC、multi-user operation
- 汎用 SQL client と任意 host log file の自動探索

## 4. フェーズとゲート

| Phase | 利用者が得る結果 | 主なタスク | ゲート |
| --- | --- | --- | --- |
| P0 | 計画、境界、Git 交接方法が固定される | 文書、ディレクトリ計画、Agent 規約 | G0 |
| P1 | Nginx と記録層を通る最小チェーンが実証される | DEV-001, DEV-002 | G1 |
| P2 | 再起動後も検索可能な耐久リクエスト記録が使える | DEV-010 系 | G2 |
| P3 | UI からルートと Docker ターゲットを管理できる | DEV-020 系 | G3 |
| P4 | 保存済みリクエストを編集、リプレイ、出力できる | DEV-030 系 | G4 |
| P5 | 自動変更ルールと集約通知が使える | DEV-040 系 | G5 |
| P6 | 会社環境候補として安全性と運用性を再評価できる | DEV-050 系 | G6 |
| P7 | 日英 UI、状態 Overview、local project / DB / log 集約を個人利用できる | DEV-060〜DEV-062 | G7 |

## 5. フェーズ詳細

### P0 — 計画と作業規約

含むもの:

- 単一ブランチ・単一実行者の規約
- Git を事実源とするタスク、状態、レビュー形式
- 递増ポーリングと停止条件
- 予定ディレクトリ構造
- 最初の実装タスク

G0 合格条件:

- 必須文書が `main` に push されている。
- `STATUS.md` が唯一のアクティブタスクを指している。
- DEV-001 が過去の会話なしで実行可能である。

G0 不合格時:

- 実装 Agent を起動せず、不足文書を修正する。

### P1 — プロキシおよび記録可能性の技術検証

DEV-001:

- Nginx の外部 TLS 入口、記録層、Nginx の内部ホストルーター、二つの Echo ターゲットを構成する。
- まず GET と小さな POST が正しいターゲットへ届くことを証明する。

DEV-002:

- GET、重複 query、JSON、URL-encoded form、multipart、任意バイト列を保存する。
- 保存後にプロセスを再起動し、記録を読み戻す。
- 一件をそのままリプレイし、一件の値を変更してリプレイする。
- 大容量本文は転送を壊さず、完全保存か明示的な `incomplete/non-replayable` 状態になることを証明する。

G1 合格条件:

- 二つの Host が交差せず正しいターゲットに到達する。
- 小さな検証本文はバイト単位で一致する。
- 重複 query/header を単純な map に潰さない。
- 保存データは再起動後に読み戻せる。
- 元リクエストの意味的リプレイと一項目変更後のリプレイが成功する。
- 100 MiB の検証アップロードが、設定した capture 上限を超えてもターゲットへ正常到達する。
- 上限超過、切断、解析失敗が成功扱いにならず、記録状態で識別できる。
- WebSocket は少なくとも接続を破壊せず、握手メタデータだけを記録できる。

G1 失敗時の処理:

1. 失敗を「設定不備」「選択ツールの制約」「要求との根本不整合」に分類する。
2. 設定不備なら同一構成で一回修正して再検証する。
3. ツール制約なら、Nginx は維持したまま記録層の代替を一案だけ検証する。
4. 二案とも本文破損、誤配送、無制限メモリ増加のいずれかを避けられない場合、P1 を `Blocked` として停止する。

### P2 — 耐久記録

利用者結果:

- リクエスト一覧、詳細、原始データ、解析ビューが再起動後も利用できる。
- 24 時間、soft 20 GB、設定可能な hard limit の削除規則が観測可能である。

必須境界:

- 原始本文と解析ビューを分離する。
- headers/query は重複と順序を保持する。
- 本文は Blob とし、検索メタデータから参照する。
- `complete`、`truncated`、`metadata-only`、`capture-failed` を区別する。
- 機密値の表示マスクと不可逆削除を混同しない。

G2 不合格時:

- 既存記録を捨てずに移行できなければ P3 へ進まない。
- 容量清掃が新しい記録を壊す場合は即時停止して復旧を優先する。

### P3 — ルート管理と Docker 発見

利用者結果:

- ルートを作成、複製、無効化できる。
- 実行中コンテナまたは手動 host/port をターゲットにできる。
- 設定は検証後に原子的に反映され、失敗時に旧設定へ戻る。

G3 不合格時:

- 不正設定一件で既存ルートが停止する場合はリリース不可。
- Docker 接続自体が機能せず、対象コンテナまたは port を選択できない場合は次へ進まない。権限制限の hardening は P6 で扱う。

### P4 — 編集、リプレイ、出力

利用者結果:

- replayable な記録を複製し、query、headers、cookie、JSON、form を編集できる。
- hop-by-hop header と Content-Length を安全に再構築できる。
- cURL と fetch を出力できる。
- POST/PUT/PATCH/DELETE の再送前に副作用警告を表示する。

G4 不合格時:

- P2 の記録形式が不足している場合、記録契約を version up し、旧記録の可否を明示する。黙って旧記録を誤送信しない。

### P5 — 変更ルールと通知

利用者結果:

- 条件に一致する将来のリクエストを規則で変更できる。
- HTTP 5xx、Nginx warn/error、ターゲット停止を区別して通知できる。
- 同一 fingerprint の通知は設定時間内に集約される。
- WebSocket で開いている管理画面へイベントを送信し、許可済みブラウザ通知を表示できる。

推奨通知初期値:

- 同一路線・同一 fingerprint は 5 分間集約する。
- 最初の一件、件数増加の集約更新、復旧時のみ通知する。
- HTTP 4xx は初期状態では通知しない。
- 502/503/504、upstream timeout、container unavailable を通知対象とする。

### P6 — 会社環境候補の再評価

DEV-050 の assessment / runbook / backup evidence は remote に保持する。2026-11-15 の product scope decision により会社共有・multi-user 対応を延期したため、独立 review と G6 判定、および会社向け hardening follow-up は `Deferred` とする。

### P7 — Personal local dashboard & observability

DEV-060:

- 英語 / 日本語の UI と永続 language selection。
- フォームを持たない Overview dashboard。
- 既存 workspace の tab 分離と `Alt+1`〜`Alt+5` shortcut。
- review remediation として Docker / Podman 共通 runtime adapter、Engine socket 解決、local CI entrypoint を追加し、Podman 上で既存13 suite を再実行する。

DEV-061（DEV-060 合格後に詳細化）:

- Docker / Podman host 全体の Compose project / container の running / stopped / health / port 集約。
- 同一 project だけでなく別 project の container も inventory に表示し、`検出済み` と `router から到達可能` を別状態として扱う。
- Routes & Containers workspace は container inventory を主画面とし、`domain + container + port` の quick setup を既定にする。route ID、scheme、manual host、description、enabled は詳細設定へ畳む。
- ProxyConsole SQLite と Docker 上の DB container の local status summary。
- Runtime Projects workspace と `Alt+6`。

DEV-062（DEV-061 合格後に詳細化）:

- Docker API と既存 ProxyConsole / Nginx log の aggregation、tail、search、severity filter。
- Logs workspace と `Alt+7`。
- 任意 host file discovery、external log service、multi-user audit は含めない。

G7 合格条件:

- 日本語 / 英語で Overview と既存 workspace の代表 flow を完了できる。
- Overview に設定フォームがなく、統計から詳細 workspace へ遷移できる。
- shortcut が入力操作を破壊せず、reload 後に language / workspace を復元する。
- Docker / Podman project、container、DB status と local logs を個別 container shell に入らず確認できる。
- P1〜P5 の全機能回帰が成功する。

## 6. タスクまたはプロジェクトを停止する条件

### 現在の Agent タスクのみ停止

- 最後の実質的な push から 120 分経過しても進展がない。
- 同じ blocking review 指摘が三回連続で解消されない。
- Agent が必要な権限、入力または外部状態を `Blocked` として報告した。
- タスク範囲を超える仕様決定が必要になった。

この停止はプロジェクト失敗を意味しない。タスクを `Blocked` または `Timed out` として、再割当または再計画を待つ。

### 現在フェーズを停止

- フェーズの必須ゲートを、許可された一回の代替検証を含めても通過できない。
- 既存の合格成果を破壊しなければ次へ進めない。
- 検証データの完全性を判定できない。

### プロジェクト全体を中止候補にする

- HTTPS を終端できず、要求された HTTP 内容の観測が原理上できない。
- リクエスト本文を保存することが利用環境の規則で禁止され、メタデータのみでは主要価値を満たさない。
- 利用者が中止を指示した。

管理面の公開範囲、Docker 管理権限、機密情報保護だけを理由にローカル機能開発を中止しない。これらは P6 の会社環境候補評価で導入可否を判断する。

全体中止は Agent が独断で決定せず、根拠を記録して利用者判断を待つ。

## 7. 次の順次開発計画

並列作業は行わず、次の checkpoint を一件ずつ完了・review してから次へ進む。

### Checkpoint A — DEV-060 review remediation: Docker / Podman compatibility

目的:

- 現在 `docker compose`、`docker cp`、`/var/run/docker.sock` に固定されている起動・検証経路を、Docker と Podman のどちらでも同じ操作契約で実行できるようにする。
- hosted CI は追加せず、開発端末上の local CI を唯一の統合検証入口とする。

実装項目:

1. `CONTAINER_RUNTIME=auto|docker|podman` を受け取る runtime adapter を一つ追加する。`auto` は利用可能な runtime を一度だけ選択し、混在実行しない。
2. adapter は少なくとも `compose config/build/up/down/ps/exec/run/start/stop/restart/logs`、container copy、Engine socket path 解決を提供する。
3. Docker は `/var/run/docker.sock`、rootless Podman は `${XDG_RUNTIME_DIR}/podman/podman.sock` など実環境で解決した socket を利用し、container 内では固定の read-only API path に mount する。socket の `chmod 0666` は廃止する。
4. discovery backend は Docker-compatible Engine API を利用しつつ、provider を `docker|podman` として返す。既存 API field は互換性を維持し、UI文言は必要な箇所から `Container Engine` へ一般化する。
5. scripts と全 integration test の直接 `docker` 呼び出し、および Python subprocess 内の `"docker"` を adapter / environment 経由へ移す。
6. `make ci-local` を追加し、preflight、syntax/config check、全13 suite、cleanup、秘密鍵・runtime artifact の Git 混入確認を一つの command で実行する。runtime 未導入時は明示的に失敗し、成功扱いにしない。
7. Podman Compose の provider 差異、rootless networking、SELinux volume label、host gateway 名の差異をテストで固定する。Docker固有の挙動へ暗黙 fallback しない。

local CI command（実装後）:

```bash
CONTAINER_RUNTIME=podman make ci-local
CONTAINER_RUNTIME=docker make ci-local
```

受入条件:

- 現在の主利用環境である Podman により、利用可能な環境では全13 suite を実行する。権限、runtime、socket、Compose provider の制約で実行不能な場合は `Deferred CI` として証拠を残し、次の実装 checkpoint へ進める。
- Docker が利用可能なローカル環境でも同じ command contract と全13 suite を実行する。環境がない場合は未実行側を明記して後日補完し、成功したとは表現しない。
- cleanup 後に test container、network、volume、証明書秘密鍵、capture/runtime data が Git または実行環境へ意図せず残らない。
- static check と runtime adapter の非破壊的検証が成功すれば code checkpoint の review は進められる。Deferred CI は G7 を閉じる前の verification backlog として保持する。

### Checkpoint B — DEV-061: host-wide inventory

目的:

- ProxyConsole の demo target だけでなく、選択した Engine 上で現在稼働・停止している container と Compose project を把握できるようにする。

実装項目:

1. discovery の既定 inventory scope を host-wide read-only scan にし、provider、project、service、container、image、state、health、network、exposed/published port を集約する。
2. ProxyConsole system container、同一 project、外部 project、明示的 discoverable label を分類する。表示対象と route target 選択可否を同じ filter にしない。
3. 各 port に `reachable`、`published-host reachable`、`network attachment required`、`stopped` などの理由付き状態を返す。
4. 外部 project の container を勝手に network 接続・再起動しない。既存 published port を優先し、変更を伴う network 操作は明示確認と rollback 設計を持つ後続操作に限定する。
5. demo container は test/example profile に分離し、通常起動時の inventory を占有しない。

受入条件:

- 別 Compose project の実 container が一覧に現れ、ProxyConsole system container と区別できる。
- container が見えても到達不能な場合は選択不可理由が表示され、既存 route を破壊しない。
- Docker と Podman の双方で同一の inventory contract を満たす。

### Checkpoint C — DEV-061: simplified route setup

目的:

- 通常の route 作成では、利用者が container 一覧から対象を選び、domain と port を確認するだけで設定を完了できるようにする。

画面構成:

1. 主画面は project grouping、検索、running filter を備えた container inventory とする。
2. container / port 行の `Route` 操作で compact setup panel を開き、必須入力は `domain`、`container`、`port` のみとする。
3. route ID は自動生成、scheme は検出値または `http`、enabled は true を既定値にする。
4. manual host/port、route ID、scheme、description、disabled creation は `詳細設定` disclosure 内へ移す。
5. apply status と既存 route table は維持するが、診断 detail はエラー時または展開時だけ表示する。
6. 保存前に routing preview を一行で表示し、保存成功後は実疎通結果と rollback action を表示する。

受入条件:

- running かつ reachable な container から3入力以内で route を作成できる。
- advanced setting を一度も開かず代表 flow を完了できる。
- stopped / unreachable target は理由を伴って選択不可となる。
- 既存の create/edit/copy/disable/delete、manual target、atomic apply/rollback flow は回帰する。

### Checkpoint D — DEV-062: provider-neutral logs

- Docker / Podman Engine API の logs を同じ contract で扱う。
- provider、project、container、severity、time range で filter し、既存 ProxyConsole / Nginx log と統合する。
- 任意 host file discovery、external log service、multi-user audit は追加しない。

