# Relay reliability checks

The acceptance target is an executable conversation: an admitted request reaches the tools, unfinished work survives a follow-up, and the final response states only verified outcomes. A successful deployment, model answer or Multica run is not a delivery receipt.

## Regression coverage

| Failure class | Tests and expected behavior |
| --- | --- |
| Sender, channel or mention mismatch | `events.test.ts`, `semantic-route.test.ts`, `worker-reply.test.ts`: reject unauthorized combinations at ingress and delivery; owner self-assignment does not grant colleagues local commands. |
| Natural language and multiple PRs | `semantic-route.test.ts`, `slack_intent_test.py`, `review_tasks_test.py`: retain current and same-thread candidates; interpret the requested subset; preserve each pending task independently. Live model checks must also include unfamiliar phrasing and explicit read-only requests. |
| Read-only business investigation | `agent_tools_cases.py`, `business_sql_cases.py`: discover registered read tools, allow bounded SELECT/CTE/JOIN queries, reject mutations and local commands from colleagues. |
| Missing GitHub evidence | `github_review_test.py`, `agent_tools_cases.py`: use REST pull endpoints, include PR conversation and review comments, distinguish missing resources from access failures, and allow reading drafts or the reviewer's own PR. |
| An unavailable old task | `agent_tools_cases.py`: one failed reconciliation cannot prevent another PR or an unrelated business investigation from executing. A preparation error can be retried through the same request-bound gateway. |
| Interrupted runtime | `NativeSessionTests` in `agent_tools_cases.py`: replay the same request without a second model run, resume the session on a new follow-up, and carry the host-recorded interruption reason. |
| Unfinished review authorization | `review_tasks_test.py`: preserve the original signed delegation and task identity; reject cross-thread, cancelled, completed, superseded and capability-revoked continuation. Explicit status-only requests remain read-only. |
| GitHub writes with uncertain results | `github_review_test.py`: recover only the matching author's task marker and commit; adopt the corresponding pending draft after a lost local receipt; do not reuse another review's reasoning or repeat an unresolved write. |
| New commits, merged or closed PRs | `github_review_test.py`, `agent_tools_cases.py`: require fresh evidence for a changed revision and report terminal state without claiming a new approval. |
| Slack rendering and delivery | `worker-reply.test.ts`: distinguish encoded transport size from visible text, keep statistics in context blocks, escape mentions, verify the original thread and recover a lost response without posting twice. |
| Token renewal and service failures | `cloudflare.test.ts`, `context-interval.test.ts`, `multica.test.ts`: validate renewal receipts, signatures, pagination, transient errors and persisted write recovery. |

## Verification sequence

1. Run `pnpm test`, `pnpm lint` and `pnpm build:cf`. Python dependencies are listed in `scripts/requirements-agent.txt`; tests use `BUSINESS_SQL_PYTHON` when the gateway interpreter differs from the shell interpreter. The owner command isolation tests require macOS.
2. Run live model interpretation cases with isolated fixture tickets and no write tools. Include a short CR request, a reference to two PRs, a plain interruption follow-up, an unrelated business question, quoted instructions and an explicit no-vote request.
3. With explicit authorization, use the owner's self-DM for real end-to-end tests. Confirm the Slack event, scoped Multica issue/run, host tool audit and delivered thread reply independently. Use the existing task state for follow-ups; never erase ledgers to force a rerun.
4. A formal review test needs an open eligible PR and a real evidence-based decision. Verify the GitHub author, commit, state and review URL. If the PR has merged, report that the formal-write test cannot use it; a read-only query does not substitute for that test.
5. Before merging, run the PR checks on the exact head and verify the merge result. Keep deployment and Git commit provenance distinct.

## Operational limits

The runtime cannot execute while its host is asleep or offline. The current one-hour signed-request validity still applies. Network outages, revoked credentials, unavailable tools, context-size limits and continuously changing PRs can prevent completion. These conditions must preserve unfinished work or produce an accurate failure; they must not be reported as successful review or delivery. Tests establish the covered behavior, not a guarantee that external services or model decisions can never fail.
