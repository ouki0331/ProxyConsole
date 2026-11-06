# ProxyConsole

ProxyConsole 是一个面向 WSL/Docker 本地环境的可观测反向代理管理项目。长期目标是统一接管 HTTP/HTTPS 入口、按域名路由到容器，并在不破坏后续能力的前提下保存请求，逐步增加查询、编辑、重放、规则修改和本地通知。

当前仓库处于规划与技术验证阶段，不应视为可用于公司环境的成品。

## 当前工作方式

- 只使用 `main` 分支。
- 同一时间只允许一个执行者修改仓库。
- 规划者负责拆任务、验收、记录审查意见和决定是否进入下一阶段。
- 执行 Agent 只读取当前任务及其明确引用的文档，不需要读取历史对话。
- Agent 之间的任务、状态、审查和交接使用日语。
- 每次可审查的产物都必须提交并推送到远端 Git。

## 文档入口

- [开发总计划](docs/development/PLAN.md)
- [顺序执行、轮询与审查协议](docs/development/WORKFLOW.md)
- [当前状态](docs/development/STATUS.md)
- [仓库目录规划](docs/architecture/REPOSITORY_LAYOUT.md)
- [请求记录契约](docs/architecture/CAPTURE_CONTRACT.md)
- [首个执行任务](docs/tasks/DEV-001.md)
- [Git 自动交接协议](.agents/README.md)

## 当前下一步

`DEV-021`：实现 Docker container/port discovery，并将发现的 target 接入 route 选择，同时保留手动 host/port。任务通过 Git mailbox 自动派发，不需要用户转发提示词。
