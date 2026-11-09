# Slack Context Assembly

**English** | [简体中文](CONTEXT-ASSEMBLY-DESIGN.md)

The Relay puts the current request first and renders each message on one JSON line, retaining original text and thread relationships. Selection uses explicit time, thread, and link rules. No model summarizes the input, and ordinary thread messages are not filtered by length, emoji, or guessed relevance.

## Deterministic input scope

B is the current mention. A is the latest earlier mention confirmed persisted to Multica in the same workspace/project/agent/thread.

| Case | Included content |
| --- | --- |
| First mention | Every current-thread message from root through B; a new root contains B itself |
| Initial nearby discussion | Latest 12 other roots in the same conversation within `[B - 30 minutes, B)`; expand the 2 most recently active threads, retaining 5 trailing replies each |
| Follow-up | Root, A, and every message in `(A, B]`, including non-mentions, other authors, bots, emoji, and attachment references |
| Initial background | Preserve the nearby discussion first delivered, identified by `initialCutoffTs`; do not refresh unrelated main-timeline messages on follow-up |
| Explicit links | Up to 3 distinct same-conversation Slack permalinks, discovered in current request, A, root, and interval messages; include root, target, and 2 preceding/following replies; a linked root includes its first 2 replies |
| No earlier A | A normal first request, or successful required recovery with no persisted mention before B: read root through B |

The entire current thread remains mandatory even when its root is old. Nearby roots are selected newest first and displayed chronologically; activity uses `latest_reply`. Sparse conversations do not widen the time window. Links are not followed recursively, other-conversation links remain text, and targets already in the mandatory interval are not fetched again. Unresolved links and optional read failures never imply that the linked content was read.

Required replies use `oldest=A`, `latest=B`, `inclusive=true` and cursor pagination. Fetch the exact root separately if Slack excludes it from this interval. See the official [replies contract](https://docs.slack.dev/reference/methods/conversations.replies/) and [single-message history contract](https://docs.slack.dev/reference/methods/conversations.history/).

## Compact presentation and wire contract

Keep the original marker on the first line, followed by the request quote, one source line, scope explanation, and JSON data. Paired `relay-payload:v1` markers and dynamically sized fences remain unchanged.

`schemaVersion: 5` renders each ordinary message on one line. Presentation omits default `origin=unknown`, empty `files`, and the empty text of a currentRequest node. Actual bot origins, attachments, and missing-content flags remain. Timestamps, author IDs, participant names, original text, thread relationships, and routing coordinates are preserved.

```json
{
  "schemaVersion": 5,
  "eventPayload": {"teamId":"T1","channelId":"C1","threadTs":"1000.000001","messageTs":"1100.000001","text":"current request"},
  "context": {
    "anchorTs":"1000.000001","sinceTs":"1050.000001","cutoffTs":"1100.000001",
    "timeline": {"status":"complete","messages":[
      {"ts":"1000.000001","authorId":"U1","text":"root","replies":{"status":"complete","messages":[
        {"ts":"1050.000001","authorId":"U1","text":"previous mention"},
        {"ts":"1060.000001","authorId":"U2","text":"non-mention discussion"},
        {"ts":"1100.000001","authorId":"U1","currentRequest":true}
      ]}}
    ]}
  }
}
```

This example omits fixed task instructions and some routing fields. Complete request text and attachments remain in eventPayload; currentRequest references them. The readable quote is limited to 4 KiB, while the data preserves the complete request. The TS reader restores omitted defaults. The Python reply adapter continues reading the same eventPayload/replyContext. Legacy bare JSON, v4 fenced payloads, and old markers remain readable. Duplicate or corrupt payload blocks are rejected. Historical Issues/comments are not rewritten.

`complete` describes only that collection's read scope. Coverage timestamps describe retained content, not unseen history. Optional branches independently carry truncated/unavailable markers; `initialStatus/initialReason` can carry initial-background gaps forward. Attachment references remain `not_loaded` and never prove content was inspected.

## Agent footer cleanup

A historical message is cleaned only when it has bot/app origin, a final context block marked `relay-delivery-<64 hex digits>`, and the adapter section → context layout. Text is reconstructed from body sections and marked `footerOmitted: true`. Multiple sections must match the adapter 3000-codepoint chunk layout before concatenation. Author, timestamp, bot origin, and attachment references remain unchanged. Words such as “Sent by”, model names, and emoji in the body are never removed by keyword.

Unmarked messages, human messages, image/attachment blocks, fields/accessories, and unsupported layouts retain their original text. Old snapshots without blocks are not heuristically cleaned. This affects new reads; historical Issues/comments and frozen snapshots are not rewritten.

## Budgets and failures

| Item | Policy |
| --- | --- |
| Required current thread | No last-20/100 limit, text clipping, or attachment-reference dropping |
| Optional message | 4 KiB text and 5 attachment references, with explicit truncation markers |
| Pagination | 5 history pages, 10 required reply pages, 3 optional reply pages; 20 conversation requests overall |
| Time | 20 seconds for message reads; current thread first, then nearby discussion and links; at most 2 side threads concurrently |
| Input response | 2 MiB per Slack response |
| Output | 48 KiB envelope and 64 KiB final Issue/comment |

Byte pressure removes optional side roots first, then optional referenced replies older than A. If root, A, `(A,B]`, and B still cannot fit, return `context_request_too_large` without creating an incomplete task or advancing A. Unfinished mandatory pagination returns `context_required_page_limit`; definite access failures or a missing root return `context_required_unavailable`. These are deterministic rejections. Temporary 429, 5xx, and timeouts use bounded queue retries. Optional failures mark the affected background. Cross-channel and cross-thread responses are always rejected.

## Persistence, cache, and recovery

- The event envelope and pending context state are frozen for 24 hours; retries do not rebuild them.
- The v3 `:sent-context-index` caches the latest persisted request/boundary and initial background for 24 hours. Message fingerprints no longer determine interval omissions. It contains minimal delivered context, never attachment content or private URLs.
- Advance A only after a successful Issue/comment write or stable-marker confirmation. Successful reads, preparation, and startup reactions are not delivery receipts.
- Missing caches, old indexes, and out-of-order events recover the latest persisted mention before B from the scoped Issue and Relay comments. Recover the overall latest persisted cursor as well, so a late event cannot move it backward.
- Old first-Issue trees are reduced to the current 30-minute / 12-root / 2-thread / 5-reply policy using only their saved evidence. They do not perpetuate large old snapshots or rewrite historical bodies.
- A normal first request, or successful required recovery with no persisted mention before B, retains root through B. Recovery API failures, corrupt state, and scope mismatches retry or reject according to their error class. Failed reads do not prove that A is absent and never bypass write reconciliation.
- Missing thread mappings recover through the unique thread marker. Multica history retention remains independent of Redis TTL.

## Upgrade and compatibility

Initial background is independent of A. A late B earlier than the first persisted request retains the original initialCutoffTs and background roots/replies with `ts <= B`. Filtering changes only the current copy, preserving the original background for later requests. Optional background remains subject to the byte budget.

If A is missing from the current Slack result, restore its text, author, and attachment references from persisted eventPayload and mark it `restoredFrom: persisted_request`. Preserve any legacy attachment-truncation flag. This is not text fetched from Slack in this read. Missing saved text rejects delivery; a missing thread root remains a separate rejection. Time filtering uses Slack message ts.

`schemaVersion: 5` versions task bodies, while v3 `:sent-context-index` versions the internal Relay cache. Deploying the new Relay enables the policy without adding environment variables, changing Agent Instructions, or rebinding the final-reply Skill.

New mentions use the new rules. An event reuses its frozen envelope while the 24-hour cache is valid, including legacy formats. After expiry, reconcile persisted delivery first; fetch context again only if preparation remains necessary. Recovered first-message backgrounds are selected under the current scope for new comments; original Issues/comments are not rewritten. Do not clear event snapshots, thread mappings, or delivery ledgers to force a format change: they own idempotency and recovery.

Read back `schemaVersion`, `eventPayload.messageTs`, `context.sinceTs`, and `initialCutoffTs` separately, then check the complete thread interval and bounded side context. See [Multica Operations](MULTICA-OPERATIONS_EN.md) for read-only verification.

## Names, ownership, and acceptance

Display names are optional metadata: up to 10 users, concurrency 4, and a 3-second deadline. Failed lookups retain IDs; email and complete profiles are excluded. Fixed task instructions explain data and original-thread replies. Background is not new authorization, markers are not signatures, and private long-lived instructions remain Runtime-owned.

Automated coverage includes first nearby threads, all 130 paginated non-mentions, long text and complete attachment references, explicit link windows, cross-conversation rejection, cache loss, out-of-order delivery, failed writes, frozen retries, v4/v5 recovery, and reply destination compatibility. Offline checks and a Worker build do not establish live Slack → Multica → reply E2E.

## Presentation and Agent configuration snapshot

The title uses a message summary plus a stable scoped-thread suffix. The readable quote shows at most 4 KiB; the complete request remains in the envelope. Quotes, JSON strings, and code fences are escaped so Slack content cannot alter structure or create Multica mentions. The serialized envelope limit remains 48 KiB and the complete presentation limit is 64 KiB. Formatting whitespace is reduced first; if the body still cannot fit, the request is rejected.

The configuration GET uses `redirect: manual` for workerd compatibility. A 3xx response degrades to unavailable without forwarding credentials to a redirect target. Real-workerd tests cover successful reads, redirect rejection, and identity mismatch.

`replyContext` sits beside `eventPayload` and `context`. It comes from an Agent configuration snapshot whose Agent and Workspace IDs were validated. Each new message performs one query with a two-second limit; failure produces `unavailable`, and the result is frozen with the envelope. An empty or invalid model becomes `null`. `serviceTier` accepts only `priority` or `default`; every other value becomes `null`. Only model, tier, source, identity, and capture time are projected. Instructions, credentials, and the complete configuration are excluded.

The footer uses the snapshot and `messageTs` frozen for that event. It displays the model only when the configured model is known and the Agent identity matches. `priority` adds Fast; `default` and `null` do not. A null tier does not prove that a default tier was disabled. When lookup fails or the model is empty, the model label is omitted while the adapter still adds the automation identity. `scripts/slack-reply.py` reads the envelope from the original Issue/comment and the display name from private configuration, sends the body as section blocks, and appends a context/mrkdwn footer. Fallback text also contains the body and footer. Task instructions point only to the Runtime send entry point; formatting and attribution are code-owned rather than model-generated.


The reply-adapter configuration lives in the private Runtime. It contains `displayName`, `agentId`, `workspaceId`, `projectId`, `teamId`, and `serverUrl`, with no credential. `--issue-id` and `--comment-id` locate the current source, and `--text-file` supplies the body. The adapter validates the Issue workspace, project, and assignee against configuration, then reads the route from the source envelope. Authentication reuses the Multica CLI and `SLACK_USER_TOKEN`. `--dry-run` renders a payload without sending it.

Each source Issue/comment gets a stable delivery block ID and an atomic `attempting/accepted/sent` record under the ignored private `.slack-reply-state/` beside the adapter configuration. The file and parent directory are synced before the POST. Slack's returned timestamp narrows post-send readback; a retry of `sent` returns the persisted result. An unknown result is checked from five minutes before the local attempt time rather than by scanning the source thread from its beginning. If no marker is found, the adapter returns `slack_delivery_unknown` and never repeats the POST automatically. Independently verify the original thread before clearing that state. Failure to acquire the same-source local lock returns `reply_delivery_busy` immediately.

This contract governs messages sent through the adapter. It is not a mandatory security proxy for every local tool or direct Slack API call. Private Runtime Skills bind the normal Relay reply entry point.

The optional final-reply Skill reads statistics and GitHub-operation candidates only from the current `MULTICA_TASK_ID` before sending. The Agent decides business relevance, while the adapter validates and renders the structured result. Unpaired, failed, truncated, or documentation-only evidence cannot become a PR/branch result. The existing source scope, at-most-once ledger, and post-send readback remain unchanged.

A definite send rate limit persists `rate_limited/retryAt` and permits retry only after cooldown. Server failures and unknown results retain `attempting`; failed reconciliation never authorizes another send. See [Configuration](CONFIGURATION_EN.md) for error classification and CLI response fields.

`--deliver-task-attachments` delivers attachments from comments created by the current task. The exact Slack root is checked before the first body send and before each file upload. Per-attachment receipts prevent repeating unknown uploads. See [Attachment delivery](../multica-skills/multica-final-reply/references/attachment-delivery.md) for discovery, download, permissions, and receipt handling.

## Build diagnostics

`relay_context` records only the event digest, read/retained counts, missing-data reasons, stage timings, and envelope bytes. It excludes chat text, names, credentials, and full snapshots. Correlate it with the separate `relay_dispatch` persistence result.

### Worker review footer

When Worker replies are enabled, the Worker renders review bodies as section blocks and metadata as separate context rows. Signed v2 receipts carry the decision, confirmed GitHub review, repository and commit, duration, and host-recorded tool counts; v1 receipts remain readable. Incomplete reviews retain statistics, task and PR links but have no approval link. Model names come from the matched run usage and are omitted when unavailable. The issue identifier comes from the verified Issue. The final attribution row retains the unique delivery marker, and fallback text includes the same information.

Marked historical messages may use mrkdwn or plain_text body and footer objects; both follow the existing footer-removal rules. Delivery ledger, source verification and post-send readback remain unchanged.


### Multiple targets and native agent receipts

Signed requests retain all PR candidates and granted capabilities from the frozen context. Explicit multiple targets are processed individually; the legacy executor caches child results so one failure does not erase another receipt. The native agent uses a persistent session for the same thread, without routing actionable requests to a text-only branch.

A `relay-agent/1` receipt binds its HMAC to the request ticket and contains body text, elapsed time, host-recorded tool counts, optional host-selected model names and signed PR child receipts. The Worker verifies both layers and rejects results belonging to another request. Native body text uses Slack formatting with raw mention syntax escaped. Tools, model, task and repository links occupy separate context rows. Approval links come from verified GitHub receipts. Target validation and the at-most-once delivery ledger remain unchanged.


### Continuing an interrupted review

Before a formal review starts, the host persists its original signed request, thread, target and stable task ID. Evidence failures, interrupted tools and missing completion receipts leave the task pending. An eligible reviewer asking about a pending review in the same thread continues that delegation through fresh evidence and a final GitHub verdict; repeating the review command is unnecessary.

An explicit status-only or no-vote request stays read-only for that turn. Withdrawal persists a cancellation. Owner cancel commands handled at ingress are carried in subsequent signed context so cancelled grants cannot resume. Completed or cancelled tasks require a new explicit review delegation before another vote. Quoted history, another thread and revoked review capability cannot authorize continuation.

Retries retain the same GitHub write marker. An uncertain previous submission is reconciled by author, commit, marker and review state before any further write. Legacy tasks can migrate only with the original signed request, the host's formal review dispatch record and its matching unfinished record. Old reply text alone never grants authority.

A merged or closed PR ends the pending delegation without claiming a new approval. Existing review links remain attributed to their actual authors.
