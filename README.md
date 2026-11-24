# ProxyConsole

ProxyConsole は、単一利用者の WSL / Linux 上の Docker / Podman ローカル開発環境向け統合コンソールです。HTTP / HTTPS の入口を統一的に受け持ち、ドメイン単位でコンテナへルーティングし、リクエストの保存と再送、変更ルールの適用、ローカル障害通知を管理します。さらにローカルプロジェクト、データベース状態、コンテナログを段階的に集約します。

P1〜P5 のローカルプロキシ、キャプチャ、ルーティング管理、リクエスト再送、ルールエンジン、通知機能はすでに実装され、独立レビューに合格しています。本プロダクトは localhost 単一利用者での利用を前提としており、会社共有、マルチユーザー、またはパブリック環境への配備を対象としません。

## Container runtime

runtime は明示指定できます。`auto` は Podman、Docker の順で利用可能な一方だけを選択し、一回の実行中に混在させません。

```bash
CONTAINER_RUNTIME=podman make up
CONTAINER_RUNTIME=docker make up
CONTAINER_RUNTIME=auto make up
```

rootless Podman は通常 `${XDG_RUNTIME_DIR}/podman/podman.sock` を使用します。別のUnix socketを使う場合は `CONTAINER_ENGINE_SOCKET` を指定します。ProxyConsole はhost socketのpermissionを変更しません。

フェーズ単位のローカルCIは次で実行します。runtime、権限、socket、Compose providerが利用できない場合は成功扱いにせず、Deferred CIとして後日補完します。

```bash
CONTAINER_RUNTIME=podman make ci-local
CONTAINER_RUNTIME=docker make ci-local
```

## 現在の運用方式

- `main` ブランチのみを使用します。
- 同一時間帯にリポジトリを変更できる実行者は1人のみです。
- 計画者（Orchestrator）がタスクの分解、受入検証、レビュー意見の記録、および次フェーズへの移行判定を担当します。
- 実装者（Implementer）は現在のタスクおよび明示的に参照されたドキュメントのみを読み込み、過去の対話履歴を読む必要はありません。
- Agent 間のタスク定義、ステータス、レビュー、および引き継ぎには日本語を使用します。
- レビュー対象となる成果物は必ずコミットし、リモート Git へプッシュします。

## ドキュメント一覧

- [開発総計画](docs/development/PLAN.md)
- [順次実行・ポーリング・レビュー規約](docs/development/WORKFLOW.md)
- [現在のステータス](docs/development/STATUS.md)
- [プロダクト機能マップとスコープ](docs/product/PRODUCT_MAP.md)
- [リポジトリ構成計画](docs/architecture/REPOSITORY_LAYOUT.md)
- [キャプチャ保存契約](docs/architecture/CAPTURE_CONTRACT.md)
- [初期タスク定義](docs/tasks/DEV-001.md)
- [Git メールボックス自動引き継ぎ規約](.agents/README.md)

## 現在の状況

`DEV-060` の英語/日本語UI、フォームのない統計Overview、ワークスペース切替は実装済みです。現在は review remediation として Docker / Podman 共通runtime、provider-neutral Engine socket、local CI entrypointを実装中です。タスクは Git メールボックスを介して自動的にディスパッチされます。
