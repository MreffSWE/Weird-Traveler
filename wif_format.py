"""
Shared WIF format core — encoding/decoding for every .wif version.

Versions
--------
v0 (legacy) : magic, width(H), height(H), channels(B), compressed(B)        — 10-byte header
v1          : magic, version(B)=1, width(H), height(H), channels(B), compressed(B) — 11-byte header
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
except ImportError:                       # crypto optional: v0/v1 still work without it
    _CRYPTO = False

    class InvalidTag(Exception):
        pass

MAGIC           = b'w.if'
FLAG_COMPRESSED = 0b001
FLAG_ENCRYPTED  = 0b010
FLAG_KDF        = 0b100      # v2 header carries its own scrypt parameters
_VALID_V2_FLAGS = tuple(FLAG_ENCRYPTED | extra
                        for extra in (0, FLAG_COMPRESSED, FLAG_KDF, FLAG_KDF | FLAG_COMPRESSED))

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

def _is_valid_legacy(data: bytes) -> bool:
    """True if `data` is a well-formed legacy v0 file (used to break the rare
    tie where a v0 image's width high-byte equals a version number)."""
    if len(data) < 10:
        return False
    width, height, channels, compressed = struct.unpack(">HHBB", data[4:10])
    if channels not in (3, 4) or compressed not in (0, 1):
        return False
    body = data[10:]
    expected = width * height * channels
    if compressed == 0:
        return len(body) == expected
    try:
        return len(zlib.decompress(body)) == expected
    except zlib.error:
        return False


def detect_version(data: bytes) -> int:
    """Return 0, 1 or 2.  Detection never needs a password."""
    if data[:4] != MAGIC:
        raise ValueError("Not a .wif file")

    # v2: version byte 2 + a valid encrypted-flags byte.  The only thing that
    # could collide is a legacy image whose width high-byte is 2 (width 512-767),
    # so confirm it isn't a well-formed legacy file first.
    if (data[4] == 2 and len(data) >= 34 and data[5] in _VALID_V2_FLAGS
            and not _is_valid_legacy(data)):
        return 2

    # v1: version byte 1.  Channels sit at byte 9 (3 or 4); in a legacy file
    # byte 9 is the 0/1 compressed flag, so a 3/4 there can only be v1.
    if data[4] == 1 and len(data) >= 11 and data[9] in (3, 4) and data[10] in (0, 1):
        return 1

    return 0


def is_encrypted(data: bytes) -> bool:
    return detect_version(data) == 2


# ── encode ───────────────────────────────────────────────────────────────────

def encode(img: Image.Image, version: int = 1, compress: bool = True,
           password=None) -> bytes:
    pil, channels = _normalize(img)
    width, height = pil.size
    pixels = pil.tobytes()
    if compress:
        pixels = zlib.compress(pixels, level=6)

    if version == 2:
        if not _CRYPTO:
            raise RuntimeError("Install the 'cryptography' package to write encrypted WIF v2 files.")
        if not password:
            raise ValueError("A password is required to write a WIF v2 file.")
        flags  = FLAG_ENCRYPTED | FLAG_KDF | (FLAG_COMPRESSED if compress else 0)
        header = struct.pack(">4sBBBBB", MAGIC, 2, flags, _SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P)
        salt   = os.urandom(16)
        nonce  = os.urandom(12)
        inner  = struct.pack(">HHB", width, height, channels) + pixels
        key    = _derive_key(password, salt, 1 << _SCRYPT_LOG2_N, _SCRYPT_R, _SCRYPT_P)
        ciphertext = AESGCM(key).encrypt(nonce, inner, header)   # header (incl. params) authenticated
        return header + salt + nonce + ciphertext

    # v1
    return struct.pack(">4sBHHBB", MAGIC, 1, width, height, channels, int(compress)) + pixels


# ── decode ───────────────────────────────────────────────────────────────────

def decode(data: bytes, password=None):
    """Return (PIL.Image, meta dict).  meta has version, encrypted, compressed,
    width, height, channels.  Raises WrongPassword for encrypted files when the
    password is missing or wrong."""
    version = detect_version(data)

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
    else:
        if version == 1:
            width, height, channels, comp = struct.unpack(">HHBB", data[5:11])
            body = data[11:]
        else:
            width, height, channels, comp = struct.unpack(">HHBB", data[4:10])
            body = data[10:]
        compressed = bool(comp)
        pixels = zlib.decompress(body) if compressed else body

    expected = width * height * channels
    if len(pixels) != expected:
        raise ValueError(f"Corrupt file: expected {expected} pixel bytes, got {len(pixels)}")

    img = Image.frombytes(_MODES[channels], (width, height), pixels)
    meta = {"version": version, "encrypted": version == 2, "compressed": compressed,
            "width": width, "height": height, "channels": channels}
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
    encrypted, plus width/height for v0/v1.  For encrypted v2, width/height are
    None (the dimensions live inside the ciphertext)."""
    version = detect_version(data)
    if version == 2:
        return {"version": 2, "encrypted": True, "width": None, "height": None}
    if version == 1:
        width, height, channels, comp = struct.unpack(">HHBB", data[5:11])
    else:
        width, height, channels, comp = struct.unpack(">HHBB", data[4:10])
    return {"version": version, "encrypted": False, "width": width, "height": height,
            "channels": channels, "compressed": bool(comp)}
