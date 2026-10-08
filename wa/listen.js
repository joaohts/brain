// Listening on/off for the sidecar. Kept free of Baileys so it can be tested
// without opening a WhatsApp connection (node --test wa/).
//
// Off means the socket is closed (the linked-device creds stay, so turning it
// back on needs no QR) and the owner has the number to themselves. The state
// is a small JSON file so a pause survives sidecar restarts:
//   {"pausedAt": <ms>, "until": <ms> | null}     absent file = listening
import fs from 'fs';

export function loadPause(file) {
  try {
    const s = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (typeof s?.pausedAt !== 'number') return null;
    return { pausedAt: s.pausedAt, until: typeof s.until === 'number' ? s.until : null };
  } catch { return null; }
}

export function savePause(file, pause) {
  if (!pause) { fs.rmSync(file, { force: true }); return; }
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(pause));
  fs.renameSync(tmp, file);
}

// Is a stored pause over at `now`? Only a timed pause ever expires.
export function expired(pause, now = Date.now()) {
  return !!pause && pause.until !== null && now >= pause.until;
}

// Messages sent to the number while it was off were not for the brain. On
// reconnect WhatsApp replays them, so they are dropped by their timestamp
// (seconds, as Baileys gives it) against the window [from, to) in ms.
export function inWindow(window, tsSeconds) {
  if (!window || tsSeconds === undefined || tsSeconds === null) return false;
  const t = Number(tsSeconds) * 1000;
  return t >= window.from && t < window.to;
}
