# リポジトリ構成計画

現在は必要になる前に空ディレクトリを作らない。各フェーズで最初のファイルを追加するときにディレクトリを作成する。

```text
ProxyConsole/
├── AGENTS.md
├── README.md
├── docs/
│   ├── architecture/
│   │   ├── REPOSITORY_LAYOUT.md
│   │   └── CAPTURE_CONTRACT.md
│   ├── decisions/             # ADR: 重要な選択と棄却理由
│   ├── development/
│   │   ├── PLAN.md
│   │   ├── STATUS.md
│   │   └── WORKFLOW.md
│   ├── reviews/               # オーケストレーターのレビュー記録
│   ├── tasks/                 # 一件ごとの自己完結タスク
│   └── templates/             # task/review のテンプレート
├── deploy/
│   ├── compose/               # ローカル/WSL 用 Compose
│   ├── nginx/
│   │   ├── edge/              # TLS 入口、capture bypass
│   │   └── router/            # Docker 内部 Host router
│   └── certificates/          # 公開可能な開発用生成手順のみ
├── services/
│   ├── recorder/              # capture adapter、保存 writer
│   ├── control-api/           # ルート、Docker、設定反映 API
│   └── notifier/              # event 集約と通知（分離が必要な場合）
├── web/
│   └── console/               # localhost 管理 UI
├── migrations/                # 永続メタデータ schema migration
├── tests/
│   ├── integration/           # proxy/capture/replay の black-box test
│   ├── fixtures/              # 合成データのみ
│   └── helpers/               # echo target 等の test helper
├── scripts/                   # 再現可能な setup/verify/cleanup
└── var/                       # runtime only、Git 管理外
    ├── captures/
    ├── blobs/
    ├── database/
    └── logs/
```

## 境界

- `deploy`: 実行トポロジーと proxy 設定。業務ロジックを置かない。
- `services/recorder`: HTTP flow を capture contract へ変換する。UI を持たない。
- `services/control-api`: ユーザー操作、設定検証、Docker discovery を扱う。request body の解析実装を重複させない。
- `web/console`: 表示とユーザー操作。Docker Socket や filesystem を直接触らない。
- `tests/integration`: 外部から観測可能なルーティング、保存、リプレイを検証する。
- `var`: 全て消去可能なローカル runtime 領域。Git へ commit しない。

## 技術選択を保留する領域

P1 の結果が出るまで control API、永続 DB、フロントエンド framework は固定しない。P1 は Compose、Nginx、mitmproxy または同等の記録層、test helper だけで完結させる。

