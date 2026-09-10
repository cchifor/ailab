// Stub standing in for the shipped LocalCredentialProvider. It reproduces exactly the base-class
// behaviour the subclass depends on: env-layer precedence reported as source 'env', a local
// reference map reported as source 'file', writable refs, and record methods that must be
// inherited untouched.
export class LocalCredentialProvider {
  constructor(ctx, config = {}) {
    this.ctx = ctx; this.config = config;
    this.env = new Map(Object.entries(config.__env ?? {}));
    this.values = new Map(Object.entries(config.__file ?? {}));
    // The `.env` fallback layers. The base reports these with source 'project-env'/'user-env',
    // NOT 'env' -- verified against dsh-credentials-local@0.1.5-alpha.2 driven through real
    // cordis. That distinction is the whole reason OpenBao may win over them.
    this.dotenv = new Map(Object.entries(config.__dotenv ?? {}));
    this.records = new Map(Object.entries(config.__records ?? {}));
    this.calls = [];
  }
  async resolve(ref) {
    this.calls.push(['resolve', ref]);
    if (this.env.has(ref)) return { value: this.env.get(ref), source: 'env' };
    if (this.values.has(ref)) return { value: this.values.get(ref), source: 'file' };
    if (this.dotenv.has(ref)) return { value: this.dotenv.get(ref), source: 'user-env' };
    return undefined;
  }
  async describe(ref) {
    if (this.env.has(ref)) return { configured: true, source: 'env', writable: false };
    if (this.values.has(ref)) return { configured: true, source: 'file', writable: true };
    if (this.dotenv.has(ref)) return { configured: true, source: 'user-env', writable: true };
    return { configured: false, writable: true };
  }
  async set(ref, value) { this.calls.push(['set', ref]); this.values.set(ref, value); }
  async unset(ref) { this.calls.push(['unset', ref]); this.values.delete(ref); }
  async readRecord(key) { this.calls.push(['readRecord', key]); return this.records.get(key); }
  async describeRecord(key) { return { configured: this.records.has(key), writable: true }; }
  async listRecords() { return [...this.records.keys()].map((k) => ({ key: k, kind: this.records.get(k)?.kind ?? 'grant' })); }
  async modifyRecord(key, mutate) {
    this.calls.push(['modifyRecord', key]);
    const current = this.records.get(key);
    const next = await mutate(current);
    if (next === undefined) return current;       // LEAVE UNCHANGED, not delete
    this.records.set(key, next); return next;
  }
  async deleteRecord(key) { this.calls.push(['deleteRecord', key]); this.records.delete(key); }
}
export default LocalCredentialProvider;
