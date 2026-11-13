export interface ThreadStore {
  get(key: string): Promise<string | null>;
  set(key: string, value: string, ttlSeconds: number): Promise<void>;
  setIfAbsent(key: string, value: string, ttlSeconds: number): Promise<boolean>;
  releaseIfOwner(key: string, owner: string): Promise<void>;
}

interface UpstashResponse {
  result?: unknown;
  error?: unknown;
}

/**
 * 使用 Upstash/Vercel KV 的 REST 命令接口，避免把 Redis 客户端打进函数包。
 * 所有请求都由显式 token 认证，Vercel 实例之间共享同一份线程状态。
 */
export class UpstashThreadStore implements ThreadStore {
  constructor(
    private readonly url: string,
    private readonly token: string,
    private readonly fetchImpl: typeof fetch = fetch,
  ) {}

  async get(key: string): Promise<string | null> {
    const result = await this.command(['GET', key]);
    return typeof result === 'string' ? result : null;
  }

  async set(key: string, value: string, ttlSeconds: number): Promise<void> {
    await this.command(['SET', key, value, 'EX', String(ttlSeconds)]);
  }

  async setIfAbsent(key: string, value: string, ttlSeconds: number): Promise<boolean> {
    const result = await this.command(['SET', key, value, 'NX', 'EX', String(ttlSeconds)]);
    return result === 'OK';
  }

  async releaseIfOwner(key: string, owner: string): Promise<void> {
    // 只删除仍由当前请求持有的锁，避免旧请求超时后误删新请求的锁。
    await this.command([
      'EVAL',
      'if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("del", KEYS[1]) else return 0 end',
      '1',
      key,
      owner,
    ]);
  }

  private async command(command: string[]): Promise<unknown> {
    const send = this.fetchImpl;
    const response = await send(this.url, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${this.token}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify(command),
      signal: AbortSignal.timeout(2_000),
    });
    if (!response.ok) throw new Error(`Thread store failed with HTTP ${response.status}`);
    const body = await response.json() as UpstashResponse;
    if (body.error !== undefined) throw new Error(`Thread store command failed: ${String(body.error)}`);
    return body.result;
  }
}

/** 用于单元测试的内存实现；生产环境必须使用共享的 REST 存储。 */
export class MemoryThreadStore implements ThreadStore {
  private readonly values = new Map<string, { value: string; expiresAt: number }>();

  async get(key: string): Promise<string | null> {
    const entry = this.values.get(key);
    if (!entry || entry.expiresAt <= Date.now()) {
      this.values.delete(key);
      return null;
    }
    return entry.value;
  }

  async set(key: string, value: string, ttlSeconds: number): Promise<void> {
    this.values.set(key, { value, expiresAt: Date.now() + ttlSeconds * 1000 });
  }

  async setIfAbsent(key: string, value: string, ttlSeconds: number): Promise<boolean> {
    const entry = this.values.get(key);
    if (entry && entry.expiresAt > Date.now()) return false;
    if (entry) this.values.delete(key);
    await this.set(key, value, ttlSeconds);
    return true;
  }

  async releaseIfOwner(key: string, owner: string): Promise<void> {
    if (await this.get(key) === owner) this.values.delete(key);
  }
}
