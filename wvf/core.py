"""
wvf.core — Weird Video Format core: an *encrypted container* around an
ordinary video file (whatever codec your source already uses — H.264, HEVC, AV1…).

WVF does not compress video itself; a real codec already did that.  WVF's whole
job is the one thing mainstream video files don't offer: strong, password-based
encryption.  A .wvf holds your video bytes sealed with AES-256-GCM under a
scrypt-derived key.  Nothing but the rough file size leaks without the password —
not even the original file name or container type.

Layout (v1)
-----------
header  (plaintext, authenticated as AAD):
    magic    4   b'w.vf'
    version  1   = 1
    flags    1   bit0 ENCRYPTED, bit1 KDF-params-present
    log2_n   1   scrypt cost (n = 2**log2_n)   ─┐ present when KDF bit set
    r        1   scrypt block size              │  (self-describing, like WIF v2)
    p        1   scrypt parallelism            ─┘
    salt     16  per-file scrypt salt
    nprefix  7   per-file nonce prefix
body:
    AES-256-GCM chunks (the STREAM construction).  The encrypted plaintext is
        [meta_len: uint32][meta JSON][video bytes]
    cut into CHUNK-sized pieces.  Chunk i is sealed with
        nonce = nprefix(7) || counter:uint32(4) || last_flag(1)
    and the 32-byte header as AAD.  The per-chunk counter defeats reordering;
    the last_flag defeats truncation; the AAD pins the KDF params and salt.

The metadata (original name + container extension) lives *inside* the encrypted
stream, so neither leaks.  Chunked AEAD means arbitrarily large videos stream
through a fixed amount of memory.

The original name/container are written by whoever sealed the file, so on the
*unwrap* path they are untrusted input — a hostile .wvf could embed a name like
``..\\..\\evil``.  Restore paths must therefore go through ``safe_output_name`` /
``sanitize_container`` (below), never the raw metadata.
"""

import io
import json
import os
import re
import struct

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.exceptions import InvalidTag
    _CRYPTO = True
except ImportError:                       # the whole format needs crypto
    _CRYPTO = False

    class InvalidTag(Exception):
        pass

__all__ = [
    "wrap_file", "unwrap_file", "unwrap_stream", "wrap_bytes", "unwrap_bytes", "peek",
    "safe_output_name", "sanitize_container",
    "WVFError", "WrongPassword", "CorruptWVF", "UnsupportedVersion",
    "MAGIC", "CHUNK", "SUPPORTED_VERSIONS",
]

MAGIC          = b'w.vf'
FLAG_ENCRYPTED = 0b01
FLAG_KDF       = 0b10

# scrypt cost for newly written files (stored per-file, so it can be raised
# later without breaking old files).  n = 2 ** log2_n.
_SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P = 14, 8, 1

CHUNK = 256 * 1024        # plaintext bytes per AEAD chunk
_TAG  = 16                # AES-GCM tag length

SUPPORTED_VERSIONS = frozenset({1})

_KEY_CACHE: dict = {}


# ── exceptions ──────────────────────────────────────────────────────────────────
# A single base so host applications can `except WVFError`.  CorruptWVF and
# UnsupportedVersion also subclass ValueError so older callers that catch
# ValueError keep working.

class WVFError(Exception):
    """Base class for every error raised by the WVF container."""


class WrongPassword(WVFError):
    """Raised when a WVF file can't be opened with the supplied password(s)."""


class CorruptWVF(WVFError, ValueError):
    """Raised when a WVF file is malformed, truncated, or has been tampered with."""


class UnsupportedVersion(WVFError, ValueError):
    """Raised when the WVF version isn't understood by this reader."""


# ── untrusted-metadata sanitisation (used on the unwrap path) ───────────────────

_SAFE_EXT = re.compile(r"^[a-z0-9]{1,8}$")
_BAD_NAME_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


def sanitize_container(container) -> str:
    """Reduce an (untrusted) container/extension string to a safe bare extension.

    Returns ``'bin'`` for anything containing separators, dots, or characters
    outside ``[a-z0-9]`` — so it can never be used to build a traversal path."""
    c = str(container or "").strip().lower().lstrip(".")
    return c if _SAFE_EXT.match(c) else "bin"


def _safe_basename(name):
    """A bare filename (no directory component, no traversal) or None.

    Cross-platform: backslashes are treated as separators even off Windows, so a
    name like ``..\\..\\evil`` collapses to ``evil`` rather than escaping."""
    if not name:
        return None
    base = os.path.basename(str(name).replace("\\", "/").rstrip("/")).strip()
    if not base or base in (".", "..") or "/" in base:
        return None
    if any(ch in _BAD_NAME_CHARS for ch in base):
        return None
    return base


def safe_output_name(meta, fallback_stem) -> str:
    """A safe filename to restore an unwrapped video to, from *untrusted* `meta`.

    Always a bare filename (no directory, no traversal).  Falls back to
    ``<fallback_stem>.<container>`` when the embedded name is missing or unsafe."""
    base = _safe_basename(meta.get("name"))
    if base:
        return base
    return f"{fallback_stem}.{sanitize_container(meta.get('container'))}"


# ── key derivation (cached per password+salt+cost for the session) ─────────────

def _derive_key(password, salt, n, r, p):
    if not _CRYPTO:
        raise RuntimeError("Install the 'cryptography' package to use WVF files.")
    pw = password.encode("utf-8") if isinstance(password, str) else bytes(password)
    ck = (pw, salt, n, r, p)
    cached = _KEY_CACHE.get(ck)
    if cached is not None:
        return cached
    key = Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(pw)
    if len(_KEY_CACHE) > 64:
        _KEY_CACHE.clear()
    _KEY_CACHE[ck] = key
    return key


def _nonce(nprefix, counter, last):
    return nprefix + struct.pack(">I", counter) + (b"\x01" if last else b"\x00")


def _pwlist(password, passwords):
    if passwords:
        return list(passwords)
    if password is None:
        raise WrongPassword("password required")
    return [password]


# ── chunking ───────────────────────────────────────────────────────────────────

def _plaintext_chunks(prefix, fin):
    """Yield (chunk, is_last) over `prefix` followed by everything left in `fin`,
    in CHUNK-sized pieces.  The final piece (<= CHUNK, or exactly CHUNK at EOF)
    is flagged last so truncation can be detected on decrypt."""
    buf = bytearray(prefix)
    eof = False
    while True:
        while len(buf) <= CHUNK and not eof:
            block = fin.read(CHUNK)
            if block:
                buf += block
            else:
                eof = True
        if len(buf) <= CHUNK:
            yield bytes(buf), True
            return
        yield bytes(buf[:CHUNK]), False
        del buf[:CHUNK]


# ── core stream encrypt / decrypt ──────────────────────────────────────────────

def _wrap(fin, fout, password, name, container, progress=None):
    if not _CRYPTO:
        raise RuntimeError("Install the 'cryptography' package to write WVF files.")
    if not password:
        raise ValueError("A password is required to write a WVF file.")
    meta = json.dumps({"name": name, "container": container},
                      separators=(",", ":")).encode("utf-8")
    prefix = struct.pack(">I", len(meta)) + meta

    salt    = os.urandom(16)
    nprefix = os.urandom(7)
    key     = _derive_key(password, salt, 1 << _SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P)
    aes     = AESGCM(key)
    flags   = FLAG_ENCRYPTED | FLAG_KDF
    header  = MAGIC + bytes([1, flags, _SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P]) + salt + nprefix

    fout.write(header)
    counter = produced = 0
    for chunk, last in _plaintext_chunks(prefix, fin):
        fout.write(aes.encrypt(_nonce(nprefix, counter, last), chunk, header))
        counter += 1
        produced += len(chunk)
        if progress:
            progress(produced)


def _read_header(fin):
    head = fin.read(6)                     # magic(4) + version(1) + flags(1)
    if len(head) < 6 or head[:4] != MAGIC:
        raise CorruptWVF("Not a .wvf file")
    version, flags = head[4], head[5]
    if flags & FLAG_KDF:
        params = fin.read(3)               # log2_n, r, p
        if len(params) < 3:
            raise CorruptWVF("Truncated WVF header (KDF params)")
        n, r, p = 1 << params[0], params[1], params[2]
        rest = fin.read(16 + 7)
        header = head + params + rest
    else:
        n, r, p = 1 << _SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P
        rest = fin.read(16 + 7)
        header = head + rest
    if len(rest) < 23:
        raise CorruptWVF("Truncated WVF header (salt/nonce)")
    salt, nprefix = rest[:16], rest[16:23]
    return header, version, flags, n, r, p, salt, nprefix


def _unwrap(fin, fout, passwords, progress=None):
    header, version, flags, n, r, p, salt, nprefix = _read_header(fin)
    if version not in SUPPORTED_VERSIONS:
        raise UnsupportedVersion(f"Unsupported WVF version {version}")

    CT = CHUNK + _TAG
    counter = produced = 0
    aes = None
    meta = None
    buf = b""

    prev = fin.read(CT)
    while True:
        cur = fin.read(CT)
        last = (cur == b"")
        nonce = _nonce(nprefix, counter, last)

        if aes is None:                    # first chunk: discover the password
            pt = None
            for pw in passwords:
                try:
                    cand = AESGCM(_derive_key(pw, salt, n, r, p))
                    pt = cand.decrypt(nonce, prev, header)
                    aes = cand
                    break
                except InvalidTag:
                    continue
            if aes is None:
                # AEAD can't tell a wrong key from a corrupt first chunk: both
                # surface as InvalidTag.  Report the common case but name both.
                raise WrongPassword(
                    "no matching password (or the first chunk is corrupt)")
        else:
            try:
                pt = aes.decrypt(nonce, prev, header)
            except InvalidTag:
                raise CorruptWVF("Corrupt or tampered WVF data")

        if meta is None:                   # strip the [len][meta] prefix
            buf += pt
            if len(buf) >= 4:
                mlen = struct.unpack(">I", buf[:4])[0]
                if len(buf) >= 4 + mlen:
                    meta = json.loads(buf[4:4 + mlen].decode("utf-8"))
                    fout.write(buf[4 + mlen:])
                    buf = b""
        else:
            fout.write(pt)

        produced += len(pt)
        if progress:
            progress(produced)
        counter += 1
        if last:
            break
        prev = cur

    if meta is None:
        raise CorruptWVF("Corrupt WVF (metadata incomplete)")
    return meta


# ── public API: files and bytes ────────────────────────────────────────────────

def wrap_file(src, dst, password, *, name=None, container=None, progress=None):
    """Encrypt the video file `src` into a .wvf at `dst`.  Returns the meta dict."""
    src, dst = str(src), str(dst)
    name = name or os.path.basename(src)
    container = container or (os.path.splitext(src)[1].lstrip(".").lower() or "bin")
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        _wrap(fin, fout, password, name, container, progress)
    return {"name": name, "container": container}


def unwrap_file(src, dst, password=None, passwords=None, progress=None):
    """Decrypt a .wvf at `src` into the original video at `dst`.  Returns meta."""
    with open(str(src), "rb") as fin, open(str(dst), "wb") as fout:
        return _unwrap(fin, fout, _pwlist(password, passwords), progress)


def unwrap_stream(src, fout, password=None, passwords=None, progress=None):
    """Decrypt a .wvf at `src`, writing the plaintext video to the binary
    file-like `fout` (e.g. a subprocess's stdin), streaming in fixed memory.

    Returns meta.  Use this to play a clip without ever writing the decrypted
    video to disk.  `fout` is not closed here — the caller owns it."""
    with open(str(src), "rb") as fin:
        return _unwrap(fin, fout, _pwlist(password, passwords), progress)


def wrap_bytes(data, password, *, name="clip", container="bin"):
    out = io.BytesIO()
    _wrap(io.BytesIO(data), out, password, name, container)
    return out.getvalue()


def unwrap_bytes(wvf, password=None, passwords=None):
    out = io.BytesIO()
    meta = _unwrap(io.BytesIO(wvf), out, _pwlist(password, passwords))
    return meta, out.getvalue()


def peek(path):
    """Cheap header-only info (no password).  Original name/type stay secret —
    they live inside the ciphertext."""
    with open(str(path), "rb") as f:
        head = f.read(6)
    if len(head) < 6 or head[:4] != MAGIC:
        raise CorruptWVF("Not a .wvf file")
    version = head[4]
    return {"format": "wvf", "version": version,
            "supported": version in SUPPORTED_VERSIONS,
            "encrypted": bool(head[5] & FLAG_ENCRYPTED),
            "file_size": os.path.getsize(str(path))}
