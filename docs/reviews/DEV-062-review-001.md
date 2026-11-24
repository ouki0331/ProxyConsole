# DEV-062 Review 001

Target commit: `b67ea13`
Reviewed implementation commits: `5526893`, `efecba2`
Reviewer: orchestrator
Decision: Changes requested

## レビュー範囲

provider-neutral Engine logs API、Docker multiplex / Podman plain decode、inventory source照合、tail / since / severity / search filter、response hard cap、Logs workspace、Alt+7、filter/source persistence、visible-only polling、unit/API/browser E2E定義をDEV-062の受入条件1〜11に沿って確認した。任意host file discovery、log永続化、container lifecycle変更はreview範囲外であり、実装にも追加されていない。

## 独立再実行した検証

```bash
node --check web/console/app.js
python3 -m py_compile services/recorder/*.py tests/unit/*.py
bash -n scripts/*.sh scripts/runtime-bin/docker tests/integration/*.sh tests/unit/*.sh
make runtime-test
git diff a5a41a9..efecba2 --check
```

Result: CHANGES REQUESTED

- runtime adapter、discovery/demo seed、DEV-062 decode/API unitを含む18件: PASS。
- JavaScript / Python / shell syntax: PASS。
- HTML parse / 593 ID重複検査: PASS。
- Engine socket mutation scan: read-only GETのみ。
- phase完了時の`make ci-local`は実装Agentが既に一回実行し、Docker/Podman未導入でDeferred CIとして正しく記録済み。利用者指示に従いreviewでは再実行していない。

## Blocking findings

### 1. API response bodyのhard capが保証されない

`EngineLogsService._engine_get_bytes()`はEngineから読むraw payloadを2 MiBで止めるが、`CaptureApiHandler._send_json()`が生成する最終JSON bodyには上限がない。2 MiBのNUL byteをplain logとして返す最小再現では、各byteがJSONの`\u0000`へescapeされ、最終bodyは`12,583,723` bytesになった。

DEV-062機能ルール3の「API response sizeにhard cap」と受入条件2に反する。`GET /api/v1/runtime/logs`の最終serialized response bodyを設定上限以内に収めること。本文を途中で改変せず、収まらないlineは省略して`truncated=true`とするなど、raw line contractを維持すること。巨大な単一line、control character、多byte UTF-8を含む回帰testを追加すること。

### 2. decoded line数を要求tail以下へ再制限していない

APIはEngineへ`tail=1000`を渡すだけで、decode後の`lines`を再制限しない。Engine responseを1001行にした最小再現では、`requested.tail == 1000`に対して`returned == 1001`となった。

provider差異や不正応答があっても既定200・最大1000行という機能ルール3をAPI contractとして保証する必要がある。decode後、severity/search filter前に要求tail以下へboundedにし、超過時は`truncated=true`を返すこと。default 200、requested 1000、filter組合せの回帰testを追加すること。

## Required actions

1. 最終JSON API bodyをhard cap以内に収め、巨大line/control character/multi-byte textのtestを追加する。
2. decode後のline数をrequested tail以下に再制限し、default 200 / max 1000 / filter順序のtestを追加する。
3. `python3 -m unittest tests.unit.test_engine_logs`、`make runtime-test`、JS/Python/shell syntax、HTML ID audit、dispatch 20差分checkを実行し、結果をDEV-062へ追記する。
4. phase local CIは既に一回実行済みなので再実行せず、既存Deferred CI証拠を維持する。

## 次の状態

DEV-062を`In progress / implementer`へ戻し、dispatch 20で上記2件の修正を依頼する。DEV-062を`Done`またはG7 passedにはしない。
