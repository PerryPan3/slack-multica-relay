# 配置说明

[English](CONFIGURATION_EN.md) | **简体中文**

Relay 与 Multica Agent Runtime 是两个独立的信任域。Relay 变量配置在 Vercel、EdgeOne 或 Cloudflare Workers；Agent Runtime 变量配置在执行目标 Multica Agent 的机器或 Pod。不要把一侧的完整环境变量复制到另一侧。

## Relay 部署环境

以下变量配置在部署平台中。凭据和 token 必须放入平台的 Secret Store。Slack ID 和服务 URL 属于普通配置，也可以作为 Secret 保存。

Slack ID 使用逗号分隔的大写标识符。`SLACK_TARGET_USER_IDS` 与 `SLACK_TARGET_SUBTEAM_IDS` 可以分别留空，但至少一项必须包含目标。blocklist 的优先级始终高于 allowlist。

| 变量 | 必填 | 取值与来源 |
| --- | --- | --- |
| `SLACK_SIGNING_SECRET` | 是 | Slack App → Basic Information → App Credentials → Signing Secret。 |
| `SLACK_TEAM_ID` | 是 | Slack Workspace ID，通常以 `T` 开头。 |
| `SLACK_TARGET_USER_IDS` | 条件必填 | 显式 mention 后会触发 Relay 的 User ID。 |
| `SLACK_TARGET_SUBTEAM_IDS` | 条件必填 | 显式 mention 后会触发 Relay 的 User Group ID。 |
| `SLACK_ALLOWED_CHANNEL_IDS` | 是 | 允许触发的 Channel ID，逗号分隔。只有确实允许所有频道时才填写 `all`；缺失或留空会拒绝启动。 |
| `SLACK_BLOCKED_CHANNEL_IDS` | 否 | 永不触发的 Channel ID；即使 allowlist 为 `all` 也优先阻止。 |
| `SLACK_ALLOWED_SENDER_IDS` | 否 | 允许触发的 User ID。缺失或留空等同 `all`；owner-only 部署应填写 owner 的准确 ID。 |
| `SLACK_BLOCKED_SENDER_IDS` | 否 | 永不允许触发的 User ID。 |
| `SLACK_BOT_TOKEN` | 条件必填 | reaction 添加、查询和清理优先使用的 Bot 身份，需 reactions:read/write。 |
| `SLACK_USER_TOKEN` | 条件必填 | 未配置 Bot 时用于 reaction 的 User 身份。 |
| `SLACK_CANCEL_KEYWORDS` | 否 | 逗号分隔的完整取消关键词，默认 cancel,取消。 |
| `SLACK_CONTEXT_TOKEN` | 是 | 带目标会话 history scopes 的 user token。consumer 用它读取有界 history/replies；如需通过 `users.info` 补充参与者姓名，还需 `users:read`。可以与 reaction 使用同一个已授权 token，但必须显式配置。 |
| `SLACK_REACTION_NAME` | 是 | 不带冒号的 Slack emoji shortcode，例如 `eyes`。reaction 只确认 Relay 已持久化，不证明 Agent 已完成。 |

Slack App Request URL 为 `https://<deployment-host>/api/slack/events`。只订阅目标频道类型需要的 message events；读取私有频道时必须把 App 安装到对应频道。

| 变量 | 必填 | 取值与来源 |
| --- | --- | --- |
| `MULTICA_API_BASE_URL` | 是 | 目标 Multica Server 的 HTTPS base URL，不含额外路径、query 或 fragment。 |
| `MULTICA_API_TOKEN` | 是 | 能访问目标 Workspace、Project 和 Agent 的专用 Multica PAT。 |
| `MULTICA_WORKSPACE_ID` | 是 | Relay Project 所属 Workspace 的 UUID。 |
| `MULTICA_PROJECT_ID` | 是 | Relay 创建 Issue 的专用 Project UUID。 |
| `MULTICA_AGENT_ID` | 是 | Relay Issue 指派的 Agent UUID。Agent 必须属于同一 Workspace，并已绑定 Runtime。 |

Relay 调用普通 Issue 和 Comment API，不需要 Multica Autopilot 或 webhook URL。

| 变量 | 必填 | 取值与来源 |
| --- | --- | --- |
| `KV_REST_API_URL` | 是 | Relay 专用 Upstash Redis 的 HTTPS REST URL。 |
| `KV_REST_API_TOKEN` | 是 | 同一个 Redis 数据库的 REST token。 |
| `QSTASH_URL` | 否 | QStash API base URL，默认 `https://qstash.upstash.io`。 |
| `QSTASH_TOKEN` | 是 | QStash publish token。 |
| `QSTASH_CURRENT_SIGNING_KEY` | 是 | 验证当前 QStash 投递的 signing key。 |
| `QSTASH_NEXT_SIGNING_KEY` | 是 | key rotation 期间用于验证下一把 key。 |
| `RELAY_CONSUMER_URL` | 是 | 准确的公开 URL：`https://<deployment-host>/api/queue/consume`，必须与 QStash 验签使用的 URL 完全一致。 |

Redis 必须由该 Relay 独占，用于保存线程映射、锁和写入确认状态。QStash 保存排队的 Slack payload，并重试失败的 consumer；其访问控制与保留时间应按 Slack 消息内容的敏感程度配置。

Redis 将 v3 已交付边界、最近请求及首次附近背景缓存 24 小时；缓存包含已交付的最小消息文本与附件引用，不含附件内容或 private URL。只有 Multica 写入确认后才推进边界；过期或乱序时从 scoped Issue/comments 恢复。准备好的 envelope 同样冻结 24 小时。新 mention 读取完整同线程间隔并沿用首次背景，重试复用冻结输入；旧 Issue/comment 不回写。Multica 的保留策略与 Redis TTL 独立。

上下文窗口和展示版本由代码固定，不是新增的环境变量：首次附近讨论为 30 分钟内最近 12 根，其中 2 个活跃线程各带 5 条回复；后续保留上次已交付 mention 到本次请求的完整同线程区间。升级不需要迁移 Redis 或修改 Agent 配置，旧索引通过 Multica 已持久化记录恢复；读取失败或状态损坏会重试或拒绝，只有恢复读取成功且没有更早 mention 才从根全读。首次背景独立于 mention 边界保存，迟到请求也保留其原始时间。具体边界见 [上下文合同](CONTEXT-ASSEMBLY-DESIGN.md)。

Relay 在发布到 QStash 前，把每个 Slack file object 投影为 `id`、`name`、`mime`、`size` 和 `contentStatus`，并额外携带由完整安全字段生成的 fingerprint。下载内容、private URL、thumbnail、shares 和凭据不会进入队列 payload。暂时性或结果不明的失败返回 503 交给 QStash 有限重试；无效队列 payload、损坏的持久状态、scope 违反、无法消除的映射歧义和确定性体积超限返回 `rejected` 与 `retryable: false`。必需区间的明确权限失败、根缺失或分页预算耗尽也拒绝启动；可选背景缺失则按上下文合同标记。

## 取消与 reaction 身份

在原任务 thread 发送目标 mention 加完整的 `cancel` 或 `取消` 可停止关联任务。`SLACK_CANCEL_KEYWORDS` 的非空列表替换默认词；只有 `SLACK_TARGET_USER_IDS` 中的发送者可取消，用户组 mention 不授予取消权，入队与消费时都复核准入。取消不创建新的 Agent 任务，也不读取上下文树。

取消意图、run ID 集合和清理进度与原线程映射一起保存在 Redis；重试只处理原 run 集合。取消期间收到的普通消息被忽略；结束后新 mention 可继续原 Issue。回读 run 终态后仅清理所选身份自己的 reaction，其他人的表情保留。`cancelled` 证明 Multica 状态及中断请求，不是独立的 daemon 停止确认，也不会撤销已完成的外部写入。

reaction 添加、查询和清理统一使用非空 `SLACK_BOT_TOKEN`，未配置时使用 `SLACK_USER_TOKEN`。所选身份需要 `reactions:read`、`reactions:write` 和目标频道访问权；调用失败不会换身份。上下文仍用 `SLACK_CONTEXT_TOKEN`，Agent 最终回复仍使用 Runtime 自己的 user token。

旧 `SLACK_REACTION_TOKEN` / `SLACK_REACTION_READ_TOKEN` 不再读取。升级前将现有凭据按原身份配置到新变量，确认后再切换代码；旧变量可保留给旧部署回滚使用。若从 User 切到 Bot，历史 User reaction 保留。回滚取消功能前应先处理队列内 `operation=cancel` 的消息，避免旧消费者将其当普通请求。QStash 重试耗尽时保留 DLQ 责任，不删除 Redis 状态来强行重发。

## Multica Agent Runtime

Relay 不读取以下变量。只有 Agent Instructions 或本地 Slack Skill 需要时，才在 Runtime 或 Agent 环境中配置。

| 变量 | 用途 |
| --- | --- |
| `RELAY_OWNER_SLACK_USER_ID` | 授权判断使用的 owner ID。必须来自已验证 Slack event author，不能使用 display name。 |
| `RELAY_SKILL_ROOT` | 已批准本地 Skill 目录的绝对路径。该路径与机器绑定，不属于本仓库。 |
| `SLACK_SKILL_ALLOWED_CHANNELS` | Slack Skill 读写时执行的 Channel allowlist，应与 Relay policy 保持一致。 |
| `SLACK_USER_TOKEN` | Agent Slack Skill 和本地 reply adapter 使用的 owner user token。 |
| `SLACK_TOKEN` | Slack Skill 需要时使用的兼容变量，必须显式指向预期的 owner identity。 |
| `SLACK_BOT_TOKEN` | 回复必须使用 owner identity 时留空，否则 Slack Skill 可能优先选择 Bot token。 |

Agent 不需要单独的 `RELAY_ALLOWED_CHANNEL_ID`。Relay 会在入队前校验 Team、channel、sender 和 mention policy，并在消费时再次校验。`SLACK_SKILL_ALLOWED_CHANNELS` 只约束经过 Slack Skill 的调用。本地 reply adapter 是独立路径：它从指定 Multica Project 的来源 Issue/comment 读取目标，并使用 `SLACK_USER_TOKEN` 直接发送。

## 配置真源与生效方式

| 配置面 | 责任真源 | 如何生效 |
| --- | --- | --- |
| Relay 准入、队列和持久化 | 上述部署环境变量 | 使用更新后的环境重新部署已测试 commit，再回读 health 与 policy 行为。 |
| 回复显示名和固定路由身份 | 传给 `scripts/slack-reply.py` 的私有 Runtime JSON | adapter 每次执行都会读取；新的 `displayName` 从下一次发送起生效。 |
| Agent 人格和操作指令 | Multica Agent Instructions | 可以用本地私有 prompt 维护候选，但必须通过 `multica agent update --instructions` 显式同步，并回读同一个 Agent ID。只修改本地 prompt 文件不会影响 Runtime。 |
| Slack 等本地工具 | 私有 Runtime Skills 与环境变量 | 更新 Runtime binding/configuration，并回读已绑定 Skill 和环境。本仓库不会自动加载这些文件。 |

## 部署验收

1. 运行 `pnpm install --frozen-lockfile`、`pnpm test` 和 `pnpm lint`。
2. 部署与测试一致的 immutable commit。
3. 确认生产域名上的 `/api/health` 正常响应。
4. 确认 Slack Request URL 验证通过。
5. 发送一条已授权 Slack event，分别核验 QStash 接收、Multica Issue 创建、reaction author、Agent 执行和最终 Slack thread 回复。
6. 验证 blocked channel 与 blocked sender 不会产生 QStash 或 Multica 副作用。

HTTP 200、确认 reaction、Issue 创建或部署完成都只能证明对应阶段，不能单独证明整条 Agent 回复链路成功。

## 可选模型 footer

consumer 在准备消息时使用现有 Multica PAT 读取一次目标 Agent 配置，deadline 为两秒，并校验身份，不需要新增凭据。model 不可用、继承或为空时记为 `null`，footer 省略 model 部分；reply adapter 始终追加配置的自动化身份。footer 表示配置快照，不代表该次 run 实际使用的 model 或计费 tier，也不会修改 Agent model 设置。冻结快照和 text/Block Kit 渲染合同见 [Slack 上下文组装](CONTEXT-ASSEMBLY-DESIGN.md)。

配置快照 GET 使用 `redirect: manual`；workerd 不支持 `redirect: error`。3xx 仍按不可用处理，不跟随跳转。私有 adapter JSON 的 `styleGuide` 与 Runtime `SKILLS.md` 索引应指向同一个实际可读的文件，更新后核对路径和该 Skill 的相对参考文件，不能仅检查 JSON 保存成功。

## 本地 reply adapter

附件发现读取完整评论树（含回复和已解决线程），再按当前 `source_task_id` 过滤；不能使用 `--roots-only`，否则 follow-up 回复中的附件会漏掉。更新 token 后同时核对实际登录 shell 的环境：`.zshenv` 中的旧导出值会覆盖 Runtime 注入的新值，仅更新 Skill `.env` 不足以证明生效。

运行 `python3 scripts/slack-reply.py --help` 查看 CLI。把 adapter 绑定到私有 Runtime Skill，并提供包含 `displayName`、`agentId`、`workspaceId`、`projectId`、`teamId` 和 `serverUrl` 的私有 JSON。该配置不包含 token；adapter 沿用 Runtime 已有的 Multica CLI 认证与 `SLACK_USER_TOKEN`。Agent 通过 `--text-file` 提供正文；路由和 model metadata 从来源 Issue/comment 回读。adapter 用 Slack section blocks 发送正文，再追加 attribution context block，格式不依赖模型正文或 persona instructions。首次发送前会通过 `conversations.replies` 确认精确根消息仍存在，避免 Slack 将无效 thread 降级为频道或 DM 根消息。

绑定 `multica-skills/multica-final-reply` 后，Agent 在发送前用其中的只读脚本生成当前 run context，并按业务证据选择相关 PR。adapter 通过可选的 `--run-context-file` 和 `--github-context-file` 校验 Issue 归属、统计来源以及 GitHub repository/branch/PR URL 的一致性，再追加运行统计和 GitHub 关联行；没有可核验成果时省略对应行。Agent 不直接控制路由、delivery marker 或重试。

adapter 根据来源 Issue/comment 生成稳定 delivery block ID，并在配置 JSON 旁的 `.slack-reply-state/` 保存私有 delivery ledger。该目录已被 Git 忽略，目录和原子 JSON 使用 owner-only 权限；文件和父目录状态都在 POST 前完成 `fsync`。正文状态依次为 `attempting`、`accepted`、`sent`：Slack 返回的 message timestamp 将发送后验证收窄到新回复，只有 thread readback 成功才进入 `sent`。后续 Runtime 重试直接返回已持久化结果，不扫描旧线程历史。

请求明确要求文件时，Agent 先用本轮 task 的 Multica 评论保存最终附件，再给 adapter 增加 `--deliver-task-attachments`。adapter 只选择 `source_task_id` 等于当前 `MULTICA_TASK_ID` 的附件，下载后核对 size 与 SHA-256，并通过配置的 `slackCliPath` 调用 Slack Skill `files_upload --as user`。每个 attachment ID 使用独立 `attempting/sent` receipt，列表顺序变化或新增附件不重传成功项；未知结果保留上传阶段和已取得的 file ID，不自动重复上传。每个附件上传前重新核对精确根消息，包括正文已送达后的重试；检查后根消息仍可能被删除。Slack Skill 明确标记 `retry_safe: true` 的确定拒绝会清理 intent，修正权限或参数后可再次执行。限制为 20 个文件、单文件 25 MiB、总计 100 MiB。文件上传需要 User token 已获批的 `files:write`；申请待审批时报告权限阻塞，不切换身份。

如果 POST 结果未知，重试从本机尝试时间前五分钟开始查找，以容纳 clock skew。找到 delivery block 后进入 `sent`；未找到则返回 `slack_delivery_unknown`，绝不自动重复 POST，因为“查询为空”不能证明 Slack 没有提交。清理这条状态前必须在原线程独立核对。同一来源的并发执行无法取得本机锁时立即返回 `reply_delivery_busy`，不会阻塞等待。`SLACK_USER_TOKEN` 除 `chat:write` 外，还需要 `conversations.replies` 对应的 history scope。

发送错误只对已知明确的鉴权、权限、参数或目标拒绝清理 intent。`internal_error`、`fatal_error`、其他服务端/未知错误、非 429 HTTP 错误和损坏响应保留 `attempting`，下一次先回读，不能盲目再次 POST。

HTTP 429 或 JSON `rate_limited`/`ratelimited` 使发送 ledger 进入 `rate_limited`，并保存 `Retry-After` 对应的 `retryAt`。CLI 返回 `slack_rate_limited`、`retryable: true` 和 `retry_after_seconds`；调用方等待后重新执行同一发送命令，等待期内不调用 Slack，到期后允许再次发送。缺失或无效的 Retry-After 使用 60 秒冷却，CLI 不自行睡眠或循环。回读时遇到限流只延后回读，不改变原 `accepted`/`attempting` 状态。

footer appearance 每次发送时都从私有配置读取。`displayName` 是完整 attribution label，可以包含 Unicode emoji 或 Slack shortcode。修改后从下一次发送生效，无需重启 Runtime 或重新部署 Relay；已有消息不会改变。修改时必须保留其他路由和身份字段。

```json
{
  "displayName": "🤖 Example"
}
```

以上示例只展示 appearance 字段；真实配置必须保留其余必填字段。

## 最终回复与私有风格配置

将 `multica-skills/multica-final-reply` 发布并绑定到目标 Agent。`FINAL_REPLY_CONFIG` 指向 owner-only 的私有 JSON；与已有 adapter 的必填字段一起保存下列可选配置。个人 Prompt、表情选择和 workspace 表情目录放在仓库外或已忽略的 `.private/` 中，不发布到公开 Skill。

```json
{
  "adapterPath": "/path/to/relay/scripts/slack-reply.py",
  "slackCliPath": "/path/to/slack-skill/scripts/slack.py",
  "styleGuide": "/path/to/private/communication/SKILL.md",
  "emojiGuide": "/path/to/private/emoji/favorites.md",
  "emojiCatalog": "/path/to/private/emoji/catalog.json",
  "icons": {"time": "⏱️", "model": "🤖", "tools": "🔧", "skills": "🪄", "github": "🔗", "multica": "↗️"}
}
```

这些是附加字段，保留原 JSON 的 `displayName`、`agentId`、`workspaceId`、`projectId`、`teamId`、`serverUrl`。图标可使用当前 workspace 已确认存在的 shortcode。未指定个人风格时使用通用回复风格；表情库不全量进入每次模型输入。

Agent 环境的 `FINAL_REPLY_APP_URL` 为 Multica 网页 HTTPS 根地址，`FINAL_REPLY_WORKSPACE_SLUG` 为工作区 slug。采集器支持等价的 `--app-url`、`--workspace-slug` 及 `--issue-identifier` 显式参数，优先于环境；未提供时只读补查，缺失则省略链接。隔离运行应显式配置环境值，不依赖宿主个人 CLI 配置。

`--run-context-file` 除统计外携带当前任务的编号与链接；adapter 核对 Issue 和当前 run 后在统计行末尾渲染任务入口。`--github-context-file` 使用 `version: 1`、`pullRequests` 数组及可选 `branches` 数组；分支项包含 `repository`（owner/repo）与 `branch`，PR 项另含 `number` 和对应的完整 GitHub `url`，合计最多五项。每项单独一个 context block，fallback 保留同样的信息。有效本轮统计不再叠加旧 envelope 的模型行，attribution 和 delivery marker 仍保留。

发布后回读 Skill 全部文件及 Agent 绑定；已有任务可能继续使用其启动时复制的旧 Skill，新任务才能证明加载。配置保存、只读 dry-run 和真实 Slack 发送分别验收。

## Cloudflare Workers

Worker 入口为 `src/cloudflare.ts`，配置为 `wrangler.jsonc`，提供与其他平台相同的三个 `/api/` 路由。它直接传递原始 Request 和 `env`，保留 Slack/QStash 签名字节，不使用后台 `waitUntil` 提前确认队列消费。`/api/health` 只确认入口可达，不检查外部配置。

Wrangler 本地开发和测试支持 Node.js 22.12+，推荐 Node.js 24 LTS；`.nvmrc` 和 `.node-version` 默认使用 24。Cloudflare Workers Builds 的 `NODE_VERSION` 设置为 `24`。Workers 执行环境是 workerd，Node.js 版本只控制构建工具，不改变 Worker 运行时。配置的 compatibility date 为 `2026-09-08`，默认启用 Node.js compatibility；共享代码显式导入 `node:buffer`，复用 `node:crypto`。参见 [Cloudflare Node.js compatibility](https://developers.cloudflare.com/workers/runtime-apis/nodejs/)。

1. 安装依赖后，运行 `pnpm exec wrangler login` 连接预期 Cloudflare 账号；将 `wrangler.jsonc` 中 `name` 改为自己的 Worker 名称。
2. 根据本文件 Relay 变量表，在 Cloudflare Worker 的 Variables and Secrets 中配置全部必填值。普通 ID/URL 在 Wrangler `vars` 中维护，避免后续 CLI 部署覆盖仅存在于控制台的普通变量；token、PAT 和 signing key 使用 Secret。也可通过 `pnpm exec wrangler secret put SLACK_SIGNING_SECRET` 交互输入，其他凭据同理，不要把值放在命令参数或 Git 中。
3. `RELAY_CONSUMER_URL` 填写实际公开地址，例如 `https://<worker>.<subdomain>.workers.dev/api/queue/consume`。QStash 必须能直接访问该地址，不能被交互登录、Cloudflare Access 或验证码拦截。
4. 本地开发可以将测试环境值写入已忽略的 `.dev.vars`，再运行 `pnpm dev:cf`。本地模式仍会调用所配置的外部服务，必须使用独立测试资源；无需凭据的自动测试使用虚构绑定且不调用 live 服务。
5. 运行 `pnpm test`、`pnpm lint` 和 `pnpm build:cf`。最后一项仅构建并验证部署包，不发布。
6. 配置完成后运行 `pnpm deploy:cf`；已有部署使用经过核对的生产配置。若普通变量由控制台管理，部署时带 `--keep-vars` 保留它们；Secret 继续留在 Cloudflare Secret Store。在 Slack 中设置公开 Events URL，再按上方部署验收逐阶段验证。生产域名变更必须同步 consumer URL。

发布记录将 Git commit、Worker version 和实际流量比例分别保存到私有部署记录；可用 Wrangler 的 `--tag`、`--message` 标识构建来源。发布后通过 `deployments list --json`、`versions view <version-id> --json` 回读目标版本，并核对 runtime、绑定与原域名。`/api/health` 应返回 200；缺少有效签名的 Slack/QStash POST 应返回 401，且不触发任务。真实 mention 的上下文和最终回复仍需独立验收。

Cloudflare 沿用现有 QStash 和 Upstash Redis，不使用 Cloudflare Queues、KV 或 D1。Python reply adapter 和持久 ledger 继续运行在私有 Multica Runtime。账号的 CPU 和 subrequest 限制仍须适配上下文读取与恢复路径；本地测试和 dry-run 不代表线上完整链路验收。

### Worker 回复个人自聊

当 `WORKER_THREAD_REPLIES=true` 时，队列消费者从目标 Multica 任务读取最终文本，使用 Worker 中的 Slack 用户凭据回复原线程。本机 Agent 只生成正文。此模式要求发送人名单中只有一位所有者，且该用户也是提及目标。

设置 `SLACK_SELF_ASSIGN_SCOPE=dm` 可接受所有者在其他单聊里明确 @自己的消息，设置为 `all` 也可接受其所在频道和多人私聊。其他人的权限仍按频道和发送人的配对检查，屏蔽名单优先。新增会话用所有者的用户凭据读取，需为对应会话类型安装用户级 history 权限并订阅消息事件。上下文限于当前线程。每次回复都依据首次冻结的消息、绑定的项目和 Agent，以及对应运行结果核对目标。队列最多等待约 60 分钟，覆盖单次本机 Agent 的 30 分钟执行时限；等待到期不代表任务完成。

启用所有者自派本机任务和 GitHub 审查后，经过准入的本人自提及也保留正式审查能力，不因会话不是专用审查频道而降级成只能读代码。同事仍只能在配置的频道发起任务。

Worker 在调用 Slack 前将发送意图存入 Redis。Slack 接受后，Worker 按返回时间戳从原线程读回带唯一标记的回复。请求结果未知时只读回，不自动重发。此模式仅支持文本，默认关闭。

### 由本机 Agent 提交 GitHub PR 审批

仅在设置了 `GITHUB_REVIEW_CHANNEL` 和 `GITHUB_REVIEW_SIGNING_KEY` 的频道启用。原有 Slack 发送人和频道准入仍然生效。用明确的 review 请求和 GitHub PR 链接触发；省略链接时，同线程必须能唯一定位一个 PR。Worker 签发的请求绑定到原消息、发送人、线程和 PR。本地运行环境校验签名后用已配置的 GitHub 身份取完整 diff、审批和行内意见，并在同一提交写入和回读正式审批。GitHub 凭据只留在本机。来源密钥分别保存在 Worker secret 和本机受限目录；本地回执账本用于结果不明时阻止重复投票。正式审批前目标提交发生变化、PR 为草稿或由当前身份创建、代码或权限不足时，原线程说明原因。

### 旧执行器的 PR 审查取证操作

常规 Slack 任务沿用原运行环境。只有来源验证通过的 PR 审查会收到该仓库适用的操作目录。Agent 可以先申请证据，再根据返回值复核结论。宿主在执行操作前核对仓库归属、参数和 PR 中的对象引用；签名请求始终绑定到原 PR。证据只在本轮流转，回帖附带调用记录。如果缺少决定性证据，不提交 GitHub 审批。

操作目录在本机私有部署的 `.private/runtime/review/evidence.json`。增加操作时指定已有 stdio MCP 的服务器名和方法、适用仓库组织、受限参数、固定调用模板以及允许返回的字段。参数为 PR 中可核对的标识符或 Jira 单号，可以再限定具体取值或完整关系名。返回值只投影登记的字段。不要登记可以自由编写查询、下载客户明细或修改系统状态的操作。保存后新审查会直接读取配置；变更后应分别测试允许的调用、越权反例和返回字段。

完整表结构操作可设置 `result_limit: 2000` 与 `require_complete: true`，超过上限时返回失败，不把截断数据作为完整结构。普通操作仍默认最多返回 100 条。`display_name` 用于页脚汇总工具名称，调用次数由宿主实际记录生成。正式审批与暂缓审批均使用签名结果回执，工具记录不进入正文。正式审批回读成功后才显示审批链接，GitHub 审批正文同时保留决定和关键依据。

可用 `GITHUB_REVIEW_SELF_CHANNEL` 指定所有者自聊，用于私下执行真实 PR 审查和验收。该入口必须是 D 开头的单聊，且消息作者与被提及者都为唯一配置的所有者。它不向其他私聊或发送人开放审批，也不改变原频道的准入规则。

核对查询对象时，程序使用 PR 差异、标题、说明，以及从当前提交实际读取的变更文件全文。线程消息和历史审查只能帮助理解问题，不能扩大可查询的对象范围。同线程能唯一定位 PR 时，“为什么暂缓”“重新查一遍”等追问可以调用只读工具。程序会带入同一线程、同一 PR 的最近一份审查或复核摘要，并将追问固定为只读模式。即使模型建议 Approve 或 Request changes，程序也只回帖解释。

启用 OWNER_LOCAL_TASKS 后，仅配置为唯一所有者的本人 @本人请求可运行本地任务。有效 PR 审查仍优先。Worker 将身份、原消息及当前线程的有限背景签名，本机再次验证。执行目录、所有者、团队及允许快进的远端由私有 runtime/owner-local/config.json 登记。普通命令只可在该工作目录内工作，禁止网络和 Git 元数据写入；更新 main 由单独的快进操作执行，遇到本地改动会停止。执行状态按原消息持久记录，结果不确定时不自动重做。

在含 PR 的线程可用“本机任务：”明确选择所有者的本机入口。单独发送“@自己 重启 Claude”可请求 Claude 桌面端正常退出并重开，退出失败不会强杀或中断终端会话。

在已授权的审查频道和所有者自聊中，每次 @ 都将本轮原话和同线程背景交给本机模型判断意图。Worker 签名确认身份、范围和实际出现的 PR 链接；模型只在正式审查、只读复核、本机任务、直接回答、澄清和能力不足之间选择。程序单独核对目标、只读要求及本机所有者身份。同一请求的选择冻结，重试不重新解释。审查正文可使用已签名的线程背景作为材料，只有 GitHub 回执能证明正式投票。超出背景预算时保留线程根及近期对话并标明裁剪。

### 旧执行器的独立只读业务排查

Worker 用 BUSINESS_CHANNEL、BUSINESS_SENDERS 和 BUSINESS_TEST_DM 指定唯一业务频道、发送人和所有者测试自聊。在已通过 Slack 准入的本人提及事件上签出单独的 business 权限。本机还会用私有 runtime/business/policy.json 核对团队、频道、所有者和发送人；同事只能获得只读业务能力。PR 审查和本机命令仍有独立入口。业务结果按签名请求和原线程交付。

本机目录从现有 MCP 连接登记固定只读方法，包括 Jira 工单查找和读取、Redshift 实际表结构和聚合、ClickHouse 元数据、Airflow 运行状态。Jira 项目按私有 jira_projects 限定。数仓只返回字段或汇总；分区汇总最多 31 天，参数必须通过标识符和来源核对。可以从工单描述或已查到的表结构继续查证。排查结论要引用本轮成功的工具结果。缺表名、不可访问或证据不足会说明缺口，不开放任意 SQL、客户明细、工单写入或 shell。自聊验收使用真实 Slack 本人提及；频道不发送测试消息。

独立业务排查支持由 Agent 编写 Redshift 汇总 SELECT。可使用非递归 CTE、显式等值 JOIN、条件计数和聚合算式。本机在 MCP 解释器中使用 scripts/requirements-business-sql.txt 对应的 SQLGlot 版本解析 Redshift 方言。程序逐层核对物理表的库、schema 以及当前线程或已核验材料中的来源，将每张表限制到同一个最多 31 天的 pt 区间，只执行重新生成的 SQL。最终结果只向 Agent 投影最多 31 行日期和数值汇总；额外一行为截断标记。没有 pt 的表、任意函数、跨库和明细输出会被拒绝。MCP 仍使用 REDSHIFT_ALLOW_WRITE=false 和数据库的只读事务。客户端时间预算不等于数据库强制取消。


### 按人员和频道扩展任务权限

多频道接入使用精确的 `频道ID:用户ID` 配对列表，多个配对用逗号分隔。`SLACK_ALLOWED_CHANNEL_IDS` 指定接入频道，`SLACK_CHANNEL_SENDER_GRANTS` 指定可触发的同事；另用 `GITHUB_REVIEW_CHANNEL_SENDER_GRANTS` 开放正式 PR 审查，`BUSINESS_CHANNEL_SENDER_GRANTS` 开放只读业务排查。三种授权分别检查，不能把人员列表与频道列表组合成所有排列。同事不会因此获得本机命令权限。

原有 `GITHUB_REVIEW_CHANNEL` 和 `BUSINESS_CHANNEL` / `BUSINESS_SENDERS` 继续有效。旧业务执行器回退时，在私有 `runtime/business/policy.json` 的 `channel_sender_grants` 数组同步对应配对。准入在接收和消费阶段复核，回帖也复核人员与频道；移除接入授权会阻止尚未发出的回帖。

频道加入列表不等于 Slack 已能交付事件。发布前确认所有者已加入频道、用户 history scope 和消息订阅已安装，并用实际配置的读取身份验证该频道。`SLACK_CHANNEL_READ_TOKEN` 可以是已授权的所有者用户凭据，也可以是已加入目标频道的机器人凭据。无需为了同事触发而向其开放私聊或其他频道。验收使用配对矩阵及私聊，群内测试须单独授权。

### 持续使用工具的 Agent

在宿主私有目录 `.private/runtime/agent/config.json` 设置 `{"enabled":true}` 后，带 `intent1` 签名的请求交给 `scripts/agent_runtime.py`。可选 `channels` 数组用于先在自聊验证；数组为空或未设置表示覆盖入口已批准的会话，不新增发送人或频道权限。关闭该开关回到旧执行器。旧执行器的固定工具模板和路由类别不约束新 Agent。

解释器沿用私有 `review/evidence.json` 的 `python`，需安装 `scripts/requirements-agent.txt`。它提供 MCP 2.x、HTTP 服务及 SQL 解析；不可用时须报告启动失败，不能假称已经查询。宿主读取当前 Codex 模型配置和已启用的 MCP 服务，在每次请求中建立仅监听本机、使用临时认证的工具连接。凭据保留在宿主，模型只收到工具定义和结果。指定的 Relay 工具逐项使用 Codex 的 `approval_mode="approve"`，表示执行既有授权；宿主仍检查请求签名、身份、工具操作和每次写入。其余工具不继承这一授权。

Agent 按线程保存 Codex 会话并恢复，直接使用原生工具循环。它可拆分多个 PR、发现相关系统的只读工具、查询表结构和执行 SELECT、CTE、JOIN。SQL 在宿主解析，拒绝写语句、锁表、未知自定义函数和多语句，结果最多 200 行。Redshift 查询必须显式选集群和数据库。其他只读工具超过 80 KB 的结果可通过 `read_result` 继续分页读取，最多保留 8 MiB；超过此容量时要求缩小上游查询。不会因用完固定轮数就把任务标为完成。

GitHub 正式审批独立核对当前请求的审查意图、目标、最新提交、重复写入和真实回执；已保存且未完成的审查委托可以在追问时继续；明确只查状态或任务已完成时不新增投票。同事没有本机命令工具。账号本人的本机命令沿用指定工作目录和隔离配置，按请求和命令记录执行意图与结果；结果未知时不自动重放。

审查材料按固定 base/head 版本分页读取。GitHub 的完整 diff 不可用时，改读变更文件的固定版本；ZIP、Tableau `.tfl` 及内嵌 XLSX 可逐层读取文本/XML，PNG 作为 MCP 图片交给模型，读取记录绑定实际 blob 和内容摘要。Approve 前必须读完所需材料；发现确定缺陷可提交 Request changes。未知格式明确报告缺少读取器，不把它误报成仓库无权限。新提交会使旧材料记录失效。

AWS 身份和 S3 元数据通过宿主已有凭据读取，提供身份检查、对象列表及对象元数据，凭据不进入模型或临时命令环境。工具目录不等于访问成功，必须核对实际读取结果；这不是 `aws s3 sync --dryrun`。隔离命令环境找不到凭据，不能作为宿主没有权限的结论。

模型执行有 30 分钟保护时限，同线程请求串行。外层给准备和清理预留额外时间；超时先请求正常退出并保存中断结果，再回收进程。已收到模型终止事件和最终答复时，即使 CLI 没有自行退出，也会收尾交付。只读工具的暂时性连接异常可重试一次，写入不自动重试。私有 `runs/native-agent` 保存会话、工具执行记录和签名结果，供追问和重试使用。真实结果来自工具回执；解释器、连接或执行失败必须明确报告，不能写成业务成功。本地电脑休眠期间不会执行任务；现有签名的一小时有效期仍适用，不能承诺跨越该窗口自动恢复。

私有原生会话关闭自动继承的 Apps 和插件工具，已配置的只读 MCP 仍通过宿主连接开放。GitHub 读取和审批统一使用本机通道；远端连接的 IP 限制不能当作本机没有仓库权限的证据。

旧审查的回执逐项核对，一项读取失败不阻止其他目标和业务查询。授权准备失败时保留只读工具，并提供同请求内的重试入口。运行阶段和中断原因保存于私有会话，后续追问带入该记录；启动失败的诊断仅保存在私有目录。GitHub 草稿在创建时即带稳定任务标识，丢失本地编号后可核对原草稿恢复。验证范围和运行限制见 [可靠性测试](RELIABILITY-TESTING.md)。
