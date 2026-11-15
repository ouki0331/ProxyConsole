# DEV-042 Review 001

Target commit: `df4db0f`
Reviewed implementation commit: `61735d9`
Reviewer: orchestrator
Decision: Changes requested

## レビュー範囲

DEV-042 の event classification、HTTP 4xx 既定除外、fingerprint aggregation、recovery、SQLite persistence、REST / CLI、WebSocket cursor resync、Notifications workspace、browser notification、failure isolation を受入条件 1〜15 に沿って確認した。security hardening、認証、権限分離、機密情報 filter は blocking 条件に含めていない。

## 独立再実行した検証

```bash
python3 -m py_compile services/recorder/*.py tests/helpers/echo_server.py
node --check web/console/app.js
bash -n tests/integration/test_notifications_console.sh
./tests/integration/test_notifications_console.sh
make clean && make config-test && make test && make down
```

Result: CHANGES REQUESTED

- DEV-042 targeted notification / WebSocket / browser E2E: PASS（全9フェーズ）
- Docker Compose / Nginx config test: PASS
- DEV-001、DEV-002、DEV-010 regression: PASS
- DEV-011 retention regression: FAIL
- 失敗後の Docker stack / volume / network cleanup: PASS

## Blocking findings

### 1. 通知用 SQLite 書き込みが既存 retention の soft-limit 回帰テストを破壊する

`make clean && make config-test && make test && make down` は `tests/integration/test_retention_cleanup.sh` の step 3 で失敗した。seed 後の容量から `soft_limit_bytes = total_seeded + 10000` を設定したにもかかわらず、retention 実行時には `database/captures.sqlite3` が増加して soft limit を超え、`cap_01_old_tie_a` と `cap_01_old_tie_b` の2件が削除された。

観測値:

- `soft_limit_bytes`: `288420`
- retention 実行時 `bytes_before`: `311188`
- `deleted_captures_count`: `2`（期待値 `0`）
- reason: `soft_limit_and_expired`（期待値 `within_soft_limit`）

DEV-042 の migration / background notification monitor が capture index と同じ SQLite を拡張・更新するため、既存 retention の容量計算と deterministic test の間に追加書き込みが入る。受入条件1「DEV-001〜DEV-041 の全 test が引き続き成功する」を満たさない。

通知テーブル追加後も retention の容量契約が一貫するよう、容量スコープまたはテストの同期方法を修正すること。単に assertion を緩めず、「soft target 以下なら capture を削除しない」という既存動作を再現可能に保つこと。

### 2. REST cursor resync が page 上限を超えた未取得 event を飛ばす

`GET /api/v1/notifications/events?since_seq=N&limit=200` は最初の200件だけを返す一方、`latest_seq` には全イベントの最大 sequence を返す。`resyncNotificationsViaRest()` は返された200件を処理した後、未取得 event が残っていても `notifLatestSeq` をこの global `latest_seq` まで進める。

その直後に WebSocket が切断した場合、次回接続は進み過ぎた cursor を使用するため、201件目以降を再取得できない。手動 Reconnect も WebSocket 接続と REST resync を並行開始するため同じ race を持つ。これは機能ルール6と受入条件10の「欠落 event を resync し、重複・欠落なく処理する」に反する。

REST / WebSocket の双方で page を全消費するまで cursor を「最後に実際に処理した seq」だけ進めること。`latest_seq` は server watermark として別管理し、未取得 page を飛ばす cursor に使用しないこと。200件超（WebSocket explicit resync は500件超）の backlog regression test を追加すること。

## Required actions

1. retention regression を修正し、`tests/integration/test_retention_cleanup.sh` を単独で成功させる。
2. REST / WebSocket resync cursor を page-safe にし、200件超／500件超 backlog の欠落なしを自動検証する。
3. `./tests/integration/test_notifications_console.sh` を再実行する。
4. `make clean && make config-test && make test && make down` を再実行し、全12 suite の成功証拠を記録する。

## 次の状態

DEV-042 を `In progress / implementer` に戻し、dispatch 14 で本 review の修正を依頼する。G5 は pending のままとし、P6 へは移行しない。
