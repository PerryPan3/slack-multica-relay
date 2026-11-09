import type { SlackThreadEvent } from "./thread-router.js";

export function isOwnerSelfMention(event:SlackThreadEvent, owner?:string, team?:string) {
  return !!owner && event.teamId===team && event.senderUserId===owner &&
    event.mention.type==="user" && event.mention.id===owner &&
    (event.text.includes("<@"+owner+">") || event.text.includes("<@"+owner+"|"));
}
