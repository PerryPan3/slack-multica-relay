import { Buffer } from "node:buffer";
import { randomUUID } from 'node:crypto';
import { digest } from './thread-router.js';
import type { RelayConfig } from './config.js';

const RETENTION = 7 * 86400;
const MAX_FAILURES = 24;
type Entry = {payload: unknown; created: number; failures: number};

// The payload and due index are committed together before acknowledging Slack.
const SAVE = `if redis.call('exists',KEYS[1]) == 0 then
  redis.call('set',KEYS[1],ARGV[1],'EX',ARGV[3]);
  redis.call('zadd',KEYS[2],ARGV[2],KEYS[1]); return 1 end; return 0`;
const SETTLE = `if redis.call('get',KEYS[3]) ~= ARGV[1] then return 0 end;
  if ARGV[2] == '' then redis.call('zrem',KEYS[2],KEYS[1]); redis.call('del',KEYS[1]);
  else redis.call('set',KEYS[1],ARGV[2],'EX',ARGV[4]); redis.call('zadd',KEYS[2],ARGV[3],KEYS[1]); end;
  redis.call('del',KEYS[3]); return 1`;

export class DurableOutbox {
  private prefix: string;
  constructor(private config: RelayConfig, private send: typeof fetch = fetch, private now = () => Date.now()) {
    this.prefix = 'relay:' + digest([config.multicaWorkspaceId,config.multicaProjectId,config.multicaAgentId,config.consumerUrl].join(':')) + ':outbox';
  }
  private async command(command: unknown[]): Promise<unknown> {
    const response = await this.send(this.config.kvRestApiUrl, {
      method:'POST', headers:{authorization:'Bearer '+this.config.kvRestApiToken,'content-type':'application/json'},
      body:JSON.stringify(command), signal:AbortSignal.timeout(2000),
    });
    if (!response.ok) throw new Error('outbox_store_http_'+response.status);
    const data = await response.json();
    if (data.error !== undefined) throw new Error('outbox_store_failed');
    return data.result;
  }
  async schedule(payload: unknown, delaySeconds = 0): Promise<string> {
    const body = JSON.stringify(payload);
    if (Buffer.byteLength(body) > 256*1024) throw new Error('body_too_large');
    const id = digest(body), key = this.prefix+':job:'+id;
    const entry: Entry = {payload,created:this.now(),failures:0};
    await this.command(['EVAL',SAVE,'2',key,this.prefix,JSON.stringify(entry),String(this.now()+delaySeconds*1000),String(RETENTION)]);
    return id;
  }
  async drain(process: (raw: string) => Promise<Response>): Promise<void> {
    const due = await this.command(['ZRANGEBYSCORE',this.prefix,'-inf',String(this.now()),'LIMIT','0','8']);
    if (!Array.isArray(due)) throw new Error('outbox_index_invalid');
    // Small batches bound scheduled invocations and leave capacity for other threads.
    for (let offset=0;offset<due.length;offset+=2) {
      await Promise.all(due.slice(offset,offset+2).map(async key => {
        if (typeof key!=='string' || !key.startsWith(this.prefix+':job:')) return;
        const owner=randomUUID(), lock=key+':lock';
        if (await this.command(['SET',lock,owner,'NX','EX','120']) !== 'OK') return;
        let entry: Entry | undefined;
        let retry = false;
        let code = 'invalid_outbox_entry';
        try {
          const raw=await this.command(['GET',key]);
          if (typeof raw==='string') {
            entry=JSON.parse(raw);
            if (!entry || !Number.isFinite(entry.created) || !Number.isSafeInteger(entry.failures) || entry.failures<0)
              throw new Error('invalid_outbox_entry');
            if (this.now()-entry.created >= RETENTION*1000) code='outbox_expired';
            else {
              const response=await process(JSON.stringify(entry.payload));
              retry=response.status===429 || response.status>=500;
              const result=await response.json() as {action?:string;reason?:string;error?:string};
              const value=result.reason ?? result.error ?? result.action;
              code=typeof value==='string' && /^[a-z0-9_]+$/.test(value) ? value : 'processing_failed';
            }
          }
        } catch {
          if (!entry) throw new Error('outbox_read_failed');
          retry=true; code='outbox_processing_failed';
        }
        if (retry && entry) entry.failures++;
        const exhausted = !!entry && entry.failures >= MAX_FAILURES;
        if (exhausted) {
          // Retain private evidence for inspection; do not post operational failures to Slack.
          await this.command(['SET',key+':failed',JSON.stringify({...entry,reason:code}),'EX',String(RETENTION)]);
        }
        const next = retry && entry && !exhausted ? JSON.stringify(entry) : '';
        await this.command(['EVAL',SETTLE,'3',key,this.prefix,lock,owner,next,
          String(this.now()+Math.min(300,60*(entry?.failures ?? 1))*1000),String(RETENTION)]);
        console.info('relay_outbox',{job:key.slice(-64),action:exhausted?'exhausted':retry?'retry':code});
      }));
    }
  }
}
