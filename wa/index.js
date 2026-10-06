// WhatsApp sidecar for the brain — envelope bridge over the brain's /turn API.
//
//   inbound : self-chat (Notes to Self) + allowlisted DMs -> debounce ->
//             POST BRAIN/turn (submit) -> poll -> reply into the same chat
//   outbound: POST :3402/send {to: "self"|jid, text} (used by brain deliver())
//
// Auth state is Baileys useMultiFileAuthState in WA_AUTH (default ./auth).
// With no session yet, a QR is printed to the log: scan it from the phone
// (WhatsApp > Linked devices). Configuration arrives as env vars exported by
// run-wa.sh from config.toml [whatsapp].
//
// Retry receipts: when a recipient device can't decrypt one of our
// messages it sends a retry receipt and Baileys re-encrypts + re-sends — but
// only if `getMessage` can hand back the original content. Without it the
// recipient's bubble stays at "Waiting for this message" forever. We keep a
// bounded cache of everything we sent and serve it from getMessage.
// Baileys' own logger goes to WA_BAILEYS_LOG (default ../data/wa-baileys.log,
// level WA_LOG_LEVEL, default info).

import makeWASocket, {
  useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason,
  downloadMediaMessage,
} from '@whiskeysockets/baileys';
import fs from 'fs';
import http from 'http';
import pino from 'pino';
import qrterm from 'qrcode-terminal';
import { moveAuthAside } from './auth.js';

const OPENAI_KEY = (() => {
  try {
    return fs.readFileSync(new URL('../.env', import.meta.url), 'utf8')
      .match(/OPENAI_API_KEY=(.+)/)[1].trim();
  } catch { return process.env.OPENAI_API_KEY; }
})();
const TRANSCRIBE_MODEL = process.env.WA_TRANSCRIBE_MODEL || 'whisper-1';
const VISION_MODEL = process.env.WA_VISION_MODEL || 'gpt-5-mini';
const LANGUAGE = process.env.WA_LANGUAGE || 'English';          // media descriptions
const TRANSCRIBE_LANG = process.env.WA_TRANSCRIBE_LANGUAGE || ''; // ISO-639-1 hint; '' = auto
const MEDIA_MAX_BYTES = 20 * 1024 * 1024;

const BRAIN = process.env.BRAIN_URL || 'http://127.0.0.1:3401';
const PORT = Number(process.env.WA_PORT || 3402);
const AUTH_DIR = process.env.WA_AUTH || './auth';
const OWNER_CHANNEL = process.env.WA_OWNER_CHANNEL || 'cli';   // logout notices
const DEBOUNCE_MS = 2000;
const POLL_MS = 2000, POLL_MAX_MS = 10 * 60 * 1000;

// -- channel policy (config-driven) -------------------------------------------
// allow.json: { "<jid>": {alias, name, number, tier} }. One brain channel per
// person: wpp:<alias>. Groups denied. The linked account's own self-chat is
// NOT a channel.
const ALLOW_FROM = JSON.parse(fs.readFileSync(
  process.env.WA_CONTACTS || new URL('./allow.json', import.meta.url)));
const GROUP_POLICY = 'deny';

const log = (...a) => console.log(new Date().toISOString(), ...a);
const sentIds = new Set();       // our own outbound ids; never re-ingest
const sentMsgs = new Map();      // id -> proto message content, for retry re-sends
const SENT_CACHE_MAX = 500;
const balog = pino({ level: process.env.WA_LOG_LEVEL || 'info' },
  pino.destination({ dest: process.env.WA_BAILEYS_LOG
    || new URL('../data/wa-baileys.log', import.meta.url).pathname, sync: false, mkdir: true }));
let selfJid = null;
let sock = null;

// -- pairing state (served on GET /qr and GET /status, localhost only) -------
const pairing = { qr: null, qrText: null, connected: false, jid: null, loggedOut: false };
let generation = 0;              // bumps on /repair; stale sockets' events are ignored

function renderQr(qr) {
  let text = null;
  qrterm.generate(qr, { small: true }, (s) => { text = s; });   // synchronous
  return text;
}

// -- brain bridge -------------------------------------------------------------
async function brainTurn(envelope, chatJid) {
  const r = await fetch(`${BRAIN}/turn`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(envelope),
  });
  const { id } = await r.json();
  const deadline = Date.now() + POLL_MAX_MS;
  let typingTimer = null;
  const stopTyping = async () => {
    if (typingTimer) {
      clearInterval(typingTimer);
      typingTimer = null;
      await sock.sendPresenceUpdate('paused', chatJid).catch(() => {});
    }
  };
  try {
    while (Date.now() < deadline) {
      await new Promise(res => setTimeout(res, POLL_MS));
      const s = await (await fetch(`${BRAIN}/turn/${id}`)).json();
      // typing starts when the turn actually holds the lock, not while queued
      if (s.status === 'running' && !typingTimer && chatJid) {
        await sock.sendPresenceUpdate('composing', chatJid).catch(() => {});
        typingTimer = setInterval(() =>
          sock.sendPresenceUpdate('composing', chatJid).catch(() => {}), 8000);
      }
      if (s.status === 'done') return s.reply;
      if (s.status === 'error') { log('brain error:', s.error); return null; }
    }
    return null;
  } finally {
    await stopTyping();
  }
}

// -- inbound: debounce per chat ----------------------------------------------
const buffers = new Map();       // chatJid -> {texts, timer, sender, tier}

function enqueue(chatJid, who, text, msgId) {
  const buf = buffers.get(chatJid) || {
    texts: [], ids: [], timer: null, t0: Date.now(),
    sender: who.name, tier: who.tier, alias: who.alias,
  };
  buf.texts.push(text);
  if (msgId) buf.ids.push(msgId);
  clearTimeout(buf.timer);
  buf.timer = setTimeout(() => flush(chatJid), DEBOUNCE_MS);
  buffers.set(chatJid, buf);
}

async function flush(chatJid) {
  const buf = buffers.get(chatJid);
  buffers.delete(chatJid);
  if (!buf) return;
  const text = buf.texts.join('\n');
  log(`[in] ${buf.sender}: ${text.slice(0, 80)}`);
  const reply = await brainTurn({
    // one channel per PERSON, alias-keyed; numbers stay in allow.json
    channel: `wpp:${buf.alias}`,
    sender: buf.sender, tier: buf.tier, text,
    // WhatsApp message id(s) of this debounced batch, for brain-side dedup
    provider_id: buf.ids.length ? `wa:${buf.ids.join(',')}` : undefined,
  }, chatJid);
  if (reply) {
    try { await send(resolveJid(chatJid), reply); }
    catch (e) { log(`[out] FAILED reply to ${chatJid}:`, e.message); }
  }
  log(`[timing] received -> answered in ${((Date.now() - buf.t0) / 1000).toFixed(1)}s`
      + ` (includes ${DEBOUNCE_MS / 1000}s debounce)`);
}

// -- media -> text (voice notes, images, PDFs feed the same envelope) ---------
function parseResponses(j) {
  if (j.output_text) return j.output_text;
  return (j.output || []).flatMap(o => o.content || [])
    .filter(c => c.type === 'output_text').map(c => c.text).join('')
    || `(model error: ${j.error?.message || 'empty output'})`;
}

async function visionCall(content) {
  const r = await fetch('https://api.openai.com/v1/responses', {
    method: 'POST',
    headers: { Authorization: `Bearer ${OPENAI_KEY}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: VISION_MODEL, input: [{ role: 'user', content }] }),
  });
  return parseResponses(await r.json());
}

async function mediaText(m) {
  const msg = m.message || {};
  const kind = msg.audioMessage ? 'audio' : msg.imageMessage ? 'image'
    : msg.documentMessage ? 'document' : null;
  if (!kind) return null;
  const meta = msg.audioMessage || msg.imageMessage || msg.documentMessage;
  if (Number(meta.fileLength || 0) > MEDIA_MAX_BYTES)
    return `[${kind}] (too large to process)`;
  const buf = await downloadMediaMessage(m, 'buffer', {},
    { logger: pino({ level: 'silent' }), reuploadRequest: sock.updateMediaMessage });
  const caption = (msg.imageMessage?.caption || msg.documentMessage?.caption || '').trim();
  const suffix = caption ? `\n[caption] ${caption}` : '';

  if (kind === 'audio') {
    const fd = new FormData();
    fd.append('file', new Blob([buf], { type: meta.mimetype || 'audio/ogg' }), 'note.ogg');
    fd.append('model', TRANSCRIBE_MODEL);
    if (TRANSCRIBE_LANG) fd.append('language', TRANSCRIBE_LANG);
    const r = await fetch('https://api.openai.com/v1/audio/transcriptions', {
      method: 'POST', headers: { Authorization: `Bearer ${OPENAI_KEY}` }, body: fd,
    });
    const j = await r.json();
    return `[voice note] ${j.text || `(transcription error: ${j.error?.message || '?'})`}`;
  }
  if (kind === 'image') {
    const desc = await visionCall([
      { type: 'input_text', text: `Describe this image concisely in ${LANGUAGE}: content, and any visible text verbatim.` },
      { type: 'input_image', image_url: `data:${meta.mimetype || 'image/jpeg'};base64,${buf.toString('base64')}` },
    ]);
    return `[image] ${desc}${suffix}`;
  }
  const name = meta.fileName || 'file';
  if ((meta.mimetype || '').includes('pdf')) {
    const extract = await visionCall([
      { type: 'input_text', text: `Extract the essential content of this PDF, concise, in ${LANGUAGE}.` },
      { type: 'input_file', filename: name, file_data: `data:application/pdf;base64,${buf.toString('base64')}` },
    ]);
    return `[pdf ${name}] ${extract}${suffix}`;
  }
  return `[document ${name}] (unsupported type: ${meta.mimetype})${suffix}`;
}

async function send(jid, text) {
  const r = await sock.sendMessage(jid, { text });
  if (r?.key?.id) {
    sentIds.add(r.key.id);
    if (r.message) {
      sentMsgs.set(r.key.id, r.message);
      if (sentMsgs.size > SENT_CACHE_MAX) sentMsgs.delete(sentMsgs.keys().next().value);
    }
  }
  log(`[out] -> ${jid}: ${text.slice(0, 80)}`);
}

// -- whatsapp connection -------------------------------------------------------
async function connect() {
  const gen = generation;
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  sock = makeWASocket({
    version, auth: state, logger: balog,
    markOnlineOnConnect: false,
    // called by Baileys when a recipient asks for a re-send (retry receipt)
    getMessage: async (key) => {
      const cached = sentMsgs.get(key.id);
      log(`[retry] re-send requested by ${key.remoteJid} for ${key.id}: `
          + (cached ? 'found in cache, re-sending' : 'NOT in cache (cannot re-send)'));
      return cached;
    },
  });
  sock.ev.on('creds.update', saveCreds);

  const me = sock;
  sock.ev.on('connection.update', (u) => {
    if (gen !== generation) return;             // superseded by /repair
    if (u.qr) {
      pairing.qr = u.qr;
      pairing.qrText = renderQr(u.qr);
      log('QR needed — scan below (also served on GET /qr):\n' + pairing.qrText);
    }
    if (u.connection === 'open') {
      selfJid = me.user.id.split(':')[0] + '@s.whatsapp.net';
      Object.assign(pairing, { qr: null, qrText: null, connected: true,
                               jid: me.user.id, loggedOut: false });
      log(`connected as ${me.user.id}; `
          + `channels: ${Object.keys(ALLOW_FROM).length} allowlisted`);
    }
    if (u.connection === 'close') {
      const code = u.lastDisconnect?.error?.output?.statusCode;
      pairing.connected = false;
      log('connection closed, code', code);
      // 515 (restartRequired) right after a QR scan is normal: reconnect.
      if (code !== DisconnectReason.loggedOut) setTimeout(() => {
        if (gen === generation) connect();
      }, 3000);
      else onLoggedOut();
    }
  });

  sock.ev.on('messages.upsert', ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const m of messages) {
      const jid = m.key.remoteJid;
      if (!jid || jid.endsWith('@g.us')) continue;           // groups: deny
      if (m.key.fromMe) continue;                            // our own sends
      const who = ALLOW_FROM[jid];
      if (!who) { log(`[drop] non-allowlisted ${jid}`); continue; }
      const text = m.message?.conversation
        || m.message?.extendedTextMessage?.text || '';
      if (text) {
        sock.readMessages([m.key]).catch(e => log('readMessages failed:', e));
        enqueue(jid, who, text, m.key.id);
        continue;
      }
      const kind = m.message?.audioMessage ? 'audio'
        : m.message?.imageMessage ? 'image'
        : m.message?.documentMessage ? 'document' : null;
      if (!kind) continue;
      sock.readMessages([m.key]).catch(e => log('readMessages failed:', e));
      (async () => {   // download + model call take seconds; keep upsert loop hot
        try {
          const t = await mediaText(m);
          if (t) { log(`[media] ${kind}: ${t.slice(0, 80)}`); enqueue(jid, who, t, m.key.id); }
        } catch (e) { log(`[media] ${kind} failed:`, e); }
      })();
    }
  });
}

// -- outbound HTTP for the brain ----------------------------------------------
// LID-only sending: alias, bare number, @s.whatsapp.net or
// @lid all resolve to the contact's @lid from allow.json. No fallback to the
// bare number — those sends never decrypt on the phone.
function resolveJid(to) {
  if (to.endsWith('@lid')) return to;
  const number = to.includes('@') ? to.split('@')[0] : to;
  const lid = Object.entries(ALLOW_FROM).find(([jid, w]) =>
    jid.endsWith('@lid') && (w.alias === to || w.number === number));
  if (!lid) throw new Error(`no @lid known for "${to}" (sends are LID-only; add it to allow.json)`);
  return lid[0];
}

// -- logout + re-pairing -------------------------------------------------------
// On logout the sidecar stays up: it tells the brain (which tells the owner on
// WA_OWNER_CHANNEL) and waits for POST /repair, normally sent by the brain's
// owner-only whatsapp_pair tool.
async function onLoggedOut() {
  Object.assign(pairing, { connected: false, jid: null, qr: null, qrText: null,
                           loggedOut: true });
  selfJid = null;
  log(`LOGGED OUT — waiting for POST /repair (the brain's whatsapp_pair tool); `
      + `${AUTH_DIR} is moved aside then, never deleted`);
  try {
    await fetch(`${BRAIN}/turn`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channel: 'system', sender: 'whatsapp sidecar', tier: 'owner',
        text: `[WHATSAPP LOGGED OUT] Use send_to on channel ${OWNER_CHANNEL} `
            + 'to tell the owner exactly: "WhatsApp logged out — ask me to pair".',
      }),
    });
  } catch (e) { log('logout notice to brain failed:', e.message); }
}

async function repair() {
  generation++;                                   // silence the old socket
  try { sock?.end?.(new Error('repair requested')); } catch {}
  const moved = moveAuthAside(AUTH_DIR);
  log(`[repair] ${moved ? `moved ${AUTH_DIR} -> ${moved}` : 'no auth dir to move'}; `
      + 'starting a fresh session (QRs follow)');
  Object.assign(pairing, { qr: null, qrText: null, connected: false, jid: null,
                           loggedOut: false });
  selfJid = null;
  connect().catch(e => log('[repair] connect failed:', e));
  return moved;
}

function json(res, code, obj) {
  res.writeHead(code, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(obj));
}

http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/status') {
    return json(res, 200, { connected: pairing.connected, jid: pairing.jid,
                            loggedOut: pairing.loggedOut });
  }
  if (req.method === 'GET' && req.url === '/qr') {
    if (pairing.connected || !pairing.qr) return json(res, 404, { error: 'no QR (paired or not ready)' });
    return json(res, 200, { qr: pairing.qr, text: pairing.qrText });
  }
  if (req.method === 'POST' && req.url === '/repair') {
    try { return json(res, 200, { ok: true, moved_to: await repair() }); }
    catch (e) { return json(res, 500, { error: String(e) }); }
  }
  if (req.method !== 'POST' || req.url !== '/send') { res.writeHead(404); return res.end(); }
  let body = '';
  req.on('data', c => body += c);
  req.on('end', async () => {
    try {
      const { to, text } = JSON.parse(body);
      // to: contact alias ("alice"), bare number, or full jid -> always @lid
      const jid = resolveJid(to);
      if (!selfJid) throw new Error('not connected yet');
      await send(jid, text);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
    } catch (e) {
      res.writeHead(500); res.end(JSON.stringify({ error: String(e) }));
    }
  });
}).listen(PORT, '127.0.0.1', () => log(`endpoints on 127.0.0.1:${PORT}: `
  + 'POST /send, GET /status, GET /qr, POST /repair'));

connect();
