// Stands in for the runtime's `cloudflare:workers` module, which exists only under
// workerd, so a test can construct the Durable Object in index.js. vitest.config.js
// points the import here.
export const env = {};

export class DurableObject {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }
}
