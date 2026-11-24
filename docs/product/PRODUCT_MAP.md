# ProxyConsole プロダクトマップ

更新日: 2026-11-24

## L0 プロダクト定義

ProxyConsole は、単一利用者が自分の WSL / Linux 上の Docker または Podman 開発環境で、ドメインルーティング、HTTP リクエスト、container service、ローカル DB 状態、ログ、変更ルール、障害通知を一つの localhost コンソールから把握・操作するためのローカル開発ツールである。

- 対象利用者: 自分の端末と Docker / Podman 環境を管理する一人の開発者。
- 解決する問題: 状態確認のたびに container CLI、個別コンテナログ、ブラウザ DevTools、アプリ独自ログ、Nginx 設定を行き来する負担。
- 約束する結果: 「何が動いているか」「どのリクエストが通ったか」「どこで失敗したか」「どの設定が効いているか」を一か所で確認し、必要なローカル操作へすぐ移動できる。
- 意味のある代替: Docker CLI / Desktop、Nginx 設定ファイル、ブラウザ DevTools、個別ログ、Postman を別々に使用すること。

## コアシナリオと価値ループ

1. Overview を開き、Proxy、Route、Docker、Capture、Storage、Incident、DB の全体状態を確認する。
2. 異常または確認したい項目から該当 workspace へ移動する。
3. Route / Request / Replay / Rule / Notification / Log の詳細を調査・操作する。
4. 結果が永続化・再捕捉・集約され、Overview に新しい状態として戻る。

## 確定したプロダクト境界

- 単一利用者・localhost 専用を製品前提とする。
- 英語と日本語をサポートする。多言語追加を可能な構造にはするが、今回ほかの言語は翻訳しない。
- Overview は統計・状態・workspace への入口だけを表示し、作成・編集フォームを置かない。
- 設定と編集操作は各 workspace に分離する。
- 会社共有、マルチユーザー、SSO、RBAC、公開インターネット配備は `Deferred` とする。
- container runtime は Docker と Podman を正式対応対象とし、runtime ごとに別 UI / API を作らず provider-neutral contract へ集約する。
- DB はまず ProxyConsole 自身の SQLite 健全性と Docker / Podman 上の DB コンテナ状態を扱う。汎用 SQL クライアントは含めない。
- ログは Docker-compatible Engine API と ProxyConsole が既に管理するローカルログを集約する。任意のホストファイル探索は含めない。

## L1 / L2 / L3 capability map

| L1 Domain | L2 Module | Feature ID | Feature | Product state | Availability |
| --- | --- | --- | --- | --- | --- |
| Proxy | Domain routing | `PRX-ROUTE-001` | 複数ドメインを Docker / manual target へ転送 | Confirmed | Available |
| Observe | Request capture | `OBS-REQ-001` | HTTP request の保存・検索・詳細表示 | Confirmed | Available |
| Operate | Replay | `OPS-REPLAY-001` | request の編集・再送・cURL/fetch 出力 | Confirmed | Available |
| Operate | Rules | `OPS-RULE-001` | 将来 request の自動変更ルール | Confirmed | Available |
| Observe | Incidents | `OBS-NOTIFY-001` | 障害集約・復旧・live notification | Confirmed | Available |
| Console | Localization | `CON-I18N-001` | 英語 / 日本語 UI と永続言語選択 | Confirmed | Available |
| Console | Overview | `CON-DASH-001` | フォームを持たないローカル状態 Dashboard | Confirmed | Available |
| Console | Navigation | `CON-NAV-001` | workspace tab と keyboard shortcut | Confirmed | Available (Alt+1〜7) |
| Runtime | Project inventory | `RUN-PROJ-001` | Docker / Podman Compose project / container の稼働・停止・health 集約 | Confirmed | Available |
| Runtime | Logs | `RUN-LOG-001` | container / proxy / Nginx log の一覧・tail・filter | Confirmed | Available |
| Runtime | Local data | `RUN-DATA-001` | SQLite と DB container の状態サマリ | Confirmed | Available |
| Collaboration | Access control | `COL-AUTH-001` | SSO / RBAC / multi-user | Deferred | Not built |
| Deployment | Shared environment | `DEP-SHARE-001` | 会社共有 host / public deployment | Deferred | Not built |

## Ready for Delivery feature detail

### CON-I18N-001 英語 / 日本語 UI

- Trigger: 利用者が初回に console を開く、または header の language selector を変更する。
- Main flow: browser locale が日本語なら日本語、それ以外は英語を初期選択し、手動選択を端末内に保存して次回復元する。
- Included: navigation、heading、label、button、empty/loading/error/status、dialog、shortcut help、ARIA label、動的な既知 status の翻訳。
- Not included: user data、domain、container name、request body、raw log、server error detail の翻訳、機械翻訳、第三言語。
- Boundary case: 不明 locale / 不明 translation key は英語へ fallback し、画面を空にしない。
- Acceptance: 全 workspace を英語と日本語で操作でき、再読込後も選択を維持し、HTML の `lang` が一致する。

### CON-DASH-001 Overview Dashboard

- Trigger: console 起動、Overview shortcut、任意 status card からの復帰。
- Main flow: 全体状態を統計カードで走査し、詳細が必要なカードから該当 workspace へ移動する。
- Included: proxy health、route enabled/total、Docker running/stopped/unavailable、24h/total capture、storage/retention、open/unacknowledged incident、SQLite health、route apply sync、last refresh。
- Not included: route/rule 作成、filter form、request editor、incident 操作、ログ本文。
- Empty / partial: API 単位で unavailable を表示し、別カードの表示を止めない。ゼロ件は正常な `0` として表示する。
- Acceptance: Overview に入力フォームがなく、各カードの値と遷移先が server state と一致する。

### CON-NAV-001 Workspace navigation

- Trigger: tab click または `Alt+1`〜`Alt+7`。
- Mapping: 1 Overview、2 Routes & Containers、3 Requests & Replay、4 Modification Rules、5 Notifications、6 Runtime Projects、7 Logs。
- Rules: input / textarea / select / contenteditable 編集中は shortcut を発火しない。既存 browser shortcut と衝突して実行不能な場合は tab click を必ず残す。
- Visible behavior: active tab、tooltip / shortcut help、focus feedback、選択 workspace の端末内保存。
- Boundary case: 未実装 workspace は shortcut mapping を先取りせず、実装 task と同時に追加する。DEV-060 では 1〜5 を実装し、6〜7 は後続 task で追加する。
- Acceptance: mouse と keyboard の双方で workspace を切り替え、reload 後に最後の workspace を復元できる。

## 次の delivery handoff

- `DEV-060`: `CON-I18N-001`, `CON-DASH-001`, `CON-NAV-001` の既存 workspace 範囲（1〜5）。
- 後続 `DEV-061`: `RUN-PROJ-001`, `RUN-DATA-001` と shortcut 6。
- 後続 `DEV-062`: `RUN-LOG-001` と shortcut 7。

