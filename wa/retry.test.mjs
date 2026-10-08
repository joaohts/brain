// node --test wa/*.test.mjs   (no network, no WhatsApp connection)
import test from 'node:test';
import assert from 'node:assert/strict';
import { refused, networkError, retrying } from './retry.js';

const econnrefused = () => Object.assign(new TypeError('fetch failed'),
  { cause: Object.assign(new Error('connect ECONNREFUSED'), { code: 'ECONNREFUSED' }) });
const reset = () => Object.assign(new TypeError('fetch failed'),
  { cause: Object.assign(new Error('socket hang up'), { code: 'ECONNRESET' }) });

test('a refused connection is retried until the brain is back', async () => {
  let calls = 0, clock = 0;
  const out = await retrying(async () => {
    if (++calls < 4) throw econnrefused();
    return 'ok';
  }, { until: 60_000, every: 2000, now: () => clock, sleep: async ms => { clock += ms; } });
  assert.equal(out, 'ok');
  assert.equal(calls, 4);
});

test('gives up at the deadline with the last error', async () => {
  let clock = 0;
  await assert.rejects(retrying(async () => { throw econnrefused(); },
    { until: 5000, every: 2000, now: () => clock, sleep: async ms => { clock += ms; } }),
    /fetch failed/);
  assert.equal(clock, 4000);
});

test('only safe failures are retried', async () => {
  assert.equal(refused(econnrefused()), true);
  assert.equal(refused(reset()), false);           // may have reached the brain
  assert.equal(networkError(reset()), true);
  assert.equal(networkError(new SyntaxError('bad json')), true);
  assert.equal(networkError(new Error('logic')), false);
  let calls = 0;
  await assert.rejects(retrying(async () => { calls++; throw reset(); },
    { until: 60_000, sleep: async () => {} }));
  assert.equal(calls, 1);
});
