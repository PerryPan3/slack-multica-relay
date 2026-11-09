export type MentionType = 'user' | 'subteam';

export interface MentionMatch {
  type: MentionType;
  id: string;
}

const USER_MENTION = /<@([A-Z0-9]+)(?:\|[^>]+)?>/gu;
const SUBTEAM_MENTION = /<!subteam\^([A-Z0-9]+)(?:\|[^>]+)?>/gu;

export function findTargetMention(
  text: string,
  targetUserIds: ReadonlySet<string>,
  targetSubteamIds: ReadonlySet<string>,
): MentionMatch | undefined {
  for (const match of text.matchAll(USER_MENTION)) {
    if (targetUserIds.has(match[1]!)) return { type: 'user', id: match[1]! };
  }
  for (const match of text.matchAll(SUBTEAM_MENTION)) {
    if (targetSubteamIds.has(match[1]!)) return { type: 'subteam', id: match[1]! };
  }
  return undefined;
}

export function isSupportedMessage(event: SlackMessageEvent): boolean {
  const subtype = typeof event.subtype === 'string' ? event.subtype : undefined;
  return event.type === 'message'
    && typeof event.channel === 'string'
    && typeof event.ts === 'string'
    && typeof event.text === 'string'
    && !event.app_id
    && !event.bot_profile
    && !event.bot_id
    && !['bot_message', 'message_changed', 'message_deleted'].includes(subtype ?? '');
}

export interface SlackMessageEvent {
  type?: unknown;
  team?: unknown;
  channel?: unknown;
  channel_type?: unknown;
  ts?: unknown;
  thread_ts?: unknown;
  user?: unknown;
  text?: unknown;
  subtype?: unknown;
  bot_id?: unknown;
  app_id?: unknown;
  bot_profile?: unknown;
  files?: unknown;
}

export function isCancelCommand(
  text: string,
  targetUserIds: ReadonlySet<string>,
  targetSubteamIds: ReadonlySet<string>,
  cancelKeywords: ReadonlySet<string>,
): boolean {
  if (!findTargetMention(text, targetUserIds, targetSubteamIds)) return false;
  const command = text
    .replace(USER_MENTION, (mention, id: string) => targetUserIds.has(id) ? '' : mention)
    .replace(SUBTEAM_MENTION, (mention, id: string) => targetSubteamIds.has(id) ? '' : mention)
    .trim()
    .toLowerCase();
  return cancelKeywords.has(command);
}

export function canCancelTask(senderUserId: string | undefined, targetUserIds: ReadonlySet<string>): boolean {
  return senderUserId !== undefined && targetUserIds.has(senderUserId);
}
