# Configuration

**English** | [简体中文](CONFIGURATION.md)

The Relay and the Multica Agent Runtime are separate trust domains. Relay variables belong to Vercel, EdgeOne, or Cloudflare Workers. Agent Runtime variables belong to the machine or Pod that executes the selected Multica Agent. Do not copy one environment wholesale into the other.

## Relay deployment environment

Configure these variables on the deployment platform. Credentials and tokens belong in its secret store. Slack IDs and service URLs are ordinary configuration values, although storing them as secrets is acceptable.

Slack IDs are comma-separated uppercase identifiers. `SLACK_TARGET_USER_IDS` and `SLACK_TARGET_SUBTEAM_IDS` may each be empty, but at least one must contain a target. Blocklists always take precedence over allowlists.

| Variable | Required | Value and source |
| --- | --- | --- |
| `SLACK_SIGNING_SECRET` | Yes | Slack App → Basic Information → App Credentials → Signing Secret. |
| `SLACK_TEAM_ID` | Yes | Slack Workspace ID, normally beginning with `T`. |
| `SLACK_TARGET_USER_IDS` | Conditional | User IDs whose explicit mention triggers the Relay. |
| `SLACK_TARGET_SUBTEAM_IDS` | Conditional | User Group IDs whose explicit mention triggers the Relay. |
| `SLACK_ALLOWED_CHANNEL_IDS` | Yes | Comma-separated Channel IDs. Use `all` only when every channel is intentionally allowed; missing or empty values prevent startup. |
| `SLACK_BLOCKED_CHANNEL_IDS` | No | Channel IDs that must never trigger, including when the allowlist is `all`. |
| `SLACK_ALLOWED_SENDER_IDS` | No | User IDs allowed to trigger. Missing or empty values mean `all`; an owner-only deployment should set the exact owner ID. |
| `SLACK_BLOCKED_SENDER_IDS` | No | User IDs that must never trigger. |
| `SLACK_BOT_TOKEN` | Conditional | Preferred identity for reaction add/read/remove; requires reactions:read and reactions:write. |
| `SLACK_USER_TOKEN` | Conditional | Used for reactions when Bot is not configured. |
| `SLACK_CANCEL_KEYWORDS` | No | Comma-separated whole-command words; defaults to cancel,取消. |
| `SLACK_CONTEXT_TOKEN` | Yes | User token with history scopes for the selected conversation types. The consumer uses it for bounded history/reply reads; `users:read` enables optional participant names through `users.info`. It may contain the same authorized token as the reaction credential, but configure it explicitly. |
| `SLACK_REACTION_NAME` | Yes | Slack emoji shortcode without colons, such as `eyes`. The reaction confirms Relay persistence, not Agent completion. |

The Slack App Request URL is `https://<deployment-host>/api/slack/events`. Subscribe only to message events required by the selected conversation types. Install the App in private channels when needed.

| Variable | Required | Value and source |
| --- | --- | --- |
| `MULTICA_API_BASE_URL` | Yes | HTTPS base URL of the target Multica Server, without a path suffix, query, or fragment. |
| `MULTICA_API_TOKEN` | Yes | Dedicated Multica PAT with access to the target Workspace, Project, and Agent. |
| `MULTICA_WORKSPACE_ID` | Yes | UUID of the Workspace that owns the Relay Project. |
| `MULTICA_PROJECT_ID` | Yes | UUID of the dedicated Project in which Relay Issues are created. |
| `MULTICA_AGENT_ID` | Yes | UUID of the Agent assigned to Relay Issues. The Agent must belong to the same Workspace and be bound to a Runtime. |

The Relay calls ordinary Issue and Comment APIs. It does not require a Multica Autopilot or webhook URL.

| Variable | Required | Value and source |
| --- | --- | --- |
| `KV_REST_API_URL` | Yes | HTTPS REST URL of a Redis database dedicated to this Relay. |
| `KV_REST_API_TOKEN` | Yes | REST token for the same Redis database. |
| `QSTASH_URL` | No | QStash API base URL. Defaults to `https://qstash.upstash.io`. |
| `QSTASH_TOKEN` | Yes | QStash publish token. |
| `QSTASH_CURRENT_SIGNING_KEY` | Yes | Current signing key used to verify QStash delivery. |
| `QSTASH_NEXT_SIGNING_KEY` | Yes | Next key used for verification during rotation. |
| `RELAY_CONSUMER_URL` | Yes | Exact public URL `https://<deployment-host>/api/queue/consume`. It must match the consumer URL used for QStash delivery and signature verification. |

The Redis database must be dedicated to this Relay. It stores thread mappings, locks, and write-confirmation state. QStash stores queued Slack payloads and retries failed consumers; configure its access and retention for the sensitivity of Slack content.

Redis caches the v3 persisted boundary, latest request, and initial nearby background for 24 hours. It contains minimal delivered message text and attachment references, never attachment content or private URLs. Advance only after confirmed Multica persistence; recover expired or out-of-order state from the scoped Issue/comments. Prepared envelopes are also frozen for 24 hours. New mentions read the complete same-thread interval and retain initial background; retries reuse frozen input. Historical bodies are not rewritten, and Multica retention is independent of Redis TTL.

Context limits and presentation versions are code-owned, not new environment variables: initial nearby discussion uses 12 roots from 30 minutes, with 5 replies from each of 2 active threads; follow-ups retain the complete current-thread interval since the previous persisted mention. Upgrades require no Redis migration or Agent configuration changes. Legacy indexes recover from persisted Multica records. Failed reads or corrupt state retry or reject; root-through-current fallback requires successful recovery with no earlier mention. Initial background is independent of the mention boundary and retains its original timestamp for late requests. See the [context contract](CONTEXT-ASSEMBLY-DESIGN_EN.md) for exact boundaries.

Before publishing to QStash, the Relay projects every Slack file object to `id`, `name`, `mime`, `size`, and `contentStatus`, plus a fingerprint derived from the complete safe fields. Downloaded content, private URLs, thumbnails, shares, and credentials never enter the queue payload. Transient or unknown-result failures return 503 for bounded QStash retry. An invalid queued payload, corrupt persisted state, scope violation, unresolvable mapping ambiguity, or deterministic size overflow returns `rejected` with `retryable: false`. Definite access failures, missing roots, and exhausted pagination in the required interval also reject dispatch; optional background gaps use context markers.

## Cancellation and reaction identity

A target mention followed by the complete `cancel` or `取消` keyword in the original task thread stops the associated runs. A non-empty `SLACK_CANCEL_KEYWORDS` list replaces those defaults. Only senders listed in `SLACK_TARGET_USER_IDS` may cancel; user-group mentions do not grant that permission. Admission is checked before enqueueing and again during consumption. Cancellation creates no Agent task and reads no context tree.

Redis persists cancellation intent, the fixed run-ID set, and remaining reaction cleanup with the thread mapping. Retries keep the original targets. Ordinary messages received during cancellation are ignored; later mentions can continue the same Issue. Terminal-state readback precedes cleanup of only the selected identity's own reactions. Multica's cancelled state and interruption request do not independently confirm daemon termination or undo completed external writes.

Reaction add/read/remove consistently select a non-empty `SLACK_BOT_TOKEN`, otherwise `SLACK_USER_TOKEN`. The chosen identity needs `reactions:read`, `reactions:write`, and channel access; API failure never switches identity. Context uses `SLACK_CONTEXT_TOKEN`, while the Agent reply uses its separate Runtime user token.

The old `SLACK_REACTION_TOKEN` and `SLACK_REACTION_READ_TOKEN` are no longer read. Configure the new variable for the existing identity before switching code; retain old variables for rollback if needed. Switching from User to Bot leaves historical User reactions intact. Drain or isolate queued `operation=cancel` events before rolling back, because an old consumer would dispatch them as ordinary tasks. Exhausted retries remain in QStash's DLQ; do not delete Redis state to force a resend.

## Multica Agent Runtime

The Relay does not read the variables below. Configure them only on the Runtime or Agent when Agent Instructions or the local Slack Skill require them.

| Variable | Purpose |
| --- | --- |
| `RELAY_OWNER_SLACK_USER_ID` | Owner ID used for authorization decisions. Derive it from the verified Slack event author, never from a display name. |
| `RELAY_SKILL_ROOT` | Absolute path to the approved local Skill directory. It is machine-specific and does not belong in this repository. |
| `SLACK_SKILL_ALLOWED_CHANNELS` | Channel allowlist enforced by the Slack Skill for reads and replies. Keep it aligned with Relay policy. |
| `SLACK_USER_TOKEN` | Owner user token used by the Agent Slack Skill and local reply adapter. |
| `SLACK_TOKEN` | Compatibility variable for Skills that require it. Point it explicitly at the intended owner identity. |
| `SLACK_BOT_TOKEN` | Leave empty when replies must use the owner identity; otherwise the Slack Skill may prefer the Bot token. |

The Agent does not need a separate `RELAY_ALLOWED_CHANNEL_ID`. The Relay validates Team, channel, sender, and mention policy before enqueueing and checks them again during consumption. `SLACK_SKILL_ALLOWED_CHANNELS` applies only to calls made through the Slack Skill. The local reply adapter is a separate path: it reads the destination from the source Issue/comment in the configured Multica Project and sends directly with `SLACK_USER_TOKEN`.

## Configuration ownership and activation

| Surface | Source of truth | How changes become active |
| --- | --- | --- |
| Relay admission, queueing, and persistence | Deployment variables listed above | Redeploy the tested commit with the updated environment, then read back health and policy behavior. |
| Reply display name and fixed routing identity | Private Runtime JSON passed to `scripts/slack-reply.py` | The adapter reads it on every invocation; a new `displayName` applies to the next send. |
| Agent personality and operating instructions | Multica Agent Instructions | A private local prompt may hold the candidate, but it must be synchronized explicitly with `multica agent update --instructions` and read back from the same Agent ID. Editing the file alone has no Runtime effect. |
| Slack and other local tools | Private Runtime Skills and environment | Update the Runtime binding/configuration and read back the bound Skill and environment. This repository does not load those files. |

## Deployment verification

1. Run `pnpm install --frozen-lockfile`, `pnpm test`, and `pnpm lint`.
2. Deploy the same immutable commit that passed validation.
3. Confirm `/api/health` on the production domain.
4. Confirm Slack accepts the Request URL.
5. Send one authorized Slack event and verify QStash acceptance, Multica Issue creation, reaction author, Agent execution, and the final Slack thread reply as separate stages.
6. Verify that a blocked channel and blocked sender produce no QStash or Multica side effect.

An HTTP 200, acknowledgement reaction, created Issue, or ready deployment proves only its corresponding stage. None proves the complete Agent reply path.

## Optional model footer

During preparation, the consumer uses the existing Multica PAT to read the selected Agent once, with a two-second deadline and identity validation. No new credential is required. An unavailable, inherited, or empty model becomes `null`, omitting only the model portion of the footer. The reply adapter always appends the configured automation identity. The footer is a configuration snapshot, not evidence of the model or billing tier used by the run, and it never changes Agent model settings. See [Slack Context Assembly](CONTEXT-ASSEMBLY-DESIGN_EN.md) for the frozen-snapshot and rendering contract.

Snapshot GET requests use `redirect: manual`; workerd does not support `redirect: error`. A 3xx response remains unavailable and is never followed. The private adapter styleGuide and Runtime SKILLS.md index should point to the same readable file. Verify the path and relative Skill references after updates, not merely a successful JSON save.

## Local reply adapter

Attachment discovery reads the complete comment tree, including replies and resolved threads, then filters by the current source_task_id. Do not use --roots-only: follow-up attachments may belong to replies. After token changes, verify the actual login-shell environment; stale exports in .zshenv can override Runtime-injected values, so updating the Skill dotenv file alone does not establish effectiveness.

Run `python3 scripts/slack-reply.py --help` for the CLI. Bind the adapter in the private Runtime Skill and provide private JSON containing `displayName`, `agentId`, `workspaceId`, `projectId`, `teamId`, and `serverUrl`. The JSON contains no token. The adapter reuses the Runtime's Multica CLI authentication and `SLACK_USER_TOKEN`. The Agent supplies body text through `--text-file`; route and model metadata are read back from the source Issue/comment. The adapter sends body sections plus an attribution context block, independent of model text or persona instructions. Before a first POST, it confirms that the exact Slack root message still exists so an invalid thread cannot be downgraded to a channel or DM root.

After binding `multica-skills/multica-final-reply`, the Agent uses its read-only helper to create current-run context before sending and selects only GitHub PRs supported by the business task evidence. The adapter accepts optional `--run-context-file` and `--github-context-file` inputs, validates Issue ownership, statistic provenance, and repository/branch/PR URL consistency, then appends run statistics and GitHub association rows. It omits fields that cannot be verified. The Agent does not control routing, delivery markers, or retries.

The adapter derives a stable delivery block ID from the source Issue/comment and stores a private delivery ledger in `.slack-reply-state/` beside the JSON configuration. The directory is ignored by Git. Its directory and atomic JSON files use owner-only permissions; file and parent-directory state are both synced before the POST. Body state advances through `attempting`, `accepted`, and `sent`. Slack's returned message timestamp narrows post-send verification to the new reply, and only successful thread readback advances to `sent`. Later Runtime retries return the persisted result without scanning old history.

When the request explicitly requires files, the Agent first stores the final artifacts on a comment created by the current task, then passes `--deliver-task-attachments`. The adapter selects only attachments whose `source_task_id` equals the current `MULTICA_TASK_ID`, downloads them, validates metadata size and SHA-256, and calls the Slack Skill configured by `slackCliPath` through `files_upload --as user`. Each attachment ID has its own `attempting`/`sent` receipt, so reordering or adding files does not upload successful items again. Unknown results retain the upload stage and any returned file ID and never authorize re-upload. The exact root is checked before each upload, including retries after the body was sent; deletion after that check remains a race. A definite Slack Skill rejection marked `retry_safe: true` clears the intent so the same source can be retried after permissions or parameters are corrected. Limits are 20 files, 25 MiB per file, and 100 MiB total. Upload requires an approved User token with `files:write`; a pending request is a permission blocker, not a reason to switch identity.

If the POST result is unknown, a retry searches from five minutes before the local attempt time to allow for clock skew. A found delivery block advances to `sent`. An empty read returns `slack_delivery_unknown` and never repeats the POST automatically, because absence does not prove Slack failed to commit. Independently verify the original thread before clearing that state. Concurrent execution for the same source returns `reply_delivery_busy` immediately rather than waiting on a blocking lock. In addition to `chat:write`, `SLACK_USER_TOKEN` needs the history scope required by `conversations.replies`.

Only known, definite authentication, permission, validation, or destination rejections clear the send intent. Internal/fatal server errors, unknown results, non-429 HTTP errors, and malformed responses retain `attempting`; later invocations reconcile by marker before any resend.

HTTP 429 or JSON `rate_limited`/`ratelimited` puts the send ledger in `rate_limited` with a persisted `retryAt` derived from `Retry-After`. The CLI reports `slack_rate_limited`, `retryable: true`, and `retry_after_seconds`. The caller waits before running the same source command again; no Slack call occurs during the cooldown, and a send becomes eligible after it expires. Missing or invalid Retry-After uses a 60-second cooldown. The CLI does not sleep or loop. A rate-limited readback preserves the original `accepted`/`attempting` state and only delays reconciliation.

Footer appearance is read from the private configuration on every send. `displayName` is the complete attribution label and may contain Unicode emoji or Slack shortcodes. A change applies to the next send without a Runtime restart or Relay redeploy; existing messages remain unchanged. Preserve every other routing and identity field.

```json
{
  "displayName": "🤖 Example"
}
```

The example shows only the appearance field. Preserve the remaining required fields in the real configuration.

## Final replies and private style configuration

Publish `multica-skills/multica-final-reply` and bind it to the target Agent. Set `FINAL_REPLY_CONFIG` to an owner-only private JSON file. Add the following optional fields alongside the existing adapter identity fields. Store personal prompts, favorite emojis, and workspace catalogs outside the repository or under ignored `.private/`; do not publish them with the public Skill.

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

Retain `displayName`, `agentId`, `workspaceId`, `projectId`, `teamId`, and `serverUrl`. Icons may use verified workspace shortcodes. A generic reply style applies when no private style is configured; catalogs are searched on demand rather than loaded for every reply.

Agent variables `FINAL_REPLY_APP_URL` and `FINAL_REPLY_WORKSPACE_SLUG` provide the Multica HTTPS web origin and workspace slug. Explicit `--app-url`, `--workspace-slug`, and `--issue-identifier` arguments take precedence; missing values use read-only discovery, and incomplete links are omitted. Isolated tasks should receive these values explicitly instead of relying on personal host CLI configuration.

`--run-context-file` carries the current Issue identifier/link alongside statistics. After checking Issue and current-run identity, the adapter appends the task link to the statistics line. `--github-context-file` uses `version: 1`, a `pullRequests` array, and an optional `branches` array. Branch entries have `repository` (owner/repo) and `branch`; PR entries also carry `number` and the corresponding full GitHub `url`, with at most five entries overall. Each entry has its own context block and equivalent fallback text. Valid current-run statistics replace the older envelope model display; attribution and the delivery marker remain.

Read back published Skill files and Agent bindings. Existing tasks may retain their startup copy; a new task proves loading. Configuration persistence, read-only dry-run, and real Slack delivery are separate validation stages.

## Cloudflare Workers

The Worker entry is `src/cloudflare.ts`, configured by `wrangler.jsonc`, with the same three `/api/` routes as the other platforms. It forwards the original Request and `env`, preserving Slack/QStash signature bytes, and does not acknowledge queue consumption early through `waitUntil`. `/api/health` checks endpoint availability, not external configuration.

Wrangler development and tests support Node.js 22.12+; Node.js 24 LTS is recommended and selected by `.nvmrc` and `.node-version`. Set `NODE_VERSION=24` in Cloudflare Workers Builds. Workers executes on workerd: this Node.js version controls build tools, not the Worker runtime. The configured compatibility date, `2026-09-08`, enables Node.js compatibility by default. Shared code explicitly imports `node:buffer` and reuses `node:crypto`. See [Cloudflare Node.js compatibility](https://developers.cloudflare.com/workers/runtime-apis/nodejs/).

1. Install dependencies, run `pnpm exec wrangler login` for the intended Cloudflare account, and set your Worker name in `wrangler.jsonc`.
2. Configure every required Relay variable listed above in the Worker's Variables and Secrets. Maintain ordinary IDs/URLs in Wrangler `vars` so later CLI deployments do not overwrite dashboard-only plain variables; tokens, PATs, and signing keys must use Secrets. Alternatively, enter credentials interactively with commands such as `pnpm exec wrangler secret put SLACK_SIGNING_SECRET`; never put values in command arguments or Git.
3. Set `RELAY_CONSUMER_URL` to the actual public endpoint, such as `https://<worker>.<subdomain>.workers.dev/api/queue/consume`. QStash must reach it directly without an interactive login, Cloudflare Access, or CAPTCHA.
4. For local development, put test values in the ignored `.dev.vars` file and run `pnpm dev:cf`. Local mode still calls configured external services, so use isolated test resources. Credential-free automated tests use fake bindings without live service calls.
5. Run `pnpm test`, `pnpm lint`, and `pnpm build:cf`. The last command builds and validates the deployment bundle without publishing.
6. After configuration, run `pnpm deploy:cf`; existing deployments use their verified production configuration. Pass `--keep-vars` when ordinary variables are managed through the dashboard; secrets remain in Cloudflare Secret Store. Configure the public Slack Events URL and perform the staged verification above. Domain changes require updating the consumer URL.

Private deployment records track the Git commit, Worker version, and actual traffic percentage separately. Wrangler `--tag` and `--message` can identify the build source. After publishing, use `deployments list --json` and `versions view <version-id> --json` to confirm the target version, runtime, bindings, and original domain. Health returns 200; Slack/QStash POSTs without valid signatures return 401 without starting a task. Real mention context and final replies still require independent acceptance.

Cloudflare retains QStash and Upstash Redis; it does not replace them with Cloudflare Queues, KV, or D1. The Python reply adapter and durable ledger stay on the private Multica Runtime. Account CPU and subrequest limits must accommodate context reads and recovery paths. Local tests and dry-run builds do not prove a live end-to-end deployment.

### GitHub pull request reviews from the local agent

Set `GITHUB_REVIEW_CHANNEL` and `GITHUB_REVIEW_SIGNING_KEY` to enable reviews in one admitted Slack channel. The Worker signs the triggering message, sender, thread, and PR. The local runtime verifies the signature, reads the diff and previous reviews using its configured GitHub account, and submits the decision against the exact commit it reviewed. It reads back the review ID, author, commit, and state before returning the approval link. Keep the GitHub credential on the host. The local receipt database records pending writes so retries can reconcile the same review. Unsupported or incomplete diffs, stale commits, draft PRs, self-reviews, and denied access cannot produce an approval. Set `GITHUB_REVIEW_WORKSPACE_SLUG` to include a Multica task link in the Slack footer.

Set `SLACK_SELF_ASSIGN_SCOPE=dm` to accept explicit self-mentions by the single owner in other direct messages. Set it to `all` to include their channels and group direct messages. Other senders still need their existing channel-specific grant. Blocklists take precedence. Enable and install the corresponding user message subscriptions and history scopes for each conversation type.

### Legacy executor: read-only evidence for pull request reviews

Verified PR reviews receive an operation catalog scoped to the repository owner. The agent can request evidence from configured stdio MCPs before choosing a verdict. Ordinary Slack requests keep their existing tool access. A required check without a verified result prevents submission. The host validates parameters against the PR and projects only approved result fields.

Extend the private host policy in `.private/runtime/review/evidence.json` with the configured server and tool name, repository owners, identifier or issue-key parameters, a fixed argument template and result projections. Optional choices and qualified-reference checks narrow object scope further. Verify the new operation, a rejection case and its output fields before using it for a review. Never add unrestricted queries, customer exports or mutation methods to the review catalog.

Complete schema operations may set `result_limit: 2000` and `require_complete: true`; overflow fails instead of presenting truncated metadata as a complete schema. Other operations retain the default 100-record limit. `display_name` groups host-recorded calls in the footer. Submitted and incomplete reviews both use signed result receipts, with tool counts outside the body. A review link appears only after GitHub readback confirms the submission; the GitHub review includes the decision and supporting findings.

`GITHUB_REVIEW_SELF_CHANNEL` optionally enables real PR reviews in the owner’s self-DM for private validation. It must be a D-prefixed conversation, with both sender and mention matching the single configured owner. This does not authorize other DMs or senders and leaves the existing channel policy unchanged.

Object checks use the PR diff, title, description and the contents of changed files actually fetched at the reviewed commit. Thread text and earlier review summaries supply context only. When the thread identifies a single PR, follow-up questions and rechecks can use the same scoped read operations. The signed follow-up request carries read-only authority; even a model recommendation to approve or request changes returns an explanation without posting a GitHub review. The most recent summary is scoped to the same thread and PR.

OWNER_LOCAL_TASKS enables local tasks only for the single configured owner mentioning themselves. Valid PR requests retain priority. The Worker signs the request identity, original message and bounded thread context. The local adapter checks the signature, owner, team and expiry. Register the workspace and allowed Git remote in the private runtime/owner-local/config.json. Commands run within that workspace without network or Git metadata writes. A separate update operation fast-forwards a clean main checkout. A per-message execution ledger prevents automatic replay when the result is uncertain.

In a PR thread the owner can explicitly select local execution with the 本机任务： prefix. An explicit standalone owner request to restart Claude can gracefully quit and reopen the desktop app. It never force-kills processes or restarts terminal sessions.

In an authorized channel or owner self-DM, each mention is interpreted locally from its text and same-thread context. The Worker signs the identity, allowed capabilities and actual PR candidates; the classifier selects review, inspection, local work, answer, clarification or unsupported. Target checks, explicit read-only constraints and owner-only execution are enforced separately. The decision is frozen per request across retries. Signed thread context is review material, not authority, and GitHub readback confirms a submitted vote. The signed request preserves the frozen, bounded thread history without a second root/recent-message truncation.

### Legacy executor: independent read-only business investigations

BUSINESS_CHANNEL, BUSINESS_SENDERS and BUSINESS_TEST_DM grant a separate signed capability to designated colleagues mentioning the owner in the designated channel, or to the owner in the dedicated validation self-DM. The local adapter checks team, channel, sender and mention against private runtime/business/policy.json before reading. It never delegates business requests to shell or PR approval. Conclusions are returned to the verified original thread.

The business catalog uses fixed read-only MCP methods for Jira lookup/search, Redshift schema and bounded aggregates, ClickHouse metadata and Airflow status. Configure permitted Jira projects in jira_projects. Warehouse output is metadata or aggregates, with at most 31 days per partition query. Only identifiers from the request, thread or verified evidence can be queried. Conclusions require successful current evidence; unsupported actions, missing inputs and insufficient evidence cannot be reported as completed checks.

Business investigations support agent-authored Redshift aggregate SELECTs with nonrecursive CTEs, explicit equality joins and conditional aggregates. Install scripts/requirements-business-sql.txt in the business MCP interpreter. The relay validates the Redshift AST and physical sources, binds every base scan to the same inclusive pt interval of at most 31 days and executes only regenerated SQL. Only numeric aggregates and dates may leave the adapter, with 31 rows plus a truncation sentinel. Tables without pt, cross-database reads, arbitrary functions and detail projections are unsupported. The upstream MCP additionally uses read-only Redshift transactions. The client wait limit does not guarantee server-side cancellation.


### Grants per colleague and channel

Multi-channel access uses comma-separated `CHANNEL_ID:USER_ID` pairs. `SLACK_ALLOWED_CHANNEL_IDS` admits channels and `SLACK_CHANNEL_SENDER_GRANTS` admits colleagues. `GITHUB_REVIEW_CHANNEL_SENDER_GRANTS` grants formal PR review; `BUSINESS_CHANNEL_SENDER_GRANTS` grants read-only investigations. Admission and capabilities are checked separately. Never form a cross-product of sender and channel lists, and never grant colleagues local commands.

Existing `GITHUB_REVIEW_CHANNEL` and `BUSINESS_CHANNEL` / `BUSINESS_SENDERS` settings remain supported. For the legacy business fallback, mirror the pairs in the private `runtime/business/policy.json` array `channel_sender_grants`. Ingress, consumption and reply delivery recheck admission; revoking a sender/channel grant prevents an unsent reply.

One-to-one colleague DMs may also be explicitly granted. Verify the colleague's DM with the owner through Slack, add the exact `D…:U…` pair to all three grant lists above, and add the DM ID to `SLACK_ALLOWED_CHANNEL_IDS`. DM reads use `SLACK_CONTEXT_TOKEN` and require installed `im:history` and the `message.im` subscription. Each pair admits only that colleague in that specific DM, without opening other DMs or group DMs. `GITHUB_REVIEW_SELF_CHANNEL` remains the owner self-DM setting.

Before deployment, verify owner membership, installed user history scopes, event subscriptions and a real channel read using the configured identity. `SLACK_CHANNEL_READ_TOKEN` may use the authorized owner user token or a bot already present in the channel. Test every colleague in granted channels, their own DM, other DMs and ungranted channels. Verify original-thread replies and revocation before delivery. Channel test messages require separate authorization.

### Native tool-using agent

Set `{"enabled":true}` in private `.private/runtime/agent/config.json` to send signed `intent1` requests to `scripts/agent_runtime.py`. An optional `channels` list restricts rollout to validation DMs; an empty or absent list enables the runtime for already admitted requests without widening ingress permissions. Disabling it restores the legacy executor. Legacy fixed catalogs and route categories do not constrain the native agent.

Install `scripts/requirements-agent.txt` in the interpreter selected by private `review/evidence.json` (`python`). It requires MCP 2.x, an HTTP server and SQL parsing. The host reads current Codex model settings and enabled MCP connections, then creates a loopback-only, authenticated, per-request MCP gateway. Credentials stay on the host. Exact Relay tools receive `approval_mode="approve"` for the owner's standing delegation; the host still validates the signed request, identity, operations and each write. Other tools do not inherit approval.

Codex sessions persist per Slack thread and use the native tool loop. The agent can process multiple PRs, discover read tools, inspect schemas and execute SELECTs, CTEs and joins. Host-side SQL validation rejects mutations, locks, unknown user-defined functions and multiple statements, and caps results at 200 rows. Redshift requires explicit cluster and database selection. Other tool results above 80 KB use host-held pages through `read_result`, up to 8 MiB; larger responses require a narrower upstream query. Fixed round counts are not treated as completion.

GitHub submissions independently verify current review intent, target, exact revision, duplicate attempts and actual receipts. Follow-ups on a persisted unfinished review continue that delegation; explicit status-only questions and completed tasks do not authorize another vote. Colleagues have no local command tool. Owner commands retain the configured workspace isolation and a per-request command ledger; unresolved attempts are not replayed.

Review material is paged at pinned base/head revisions. If GitHub cannot return a complete combined diff, read the changed blobs at those revisions. The artifact reader supports text, nested ZIP/Tableau/XLSX contents and PNG images delivered as MCP image content. Approve requires complete required evidence coverage; a decisive defect can support Request changes. Unknown formats report a missing reader, not a repository permission failure. A changed revision invalidates old evidence coverage.

AWS identity and S3 object metadata use existing host credentials through bounded read operations. Configured profiles alone do not prove access. This does not execute `aws s3 sync --dryrun`; missing credentials inside an isolated owner command do not establish missing host access.

Model execution has a 30-minute watchdog and each thread runs serially. The outer dispatcher allows additional preparation and cleanup time. It requests graceful shutdown before killing the task process tree. A completed model turn and final response trigger cleanup even if CLI background workers hang. Transient read errors retry once; writes never retry automatically. The Worker waits up to roughly 60 minutes. Private `runs/native-agent` stores session, tool audit and signed results for continuation and replay. Failures are reported as failures. The host cannot execute while asleep, and the existing one-hour signed-request expiry still applies; recovery beyond that window is not guaranteed.

The private native session disables automatic app/plugin tool inheritance. Configured read MCPs remain accessible through the request-bound host gateway. GitHub reads and review submissions use the host connection; remote connector IP restrictions do not establish that the host lacks repository access.

When owner local self-assignment and GitHub review are enabled, admitted owner self-mentions retain formal review capability in every permitted self-assignment conversation. Colleagues remain restricted to their configured channels.

Old review receipts reconcile independently: one failure cannot stop other targets or business queries. Failed authorization preparation retains read tools and offers a retry within the same request. Private session state carries the execution phase and interruption reason into follow-ups; startup diagnostics remain private. GitHub drafts carry a stable task marker from creation, allowing recovery after losing their local IDs. See [reliability testing](RELIABILITY-TESTING.md) for coverage and operational limits.
