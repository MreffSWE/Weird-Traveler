"""
Shared WIF format core — encoding/decoding for every .wif version.

Versions
--------
v1          : magic, version(B)=1, width(H), height(H), channels(B), compressed(B) — 11-byte header
v3          : magic, version(B)=3, flags(B), width(H), height(H), channels(B)       — 11-byte header
              flags = COMPRESSED | FILTERED; pixels are (optionally) spatially filtered, then
              (optionally) zlib'd.  Decode reverses that order: unzip, then unfilter.
v2          : magic, version(B)=2, flags(B), [log2_n(B), r(B), p(B) when flags&KDF],
              salt(16), nonce(12), AES-GCM(ciphertext+tag)

v2 is encrypted: the key is derived from a password with scrypt (per-file salt),
then the payload — width, height, channels and pixels — is sealed with
AES-256-GCM.  Nothing but the rough file size leaks without the password.

v2 is self-describing: each file stores its own scrypt cost parameters
(log2_n, r, p) in the header, which is authenticated alongside the ciphertext.
So the cost can be tuned later without breaking existing files, and a tool only
needs the file (plus the password) to decrypt it — never matching constants.
Files written before this carry no params (flags&KDF == 0) and are read with the
fixed legacy cost below.

This module never prompts for anything.  Callers pass a password (or a list of
candidate passwords) explicitly; a wrong/missing password raises WrongPassword.
"""

import os
import struct
import zlib

from PIL import Image

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.exceptions import InvalidTag
    _CRYPTO = True
except ImportError:                       # crypto optional: v1 (plain) still works without it
    _CRYPTO = False

    class InvalidTag(Exception):
        pass

try:
    import wif_filter                      # numpy-based spatial filtering for v3
    _FILTER = True
except ImportError:                        # numpy optional: only needed for filtered v3 files
    _FILTER = False

MAGIC           = b'w.if'
FLAG_COMPRESSED = 0b001
FLAG_ENCRYPTED  = 0b010
FLAG_KDF        = 0b100      # v2 header carries its own scrypt parameters
FLAG_FILTERED   = 0b1000     # v3 payload ran through PNG-style spatial filtering (wif_filter)
_VALID_V2_FLAGS = tuple(FLAG_ENCRYPTED | (FLAG_COMPRESSED * c) | (FLAG_KDF * k) | (FLAG_FILTERED * f)
                        for c in (0, 1) for k in (0, 1) for f in (0, 1))
_VALID_V3_FLAGS = (0, FLAG_COMPRESSED, FLAG_FILTERED, FLAG_COMPRESSED | FLAG_FILTERED)

# scrypt cost for NEWLY written files (stored in each file, so it can be tuned
# later without breaking existing ones).  Actual n = 2 ** log2_n.
_SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P = 14, 8, 1
# Fixed cost assumed for legacy v2 files written before params were stored.
_V2_LEGACY_KDF = (2 ** 14, 8, 1)

_MODES = {1: "L", 3: "RGB", 4: "RGBA"}


class WrongPassword(Exception):
    """Raised when an encrypted file can't be opened with the supplied password."""


# ── key derivation (cached per password+salt for the session) ──────────────────

_KEY_CACHE: dict = {}


def _derive_key(password, salt: bytes, n: int, r: int, p: int) -> bytes:
    if not _CRYPTO:
        raise RuntimeError("Install the 'cryptography' package to use encrypted WIF v2 files.")
    pw = password.encode("utf-8") if isinstance(password, str) else bytes(password)
    cache_key = (pw, salt, n, r, p)
    cached = _KEY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    key = Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(pw)
    if len(_KEY_CACHE) > 256:
        _KEY_CACHE.clear()
    _KEY_CACHE[cache_key] = key
    return key


# ── pixel helpers ──────────────────────────────────────────────────────────────

def _normalize(img: Image.Image):
    """Return (image-in-right-mode, channel-count): RGBA only if truly transparent."""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    if has_alpha:
        rgba = img.convert("RGBA")
        if rgba.getextrema()[3][0] < 255:
            return rgba, 4
        return img.convert("RGB"), 3
    return img.convert("RGB"), 3


# ── version detection ──────────────────────────────────────────────────────────

def detect_version(data: bytes) -> int:
    """Return 1, 2 or 3.  Detection never needs a password.

    Every WIF file carries a version byte at offset 4, so detection is exact —
    no heuristics (the old headerless v0 format is no longer supported)."""
    if data[:4] != MAGIC:
        raise ValueError("Not a .wif file")
    if data[4] == 2 and len(data) >= 34 and data[5] in _VALID_V2_FLAGS:
        return 2
    if data[4] == 3 and len(data) >= 11 and data[5] in _VALID_V3_FLAGS:
        return 3
    if data[4] == 1 and len(data) >= 11:
        return 1
    raise ValueError("Unsupported or corrupt WIF file (unknown version)")


def is_encrypted(data: bytes) -> bool:
    return detect_version(data) == 2


# ── encode ───────────────────────────────────────────────────────────────────

def encode(img: Image.Image, version: int = 1, compress: bool = True,
           password=None, filtered: bool = False, fast: bool = True) -> bytes:
    if filtered and not _FILTER:
        raise RuntimeError("Install numpy to use spatial filtering (wif_filter).")
    pil, channels = _normalize(img)
    width, height = pil.size
    pixels = pil.tobytes()
    if filtered:                                  # spatial prediction BEFORE zlib
        pixels = wif_filter.filter_pixels(pixels, width, height, channels, fast=fast)
    if compress:
        pixels = zlib.compress(pixels, level=6)

    if version == 2:
        if not _CRYPTO:
            raise RuntimeError("Install the 'cryptography' package to write encrypted WIF v2 files.")
        if not password:
            raise ValueError("A password is required to write a WIF v2 file.")
        flags  = (FLAG_ENCRYPTED | FLAG_KDF
                  | (FLAG_COMPRESSED if compress else 0)
                  | (FLAG_FILTERED if filtered else 0))
        header = struct.pack(">4sBBBBB", MAGIC, 2, flags, _SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P)
        salt   = os.urandom(16)
        nonce  = os.urandom(12)
        inner  = struct.pack(">HHB", width, height, channels) + pixels
        key    = _derive_key(password, salt, 1 << _SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P)
        ciphertext = AESGCM(key).encrypt(nonce, inner, header)   # header (incl. params) authenticated
        return header + salt + nonce + ciphertext

    if filtered or version == 3:
        flags = (FLAG_COMPRESSED if compress else 0) | (FLAG_FILTERED if filtered else 0)
        return struct.pack(">4sBBHHB", MAGIC, 3, flags, width, height, channels) + pixels

    # v1
    return struct.pack(">4sBHHBB", MAGIC, 1, width, height, channels, int(compress)) + pixels


# ── decode ───────────────────────────────────────────────────────────────────

def decode(data: bytes, password=None):
    """Return (PIL.Image, meta dict).  meta has version, encrypted, compressed,
    filtered, width, height, channels.  Raises WrongPassword for encrypted files
    when the password is missing or wrong."""
    version = detect_version(data)
    filtered = False

    if version == 2:
        if not _CRYPTO:
            raise RuntimeError("Install the 'cryptography' package to open encrypted WIF v2 files.")
        if password is None:
            raise WrongPassword("password required")
        flags      = data[5]
        compressed = bool(flags & FLAG_COMPRESSED)
        if flags & FLAG_KDF:                       # self-describing: params in header
            log2_n, r, p = data[6], data[7], data[8]
            n          = 1 << log2_n
            header     = data[:9]
            salt       = data[9:25]
            nonce      = data[25:37]
            ciphertext = data[37:]
        else:                                      # legacy v2: fixed cost, shorter header
            n, r, p    = _V2_LEGACY_KDF
            header     = data[:6]
            salt       = data[6:22]
            nonce      = data[22:34]
            ciphertext = data[34:]
        key = _derive_key(password, salt, n, r, p)
        try:
            inner = AESGCM(key).decrypt(nonce, ciphertext, header)
        except InvalidTag:
            raise WrongPassword("wrong password")
        width, height, channels = struct.unpack(">HHB", inner[:5])
        pixels = inner[5:]
        if compressed:
            pixels = zlib.decompress(pixels)
        filtered = bool(flags & FLAG_FILTERED)
        if filtered:
            if not _FILTER:
                raise RuntimeError("Install numpy to open spatially filtered WIF files.")
            pixels = wif_filter.unfilter_pixels(pixels, width, height, channels)
    elif version == 3:   # plain, optionally spatially filtered
        flags      = data[5]
        compressed = bool(flags & FLAG_COMPRESSED)
        filtered   = bool(flags & FLAG_FILTERED)
        width, height, channels = struct.unpack(">HHB", data[6:11])
        pixels = data[11:]
        if compressed:
            pixels = zlib.decompress(pixels)
        if filtered:
            if not _FILTER:
                raise RuntimeError("Install numpy to open spatially filtered (v3) WIF files.")
            pixels = wif_filter.unfilter_pixels(pixels, width, height, channels)
    else:   # v1 (plain)
        width, height, channels, comp = struct.unpack(">HHBB", data[5:11])
        body = data[11:]
        compressed = bool(comp)
        pixels = zlib.decompress(body) if compressed else body

    expected = width * height * channels
    if len(pixels) != expected:
        raise ValueError(f"Corrupt file: expected {expected} pixel bytes, got {len(pixels)}")

    img = Image.frombytes(_MODES[channels], (width, height), pixels)
    meta = {"version": version, "encrypted": version == 2, "compressed": compressed,
            "filtered": filtered, "width": width, "height": height, "channels": channels}
    return img, meta


def decode_try(data: bytes, passwords):
    """Decode, trying each password in `passwords` for encrypted files.
    Returns (img, meta, used_password).  used_password is None for unencrypted
    files.  Raises WrongPassword if none of the passwords work."""
    if not is_encrypted(data):
        img, meta = decode(data)
        return img, meta, None
    for pw in passwords:
        try:
            img, meta = decode(data, pw)
            return img, meta, pw
        except WrongPassword:
            continue
    raise WrongPassword("no matching password")


def peek(data: bytes) -> dict:
    """Cheap metadata without decryption or pixel work.  Returns version and
    encrypted, plus width/height for v1.  For encrypted v2, width/height are
    None (the dimensions live inside the ciphertext)."""
    version = detect_version(data)
    if version == 2:
        return {"version": 2, "encrypted": True, "width": None, "height": None}
    if version == 3:
        flags = data[5]
        width, height, channels = struct.unpack(">HHB", data[6:11])
        return {"version": 3, "encrypted": False, "width": width, "height": height,
                "channels": channels, "compressed": bool(flags & FLAG_COMPRESSED),
                "filtered": bool(flags & FLAG_FILTERED)}
    width, height, channels, comp = struct.unpack(">HHBB", data[5:11])
    return {"version": version, "encrypted": False, "width": width, "height": height,
            "channels": channels, "compressed": bool(comp)}
