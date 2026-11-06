# DEV-020 Review 001

Target commit: `3c1ad96`  
Reviewed implementation commit: `f1f359c`  
Reviewer: orchestrator  
Decision: Approved

## レビュー範囲

DEV-020 の route 永続 model、CRUD/copy/enable/disable、Nginx 設定生成、検証後の原子的反映、reload failure 時の rollback、API/CLI、および既存 capture chain との統合を対象とした。security hardening、認証、権限分離は blocking 条件に含めていない。

## 再実行した検証

```bash
python3 -m py_compile services/recorder/*.py tests/helpers/echo_server.py
bash -n scripts/*.sh tests/integration/*.sh deploy/nginx/router/router-entrypoint.sh
make clean && make test && make down
```

Result: PASS

- DEV-001/DEV-002/DEV-010/DEV-011 regression: PASS
- route create/list/get/update/delete: PASS
- route copy と複数 domain の同一 upstream 共有: PASS
- enable/disable の live routing 反映: PASS
- duplicate domain、無効 scheme/port、空 host の拒否: PASS
- Nginx candidate validation と原子的 replace/reload: PASS
- validation/reload failure 時の旧 route 継続と rollback: PASS
- container restart 後の desired/apply state 永続性: PASS
- 動的 route の inspector/capture/index/retention chain 通過: PASS

## Blocking findings

なし。

## Non-blocking findings

- Docker container discovery と Web 管理 UI は後続 task の明示的スコープである。
- 実装で追加された `keepalive_timeout 0` は reload 直後に旧 worker の接続が再利用される機能上の競合を防止しており、受入結果と整合する。

## Required actions

なし。

## 次の状態

DEV-020 を `Done` とし、DEV-021 Docker discovery と route target 選択を開始する。
