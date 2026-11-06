# 順次実行・ポーリング・レビュー手順

## 1. 役割

### オーケストレーター

- 全体計画と現在位置を維持する。
- 次の一件だけをタスク文書として準備する。
- 実装 Agent の push を監視する。
- 成果物を実行・検証し、レビュー文書を作成する。
- ゲートの合否と次フェーズ移行を決める。

### 実装 Agent

- 割り当てられた一件だけを実装する。
- 検証証拠と未検証事項を記録する。
- 成果を `main` に push し、`Ready for review` とする。
- レビュー修正時は最新レビュー文書だけを追加コンテキストとして使用する。

## 2. 単一作業者ロック

`docs/development/STATUS.md` の `Owner` がロックである。

- `none`: 誰も実装していない。
- `implementer`: 実装 Agent だけがコードを変更できる。
- `orchestrator`: オーケストレーターだけがレビュー、計画、状態を変更できる。

引き継ぎは必ず commit/push 後に行う。口頭だけで Owner を変更しない。

## 3. タスク開始

1. オーケストレーターがタスク文書を作成する。
2. `STATUS.md` を `In progress / implementer` に変更する。
3. commit/push する。
4. 実装 Agent に、リポジトリ URL、Task ID、読むべき文書、終了条件だけを送る。
5. 実装 Agent は `git pull --ff-only` 後に作業する。

## 4. Git を使った递増ポーリング

基準時刻は「実装 Agent へタスクを送った時刻」または「最後の実質的な進展を含む push の時刻」とする。

チェックポイント:

```text
+10分 → +20分 → +30分 → +60分 → +120分
```

各チェックで行うこと:

1. `git fetch origin main`
2. `origin/main` の commit hash と前回 hash を比較
3. 新規 commit があれば、差分、Task ID、テスト証拠、`STATUS.md` を確認
4. `Ready for review` ならポーリングを終了してレビューへ移る
5. `Blocked` ならポーリングを終了して停止判断へ移る
6. 実質的な進展があれば inactivity timer をリセットする

実質的な進展に含むもの:

- コード、テスト、構成、検証結果の追加
- 再現可能な失敗証拠とブロック理由
- レビュー指摘に対する修正

実質的な進展に含まないもの:

- timestamp だけの変更
- 空コミット
- 根拠のない「作業中」更新
- formatting だけでタスク結果に影響しない変更

120 分間実質的な進展がない場合:

- 現在の Agent 作業を中断する。
- `STATUS.md` を `Timed out / orchestrator` にする。
- 既存 remote commit までを保存し、未 push の成果は存在しないものとして扱う。
- 自動的に次フェーズへ進まない。

この递増チェックは一つの実行タスクにだけ付随する。常設の10分周期ジョブにはしない。

## 5. レビュー

1. オーケストレーターが `origin/main` を fast-forward で取得する。
2. タスク記載の自動検証を自分で再実行する。
3. acceptance criteria と実際の証拠を比較する。
4. 指摘があれば `docs/reviews/<TASK-ID>-review-<NNN>.md` を作成する。
5. review 文書と `STATUS.md` を commit/push する。
6. Owner を `implementer` に戻し、実装 Agent へ Task ID と review 文書パスだけを送る。

レビュー判定:

- `Approved`: 全 blocking criteria を満たす。
- `Changes requested`: 修正可能な blocking finding がある。
- `Blocked`: 環境、権限、製品判断または根本制約が必要。
- `Rejected`: 成果がタスク目的と異なり、修正より再計画が妥当。

同じ blocking finding が三回連続した場合は修正ループを停止し、`Blocked` とする。

## 6. タスク完了と次フェーズ

`Done` にできるのはオーケストレーターだけである。

完了時:

1. review が `Approved` である。
2. `STATUS.md` に検証済み commit hash を記録する。
3. タスク文書を `Done` にする。
4. フェーズ内の残タスクを確認する。
5. フェーズゲートを満たした場合だけ次フェーズの最初のタスクを作成する。

次フェーズへ進まない条件:

- blocking acceptance criteria が一つでも未検証
- 「動くはず」で、再現可能なコマンド証拠がない
- runtime data または secret が Git に入っている
- ロールバックまたは失敗状態が確認されていない
- ゲートに必要な利用者判断が未解決

