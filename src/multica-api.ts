export interface MulticaIssue {
  identifier?: string;
  id: string;
  title: string;
  description?: string | null;
  project_id?: string | null;
  assignee_id?: string | null;
  assignee_type?: string | null;
}
export interface MulticaComment {
  id: string;
  content: string;
  trigger_outcomes?: unknown;
}
export interface ApiConfig {
  multicaApiBaseUrl: string;
  multicaApiToken: string;
  multicaWorkspaceId: string;
  multicaProjectId: string;
  multicaAgentId: string;
}
// A stale or unavailable heartbeat is not an execution failure. Leave queued work untouched.
export async function runtimeOnline(config: ApiConfig, runtimeId: string | undefined, fetchImpl: typeof fetch = fetch, now = Date.now()): Promise<boolean> {
  if (!runtimeId) return false;
  try {
    const response = await api(config, '/api/runtimes', {redirect:'manual',signal:AbortSignal.timeout(5000)}, fetchImpl);
    if (!response.ok) return false;
    const rows: unknown = await response.json();
    if (!Array.isArray(rows)) return false;
    const runtime = rows.find(row => object(row) && row.id === runtimeId && row.workspace_id === config.multicaWorkspaceId);
    if (!runtime || runtime.status !== 'online' || typeof runtime.last_seen_at !== 'string') return false;
    const age = now - Date.parse(runtime.last_seen_at);
    return age >= -5000 && age < 90000;
  } catch { return false; }
}
export interface SlackReplyContext {
  type: "slack_reply_context";
  source: "agent_config";
  agentId: string;
  capturedAt: string;
  status: "available" | "unavailable";
  model: string | null;
  serviceTier: "priority" | "default" | null;
}
export class ApiError extends Error {
  constructor(readonly status: number) {
    super("multica_http_error");
  }
}
function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function issue(value: unknown): MulticaIssue {
  if (
    !object(value) ||
    typeof value.id !== "string" ||
    typeof value.title !== "string"
  )
    throw new Error("invalid_multica_response");
  return value as unknown as MulticaIssue;
}
async function api(
  config: ApiConfig,
  path: string,
  init: RequestInit = {},
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  return fetchImpl(config.multicaApiBaseUrl + path, {
    ...init,
    headers: {
      authorization: `Bearer ${config.multicaApiToken}`,
      "content-type": "application/json",
      "x-workspace-id": config.multicaWorkspaceId,
      ...init.headers,
    },
    signal: init.signal ?? AbortSignal.timeout(8000),
  });
}
export async function getSlackReplyContext(
  config: ApiConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<SlackReplyContext> {
  let model: string | null = null;
  let serviceTier: SlackReplyContext["serviceTier"] = null;
  let status: SlackReplyContext["status"] = "unavailable";
  try {
    const response = await api(
      config,
      `/api/agents/${encodeURIComponent(config.multicaAgentId)}`,
      // workerd only supports follow/manual. A redirect remains a non-ok
      // response, so credentials never follow a Location to another target.
      { signal: AbortSignal.timeout(2000), redirect: "manual" },
      fetchImpl,
    );
    if (!response.ok) throw new ApiError(response.status);
    const body: unknown = await response.json();
    if (
      !object(body) ||
      body.id !== config.multicaAgentId ||
      body.workspace_id !== config.multicaWorkspaceId
    )
      throw new Error("invalid_multica_response");
    // 仅透传可展示的配置字段，避免把指令、凭据或 Slack 标记带入 footer。
    const candidate = typeof body.model === "string" ? body.model.trim() : "";
    if (/^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$/u.test(candidate))
      model = candidate;
    if (body.service_tier === "priority" || body.service_tier === "default")
      serviceTier = body.service_tier;
    status = "available";
  } catch {
    // footer 是可选信息；查询失败不阻断任务，也不输出上游响应或异常正文。
    console.warn("relay_reply_context", { reason: "agent_config_unavailable" });
  }
  return {
    type: "slack_reply_context",
    source: "agent_config",
    agentId: config.multicaAgentId,
    capturedAt: new Date().toISOString(),
    status,
    model,
    serviceTier,
  };
}
export async function findIssue(
  config: ApiConfig,
  marker: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaIssue | undefined> {
  const identity = marker.match(/[a-f0-9]{64}(?= -->$)/u)?.[0];
  if (!identity) throw new Error("invalid_thread_state");
  const query = new URLSearchParams({ q: identity, limit: "20", include_closed: "true" });
  const response = await api(config, "/api/issues/search?" + query, {}, fetchImpl);
  if (!response.ok) throw new ApiError(response.status);
  const body: unknown = await response.json();
  if (!object(body) || !Array.isArray(body.issues))
    throw new Error("invalid_multica_response");
  const matches = body.issues.map(issue).filter((x) => x.description?.startsWith(marker + "\n"));
  if (matches.length > 1) throw new Error("ambiguous_issue_mapping");
  const candidate = matches[0];
  if (!candidate) return;
  if (
    candidate.project_id !== config.multicaProjectId ||
    candidate.assignee_type !== "agent" ||
    candidate.assignee_id !== config.multicaAgentId
  )
    throw new Error("invalid_issue_scope");
  return candidate;
}
export async function createIssue(
  config: ApiConfig,
  title: string,
  description: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaIssue> {
  const response = await api(
    config,
    "/api/issues",
    {
      method: "POST",
      body: JSON.stringify({
        title,
        description,
        project_id: config.multicaProjectId,
        assignee_type: "agent",
        assignee_id: config.multicaAgentId,
        status: "todo",
      }),
    },
    fetchImpl,
  );
  if (response.status === 409) {
    const body: unknown = await response.json();
    if (
      object(body) &&
      body.code === "active_duplicate_issue" &&
      object(body.issue)
    ) {
      const existing = issue(body.issue),
        marker = description.split("\n")[0]!;
      if (
        existing.project_id === config.multicaProjectId &&
        existing.assignee_type === "agent" &&
        existing.assignee_id === config.multicaAgentId &&
        existing.description?.startsWith(marker + "\n")
      )
        return existing;
    }
    throw new ApiError(409);
  }
  if (!response.ok) throw new ApiError(response.status);
  return issue(await response.json());
}
export async function findComment(
  config: ApiConfig,
  issueId: string,
  marker: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaComment | undefined> {
  for await (const page of issueCommentPages(config, issueId, fetchImpl)) {
    const match = page.find((comment) => comment.content.startsWith(marker+'\n'));
    if (match) return match;
  }
}
async function* issueCommentPages(
  config: ApiConfig,
  issueId: string,
  fetchImpl: typeof fetch,
): AsyncGenerator<MulticaComment[]> {
  let before = "",
    beforeId = "";
  for (let page = 0; page < 50; page++) {
    const query = new URLSearchParams({ recent: "100" });
    if (before) {
      query.set("before", before);
      query.set("before_id", beforeId);
    }
    const response = await api(
      config,
      `/api/issues/${encodeURIComponent(issueId)}/comments?${query}`,
      {},
      fetchImpl,
    );
    if (!response.ok) throw new ApiError(response.status);
    const body: unknown = await response.json();
    if (
      !Array.isArray(body) ||
      body.some(
        (x) =>
          !object(x) ||
          typeof x.id !== "string" ||
          typeof x.content !== "string",
      )
    )
      throw new Error("invalid_multica_response");
    yield body as MulticaComment[];
    const next = response.headers.get("X-Multica-Next-Before");
    const nextId = response.headers.get("X-Multica-Next-Before-Id");
    if (!next && !nextId) return;
    if (!next || !nextId || (next === before && nextId === beforeId))
      throw new Error("invalid_comment_cursor");
    before = next;
    beforeId = nextId;
  }
  throw new Error("comment_lookup_limit");
}
export async function createComment(
  config: ApiConfig,
  issueId: string,
  content: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaComment> {
  const response = await api(
    config,
    `/api/issues/${encodeURIComponent(issueId)}/comments`,
    { method: "POST", body: JSON.stringify({ content, type: "comment" }) },
    fetchImpl,
  );
  if (!response.ok) throw new ApiError(response.status);
  const body: unknown = await response.json();
  if (
    !object(body) ||
    typeof body.id !== "string" ||
    typeof body.content !== "string"
  )
    throw new Error("invalid_multica_response");
  return body as unknown as MulticaComment;
}

export interface IssueRun {
  usage?: { model?: unknown }[];
  trigger_comment_id?: string | null;
  result?: { output?: unknown } | null;
  id: string;
  issue_id: string;
  workspace_id: string;
  agent_id: string;
  status: string;
}
export function isActiveRun(run: IssueRun): boolean {
  return [
    "queued",
    "dispatched",
    "running",
    "waiting_local_directory",
    "deferred",
  ].includes(run.status);
}
export async function getIssue(
  config: ApiConfig,
  issueId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaIssue> {
  const response = await api(
    config,
    `/api/issues/${encodeURIComponent(issueId)}`,
    {},
    fetchImpl,
  );
  if (!response.ok) throw new ApiError(response.status);
  const result = issue(await response.json());
  if (
    result.id !== issueId ||
    result.project_id !== config.multicaProjectId ||
    result.assignee_type !== "agent" ||
    result.assignee_id !== config.multicaAgentId
  )
    throw new Error("invalid_issue_scope");
  return result;
}
export async function listIssueRuns(
  config: ApiConfig,
  issueId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<IssueRun[]> {
  const response = await api(
    config,
    `/api/issues/${encodeURIComponent(issueId)}/task-runs`,
    {},
    fetchImpl,
  );
  if (!response.ok) throw new ApiError(response.status);
  const body: unknown = await response.json();
  if (
    !Array.isArray(body) ||
    body.some(
      (run) =>
        !object(run) ||
        typeof run.id !== "string" ||
        run.issue_id !== issueId ||
        run.workspace_id !== config.multicaWorkspaceId ||
        typeof run.agent_id !== "string" ||
        ![
          "queued",
          "dispatched",
          "running",
          "waiting_local_directory",
          "deferred",
          "completed",
          "failed",
          "cancelled",
        ].includes(String(run.status)),
    )
  )
    throw new Error("invalid_multica_response");
  return body as IssueRun[];
}
export async function cancelIssueRun(
  config: ApiConfig,
  issueId: string,
  runId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  // 使用绑定 issue 的接口，让服务端再次校验运行归属；终态另行 GET 回读。
  const response = await api(
    config,
    `/api/issues/${encodeURIComponent(issueId)}/tasks/${encodeURIComponent(runId)}/cancel`,
    { method: "POST", body: "{}" },
    fetchImpl,
  );
  if (!response.ok) throw new ApiError(response.status);
}

export async function listRelayMessageContents(
  config: ApiConfig,
  issueId: string,
  fetchImpl: typeof fetch,
): Promise<string[]> {
  const contents: string[] = [];
  for await (const page of issueCommentPages(config, issueId, fetchImpl)) {
    for (const comment of page) {
      if (/^<!-- relay-message:[a-f0-9]{64} -->\n/u.test(comment.content))
        contents.push(comment.content);
    }
  }
  return contents;
}
