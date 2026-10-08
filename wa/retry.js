// The brain restarts (deploys, Restart=always) while turns are in flight.
// Its inbox is durable and systemd brings it back within seconds, so a call
// that fails on the connection is retried instead of crashing the sidecar and
// losing the reply. Kept free of Baileys so it can be tested without opening a
// WhatsApp connection (node --test wa/*.test.mjs).

// The request never reached the brain: always safe to send again.
export function refused(e) {
  return (e?.cause?.code || e?.code) === 'ECONNREFUSED';
}

// Any failure of the request itself (not an HTTP status: the brain answers
// those deliberately).
export function networkError(e) {
  return e?.name === 'TypeError' || !!(e?.cause?.code || e?.code) || e instanceof SyntaxError;
}

// fn() until it resolves; a failure for which retryable(e) is true is retried
// every `every` ms while the next try still starts before `until`.
export async function retrying(fn, { until, every = 2000, retryable = refused,
                                     onRetry = () => {},
                                     sleep = ms => new Promise(r => setTimeout(r, ms)),
                                     now = Date.now } = {}) {
  for (let attempt = 1; ; attempt++) {
    try { return await fn(); }
    catch (e) {
      if (!retryable(e) || now() + every > until) throw e;
      onRetry(e, attempt);
      await sleep(every);
    }
  }
}
