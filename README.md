# ProxyConsole

ProxyConsole 是一个面向单人 WSL/Docker 本地开发环境的聚合控制台。它统一接管 HTTP/HTTPS 入口、按域名路由到容器、保存与重放请求、管理修改规则和本地通知，并逐步聚合本机项目、数据库状态与容器日志。

P1～P5 的本地代理、捕获、路由管理、重放、规则和通知功能已经实现并通过独立复审。产品保持 localhost 单人使用，不以公司共享、多用户或公开部署为目标。

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
- [产品能力与范围](docs/product/PRODUCT_MAP.md)
- [仓库目录规划](docs/architecture/REPOSITORY_LAYOUT.md)
- [请求记录契约](docs/architecture/CAPTURE_CONTRACT.md)
- [首个执行任务](docs/tasks/DEV-001.md)
- [Git 自动交接协议](.agents/README.md)

## 当前下一步

`DEV-060`：增加英语 / 日语界面、无表单的统计 Overview，以及既有 workspace 的快捷键切换。任务通过 Git mailbox 自动派发，不需要用户转发提示词。
