# Slack → Multica Relay 项目规则

## 职责与信任边界

- 本仓库负责 Slack event 准入、QStash 投递、上下文组装、Multica Issue/comment 路由，以及本地 reply adapter 的确定性发送协议。
- 云端 Relay 拥有入口、准入复核、队列消费和 Multica 写入；Multica Agent Runtime 拥有人格、私有资料、工具绑定和 Agent 执行；`scripts/slack-reply.py` 拥有最终 Slack 消息渲染与 delivery ledger。
- 私有 prompt、persona、Skills、部署记录和凭据只保存在批准的私有环境，不复制进公开仓库、Issue、日志或测试 fixture。
- Redis、QStash、Multica、Agent run 和 Slack reply 是不同状态域；一个阶段成功不能替代下一阶段的证据。

## 按任务读取真源

| 任务 | 必须读取 |
| --- | --- |
| 了解用途、架构或本地验证 | `README.md` 或 `README_EN.md` |
| 修改环境变量、准入、凭据边界或部署流程 | `docs/CONFIGURATION.md` |
| 修改上下文、envelope、marker、恢复、footer 或 delivery ledger | `docs/CONTEXT-ASSEMBLY-DESIGN.md` |
| 操作 live Multica Agent、Project、Issue、run、Skill 或 Runtime | `docs/MULTICA-OPERATIONS.md` |

当前代码、配置 schema、CLI `--help` 和 live readback 证明当前行为或外部状态；上表指定的合同文档定义预期协议。二者不一致时先判断是实现回归还是已授权的合同变更，再修复责任真源及受影响引用，不能默认让代码覆盖合同或让文档掩盖回归。

## 不可破坏的不变量

- Slack ingress 校验签名和时间戳；Team、mention、channel 与 sender policy 在入队前检查，并在消费时复核。
- 同一事件只使用准备阶段冻结的 envelope 和 `replyContext`；新 mention 才重新读取上下文和 Agent 配置。
- Slack file object 在进入 QStash 前完成最小投影；队列不得包含 private URL、附件内容或凭据，日志和错误还不得包含聊天正文或姓名。
- Multica 写入使用 Redis 状态和稳定 marker 恢复。结果不明时先回读，不能盲目重复创建 Issue/comment。
- Slack 最终发送遵守 at-most-once：`sent` 复用持久结果；`slack_delivery_unknown` 必须在原线程独立核对，未经核对不得重发或清理 ledger。
- 只有暂时性或结果不明的操作进入有限重试；确定性超限与无效输入必须终止重试并给出稳定错误码。
- `complete`、HTTP 200、reaction、Issue persisted、Agent completed 和 Slack delivered 各自独立陈述。

## 修改纪律

- 单点问题保持单点责任；涉及协议时检查实现、类型、配置、合同文档、兼容/迁移和反例测试，只同步实际受影响的表面。
- 保留任务范围外的现有改动。新增依赖、公共抽象、兼容层或部署面必须由当前契约直接要求。
- 远端 Git 写入、历史改写、部署和 live 外部写入只在当前对话已明确授权的范围内执行；写后按精确目标回读。
- Agent Instructions 是整份替换，不是追加。任务协议留在 Relay，长期人格与私有规则留在 Multica Agent 配置或原生执行器明确加载的私有角色档案。

## 完成标准

- 代码改动运行 `pnpm test`、`pnpm lint` 和 `git diff --check`；测试必须覆盖成功路径及被修改的失败、重试或隐私边界。
- 文档改动检查中英文入口、相对链接、术语和当前代码合同；删除文档时确认没有遗留引用。
- 发布验收分别核对 ingress、QStash、Redis/Multica persistence、Agent run 和原 Slack thread reply。离线测试或 deployment ready 不得写成 live E2E 成功。
