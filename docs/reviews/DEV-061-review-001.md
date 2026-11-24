# DEV-061 Review 001

Target commit: `bcedfb3`
Reviewed implementation commits: `5b3ad24`, `6916904`, `bcedfb3`
Reviewer: orchestrator
Decision: Approved (Deferred CI recorded)

## レビュー範囲

host-wide Docker / Podman inventory、project/container classification、reachability、Runtime Projects / Alt+6、quick route / advanced manual設定、demo profile分離、unit/static/browser E2E定義を対象とした。

## コード確認結果

- Engine API操作は引き続きGETだけで、外部containerのnetwork/lifecycle/labelを変更するAPIは追加されていない。
- 既存discovery/route contractの既定scopeは`project`のまま維持され、host-wide quick routeだけ`target_scope=all`を明示する。
- external/standalone target IDはcontainer IDベースでproject間のservice名衝突を避け、same-projectの既存`docker:*` IDを維持する。
- shared router networkとpublished host portだけを選択可能にし、loopback-only、network attachment required、stoppedは理由付きでdisabledにする。
- quick routeは既存RouteManagerのatomic apply/rollbackを再利用し、domain-only flowとadvanced manual flowを分離する。
- demo containerとseed routeは`demo` / `test` profileでのみ有効になり、通常起動では追加されない。既存データは削除しない。
- HTML ID 568件は重複なし。Alt+6は既存input/dialog guardを共有する。

## 独立再実行した検証

```bash
make runtime-test
node --check web/console/app.js
python3 -m py_compile services/recorder/*.py tests/unit/*.py
bash -n scripts/*.sh scripts/runtime-bin/docker tests/integration/*.sh tests/unit/*.sh
git diff 580f48c..bcedfb3 --check
```

Result: PASS

- runtime / inventory / provider / demo seed unit 6件: PASS
- JS / Python / shell syntax: PASS
- HTML parse / duplicate ID audit: PASS
- Engine API mutation scan: read-only GETのみ

## Deferred CI

- Podman local CIは`podman` command未導入で実行不能。
- Docker local CIは`docker` command未導入で実行不能。
- external project、published host gateway、Compose profile、browser quick routeはG7前のVerification backlogとして保持する。

## Blocking findings

なし。

## Non-blocking findings

- 実Engine上のhost alias到達性はprovider/network構成に依存するため、local CI補完時に確認する。
- scaled serviceのsame-project legacy target ID衝突は既存contractとの互換を優先し、本タスクでは変更していない。

## 判定

利用可能な独立検証とread-only境界を満たし、integration/browser実行不能は利用者承認済みDeferred CIとして明示されている。DEV-061を`Done`とし、DEV-062へ進める。

