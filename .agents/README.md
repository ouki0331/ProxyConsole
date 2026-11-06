# Git Mailbox Protocol

このディレクトリは、オーケストレーターと外部実装 Agent が人手を介さずに交互作業するための mailbox である。

## ファイル

- `dispatch.json`: オーケストレーターから実装 Agent への最新 command。
- `implementer-status.json`: 実装 Agent からオーケストレーターへの acknowledgement と結果。

どちらも Git の `origin/main` を事実源とする。ローカル working tree の内容だけで判断しない。

## Dispatch ID

`dispatch_id` は単調増加する整数である。

- 実装 Agent は、自分が処理済みの `acknowledged_dispatch_id` より大きい dispatch だけを処理する。
- 同じ dispatch を二重実行しない。
- オーケストレーターは review が完了するまで次の dispatch ID を発行しない。

## 実装 Agent の待機動作

task の成果を push した後、実装 Agent は終了せず、次の確認点で `origin/main` を fetch する。

```text
+10分 → +20分 → +30分 → +60分 → +120分
```

`scripts/poll-agent-dispatch.sh <処理済みdispatch_id>` を使用できる。新しい dispatch を検出したら次を行う。

1. `git pull --ff-only origin main`
2. `dispatch.json` の task と instruction file を読む
3. `implementer-status.json` を `working` に更新して push
4. task を実装、検証、push
5. `implementer-status.json` を `ready_for_review` または `blocked` に更新して push
6. 次の dispatch を待つ

120 分で新しい dispatch がなければ、待機 loop は終了してよい。Git 自体には停止中の Agent を起動する機能がないため、長期間停止後の自動再開には外部 scheduler が必要である。

## オーケストレーターの動作

オーケストレーターは `implementer-status.json` と `STATUS.md` を監視する。

- `ready_for_review`: 機能 review を開始する。
- `blocked`: 証拠を確認し、修正 dispatch または停止判断を行う。
- review 合格: review 文書と次の dispatch を同じ Git commit で発行する。
- review 不合格: review 文書と同じ task の修正 dispatch を発行する。

利用者へ task prompt の転送を依頼しない。

