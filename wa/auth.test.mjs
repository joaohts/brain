// node --test wa/   (no network, no WhatsApp connection)
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { moveAuthAside } from './auth.js';

test('moveAuthAside renames the auth dir and keeps its files', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-auth-'));
  const auth = path.join(root, 'auth');
  fs.mkdirSync(auth);
  fs.writeFileSync(path.join(auth, 'creds.json'), '{"x":1}');
  const when = new Date('2026-10-05T21:30:00.123Z');
  const dest = moveAuthAside(auth, when);
  assert.equal(dest, `${auth}.old-20261005T213000Z`);
  assert.equal(fs.existsSync(auth), false);
  assert.equal(fs.readFileSync(path.join(dest, 'creds.json'), 'utf8'), '{"x":1}');
  // a second repair in the same second never overwrites the first backup
  fs.mkdirSync(auth);
  const dest2 = moveAuthAside(auth, when);
  assert.equal(dest2, `${auth}.old-20261005T213000Z-1`);
  assert.ok(fs.existsSync(dest) && fs.existsSync(dest2));
  fs.rmSync(root, { recursive: true });
});

test('moveAuthAside is a no-op when there is no auth dir', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-auth-'));
  assert.equal(moveAuthAside(path.join(root, 'auth')), null);
  fs.rmSync(root, { recursive: true });
});
