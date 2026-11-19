import type { RelayConfig } from './config.js';
import { digest, STATE_TTL_SECONDS, type SlackThreadEvent } from './thread-router.js';
import { UpstashThreadStore } from './thread-store.js';

const reasons: Record<string,string> = {
  model_not_found: '配置的模型不受当前登录方式支持，需要修正 Slack Agent 的专用模型配置。',
  model_capacity: '模型服务持续返回容量或节流错误，有限自动恢复已用完。',
  model_rate_limit: '模型服务持续限流，内置重试后仍未恢复。',
  model_turn_failed: '模型调用失败，需要检查私有运行诊断。',
  model_recovery_exhausted: '连接恢复后连续三次执行仍无法推进，已停止自动恢复；会话与审批记录已保留。',
  recovery_dispatch_unknown: '续接请求的提交结果无法确认，已停止重复提交；需要核对 Multica 执行记录。',
  execution_failed: '执行中断，需要检查私有运行诊断和已有结果。',
};

export async function notifyOwnerFailure(event:SlackThreadEvent, message:string, code:string,
  ownerChannel:string|undefined, config:RelayConfig, send:typeof fetch) {
  if (!ownerChannel || !/^D[A-Z0-9]+$/.test(ownerChannel)) return 'failure_notification_unconfigured';
  if (!config.allowedChannelIds.has(ownerChannel) || config.blockedChannelIds.has(ownerChannel))
    throw new Error('failure_notification_identity_rejected');
  const destination=ownerChannel;
  const [owner]=config.allowedSenderIds;
  if (config.allowAllSenders || config.allowedSenderIds.size!==1 || !config.targetUserIds.has(owner))
    throw new Error('failure_notification_identity_rejected');
  const store=new UpstashThreadStore(config.kvRestApiUrl,config.kvRestApiToken,send);
  const scope=digest([config.multicaWorkspaceId,config.multicaProjectId,config.multicaAgentId].join(':'));
  const ledger='relay:'+scope+':failure:'+message;
  const receipt=await store.get(ledger);
  if (receipt?.startsWith('sent:')) return 'failure_owner_notified';
  const headers={authorization:'Bearer '+config.slackContextToken};
  async function read(method:string,query:Record<string,string>={}) {
    const response=await send('https://slack.com/api/'+method+'?'+new URLSearchParams(query),{headers});
    const body=await response.json();
    if (!response.ok || !body.ok) {
      const code=typeof body.error==='string' && /^[a-z_]{1,64}$/.test(body.error) ? body.error : 'read_failed';
      throw new Error('failure_notification_'+method.replace('.','_')+'_'+code);
    }
    return body;
  }
  const identity=await read('auth.test');
  // Destination comes only from the deployment's verified owner self-DM, never the requester.
  // Reading DM metadata would require im:read, beyond the installed history/write scopes.
  if(identity.user_id!==owner || identity.team_id!==config.teamId)
    throw new Error('failure_notification_identity_rejected');
  const marker='relay-delivery-'+digest(ledger);
  async function verify(ts?:string) {
    let cursor='';
    for(let page=0;page<10;page++) {
      const body=await read('conversations.history',{channel:destination,limit:'100',
        ...(ts?{oldest:ts,latest:ts,inclusive:'true'}:{oldest:event.messageTs}),...(cursor?{cursor}:{})});
      const match=body.messages?.find((m:{ts:string;user:string;blocks?:{block_id?:string}[]})=>
        m.user===owner && (!ts || m.ts===ts) && m.blocks?.some(b=>b.block_id===marker));
      if(match) {await store.set(ledger,'sent:'+match.ts,STATE_TTL_SECONDS);return 'failure_owner_notified';}
      cursor=body.response_metadata?.next_cursor || '';
      if(!cursor) break;
    }
    throw new Error('slack_delivery_unknown');
  }
  if(receipt) return verify(receipt.startsWith('accepted:')?receipt.slice(9):undefined);
  const link='https://app.slack.com/archives/'+event.channelId+'/p'+event.messageTs.replace('.','');
  const text='有一项 Slack Agent 任务需要处理。'+(reasons[code] || reasons.execution_failed)+
    '\n已有进度和审批回执已保留；这次没有在群里发布故障提示。\n原请求：'+link;
  if(!await store.setIfAbsent(ledger,'attempting',STATE_TTL_SECONDS)) return 'failure_notification_in_progress';
  const response=await send('https://slack.com/api/chat.postMessage',{method:'POST',headers:{...headers,'content-type':'application/json'},
    body:JSON.stringify({channel:ownerChannel,text,mrkdwn:false,parse:'none',unfurl_links:false,unfurl_media:false,
      blocks:[{type:'section',text:{type:'plain_text',text}},{type:'context',block_id:marker,elements:[{type:'plain_text',text:'Sent by 🤖 Perry’s Agent'}]}]})});
  if(response.status===429) {await store.releaseIfOwner(ledger,'attempting');throw new Error('slack_rate_limited');}
  const body=await response.json();
  if(!response.ok || !body.ok || body.channel!==ownerChannel || !/^\d+\.\d+$/.test(body.ts)) throw new Error('slack_delivery_unknown');
  await store.set(ledger,'accepted:'+body.ts,STATE_TTL_SECONDS);
  return verify(body.ts);
}
