"""Owner-only tools over an Obsidian vault on this host ([vault] integration).

The vault is a directory of Markdown notes, usually a git checkout that some
other process syncs. These tools never run git: they read and write files,
and sync stays someone else's job.

Confinement. Every path the model passes is relative to the vault root. It is
walked one component at a time with O_NOFOLLOW from a directory fd opened on
the root, so neither "..", an absolute path, nor a symlink (planted before or
during the call) can reach outside. Reads may follow a symlink whose real
target is still inside the vault (e.g. AGENTS.md -> CLAUDE.md); writes never
go through a symlink. Hidden components (.git, .obsidian, ...) are invisible,
`read_only` prefixes can be read but not written, and `deny` globs can't be
touched at all. Only `.md` notes are read or written.

Writes. A note is written to a temp file in the same directory, fsynced, and
renamed over the target, so a reader never sees half a note. Concurrent
edits are caught optimistically: vault_read returns the note's sha256, a
whole-note overwrite must pass it back as base_sha256, and every write
re-checks the file just before the rename. A per-process lock serializes the
brain's own writes."""

import errno
import fnmatch
import hashlib
import os
import stat
import threading
import time
import unicodedata

NOTE_EXT = ".md"
_write_lock = threading.Lock()


class VaultError(Exception):
    """A refusal or failure worth showing to the model verbatim."""


# -- paths ----------------------------------------------------------------------

def _parts(rel: str) -> list[str]:
    """Validated components of a vault-relative path ('' = the root)."""
    if not isinstance(rel, str):
        raise VaultError("path must be a string")
    rel = unicodedata.normalize("NFC", rel.strip().replace("\\", "/"))
    if "\x00" in rel:
        raise VaultError("invalid path")
    if rel.startswith("/") or rel.startswith("~"):
        raise VaultError("paths are relative to the vault root")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    for p in parts:
        if p == "..":
            raise VaultError("'..' is not allowed in vault paths")
        if p.startswith("."):
            raise VaultError("hidden files and folders are off limits")
    return parts


def _rel(parts) -> str:
    return "/".join(parts)


def _check_policy(cfg_v, parts, write: bool):
    rel = _rel(parts)
    for pat in cfg_v["deny"]:
        if fnmatch.fnmatchcase(rel, pat) or any(
                fnmatch.fnmatchcase(_rel(parts[:i]), pat)
                for i in range(1, len(parts))):
            raise VaultError(f"{rel} is off limits")
    if write:
        for pre in cfg_v["read_only"]:
            pre = pre.strip("/")
            if pre and (rel == pre or rel.startswith(pre + "/")):
                raise VaultError(f"{pre}/ is read-only")


def _open_root(cfg_v) -> int:
    try:
        return os.open(cfg_v["path"], os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise VaultError(f"vault unavailable: {e.strerror}")


def _walk_dirs(root_fd: int, parts, create=False) -> int:
    """fd of the directory parts name, walked without following symlinks.
    The caller closes it. create=True makes missing directories."""
    fd = os.dup(root_fd)
    try:
        for p in parts:
            try:
                nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise VaultError(f"no such folder: {p}")
                os.mkdir(p, 0o775, dir_fd=fd)
                nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise VaultError(f"{p} is a link or not a folder")
                raise
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _real_parts(cfg_v, parts) -> list[str]:
    """Read side: resolve symlinks, and insist the real target is a visible,
    allowed path inside the vault."""
    root = os.path.realpath(cfg_v["path"])
    real = os.path.realpath(os.path.join(root, *parts))
    if real != root and not real.startswith(root + os.sep):
        raise VaultError(f"{_rel(parts)} points outside the vault")
    rp = [] if real == root else os.path.relpath(real, root).split(os.sep)
    if any(p.startswith(".") for p in rp):
        raise VaultError("hidden files and folders are off limits")
    return rp


def _note_parts(cfg_v, path, write: bool) -> list[str]:
    parts = _parts(path)
    if not parts:
        raise VaultError("a note path is required")
    if not parts[-1].endswith(NOTE_EXT):
        if write:
            raise VaultError(f"only {NOTE_EXT} notes can be written")
        if not parts[-1].lower().endswith(NOTE_EXT):
            raise VaultError(f"only {NOTE_EXT} notes can be read")
    _check_policy(cfg_v, parts, write)
    return parts


# -- reading --------------------------------------------------------------------

def _read_at(dir_fd: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise VaultError(f"{name} is not a regular file")
        if st.st_size > limit:
            raise VaultError(f"{name} is too large ({st.st_size} bytes, "
                             f"limit {limit})")
        with os.fdopen(os.dup(fd), "rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise VaultError(f"{name} is too large (limit {limit} bytes)")
        return data
    finally:
        os.close(fd)


def _load(cfg_v, parts) -> bytes | None:
    """Current bytes of a note, or None if it does not exist."""
    root = _open_root(cfg_v)
    try:
        d = _walk_dirs(root, parts[:-1])
    except VaultError:
        os.close(root)
        return None
    try:
        return _read_at(d, parts[-1], cfg_v["max_note_bytes"])
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise VaultError(f"{_rel(parts)} is a link; links are not written")
        raise
    finally:
        os.close(d)
        os.close(root)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_note(cfg_v, path, offset=1, limit=None) -> str:
    parts = _note_parts(cfg_v, path, write=False)
    real = _real_parts(cfg_v, parts)
    if real != parts:
        _note_parts(cfg_v, _rel(real), write=False)
    root = _open_root(cfg_v)
    try:
        d = _walk_dirs(root, real[:-1])
        try:
            data = _read_at(d, real[-1], cfg_v["max_note_bytes"])
        except FileNotFoundError:
            raise VaultError(f"no such note: {_rel(parts)}")
        finally:
            os.close(d)
    finally:
        os.close(root)
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    first = max(1, int(offset or 1))
    chunk, used, last = [], 0, first - 1
    cap = cfg_v["max_read_chars"]
    n = int(limit) if limit else len(lines)
    for i in range(first - 1, min(len(lines), first - 1 + max(1, n))):
        if used + len(lines[i]) + 1 > cap and chunk:
            break
        chunk.append(lines[i][:cap])
        used += len(lines[i]) + 1
        last = i + 1
    head = (f"{_rel(parts)} · {len(lines)} lines · sha256 {sha(data)}"
            + (f" · real path {_rel(real)}" if real != parts else ""))
    if last < len(lines) or first > 1:
        head += f" · showing lines {first}-{last}"
        if last < len(lines):
            head += f" (continue with offset={last + 1})"
    return head + "\n\n" + "\n".join(chunk)


def _visible(name: str) -> bool:
    return not name.startswith(".")


def _denied(cfg_v, rel: str) -> bool:
    try:
        _check_policy(cfg_v, rel.split("/"), write=False)
        return False
    except VaultError:
        return True


def list_dir(cfg_v, path="", recursive=False) -> str:
    parts = _parts(path)
    if parts:
        _check_policy(cfg_v, parts, write=False)
    cap = cfg_v["max_results"]
    root = _open_root(cfg_v)
    out, more, stack = [], False, []
    try:
        stack.append((_walk_dirs(root, parts), parts))
        while stack:
            fd, at = stack.pop(0)
            try:
                with os.scandir(fd) as it:
                    entries = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
                for e in entries:
                    if not _visible(e.name):
                        continue
                    rel = _rel(at + [e.name])
                    if _denied(cfg_v, rel):
                        continue
                    if e.is_symlink():
                        if e.name.endswith(NOTE_EXT):
                            out.append(f"{rel} (link)")
                        continue
                    if e.is_dir(follow_symlinks=False):
                        out.append(rel + "/")
                        if recursive:
                            try:
                                stack.append((os.open(e.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd), at + [e.name]))
                            except OSError:
                                pass
                    elif e.name.endswith(NOTE_EXT):
                        st = e.stat(follow_symlinks=False)
                        out.append(f"{rel} ({st.st_size} B, "
                                   f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))})")
                    if len(out) >= cap:
                        more = True
                        break
            finally:
                os.close(fd)
            if more:
                break
    finally:
        for fd, _ in stack:
            os.close(fd)
        os.close(root)
    if not out:
        return f"(empty: {_rel(parts) or 'vault root'})"
    tail = (f"\n... (stopped at {cap} entries; list a subfolder)" if more else "")
    return "\n".join(out) + tail


def search(cfg_v, query, path="", max_results=None) -> str:
    """Case-insensitive substring search over note names and contents."""
    q = str(query or "").strip()
    if len(q) < 2:
        raise VaultError("search needs a query of at least 2 characters")
    ql = unicodedata.normalize("NFC", q).casefold()
    parts = _parts(path)
    if parts:
        _check_policy(cfg_v, parts, write=False)
    cap = max(1, min(int(max_results or cfg_v["max_results"]),
                     cfg_v["max_results"]))
    deadline = time.monotonic() + cfg_v["search_seconds"]
    root = _open_root(cfg_v)
    hits, scanned, timed_out = [], 0, False
    try:
        stack = [(_walk_dirs(root, parts), parts)]
        try:
            while stack and len(hits) < cap:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                fd, at = stack.pop()
                try:
                    with os.scandir(fd) as it:
                        entries = sorted(it, key=lambda e: e.name)
                    for e in entries:
                        if len(hits) >= cap:
                            break
                        rel = _rel(at + [e.name])
                        if not _visible(e.name) or _denied(cfg_v, rel) \
                                or e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            try:
                                stack.append((os.open(e.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd), at + [e.name]))
                            except OSError:
                                pass
                            continue
                        if not e.name.endswith(NOTE_EXT):
                            continue
                        name_hit = ql in unicodedata.normalize("NFC", e.name).casefold()
                        try:
                            data = _read_at(fd, e.name, cfg_v["max_note_bytes"])
                        except (OSError, VaultError):
                            if name_hit:
                                hits.append(f"{rel} (name match)")
                            continue
                        scanned += 1
                        lines = [f"  {i}: {ln.strip()[:200]}"
                                 for i, ln in enumerate(data.decode("utf-8", "replace").splitlines(), 1)
                                 if ql in unicodedata.normalize("NFC", ln).casefold()]
                        if name_hit or lines:
                            more = f"\n  ... +{len(lines) - 3} more" if len(lines) > 3 else ""
                            hits.append(rel + ("\n" + "\n".join(lines[:3]) + more if lines else " (name match)"))
                finally:
                    os.close(fd)
        finally:
            for fd, _ in stack:
                os.close(fd)
    finally:
        os.close(root)
    if not hits:
        note = " (time limit hit; narrow with path)" if timed_out else ""
        return f"no matches for {q!r} in {scanned} notes{note}"
    tail = ""
    if len(hits) >= cap:
        tail = f"\n... (stopped at {cap} notes; narrow the query or path)"
    elif timed_out:
        tail = "\n... (time limit hit; results incomplete, narrow with path)"
    return "\n".join(hits) + tail


# -- writing --------------------------------------------------------------------

def _atomic_write(cfg_v, parts, data: bytes, expect: bytes | None,
                  create_only: bool):
    """Replace parts with data. expect is the content the caller based its
    edit on (None = must not exist); it is re-checked right before the
    rename so a concurrent change is reported instead of clobbered."""
    root = _open_root(cfg_v)
    try:
        d = _walk_dirs(root, parts[:-1], create=create_only)
    except BaseException:
        os.close(root)
        raise
    name = parts[-1]
    tmp = f".{name}.brain-{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        mode = 0o664
        try:
            st = os.stat(name, dir_fd=d, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise VaultError(f"{_rel(parts)} is a link; links are not written")
            if not stat.S_ISREG(st.st_mode):
                raise VaultError(f"{_rel(parts)} is not a regular file")
            mode = stat.S_IMODE(st.st_mode)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     mode, dir_fd=d)
        try:
            with os.fdopen(os.dup(fd), "wb") as f:
                f.write(data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            if create_only:
                # link() fails if the name exists: no clobbering a note that
                # appeared since the caller looked
                try:
                    os.link(tmp, name, src_dir_fd=d, dst_dir_fd=d,
                            follow_symlinks=False)
                except FileExistsError:
                    raise VaultError(f"{_rel(parts)} already exists; read it "
                                     f"and edit instead")
                os.unlink(tmp, dir_fd=d)
            else:
                try:
                    now = _read_at(d, name, cfg_v["max_note_bytes"])
                except FileNotFoundError:
                    now = None
                except OSError as e:
                    if e.errno == errno.ELOOP:
                        raise VaultError(f"{_rel(parts)} is a link; links are not written")
                    raise
                if now != expect:
                    raise VaultError(
                        f"{_rel(parts)} changed while editing (now sha256 "
                        f"{sha(now) if now is not None else 'deleted'}); "
                        f"read it again and redo the edit")
                os.replace(tmp, name, src_dir_fd=d, dst_dir_fd=d)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=d)
            except FileNotFoundError:
                pass
            raise
        os.fsync(d)
    finally:
        os.close(d)
        os.close(root)


def _encode(cfg_v, text: str) -> bytes:
    if not isinstance(text, str):
        raise VaultError("content must be text")
    data = text.encode("utf-8")
    if len(data) > cfg_v["max_write_bytes"]:
        raise VaultError(f"note would be {len(data)} bytes; the limit is "
                         f"{cfg_v['max_write_bytes']}")
    return data


def create_note(cfg_v, path, content) -> str:
    parts = _note_parts(cfg_v, path, write=True)
    data = _encode(cfg_v, content)
    with _write_lock:
        _atomic_write(cfg_v, parts, data, None, create_only=True)
    return f"created {_rel(parts)} ({len(data)} bytes, sha256 {sha(data)})"


def edit_note(cfg_v, path, mode, content="", old_text="", base_sha256="") -> str:
    """mode: append | replace (old_text -> content, exactly one match) |
    overwrite (whole note; base_sha256 from vault_read required)."""
    parts = _note_parts(cfg_v, path, write=True)
    base = str(base_sha256 or "").strip().lower()
    if mode == "overwrite" and not base:
        raise VaultError("overwrite needs base_sha256 from vault_read, so a "
                         "newer version is never clobbered")
    if mode not in ("append", "replace", "overwrite"):
        raise VaultError("mode must be append, replace or overwrite")
    with _write_lock:
        cur = _load(cfg_v, parts)
        if cur is None:
            raise VaultError(f"no such note: {_rel(parts)} (use vault_create)")
        if base and sha(cur) != base:
            raise VaultError(f"{_rel(parts)} changed since it was read (now "
                             f"sha256 {sha(cur)}); read it again and redo the edit")
        try:
            text = cur.decode("utf-8")
        except UnicodeDecodeError:
            raise VaultError(f"{_rel(parts)} is not UTF-8 text; not editing it")
        if mode == "append":
            sep = "" if not text or text.endswith("\n") else "\n"
            new = text + sep + str(content)
        elif mode == "replace":
            if not old_text:
                raise VaultError("replace needs old_text")
            n = text.count(old_text)
            if n != 1:
                raise VaultError(f"old_text matches {n} times; it must match "
                                 f"exactly once (add surrounding context)")
            new = text.replace(old_text, str(content), 1)
        else:
            new = str(content)
        data = _encode(cfg_v, new)
        _atomic_write(cfg_v, parts, data, cur, create_only=False)
    return (f"saved {_rel(parts)} ({mode}; {len(data)} bytes, sha256 "
            f"{sha(data)})")
