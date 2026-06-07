# WIF Image Format Specification

WIF (`.wif`) is a simple binary image format. It stores raw RGBA, RGB, or
grayscale pixel data — optionally zlib-compressed — and supports optional
AES-256-GCM encryption.

---

## Magic bytes

Every WIF file begins with the 4-byte signature `w.if` (ASCII, no null terminator).

---

## Versions

Three versions exist. Version detection is done by inspecting the byte at
offset 4 (the byte immediately after the magic), with some fallback logic to
distinguish v0 from v1/v2 when the high byte of a v0 width could be 1 or 2.

| Version | Description |
|---------|-------------|
| **v0** | Legacy — 10-byte header, no version field |
| **v1** | Current unencrypted format — 11-byte header |
| **v2** | Encrypted — variable-length header (no plaintext dimensions) |

---

## v1 — Unencrypted (current)

### Header (11 bytes, big-endian)

| Offset | Size | Type | Value |
|--------|------|------|-------|
| 0 | 4 | bytes | Magic: `w.if` |
| 4 | 1 | uint8 | Version: `1` |
| 5 | 2 | uint16 | Image width in pixels |
| 7 | 2 | uint16 | Image height in pixels |
| 9 | 1 | uint8 | Channel count: `1` (L), `3` (RGB), or `4` (RGBA) |
| 10 | 1 | uint8 | Compressed flag: `1` = zlib-compressed, `0` = raw |

### Body

Immediately after the header: pixel bytes for the image, row-major, top to bottom.

- **Raw** (`compressed=0`): exactly `width × height × channels` bytes.
- **Compressed** (`compressed=1`): the same pixel bytes, compressed with
  `zlib.compress(pixels, level=6)`. Decompress with `zlib.decompress()` to get
  the raw pixels.

Struct pattern: `">4sBHHBB"` followed by body.

---

## v0 — Legacy

Same as v1 but without the version byte. Byte 4 is the high byte of the image
width (not a version indicator).

### Header (10 bytes, big-endian)

| Offset | Size | Type | Value |
|--------|------|------|-------|
| 0 | 4 | bytes | Magic: `w.if` |
| 4 | 2 | uint16 | Image width in pixels |
| 6 | 2 | uint16 | Image height in pixels |
| 8 | 1 | uint8 | Channel count: `3` (RGB) or `4` (RGBA) |
| 9 | 1 | uint8 | Compressed flag: `1` = compressed, `0` = raw |

Body layout is identical to v1.

---

## v2 — Encrypted

All image metadata (dimensions, channels) and all pixel data are sealed inside
an AES-256-GCM ciphertext. Only the rough file size leaks without the password.

### Header layout

v2 is **self-describing**: each file stores its own scrypt cost parameters, so
the cost can be tuned later without breaking existing files. Files written
before this feature carry no parameters (the KDF flag is clear) and are read
with the fixed legacy cost shown below.

| Offset | Size | Type | Value |
|--------|------|------|-------|
| 0 | 4 | bytes | Magic: `w.if` |
| 4 | 1 | uint8 | Version: `2` |
| 5 | 1 | uint8 | Flags (see below) |
| 6 | 1 | uint8 | scrypt `log2_n` — **only when the KDF flag is set** |
| 7 | 1 | uint8 | scrypt `r` — **only when the KDF flag is set** |
| 8 | 1 | uint8 | scrypt `p` — **only when the KDF flag is set** |
| 6 / 9 | 16 | bytes | Per-file random salt |
| 22 / 25 | 12 | bytes | Per-file random nonce (AES-GCM) |
| 34 / 37 | variable | bytes | AES-GCM ciphertext + 16-byte authentication tag |

When the KDF flag is set (all modern files) the salt/nonce/ciphertext shift 3
bytes later to make room for the parameter bytes — salt at 9, nonce at 25,
ciphertext at 37. Legacy v2 files (KDF flag clear) place salt at 6, nonce at 22,
ciphertext at 34.

### Flags byte (offset 5)

| Bit | Meaning |
|-----|---------|
| `0b001` | Pixels inside the plaintext are zlib-compressed |
| `0b010` | File is encrypted (always set in v2) |
| `0b100` | Header carries scrypt parameters (`log2_n`, `r`, `p`) |

Valid values: `0b010`, `0b011`, `0b110`, `0b111`. New files always set the
encrypted **and** KDF bits (`0b110`, or `0b111` when compressed).

### Key derivation

The 256-bit AES key is derived from the password with **scrypt**. New files read
the parameters from the header; legacy files use the fixed defaults:

| Parameter | New files (from header) | Legacy default |
|-----------|-------------------------|----------------|
| n (CPU/memory cost) | `2 ** log2_n` (default `log2_n = 14` → 16384) | 16384 |
| r (block size) | `r` byte (default 8) | 8 |
| p (parallelism) | `p` byte (default 1) | 1 |
| key length | 32 bytes | 32 bytes |
| salt | 16 random bytes from the header | 16 bytes |

Password is UTF-8 encoded before derivation. Because the cost lives in the file,
a tool only needs the file plus the password to decrypt it — never a matching
hard-coded constant.

### Encryption

Algorithm: **AES-256-GCM**

- **Nonce**: 12 random bytes from the header.
- **AAD** (authenticated additional data): the entire plaintext header —
  `magic + version + flags`, plus the 3 scrypt-parameter bytes when present
  (offsets 0–8 for modern files, 0–5 for legacy). This authenticates the header,
  including the KDF cost, so it cannot be tampered with to weaken the derivation.
- **Plaintext** (`inner`): `width(2) | height(2) | channels(1) | pixels`
  — big-endian, pixels optionally zlib-compressed per the compressed flag.
- **Output**: ciphertext + 16-byte GCM authentication tag.

Decryption failure (wrong key or tampered header) raises `InvalidTag` from the
`cryptography` library, mapped to `WrongPassword` by `wif_format.py`.

### v2 inner plaintext layout

| Offset | Size | Type | Value |
|--------|------|------|-------|
| 0 | 2 | uint16 (BE) | Image width |
| 2 | 2 | uint16 (BE) | Image height |
| 4 | 1 | uint8 | Channel count: `1`, `3`, or `4` |
| 5 | variable | bytes | Pixel data (raw or zlib-compressed) |

---

## Pixel data

Pixels are packed row-major (left to right, top to bottom). Each channel is
one byte (0–255).

| Channel count | Pillow mode | Bytes per pixel |
|---------------|-------------|-----------------|
| 1 | `L` (grayscale) | 1 |
| 3 | `RGB` | 3 |
| 4 | `RGBA` | 4 |

### Automatic mode selection (encoder)

The encoder in `wif_format.py` normalizes the image before writing:

1. If the image has a real alpha channel **and** at least one pixel is not fully
   opaque (`alpha < 255`), store as **RGBA** (4 channels).
2. Otherwise store as **RGB** (3 channels), discarding the alpha channel.

---

## Python quick reference

All encoding and decoding lives in [`wif_format.py`](wif_format.py).

```python
import wif_format
from PIL import Image

# Encode
img  = Image.open("photo.png")
data = wif_format.encode(img, version=1, compress=True)           # plain v1
data = wif_format.encode(img, version=2, compress=True, password="secret")  # encrypted v2

# Decode
img, meta = wif_format.decode(data)               # unencrypted
img, meta = wif_format.decode(data, "secret")     # encrypted

# meta keys: version, encrypted, compressed, width, height, channels

# Cheap metadata without decryption
info = wif_format.peek(data)    # {version, encrypted, width, height, channels, compressed}
                                 # width/height are None for v2

# Check encryption
wif_format.is_encrypted(data)   # True / False

# Try multiple passwords (useful for browser/viewer sessions)
img, meta, used_pw = wif_format.decode_try(data, ["pw1", "pw2"])
```