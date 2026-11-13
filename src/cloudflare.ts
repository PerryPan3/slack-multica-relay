import { DurableOutbox } from "./durable-outbox.js";
import { loadRelayConfig } from "./config.js";
import { runtimeOnline } from "./multica-api.js";
import { acceptSlack, consumeQueue, json, processRelayPayload, recoverConfiguredMessages } from './http.js';

export default {
  async scheduled(_event: {cron?: string}, env: Record<string, string | undefined>): Promise<void> {
    if (env.RELAY_DURABLE_SCHEDULER === 'true' && _event.cron !== '15 1 * * *') {
      const config = loadRelayConfig(env);
      if (!await runtimeOnline(config, env.MULTICA_RUNTIME_ID)) return;
      try { await recoverConfiguredMessages(env); }
      catch { console.warn('relay_recovery',{action:'retry'}); }
      await new DurableOutbox(config).drain(raw => processRelayPayload(raw, env));
      return;
    }
    const baseUrl = new URL(env.MULTICA_API_BASE_URL ?? '');
    if (baseUrl.protocol !== 'https:' || baseUrl.username || baseUrl.password || !env.MULTICA_API_TOKEN)
      throw new Error('invalid_renewal_configuration');

    const response = await fetch(new URL('/api/tokens/current/renew', baseUrl), {
      method: 'POST',
      headers: { authorization: 'Bearer ' + env.MULTICA_API_TOKEN },
      redirect: 'manual',
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error('multica_renewal_http_' + response.status);
    const {renewed, expires_at} = await response.json();
    if (typeof renewed !== 'boolean' || typeof expires_at !== 'string' ||
        !(expires_at === '' && !renewed || Date.parse(expires_at) > Date.now()))
      throw new Error('invalid_renewal_receipt');
    console.info('multica_token_renewal', {renewed, expires_at});
  },

  async fetch(request: Request, env: Record<string, string | undefined>): Promise<Response> {
    const path = new URL(request.url).pathname;
    if (path === '/api/slack/events') return acceptSlack(request, env);
    if (path === '/api/queue/consume') return consumeQueue(request, env);
    if (path === '/api/health') {
      if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
      return json({ ok: true, service: 'slack-multica-relay' });
    }
    return json({ error: 'not_found' }, 404);
  },
};
