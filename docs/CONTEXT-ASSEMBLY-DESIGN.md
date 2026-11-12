# Slack 上下文组装

[English](CONTEXT-ASSEMBLY-DESIGN_EN.md) | **简体中文**

Relay 把当前请求放在最前，每条对话压成一行 JSON，保留线程关系和原话。上下文选择由时间、线程和明确链接决定，不调用模型做预摘要，也不按字数、表情或主观相关性筛掉同线程发言。

## 确定的输入范围

B 为本次 mention；A 为同一 workspace/project/agent/thread 中，时间早于 B、且已确认写入 Multica 的最近一次 mention。

| 场景 | 带入内容 |
| --- | --- |
| 首次 mention | 当前线程根到 B 的全部对话；新建根消息时就是 B 本身 |
| 首次附近讨论 | 同一会话 `[B - 30 分钟, B)` 内最近 12 条其他根消息；其中最近活跃的 2 个线程，各带末尾 5 条回复 |
| 同线程再次 mention | 根、A、`(A, B]` 内所有对话，包括 non-mention、机器人、其他作者、表情和附件引用 |
| 首次背景 | 保留首次已交付的附近讨论，记录 `initialCutoffTs`；后续不自动刷新不相关的主时间线 |
| 明确链接 | 本次请求、A、当前根及本轮对话里的同会话 Slack permalink；最多展开 3 个去重目标，带根、目标及前后各 2 条回复；引用根时带其后 2 条回复 |
| 没有更早的 A | 正常首次请求，或必要恢复读取成功但未找到早于 B 的已交付 mention：从根到 B 全读 |

首次线程即使很早开始，也保留完整当前线程；附近窗口围绕首次 mention。附近主消息按时间倒序选取、顺序展示；旁支按 `latest_reply` 排序。少于上限就照实带入，不扩到更早时间。明确链接不递归展开，跨会话链接只保留原文；当前必需区间中已经存在的目标不重复读取。链接不能解析、权限不足或可选读取预算不足时，不声称已看过。

必需区间通过 `conversations.replies` 的 `oldest=A`、`latest=B`、`inclusive=true` 读取并完成游标分页；根不在结果中时单独精确读取。时间过滤与游标可组合，依据 [Slack replies 合同](https://docs.slack.dev/reference/methods/conversations.replies/)。单消息定位使用 [Slack history 合同](https://docs.slack.dev/reference/methods/conversations.history/)。

## 紧凑展示与数据合同

第一行保持原 marker，之后是请求引用、单行来源、范围说明和 JSON 数据区。`relay-payload:v1` 成对标记及动态长度的数据围栏保持不变。

`schemaVersion: 5` 每条普通消息独占一行；`origin=unknown`、空 `files` 和 currentRequest 的空正文在展示中省略。有附件、bot 来源或缺失标记时保留实际字段。时间戳、作者 ID、姓名映射、原文、回复父子关系和回复路由均保留，不把旁支回复摊平成主消息。

用户通过受信任应用代发时，`eventPayload.sourceAppId` 保存 Slack 原始 `app_id`，并随队列、冻结快照和任务展示保留。普通用户消息不含该字段，兼容已有快照。这个字段用于准入和回帖来源复核，不能由正文推断。

```json
{
  "schemaVersion": 5,
  "eventPayload": {"teamId":"T1","channelId":"C1","threadTs":"1000.000001","messageTs":"1100.000001","text":"本次请求"},
  "context": {
    "anchorTs":"1000.000001","sinceTs":"1050.000001","cutoffTs":"1100.000001",
    "timeline": {"status":"complete","messages":[
      {"ts":"1000.000001","authorId":"U1","text":"线程根","replies":{"status":"complete","messages":[
        {"ts":"1050.000001","authorId":"U1","text":"上次 mention"},
        {"ts":"1060.000001","authorId":"U2","text":"中间未 mention 的讨论"},
        {"ts":"1100.000001","authorId":"U1","currentRequest":true}
      ]}}
    ]}
  }
}
```

示例省略固定任务解释和部分路由字段。完整请求及附件在 `eventPayload` 中，`currentRequest` 节点只引用它；首屏原文引用最多 4 KiB，数据区保留完整请求。TS 恢复解析器补回省略的默认字段。Python reply adapter 继续读取同一个 eventPayload/replyContext。历史裸 JSON、v4 围栏和旧 marker 仍可恢复；重复或损坏的数据区拒绝恢复。旧 Issue/comment 不回写。

`complete` 仅描述对应读取范围。`coveredFromTs/coveredThroughTs` 是实际保留范围，不证明区间之外没有消息；可选旁支独立标记 truncated/unavailable，首次背景的缺失可通过 `initialStatus/initialReason` 延续。附件始终 `not_loaded`，不把附件引用当成已读取内容。

## Agent Footer 降噪

历史消息只有同时具备 bot/app 来源、尾部 `relay-delivery-<64位十六进制>` context 标记，以及 adapter 的 section → context 布局时，才从正文 section 提取文本并标记 `footerOmitted: true`。多个正文 section 仅接受 adapter 的 3000 Unicode 字符分块形式，直接拼回正文。作者、时间戳、bot 来源与附件引用不变；正文中出现的 “Sent by”、模型名或表情不按关键词删除。

未标记消息、人类消息、含图片/附件 block、fields/accessory 或不支持的布局保留原 text。旧快照已丢失 blocks 时也不猜测删 Footer；只影响后续新读取，历史 Issue/comment 和冻结快照不回写。

## 预算与失败

| 项目 | 上限与策略 |
| --- | --- |
| 当前线程必需内容 | 不按最近 20/100 条裁剪；不截断原文或删附件引用 |
| 可选消息 | 正文 4 KiB、最多 5 个附件引用；超限显式标记 |
| 分页 | history 最多 5 页；必需 replies 最多 10 页；可选 replies 最多 3 页；conversation 请求总计 20 次 |
| 时间 | Slack 消息共 20 秒；先当前线程，再附近讨论和明确链接；旁支并发最多 2 |
| 输入响应 | 每个 Slack 响应最多 2 MiB |
| 输出 | 完整 envelope 48 KiB、最终 Issue/comment 64 KiB |

字节不足先移除可选旁支，再移除 A 之前仅因链接加入的可选回复。根、A、`(A,B]` 和 B 正文仍放不下时返回 `context_request_too_large`，不启动带缺失对话的任务，也不推进 A。必需分页到不了末尾返回 `context_required_page_limit`；明确权限失败或根缺失返回 `context_required_unavailable`，均为确定性拒绝。429、5xx、超时交由队列有限重试。可选读取失败只标记相应背景；跨频道或跨线程响应始终拒绝。

## 持久化、缓存与恢复

- 同一事件的 `:envelope` 和待提交上下文状态冻结 24 小时，重试不重建。
- `:sent-context-index` v3 缓存最近已写入的请求、边界和首次背景 24 小时；不再用消息指纹决定是否省略中间发言。缓存含已交付的最小上下文，无附件内容或 private URL。
- 只有 Issue/comment 写入成功或稳定 marker 回读确认后才推进 A。读取成功、准备成功、启动 reaction 都不是交付回执。
- 缓存丢失、旧版索引或乱序事件，从当前 scoped Issue 和 Relay comments 恢复早于 B 的最近已交付 mention；同时恢复全局最新已交付边界，防止迟到事件倒退索引。
- 旧版首次 Issue 的附近树按 30 分钟 / 12 根 / 2 线程 / 5 回复重新选取，避免旧的大快照延续到新 comment；只使用原快照已有证据，不补写历史。
- 正常首次请求，或必要恢复读取成功但未找到早于 B 的已交付 mention 时，保留完整根到 B。恢复接口失败、状态损坏或归属不匹配时按错误类型重试或拒绝，不把读取失败当成“没有 A”，也不绕过写入结果核对。
- thread mapping 缺失时通过唯一 thread marker 搜索恢复；Multica 历史保留策略与 Redis TTL 独立。

## 升级与兼容

首次背景与 A 分开保存：迟到的 B 即使早于首次已交付请求，仍保留原 `initialCutoffTs` 和该背景中 `ts <= B` 的根消息及回复。筛选只作用于本轮副本，不改变后续可复用的原始背景；可选背景仍受字节预算约束。

A 在本次 Slack 结果中缺失时，使用已持久化 eventPayload 的正文、作者和附件引用补入，并标记 `restoredFrom: persisted_request`；旧附件截断标记也保留。这不是本次 Slack 读取的正文。无法取得已保存正文时拒绝交付；线程根读取失败仍单独拒绝。时间边界按 Slack 消息 `ts` 判断。

`schemaVersion: 5` 是任务正文版本，`:sent-context-index` v3 是 Relay 内部缓存版本，两者独立。部署新版 Relay 即可启用，无需新增变量、修改 Agent Instructions 或重新绑定最终回复 Skill。

新 mention 使用新规则；同一事件的冻结 envelope 在 24 小时缓存有效期内复用，即使它是旧格式。缓存过期后先核对持久化记录，确需重新准备时才读取上下文。旧版首次背景在恢复后按当前范围重新选取，再写入新的 comment，原 Issue/comment 不回写。不要为强制切换格式清理事件快照、线程映射或 delivery ledger；这些记录承担幂等与恢复职责。

核验时分别比较任务正文中的 `schemaVersion`、`eventPayload.messageTs`、`context.sinceTs` 和 `initialCutoffTs`，再检查同线程区间及旁支范围。具体只读步骤见 [Multica 操作说明](MULTICA-OPERATIONS.md)。

## 姓名、职责与验收

姓名映射只供阅读：最多解析 10 个用户、并发 4、预算 3 秒；失败保留 ID，不带邮箱或完整 profile。固定 task.instructions 只解释数据和原线程回复合同；背景不是新授权，marker 不是签名。长期人格与私有规则仍属于 Runtime。

自动化覆盖首次附近讨论、完整 130 条跨页 non-mention、长正文和全部附件引用、明确链接窗口、跨会话拒绝、缓存丢失、乱序、失败不推进、重试冻结、v4/v5 恢复及发送目的地兼容。离线测试和 Worker build 不等于真实 Slack → Multica → 回复 E2E。

## 展示与模型配置快照

标题使用消息摘要 + 稳定 scoped thread 短标识。正文引用仅展示前 4 KiB，完整请求在 envelope 中保留；安全转义引用、JSON 字符串和数据围栏，避免 Slack 文本改变结构或触发 Multica mention。原有 envelope 48 KiB 上限不变，展示正文整体上限 64 KiB，超出时先减少 JSON 排版空白，仍放不下则拒绝发送。

配置 GET 使用 `redirect: manual`，以兼容 workerd；3xx 按失败降级，不把凭据转发到重定向目标。真实 workerd 测试覆盖成功读取、重定向拒绝和身份不匹配。

`replyContext` 与 eventPayload/context 同级，来自经过 Agent/Workspace ID 校验的 Agent 配置快照。每次新消息准备时查询一次，最多 2 秒，失败标记 unavailable；随 envelope 冻结。模型为空/非法时为 null，serviceTier 只接受 priority/default，否则为 null。仅投影模型、档位、来源、身份和采集时间，不传 instructions、凭据或完整配置。

footer 使用每个新事件各自冻结的快照和 `messageTs`；只有配置模型已知且 Agent 匹配时显示。priority 追加 Fast，default/null 不追加 Fast，null 不证明默认档位关闭。查询失败或模型为空时省略模型字段，自动化标识仍由发送适配器追加。scripts/slack-reply.py 从原 Issue/Comment 回读 envelope，从私有配置读取固定显示名，将正文发送为 section blocks，并追加 context/mrkdwn footer；fallback text 同样包含正文和 footer。任务说明只指向运行时发送入口，格式与标识由代码负责，人格 Instructions 不要求模型生成标识。



发送适配器配置保存在私有 Runtime 中，字段为 displayName（包含 emoji 的完整显示文字）、agentId、workspaceId、projectId、teamId、serverUrl，不包含凭据。--issue-id/--comment-id 定位本次来源，--text-file 提供正文；适配器核对 Issue 的 workspace/project/assignee 与配置一致，路由取自原 envelope。认证沿用 multica CLI 和 `SLACK_USER_TOKEN`。--dry-run 只生成 payload，不发消息。每个 source Issue/comment 生成稳定的 delivery block ID，并在配置旁、已忽略的私有 `.slack-reply-state/` 原子记录 `attempting/accepted/sent`；文件和父目录均在 POST 前同步。Slack 返回的消息时间用于窄范围发送后回读；`sent` 重跑直接返回持久结果。结果不明时从本机尝试时间前五分钟开始核对，不从原请求扫描整条长线程；查不到 marker 时返回 `slack_delivery_unknown`，不得自动重复 POST，清理该状态前必须在原线程独立核对。本机同源锁不可获得时立即返回 `reply_delivery_busy`。

这里保证经适配器发送的回复格式；它不是对全部本地工具或直接 Slack API 调用的强制安全代理。普通 Relay 回复的调用入口通过私有 Skills 配置绑定。

可选的最终回复 Skill 在发送前只读采集当前 `MULTICA_TASK_ID` 的运行统计和 GitHub 操作候选。业务相关性由 Agent 判断，结构化结果交给 adapter 校验并渲染；未唯一配对、失败、截断或仅出现在文档示例中的记录不能作为 PR/分支成果。现有 source scope、at-most-once ledger 和发送后回读保持不变。


发送明确限流时持久化 `rate_limited/retryAt`，到期才允许重试；服务端错误和未知结果保留 `attempting`，回读失败不授权再次发送。详细错误分类与 CLI 返回字段见 [配置说明](CONFIGURATION.md)。

`--deliver-task-attachments` 交付当前 task 的 Multica 评论附件。正文首次发送及每个附件上传前均检查精确 Slack 根消息；附件逐个保存回执，未知上传不重复执行。附件发现、下载、权限与回执合同见 [附件交付](../multica-skills/multica-final-reply/references/attachment-delivery.md)。

## 构建诊断

`relay_context` 只记录事件摘要、读取/保留数量、缺失原因、阶段耗时和 envelope 字节数；不包含聊天正文、姓名、凭据或完整快照。与 `relay_dispatch` 的写入结果分别核对。

### Worker 审查回复页脚

启用 Worker 回复时，Worker 负责渲染审查结果，使用正文 section 和独立 context 行。签名回执 v2 分别携带决定、GitHub 回执、仓库和提交、耗时及宿主记录的工具汇总。兼容读取 v1 回执。暂缓审批也保留统计、任务及 PR 链接，但没有审批链接。模型取自匹配运行的 usage，缺失时省略，不从正文猜测。任务编号来自已核对的 Issue，最后一行保留自动回复标识和唯一 delivery marker。纯文本回退包含相同内容。

带可信布局标记的历史消息可使用 mrkdwn 或 plain_text 正文及页脚；两种格式均按原有规则移除页脚，避免把运行统计重复送进下一轮。发送账本、原请求复核与发送后回读规则不变。


### 多目标与原生 Agent 回执

签名请求保存冻结上下文中的全部 PR 候选和已批准能力。多个明确目标可以顺序处理；旧执行器逐项缓存结果，一项失败不抹去另一项的回执。新 Agent 直接使用同线程持久会话，任务内容不再预分配给只能输出文本的分支。

`relay-agent/1` 回执用请求 ticket 绑定 HMAC，包含正文、耗时、宿主工具汇总、可选的宿主模型名和各 PR 的已签名回执。Worker 验证外层及每个子回执后渲染，拒绝混入其他请求的结果。正文按 Slack 格式展示并转义原始提及标记；工具、模型、任务和仓库链接放在独立 context 行。实际审批链接来自 GitHub 读回的回执。发送目标复核和 at-most-once 账本不变。


### 中断审查的授权延续

本机在开始正式审查时保存原始签名请求、线程与目标以及稳定任务 ID。取证失败、工具中断或无完成回执时保持 pending。当前仍具备 review 权限的人在同一线程追问暂缓原因或进展时，默认继续这项未完成委托，读取最新代码和必要数据后完成审批；不要求重复输入 review。

明确只查状态或不投票时，本轮只读；明确撤回时保存取消状态。入口处理的所有者 cancel 命令也通过后续签名上下文传递，防止下一轮恢复已取消的委托。完成或取消后，普通追问不能产生新的审批。新的正式委托才创建新的任务 ID。只靠聊天中的旧指令、不同线程或被收回的 review 能力均不能恢复授权。

同一委托的重试复用 GitHub 写入标识。上次可能已经提交时先回读作者、提交、标识及审批状态，再结束任务，不重复投票。旧执行器的任务只能凭原始签名请求、宿主保存的正式 review 分派记录及对应未完成记录迁移；旧回复文字本身不构成授权。

PR 已合并或关闭时结束待办委托，不声称新增了审批；已有 review 链接仍注明实际作者。
