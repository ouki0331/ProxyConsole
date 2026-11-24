# DEV-060 Review 002

Target commit: `491aae5`
Reviewed implementation commit: `9ffc51d`
Reviewer: orchestrator
Decision: Approved (Deferred CI recorded)

## レビュー範囲

dispatch 17 の Docker / Podman runtime adapter、provider-neutral Engine socket、runtime-neutral scripts/tests、local CI entrypoint、DEV-060 review remediation受入条件を対象とした。利用者判断により、runtime・権限・socket・Compose providerがない環境のintegration CIは非blockingのDeferred CIとして扱う。

## コード確認結果

- `CONTAINER_RUNTIME=auto|docker|podman` は一つのproviderだけを選択し、明示指定が利用不能な場合に他方へfallbackしない。
- Docker Compose v2、`podman compose`、`podman-compose` はDocker-compatible shimへ集約され、shellとPython subprocessが同じruntime選択を継承する。
- host Engine socketは環境ごとに解決され、control-api内は `/run/proxyconsole/engine.sock` に固定された。host socketの `chmod 0666` は削除された。
- discovery responseの `provider` は互換追加であり、既存route target IDと保存済みAPI fieldを変更していない。
- local CIは一意なCompose projectを使用し、既存 `var` を退避・復元してtest resourceだけをcleanupする。
- review中の `git diff --check` が新規3scriptの末尾空行を検出したため、`491aae5` で修正した。

## 独立再実行した検証

```bash
make runtime-test
node --check web/console/app.js
python3 -m py_compile services/recorder/*.py tests/helpers/echo_server.py tests/unit/*.py
bash -n scripts/*.sh scripts/runtime-bin/docker tests/integration/*.sh tests/unit/*.sh
git diff --check
```

Result: PASS

- runtime選択・shim転送 shell test: PASS
- discovery provider unit test 3件: PASS
- JavaScript / Python / shell syntax: PASS
- whitespace check: PASS after `491aae5`

## Deferred CI

- `CONTAINER_RUNTIME=podman make ci-local`: `podman` command未導入のため実装Agent環境で実行不能。成功扱いにしていない。
- `CONTAINER_RUNTIME=docker make ci-local`: `docker` command未導入のためreview 001から実行不能。成功扱いにしていない。
- 両方をG7完了前のVerification backlogとして保持する。

## Blocking findings

なし。

## Non-blocking findings

- 実Podman環境ではrootless socket ownership、SELinux、Compose provider差異をlocal CIで確認する必要がある。
- 実Docker環境ではCompose v2、host gateway、socket group accessを同じlocal CI contractで確認する必要がある。

## 判定

利用可能な独立検証はすべて成功し、runtime未導入によるintegration CIは利用者承認済みのDeferred CIとして明示されている。DEV-060を`Done`とし、DEV-061へ進める。

