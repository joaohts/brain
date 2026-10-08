// Auth-dir handling for re-pairing. Kept free of Baileys so it can be tested
// without opening a WhatsApp connection (node --test wa/*.test.mjs).
import fs from 'fs';

// Move the Baileys auth dir aside as <dir>.old-<timestamp>. Never deletes:
// an old session can still be inspected or restored by hand. Returns the new
// path, or null when there was nothing to move.
export function moveAuthAside(dir, now = new Date()) {
  if (!fs.existsSync(dir)) return null;
  const stamp = now.toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  let dest = `${dir}.old-${stamp}`;
  for (let n = 1; fs.existsSync(dest); n++) dest = `${dir}.old-${stamp}-${n}`;
  fs.renameSync(dir, dest);
  return dest;
}
