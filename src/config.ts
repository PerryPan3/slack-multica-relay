export interface RelayConfig {
  signingSecret: string;
  teamId: string;
  targetUserIds: Set<string>;
  targetSubteamIds: Set<string>;
  cancelKeywords: Set<string>;
  allowedChannelIds: Set<string>;
  allowAllChannels: boolean;
  selfAssignScope?: "dm" | "all";
  blockedChannelIds: Set<string>;
  allowedSenderIds: Set<string>;
  allowAllSenders: boolean;
  blockedSenderIds: Set<string>;
  multicaApiBaseUrl: string;
  multicaApiToken: string;
  multicaWorkspaceId: string;
  multicaProjectId: string;
  multicaAgentId: string;
  slackReactionToken: string;
  slackContextToken: string;
  slackReactionName: string;
  kvRestApiUrl: string;
  kvRestApiToken: string;
  queueUrl: string;
  queueToken: string;
  queueCurrentSigningKey: string;
  queueNextSigningKey: string;
  consumerUrl: string;
}
export function loadRelayConfig(
  env: NodeJS.ProcessEnv = process.env,
): RelayConfig {
  const allowedChannels = policyIds(required(env, "SLACK_ALLOWED_CHANNEL_IDS"));
  const blockedChannelIds = ids(env.SLACK_BLOCKED_CHANNEL_IDS);
  const allowedSenders = policyIds(env.SLACK_ALLOWED_SENDER_IDS || "all");
  const blockedSenderIds = ids(env.SLACK_BLOCKED_SENDER_IDS);
  const targetUserIds = ids(env.SLACK_TARGET_USER_IDS);
  const targetSubteamIds = ids(env.SLACK_TARGET_SUBTEAM_IDS);
  if (!targetUserIds.size && !targetSubteamIds.size)
    throw new Error("missing_mention_target");
  return {
    signingSecret: required(env, "SLACK_SIGNING_SECRET"),
    teamId: required(env, "SLACK_TEAM_ID"),
    allowedChannelIds: allowedChannels.ids,
    allowAllChannels: allowedChannels.all,
    selfAssignScope: env.SLACK_SELF_ASSIGN_SCOPE === "all" || env.SLACK_SELF_ASSIGN_SCOPE === "dm" ? env.SLACK_SELF_ASSIGN_SCOPE : undefined,
    blockedChannelIds,
    allowedSenderIds: allowedSenders.ids,
    allowAllSenders: allowedSenders.all,
    blockedSenderIds,
    targetUserIds,
    targetSubteamIds,
    cancelKeywords: cancelKeywords(env.SLACK_CANCEL_KEYWORDS),
    multicaApiBaseUrl: https(required(env, "MULTICA_API_BASE_URL")),
    multicaApiToken: required(env, "MULTICA_API_TOKEN"),
    multicaWorkspaceId: required(env, "MULTICA_WORKSPACE_ID"),
    multicaProjectId: required(env, "MULTICA_PROJECT_ID"),
    multicaAgentId: required(env, "MULTICA_AGENT_ID"),
    slackReactionToken:
      env.SLACK_BOT_TOKEN?.trim() || required(env, "SLACK_USER_TOKEN"),
    slackContextToken: required(env, "SLACK_CONTEXT_TOKEN"),
    slackReactionName: required(env, "SLACK_REACTION_NAME").replace(
      /^:+|:+$/gu,
      "",
    ),
    kvRestApiUrl: https(required(env, "KV_REST_API_URL")),
    kvRestApiToken: required(env, "KV_REST_API_TOKEN"),
    queueUrl: https(env.QSTASH_URL?.trim() || "https://qstash.upstash.io"),
    queueToken: required(env, "QSTASH_TOKEN"),
    queueCurrentSigningKey: required(env, "QSTASH_CURRENT_SIGNING_KEY"),
    queueNextSigningKey: required(env, "QSTASH_NEXT_SIGNING_KEY"),
    consumerUrl: https(required(env, "RELAY_CONSUMER_URL")),
  };
}
function required(env: NodeJS.ProcessEnv, key: string): string {
  const value = env[key]?.trim();
  if (!value) throw new Error("relay_not_configured");
  return value;
}
function ids(value: string | undefined): Set<string> {
  const values = (value ?? "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
  if (values.some((x) => !/^[A-Z][A-Z0-9]+$/u.test(x)))
    throw new Error("invalid_identifier");
  return new Set(values);
}
function policyIds(value: string): { ids: Set<string>; all: boolean } {
  const normalized = value.trim();
  if (normalized.toLowerCase() === "all") return { ids: new Set(), all: true };
  const parsed = ids(normalized);
  if (!parsed.size) throw new Error("invalid_allowlist");
  return { ids: parsed, all: false };
}
function https(value: string): string {
  const url = new URL(value);
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.hash ||
    url.search
  )
    throw new Error("invalid_service_url");
  return value.replace(/\/+$/u, "");
}

function cancelKeywords(value: string | undefined): Set<string> {
  const keywords = (value ?? "")
    .split(",")
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);
  return new Set(keywords.length ? keywords : ["cancel", "取消"]);
}
