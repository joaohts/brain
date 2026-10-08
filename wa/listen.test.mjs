// node --test wa/   (no network, no WhatsApp connection)
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { loadPause, savePause, expired, inWindow } from './listen.js';

test('a pause survives a restart and clearing it removes the file', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-listen-'));
  const file = path.join(root, 'wa-listen.json');
  assert.equal(loadPause(file), null);                      // no file = listening
  savePause(file, { pausedAt: 1000, until: 5000 });
  assert.deepEqual(loadPause(file), { pausedAt: 1000, until: 5000 });
  savePause(file, { pausedAt: 1000, until: null });
  assert.deepEqual(loadPause(file), { pausedAt: 1000, until: null });
  savePause(file, null);
  assert.equal(fs.existsSync(file), false);
  fs.writeFileSync(file, 'not json');
  assert.equal(loadPause(file), null);                      // corrupt = listening
  fs.rmSync(root, { recursive: true });
});

test('only a timed pause expires', () => {
  assert.equal(expired(null, 10), false);
  assert.equal(expired({ pausedAt: 0, until: null }, 1e15), false);
  assert.equal(expired({ pausedAt: 0, until: 100 }, 99), false);
  assert.equal(expired({ pausedAt: 0, until: 100 }, 100), true);
});

test('messages sent while off are inside the drop window', () => {
  const w = { from: 10_000, to: 20_000 };                  // ms
  assert.equal(inWindow(w, 9), false);                      // seconds, before
  assert.equal(inWindow(w, 10), true);
  assert.equal(inWindow(w, 19), true);
  assert.equal(inWindow(w, 20), false);                     // after resume
  assert.equal(inWindow(null, 15), false);
  assert.equal(inWindow(w, undefined), false);
  assert.equal(inWindow(w, { valueOf: () => 15 }), true);   // Baileys Long
});
