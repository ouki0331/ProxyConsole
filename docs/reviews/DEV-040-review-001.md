# DEV-040 Review 001

Target commit: `26435d7`
Reviewed implementation commit: `9ce6c42`
Reviewer: orchestrator
Decision: Changes requested

## レビュー範囲

DEV-040 の persistent rule model、CRUD/copy/enable/disable/priority、match/action engine、request-time application、preview、fail-open、capture/index metadata、API/CLI、integration test を受入条件 1〜15 に沿って確認した。security hardening、認証、機密情報 filter は blocking 条件に含めていない。

## 再実行した検証

```bash
make clean && make config-test && make test && make down
```

Result: INCOMPLETE (environment failure)

- config test と DEV-001〜DEV-022 の regression は PASS。
- DEV-030 test 開始時、Docker Desktop の overlay2 が `input/output error`、cleanup 時に `read-only file system` を返したため、全 suite の独立再実行は完了していない。
- 上記とは別に、コード確認と最小再現で以下の機能上の blocking finding を確認した。

## Blocking findings

### 1. 後続 rule が先行 rule 適用前の query/header に誤一致する

`services/recorder/rule_engine.py` の query/header match は working state と original state を OR 評価している。このため、先行 rule が `old` を `new` に変更した後も、後続の `old` 条件が一致する。最小再現では `matched_rule_ids == ["r1", "r2"]` となった。

受入条件 7 と機能ルール 8 に反する。後続 rule の match は、その評価時点の working state を入力として判定すること。

### 2. streamed request で転送内容と rule metadata が一致しない

`services/recorder/addon.py` は streamed request も engine で評価するが、`not is_streamed` 条件により query/header/cookie を含む全変更を実 request へ反映しない。一方、engine は非 body action を `applied` と記録できるため、capture では適用済みでも target には元 request が到達する。

非 body action を実際に適用するか、streamed request 全体を `skipped` として記録し、転送結果と metadata を一致させること。

### 3. Host/hop-by-hop action が applied と記録された後に破棄される

header action は change summary と `applied_rule_ids` に追加された後、最終 normalization で `Host`、標準 hop-by-hop header、`Connection` token 指定 header が除外される。Host set と hop-by-hop add だけの rule でも `outcome=applied` になる一方、target には変更が届かない。

受入条件 12・13、機能ルール 6・10 に反する。保護対象 action を保存時に拒否するか、実際の最終転送結果に従って skipped/failed outcome と summary を構築すること。

## Required actions

1. 上記 3 件を修正する。
2. 逆向き rule chain、streamed request、保護対象 header のみを操作する rule の regression test を追加する。
3. Docker runtime を復旧できる環境で `make clean && make config-test && make test && make down` を再実行し、結果を記録する。

## 次の状態

DEV-040 を `In progress / implementer` に戻し、dispatch 11 で本 review の修正を依頼する。DEV-041 へは移行しない。
