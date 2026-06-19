"""
WIF Browser — navigate folders and browse .wif files as thumbnails.

Controls:
  Double-click folder    → enter folder
  Double-click .wif      → open in wif_viewer.py
  Double-click .wvf      → play the encrypted video (streamed via ffplay)
  Backspace / Up button  → parent folder
  Enter in path bar      → navigate to typed path
  Mouse wheel            → scroll grid
  Arrow keys             → move selection (Shift extends; Home/End = first/last)
  Enter (in grid)        → open the selected item
  Drag on empty space    → rubber-band select (Ctrl-drag adds to selection)
  Right-click            → menu (Open, Show in Explorer, Copy path, …)
"""

import atexit
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from collections import deque
from queue import Queue, Empty
from tkinter import filedialog, messagebox, simpledialog
from PIL import Image, ImageTk, ImageDraw, ImageSequence

import wif_format

# WVF — the sibling "Weird Video Format": an AES-256-GCM encrypted container
# around an ordinary video file.  Optional: the browser still runs without it
# (the .wvf features simply stay hidden).  Importing wvf.player here also lets us
# reuse its ffplay discovery + stream-into-ffplay playback (no plaintext on disk).
try:
    import wvf
    from wvf import player as wvf_player
    _WVF = True
except Exception:
    _WVF = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt  import Scrypt  as _Scrypt
    _WTAG_CRYPTO = True
except ImportError:
    _WTAG_CRYPTO = False

try:
    import pillow_avif   # registers AVIF codec with Pillow
    _AVIF = True
except ImportError:
    _AVIF = False

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    from send2trash import send2trash as _send2trash
    _TRASH = True
except ImportError:
    _TRASH = False

# ── constants ────────────────────────────────────────────────────────────────

GAP = 10

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".ico", ".avif"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"}
WEIRD_EXTS = {".wif", ".wvf"}          # this project's own formats (image + video)
MEDIA_EXTS = WEIRD_EXTS | IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

VIEWER_EXTS = {".wif"} | IMAGE_EXTS   # file types the in-browser lightbox can open
                                       # (.wvf is video — it plays, it isn't shown here)

FILTER_WIF   = "Folders + WIF/WVF"
FILTER_MEDIA = "Folders + Media"
FILTER_ALL   = "Folders + All files"
FILTERS      = [FILTER_WIF, FILTER_MEDIA, FILTER_ALL]

SORT_NAME, SORT_DATE, SORT_SIZE, SORT_TYPE = "Name", "Date", "Size", "Type"
SORT_OPTIONS = [SORT_NAME, SORT_DATE, SORT_SIZE, SORT_TYPE]

# (thumb_w, thumb_h, cell_w, cell_h)
SIZE_SMALL = (140, 110, 164, 158)
SIZE_LARGE = (260, 200, 300, 248)
HOVER_PREVIEW = (520, 460)   # max size of the hover-zoom popup image

# Lightbox filmstrip — a row of small thumbnails along the bottom of the overlay
FILM_THUMB = (72, 54)        # (w, h) of each filmstrip thumbnail
FILM_MAX   = 11              # max thumbnails shown in the strip window
FILM_BAR_H = FILM_THUMB[1] + 18   # bottom space reserved for the strip

# active size — updated by the toggle
THUMB_W, THUMB_H, CELL_W, CELL_H = SIZE_SMALL

# ── themes ───────────────────────────────────────────────────────────────────
THEMES = {
    "Light": dict(bg="#f0f0f0", fg="#1f1f1f", fg_dim="#6a6a6a",
                  cell_bg="#e3e3e3", cell_hl="#9dc3f0", cell_sel="#cfe2fb",
                  preview_bg="#dcdcdc", toolbar_bg="#e6e6e6",
                  btn_bg="#d6d6d6", btn_active="#bdbdbd", active_fg="black"),
    "Dark":  dict(bg="#1e1e1e", fg="#cccccc", fg_dim="#777777",
                  cell_bg="#2a2a2a", cell_hl="#3a5a8a", cell_sel="#2a3a5a",
                  preview_bg="#2f2f2f", toolbar_bg="#2d2d2d",
                  btn_bg="#3c3c3c", btn_active="#555555", active_fg="white"),
    # Red — derived from the Riksrevisionen band page (dark base, red accents)
    "Red":   dict(bg="#0a0a0a", fg="#dddddd", fg_dim="#888888",
                  cell_bg="#141414", cell_hl="#8c2a22", cell_sel="#5e1f19",
                  preview_bg="#1a1a1a", toolbar_bg="#111111",
                  btn_bg="#1a1a1a", btn_active="#c0392b", active_fg="#f0f0f0"),
}
THEME_NAMES   = ["Light", "Dark", "Red"]
DEFAULT_THEME = "Dark"

# Live theme colours — module globals, reassigned by _set_theme()
BG = FG = FG_DIM = CELL_BG = CELL_HL = CELL_SEL = PREVIEW_BG = ""
TOOLBAR_BG = BTN_BG = BTN_ACTIVE = ACTIVE_FG = ""


def _set_theme(name: str):
    t = THEMES.get(name, THEMES[DEFAULT_THEME])
    global BG, FG, FG_DIM, CELL_BG, CELL_HL, CELL_SEL, PREVIEW_BG
    global TOOLBAR_BG, BTN_BG, BTN_ACTIVE, ACTIVE_FG
    BG, FG, FG_DIM = t["bg"], t["fg"], t["fg_dim"]
    CELL_BG, CELL_HL, CELL_SEL, PREVIEW_BG = t["cell_bg"], t["cell_hl"], t["cell_sel"], t["preview_bg"]
    TOOLBAR_BG, BTN_BG, BTN_ACTIVE, ACTIVE_FG = t["toolbar_bg"], t["btn_bg"], t["btn_active"], t["active_fg"]


_set_theme(DEFAULT_THEME)   # initialise the colour globals at import

# When frozen as a single exe, re-spawn ourselves with --viewer <path>;
# when running from source, spawn the viewer .py with the current interpreter.
_FROZEN = getattr(sys, "frozen", False)
VIEWER  = Path(__file__).parent / "wif_viewer.py"   # source mode only


def _viewer_cmd(path: Path) -> list:
    if _FROZEN:
        return [sys.executable, "--viewer", str(path)]
    return [sys.executable, str(VIEWER), str(path)]
SCRIPT_DIR  = Path(__file__).parent
TEMP_TXT    = SCRIPT_DIR / "temp.txt"
CONFIG_PATH = SCRIPT_DIR / "wif_settings.json"
TAGS_PATH   = SCRIPT_DIR / "wif_tags.json"

# ── tag-file encryption (WTAG blob: same AES-256-GCM / scrypt as WIF v2) ─────
#
# Binary layout:  WTAG_MAGIC(4) + log2_n(1) + r(1) + p(1)   ← 7-byte header (authenticated)
#                 + salt(16) + nonce(12) + AES-GCM ciphertext+tag
#
WTAG_MAGIC             = b"WTAG"
_WTAG_N, _WTAG_R, _WTAG_P = 14, 8, 1   # scrypt cost — stored in the header so it can change later


def _wtag_encrypt(plaintext: bytes, password: str) -> bytes:
    """Encrypt plaintext bytes with AES-256-GCM; return the WTAG blob."""
    if not _WTAG_CRYPTO:
        raise RuntimeError("Install 'cryptography' to use encrypted tags.")
    pw    = password.encode("utf-8")
    salt  = os.urandom(16)
    nonce = os.urandom(12)
    hdr   = struct.pack(">4sBBB", WTAG_MAGIC, _WTAG_N, _WTAG_R, _WTAG_P)
    key   = _Scrypt(salt=salt, length=32, n=2 ** _WTAG_N, r=_WTAG_R, p=_WTAG_P).derive(pw)
    ct    = _AESGCM(key).encrypt(nonce, plaintext, hdr)   # hdr is authenticated
    return hdr + salt + nonce + ct


def _wtag_decrypt(data: bytes, password: str) -> bytes:
    """Return the decrypted plaintext, or raise wif_format.WrongPassword."""
    if len(data) < 39 or data[:4] != WTAG_MAGIC:
        raise ValueError("Not an encrypted tags file")
    if not _WTAG_CRYPTO:
        raise RuntimeError("Install 'cryptography' to use encrypted tags.")
    log2n, r, p = struct.unpack_from(">BBB", data, 4)
    hdr, salt, nonce, ct = data[:7], data[7:23], data[23:35], data[35:]
    pw  = password.encode("utf-8")
    key = _Scrypt(salt=salt, length=32, n=2 ** log2n, r=r, p=p).derive(pw)
    try:
        return _AESGCM(key).decrypt(nonce, ct, hdr)
    except Exception:
        raise wif_format.WrongPassword("wrong password")


def _wtag_is_encrypted(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == WTAG_MAGIC


# ── WVF helpers (encrypted-video container) ──────────────────────────────────
#
# A .wvf hides everything but its rough size, so the only way to know whether a
# known password unlocks it is to try decrypting.  We never want to decrypt a
# whole video just to draw a padlock, though — so we stream-decrypt into a sink
# that aborts the moment the first plaintext bytes arrive.  That proves the
# password (the first chunk's AEAD tag verified) after touching only ~256 KB,
# in memory, with nothing written to disk.

class _FirstChunkReached(Exception):
    """Signals that wvf decryption produced output — i.e. the password is good."""


class _AbortAfterFirstWrite:
    """A write-sink for wvf.unwrap_stream that stops decryption at the first
    plaintext bytes.  Lets us verify a password without decrypting the whole
    video and without ever putting plaintext on disk."""

    def write(self, _b):
        raise _FirstChunkReached

    def flush(self):
        pass


def wvf_matching_password(path: Path, passwords):
    """Return the password from `passwords` that unlocks the .wvf at `path`, or
    None if none do (or wvf/cryptography isn't available).  Cheap: decrypts only
    the first chunk, in memory — nothing is written to disk."""
    if not _WVF or not passwords:
        return None
    for pw in passwords:
        try:
            wvf.unwrap_stream(path, _AbortAfterFirstWrite(), password=pw)
            return pw            # whole (tiny) file decrypted before any write
        except _FirstChunkReached:
            return pw            # first chunk verified — this password is correct
        except wvf.WrongPassword:
            continue             # not this one — try the next
        except Exception:
            return None          # corrupt / not a .wvf / no crypto — treat as locked
    return None


# ── image helpers ────────────────────────────────────────────────────────────

def load_wif_thumbnail(path: Path, thumb_w: int, thumb_h: int, passwords) -> Image.Image:
    """Decode a .wif into a thumbnail, decrypting with a known password when
    needed.  Encrypted files get a small padlock badge; a locked file (no
    matching password) raises wif_format.WrongPassword."""
    data = path.read_bytes()
    if wif_format.is_encrypted(data):
        img, _meta, _pw = wif_format.decode_try(data, passwords)   # raises if locked
        img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
        return _lock_badge(img)
    img, _meta = wif_format.decode(data)
    img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
    return img


def make_locked_icon(thumb_w: int, thumb_h: int) -> Image.Image:
    """Placeholder shown for an encrypted .wif that no known password unlocks."""
    img = Image.new("RGBA", (thumb_w, thumb_h), (44, 44, 52, 255))
    d = ImageDraw.Draw(img)
    s = int(min(thumb_w, thumb_h) * 0.30)
    cx, cy = thumb_w // 2, thumb_h // 2 + s // 6
    d.arc([cx - s // 2, cy - s, cx + s // 2, cy], start=180, end=360,
          fill=(150, 150, 160, 255), width=max(2, s // 6))                  # shackle
    d.rounded_rectangle([cx - s // 2, cy - s // 4, cx + s // 2, cy + s // 2],
                        radius=4, fill=(210, 180, 70, 255))                  # body
    d.ellipse([cx - 2, cy + s // 16, cx + 2, cy + s // 16 + 5], fill=(60, 50, 20, 255))  # keyhole
    return img


def _lock_badge(img: Image.Image) -> Image.Image:
    """Overlay a small padlock in the corner of a decrypted thumbnail."""
    img = img.convert("RGBA")
    w, h = img.size
    s = max(12, min(w, h) // 6)
    bx, by = w - s - 3, h - s - 3
    d = ImageDraw.Draw(img)
    d.ellipse([bx - 2, by - 2, bx + s + 2, by + s + 2], fill=(0, 0, 0, 150))
    d.arc([bx + s // 4, by + s // 8, bx + 3 * s // 4, by + 5 * s // 8],
          start=180, end=360, fill=(235, 205, 80, 255), width=2)
    d.rounded_rectangle([bx + s // 4, by + s // 2, bx + 3 * s // 4, by + 7 * s // 8],
                        radius=2, fill=(235, 205, 80, 255))
    return img


def make_folder_icon(thumb_w: int, thumb_h: int) -> Image.Image:
    img = Image.new("RGBA", (thumb_w, thumb_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = thumb_w // 2, thumb_h // 2 + 4
    hw = int(thumb_w * 0.28)   # half-width scales with size
    hh = int(thumb_h * 0.25)
    d.rounded_rectangle([cx - hw, cy - hh, cx + hw, cy + int(hh * 1.5)], radius=6, fill="#c8880f")
    d.rounded_rectangle([cx - hw, cy - int(hh * 1.45), cx - int(hw * 0.1), cy - int(hh * 0.8)],
                        radius=5, fill="#e8a020")
    d.rounded_rectangle([cx - hw, cy - hh, cx + hw, cy - int(hh * 0.35)], radius=6, fill="#d9950f")
    return img


def make_error_icon(thumb_w: int, thumb_h: int) -> Image.Image:
    img = Image.new("RGBA", (thumb_w, thumb_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = thumb_w // 2, thumb_h // 2
    r = int(min(thumb_w, thumb_h) * 0.28)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline="#884444", width=2)
    d.line([cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2], fill="#884444", width=2)
    d.line([cx + r // 2, cy - r // 2, cx - r // 2, cy + r // 2], fill="#884444", width=2)
    return img


def make_audio_icon(thumb_w: int, thumb_h: int) -> Image.Image:
    img = Image.new("RGBA", (thumb_w, thumb_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = thumb_w // 2, thumb_h // 2
    s = int(min(thumb_w, thumb_h) * 0.28)
    # musical note stem
    d.rectangle([cx, cy - s, cx + s // 4, cy + s // 2], fill="#7a9fd4")
    # note head
    d.ellipse([cx - s // 3, cy + s // 3, cx + s // 2, cy + s], fill="#7a9fd4")
    # flag
    d.line([cx + s // 4, cy - s, cx + s, cy - s // 2], fill="#7a9fd4",
           width=max(2, s // 8))
    return img


def make_video_icon(thumb_w: int, thumb_h: int) -> Image.Image:
    """Fallback when cv2 is not installed."""
    img = Image.new("RGBA", (thumb_w, thumb_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = thumb_w // 2, thumb_h // 2
    s = int(min(thumb_w, thumb_h) * 0.32)
    # play triangle
    d.polygon([cx - s, cy - s, cx - s, cy + s, cx + s, cy], fill="#7a9fd4")
    return img


def _draw_padlock(d: "ImageDraw.ImageDraw", px: int, py: int, ps: int, locked: bool):
    """Draw a padlock into ImageDraw `d`, fitted to a ps×ps box at (px, py).

    Locked  → grey body, closed shackle (a full ∩).
    Unlocked → gold body, *open* shackle (the arc stops short of the right leg,
               leaving the visible gap that reads as "unlatched")."""
    body = (150, 154, 170, 255) if locked else (210, 180, 70, 255)
    shac = (110, 114, 130, 255) if locked else (170, 146, 60, 255)
    w        = max(2, ps // 6)
    body_top = py + ps // 3
    end      = 360 if locked else 315       # <360 leaves the shackle hanging open
    d.arc([px + ps // 6, py, px + ps - ps // 6, py + ps],
          start=180, end=end, fill=shac, width=w)                    # shackle
    d.rounded_rectangle([px, body_top, px + ps, py + ps], radius=3, fill=body)  # body
    kx = px + ps // 2
    ky = body_top + (py + ps - body_top) // 2
    d.ellipse([kx - 2, ky - 2, kx + 2, ky + 2], fill=(40, 36, 18, 255))         # keyhole


def _draw_play_overlay(img: Image.Image) -> Image.Image:
    """Overlay a centred play glyph (dark disc + white triangle) onto a frame."""
    d = ImageDraw.Draw(img)
    iw, ih = img.size
    cx, cy = iw // 2, ih // 2
    s = int(min(iw, ih) * 0.18)
    d.ellipse([cx - s - 2, cy - s - 2, cx + s + 2, cy + s + 2], fill=(0, 0, 0, 140))
    d.polygon([cx - s // 2, cy - s // 2, cx - s // 2, cy + s // 2, cx + s, cy],
              fill=(255, 255, 255, 220))
    return img


def make_wvf_icon(thumb_w: int, thumb_h: int, locked: bool = True) -> Image.Image:
    """Fallback tile for a .wvf when no real frame is available.

    A dark screen with film sprocket holes, a play triangle, and a padlock —
    grey/closed when locked, gold/open when a known password unlocks it (used
    when frame extraction is skipped: no cv2, file too large, or a decode error).
    """
    img = Image.new("RGBA", (thumb_w, thumb_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Screen
    m = max(6, min(thumb_w, thumb_h) // 10)
    sx0, sy0, sx1, sy1 = m, m, thumb_w - m, thumb_h - m
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=6,
                        fill=(28, 30, 38, 255), outline=(70, 74, 88, 255), width=1)

    # Film sprocket holes down both edges
    hole_w = max(3, (sx1 - sx0) // 14)
    hole_h = max(3, (sy1 - sy0) // 12)
    step   = hole_h * 2
    y = sy0 + hole_h
    while y + hole_h <= sy1:
        d.rounded_rectangle([sx0 + hole_w, y, sx0 + 2 * hole_w, y + hole_h],
                            radius=1, fill=(55, 58, 68, 255))
        d.rounded_rectangle([sx1 - 2 * hole_w, y, sx1 - hole_w, y + hole_h],
                            radius=1, fill=(55, 58, 68, 255))
        y += step

    # Play triangle (bright when unlocked, dim when locked)
    cx, cy = thumb_w // 2, thumb_h // 2
    s = int(min(thumb_w, thumb_h) * 0.18)
    play = (122, 159, 212, 255) if not locked else (90, 94, 108, 255)
    d.polygon([cx - s // 2, cy - s, cx - s // 2, cy + s, cx + s, cy], fill=play)

    # Padlock — bottom-right.  Open gold when unlocked, closed grey when locked.
    ps = max(12, min(thumb_w, thumb_h) // 5)
    _draw_padlock(d, sx1 - ps - 3, sy1 - ps - 1, ps, locked)
    return img


def _wvf_corner_badge(img: Image.Image, locked: bool = False) -> Image.Image:
    """Overlay a small padlock in the bottom-right corner of a frame thumbnail,
    on a subtle dark backing so it stays legible over any frame."""
    img = img.convert("RGBA")
    w, h = img.size
    s   = max(14, min(w, h) // 5)
    pad = 3
    px, py = w - s - pad, h - s - pad
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([px - pad, py - pad, w - 1, h - 1], radius=4, fill=(0, 0, 0, 120))
    _draw_padlock(d, px, py, s, locked)
    return img


# Don't decrypt videos larger than this just to draw a thumbnail — fall back to
# the sealed-video tile instead (frame extraction needs the whole file on disk).
WVF_THUMB_MAX_MB = 400


def _wvf_frame_thumbnail(path: Path, pw: str, thumb_w: int, thumb_h: int):
    """Decrypt a .wvf to a temp file, grab a representative frame, scrub the temp.

    Returns the frame as a thumbnail (play glyph + open padlock), or None when a
    frame can't be had (no cv2/wvf, file too big, or the video won't decode).
    The decrypted temp is plaintext, so it is securely overwritten before delete."""
    if not _CV2 or not _WVF:
        return None
    try:
        if path.stat().st_size > WVF_THUMB_MAX_MB * 1024 * 1024:
            return None
    except OSError:
        return None

    fd, tmp = tempfile.mkstemp(prefix="wvf_thumb_")
    os.close(fd)
    target = None
    try:
        meta = wvf.unwrap_file(path, tmp, password=pw)
        # Give the temp the real container extension so ffmpeg picks the demuxer.
        target = f"{tmp}.{wvf.sanitize_container(meta.get('container'))}"
        os.replace(tmp, target)
        tmp = None
        cap = cv2.VideoCapture(target)
        total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if total > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * 0.1))   # 10% in
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return None
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame)
        img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
        img = _draw_play_overlay(img.convert("RGBA"))
        return _wvf_corner_badge(img, locked=False)
    except Exception:
        return None
    finally:
        for p in (tmp, target):
            if p and os.path.exists(p):
                try:
                    secure_delete(Path(p))
                except OSError:
                    pass


def load_wvf_thumbnail(path: Path, thumb_w: int, thumb_h: int, passwords) -> Image.Image:
    """Thumbnail for a .wvf.

    Locked (no known password) → a sealed-video tile with a closed padlock; the
    unlock state is decided cheaply (first-chunk trial decrypt), nothing touches
    disk.  Unlocked → decrypt the video, show a real frame with an *open*
    padlock badge; if a frame can't be extracted, fall back to the open-padlock
    tile."""
    pw = wvf_matching_password(path, passwords)
    if pw is None:
        return make_wvf_icon(thumb_w, thumb_h, locked=True)
    frame = _wvf_frame_thumbnail(path, pw, thumb_w, thumb_h)
    if frame is not None:
        return frame
    return make_wvf_icon(thumb_w, thumb_h, locked=False)


def _gif_stack(img: Image.Image) -> Image.Image:
    """Give a GIF thumbnail a stacked-frames appearance.

    The main image is shrunk slightly and placed at the top-left; two darker
    rectangles peek out from the bottom-right, suggesting a pile of slides.
    """
    w, h = img.size
    off = max(3, min(8, w // 20))   # offset per frame, scales with thumbnail size

    # Shrink main image so the stack edges are visible
    main = img.resize((w - 2 * off, h - 2 * off), Image.LANCZOS)

    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    # Back frame — darkest, furthest back
    d.rectangle([2*off, 2*off, w - 1, h - 1],
                fill=(55, 55, 55, 240), outline=(85, 85, 85, 255), width=1)
    # Middle frame
    d.rectangle([off, off, w - off - 1, h - off - 1],
                fill=(80, 80, 80, 240), outline=(108, 108, 108, 255), width=1)

    # Main image on top, anchored to the top-left
    canvas.paste(main, (0, 0), main)
    return canvas


def load_image_thumbnail(path: Path, thumb_w: int, thumb_h: int) -> Image.Image:
    ext = path.suffix.lower()
    if ext == ".avif" and not _AVIF:
        raise ValueError("AVIF not supported — run: pip install pillow-avif-plugin")
    img = Image.open(path)
    # JPEG draft mode: ask the decoder to downscale *while* loading — much faster
    # and lighter on big JPEGs.  Harmless no-op for formats that don't support it.
    if ext in (".jpg", ".jpeg"):
        img.draft("RGB", (thumb_w, thumb_h))
    img = img.convert("RGBA")
    img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
    if ext == ".gif":
        img = _gif_stack(img)
    return img


def load_video_thumbnail(path: Path, thumb_w: int, thumb_h: int) -> Image.Image:
    if not _CV2:
        return make_video_icon(thumb_w, thumb_h)
    cap = cv2.VideoCapture(str(path))
    # seek to 10% into the video for a more interesting frame
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if total > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * 0.1))
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return make_video_icon(thumb_w, thumb_h)
    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame)
    img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
    return _draw_play_overlay(img)


PREVIEW_EXTS = IMAGE_EXTS | {".wif"}


def _gather_preview_media(folder: Path, limit: int = 40) -> list:
    """Media files to preview for `folder`.

    Prefers files directly inside the folder.  If the folder has no media of
    its own, walk its subfolders breadth-first (shallowest first) and pull
    media from there — so a folder that only contains subfolders still gets a
    meaningful preview.  Stops as soon as `limit` files are collected.
    """
    direct = []
    try:
        for f in folder.iterdir():
            try:
                if f.is_file() and f.suffix.lower() in PREVIEW_EXTS:
                    direct.append(f)
                    if len(direct) >= limit:
                        return direct
            except OSError:
                pass
    except OSError:
        return direct
    if direct:
        return direct

    # No direct media — descend into subfolders, nearest first
    found, queue = [], deque()
    try:
        for d in folder.iterdir():
            try:
                if d.is_dir():
                    queue.append(d)
            except OSError:
                pass
    except OSError:
        return found

    while queue and len(found) < limit:
        current = queue.popleft()
        subdirs = []
        try:
            for f in current.iterdir():
                try:
                    if f.is_file() and f.suffix.lower() in PREVIEW_EXTS:
                        found.append(f)
                        if len(found) >= limit:
                            break
                    elif f.is_dir():
                        subdirs.append(f)
                except OSError:
                    pass
        except OSError:
            pass
        queue.extend(subdirs)   # explore sibling folders before going deeper
    return found


def _cover_crop(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale to fill a w×h box preserving aspect ratio, then centre-crop (no squish)."""
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    iw, ih = img.size
    if iw == 0 or ih == 0:
        return img.resize((w, h), Image.LANCZOS)
    scale = max(w / iw, h / ih)
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _even_pick(items: list, k: int) -> list:
    """Deterministically pick k items spread evenly across the list."""
    n = len(items)
    if n == 0:
        return []
    if k >= n:
        return list(items)
    return [items[i * n // k] for i in range(k)]


def _count_media(folder: Path) -> int:
    """Count media files directly inside `folder` (fast scandir, no recursion)."""
    cnt = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False) and Path(e.name).suffix.lower() in PREVIEW_EXTS:
                        cnt += 1
                except OSError:
                    pass
    except OSError:
        pass
    return cnt


def load_preview_data(folder: Path, img_w: int, img_h: int, n_slots: int = 4, passwords=()) -> dict:
    """A folder-preview row's data: a deterministic, evenly-spaced pool of
    cover-cropped thumbnails (with their paths) plus a subtitle (count · date).
    Descends into subfolders when the folder has no media of its own."""
    candidates = sorted(_gather_preview_media(folder), key=lambda p: _natural_key(p.name))

    count = _count_media(folder)
    date_str = ""
    try:
        date_str = datetime.fromtimestamp(folder.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        pass
    if count:
        head = f"{count} image{'s' if count != 1 else ''}"
    elif candidates:
        head = "in subfolders"
    else:
        head = "empty"
    subtitle = head + (f"   ·   {date_str}" if date_str else "")

    pool_n = min(len(candidates), max(n_slots + 3, 5), 12)
    imgs, paths = [], []
    for p in _even_pick(candidates, pool_n):
        try:
            if p.suffix.lower() == ".wif":
                raw, _m, _pw = wif_format.decode_try(p.read_bytes(), passwords)
            else:
                raw = Image.open(p)
            imgs.append(_cover_crop(raw, img_w, img_h))
            paths.append(p)
        except Exception:
            pass
    return {"imgs": imgs, "paths": paths, "subtitle": subtitle}


# ── secure delete ────────────────────────────────────────────────────────────

def secure_delete(path: Path):
    """Overwrite file contents with zeros, flush to disk, then delete."""
    size  = path.stat().st_size
    chunk = b"\x00" * (1 << 20)
    with open(path, "r+b") as fh:
        remaining = size
        while remaining:
            n = min(len(chunk), remaining)
            fh.write(chunk[:n])
            remaining -= n
        fh.flush()
        os.fsync(fh.fileno())
    path.unlink()


def _list_block(title: str, items: list, limit: int = 15) -> str:
    """Format a capped list for a summary message box."""
    text = f"{title}\n" + "\n".join(items[:limit])
    if len(items) > limit:
        text += f"\n… and {len(items) - limit} more"
    return text


def _unique_path(path: Path) -> Path:
    """Return `path` if it's free, otherwise the same name with ' (2)', ' (3)', …
    inserted before the suffix — so an existing file is never overwritten."""
    if not path.exists():
        return path
    i = 2
    while True:
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
        i += 1


def _natural_key(name: str):
    """Sort key that orders embedded numbers numerically, so 'img2' sorts before
    'img10' instead of after it.  The name is split into alternating text/number
    chunks; digit runs compare as ints, the rest case-folded."""
    return [int(c) if c.isdigit() else c.lower()
            for c in re.split(r"(\d+)", name)]


# ── Cell widget ──────────────────────────────────────────────────────────────

class Cell(tk.Frame):
    """A recycled thumbnail tile.  Created once and re-bound to different Items as
    the grid scrolls (see WifBrowser._reflow), so a huge folder needs only enough
    tiles to cover the viewport."""
    def __init__(self, parent, on_dbl, on_select, on_right_click,
                 cell_w: int, cell_h: int, thumb_w: int, thumb_h: int,
                 on_hover=None, on_unhover=None, on_drag=None, on_drop=None):
        super().__init__(parent, bg=CELL_BG, width=cell_w, height=cell_h, cursor="hand2")
        self.pack_propagate(False)
        self._cell_w   = cell_w
        self.item      = None
        self.path      = None
        self.is_folder = False
        self._photo    = None
        self._selected = False

        self.img_lbl = tk.Label(self, bg=CELL_BG, width=thumb_w, height=thumb_h)
        self.img_lbl.pack(pady=(6, 4))

        self.name_lbl = tk.Label(
            self, text="", bg=CELL_BG, fg=FG,
            wraplength=cell_w - 12, justify=tk.CENTER,
            font=("Segoe UI", 9)
        )
        self.name_lbl.pack(padx=4)

        self.tags_lbl = tk.Label(
            self, text="", bg=CELL_BG, fg=FG_DIM,
            wraplength=cell_w - 12, justify=tk.CENTER,
            font=("Segoe UI", 7)
        )
        self.tags_lbl.pack(padx=4)

        for w in (self, self.img_lbl, self.name_lbl, self.tags_lbl):
            w.bind("<Double-Button-1>", lambda e: on_dbl(self))
            w.bind("<Button-1>",        lambda e: on_select(self, e))
            w.bind("<Button-3>",        lambda e: on_right_click(self, e))
            w.bind("<Enter>",           lambda e: self._tint(CELL_HL))
            w.bind("<Leave>",           lambda e: self._tint(CELL_SEL if self._selected else CELL_BG))
            # Drag-to-move: press is handled by on_select; motion past a threshold
            # begins a drag, release drops onto the folder under the cursor.
            if on_drag is not None:
                w.bind("<B1-Motion>",       lambda e: on_drag(self, e), add="+")
                w.bind("<ButtonRelease-1>", lambda e: on_drop(self, e), add="+")

        # Hover-zoom — fires on the thumbnail image; the browser ignores it
        # unless it's in "hover" size mode.
        if on_hover is not None:
            self.img_lbl.bind("<Enter>", lambda e: on_hover(self, e),   add="+")
            self.img_lbl.bind("<Leave>", lambda e: on_unhover(self, e), add="+")

    def rebind(self, item, selected: bool, tags: str = "", cut: bool = False):
        """Point this recycled tile at `item` and refresh its name, image and tint.
        `cut` dims the name to show the item is on the clipboard for a move."""
        self.item      = item
        self.path      = item.path
        self.is_folder = item.is_folder
        name = item.display_name or item.path.name or str(item.path)
        max_chars = ((self._cell_w - 12) // 7) * 2   # ~7px/char, two lines
        if len(name) > max_chars:
            name = name[:max_chars - 3] + "..."
        self.name_lbl.configure(text=name, fg=(FG_DIM if cut else FG))
        self.tags_lbl.configure(text=tags)
        self._photo = item.photo
        self.img_lbl.configure(image=item.photo if item.photo is not None else "")
        self._selected = selected
        self._tint(CELL_SEL if selected else CELL_BG)

    def set_image(self, photo: ImageTk.PhotoImage):
        self._photo = photo          # keep reference so GC doesn't collect it
        self.img_lbl.configure(image=photo)

    def _tint(self, color: str):
        self.configure(bg=color)
        self.img_lbl.configure(bg=color)
        self.name_lbl.configure(bg=color)
        self.tags_lbl.configure(bg=color)


# ── Preview row widget ───────────────────────────────────────────────────────

class PreviewRow(tk.Frame):
    """One row in preview mode: folder name + count/date on the left, a strip of
    cover-cropped thumbnails on the right.  Hovering scrubs through more of the
    folder's images; clicking a thumbnail opens it in the lightbox."""
    NAME_W = 210

    def __init__(self, parent, path: Path, on_dbl,
                 img_w: int = 120, img_h: int = 90, n_slots: int = 4,
                 on_image_click=None, scrub_enabled=None,
                 on_slot_hover=None, on_slot_unhover=None):
        row_h = img_h + 16
        super().__init__(parent, bg=CELL_BG, height=row_h, cursor="hand2")
        self.pack_propagate(False)
        self.path           = path
        self.is_folder      = True
        self.img_w          = img_w
        self.img_h          = img_h
        self.n_slots        = n_slots
        self._on_image_click  = on_image_click
        self._scrub_enabled   = scrub_enabled   # callable -> bool (checked live), or None
        self._on_slot_hover   = on_slot_hover   # (label, path, event) -> None
        self._on_slot_unhover = on_slot_unhover # (label, event) -> None

        self._pool_imgs     = []     # PIL cover-cropped thumbnails (scrub pool)
        self._pool_paths    = []
        self._slot_photos   = []     # PhotoImages currently displayed (GC refs)
        self._visible_paths = [None] * n_slots
        self._scrub_offset  = 0
        self._scrub_job     = None
        self._stop_job      = None

        # ── folder name / info panel ──
        name_panel = tk.Frame(self, bg=PREVIEW_BG, width=self.NAME_W)
        name_panel.pack_propagate(False)
        name_panel.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4))

        tk.Label(name_panel, text="▶", bg=PREVIEW_BG, fg="#c8880f",
                 font=("Segoe UI", 13)).pack(side=tk.LEFT, padx=(10, 6))

        text_box = tk.Frame(name_panel, bg=PREVIEW_BG)
        text_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=6)

        name = path.name or str(path)
        max_chars = (self.NAME_W - 64) // 7
        if len(name) > max_chars:
            name = name[:max_chars - 1] + "…"
        tk.Label(text_box, text=name, bg=PREVIEW_BG, fg=FG, font=("Segoe UI", 9, "bold"),
                 anchor="w", justify=tk.LEFT, wraplength=self.NAME_W - 64).pack(
            side=tk.TOP, anchor="w", fill=tk.X)
        self._info_lbl = tk.Label(text_box, text="…", bg=PREVIEW_BG, fg=FG_DIM,
                                  font=("Segoe UI", 8), anchor="w")
        self._info_lbl.pack(side=tk.TOP, anchor="w", fill=tk.X, pady=(2, 0))

        # ── image slots ──
        self._img_lbls = []
        for i in range(n_slots):
            lbl = tk.Label(self, bg=CELL_BG, width=img_w, height=img_h, cursor="hand2")
            lbl.pack(side=tk.LEFT, padx=3, pady=4)
            lbl.bind("<Button-1>", lambda e, idx=i: self._click_slot(idx))
            self._img_lbls.append(lbl)

        panel_widgets = [name_panel] + list(name_panel.winfo_children()) + list(text_box.winfo_children())
        for w in [self] + panel_widgets:
            w.bind("<Double-Button-1>", lambda e: on_dbl(self))   # enter folder
        for w in [self] + panel_widgets + self._img_lbls:
            w.bind("<Enter>", lambda e: self._hover_on())
            w.bind("<Leave>", lambda e: self._hover_off())

        # Per-slot hover-zoom: each label individually so we know which path it shows.
        # The binding is refreshed in _render_window whenever the displayed path changes.
        if on_slot_hover is not None:
            for lbl in self._img_lbls:
                lbl.bind("<Enter>",
                         lambda e, l=lbl: on_slot_hover(l, self._slot_path_for(l), e),
                         add="+")
                lbl.bind("<Leave>",
                         lambda e, l=lbl: on_slot_unhover(l, e),
                         add="+")

    def _slot_path_for(self, lbl) -> Path | None:
        """Return the path currently displayed in label `lbl`, or None."""
        try:
            idx = self._img_lbls.index(lbl)
            return self._visible_paths[idx] if idx < len(self._visible_paths) else None
        except ValueError:
            return None

    # ── content ──
    def set_preview(self, data: dict):
        self._pool_imgs  = data.get("imgs", [])
        self._pool_paths = data.get("paths", [])
        self._info_lbl.configure(text=data.get("subtitle", ""))
        self._scrub_offset = 0
        self._render_window(0)

    def _render_window(self, offset: int):
        n = len(self._pool_imgs)
        self._slot_photos = []
        self._visible_paths = [None] * self.n_slots
        shown = min(self.n_slots, n)
        for i in range(self.n_slots):
            if i < shown:
                idx = (offset + i) % n
                photo = ImageTk.PhotoImage(self._pool_imgs[idx])
                self._slot_photos.append(photo)
                self._visible_paths[i] = self._pool_paths[idx]
                self._img_lbls[i].configure(image=photo)
            else:
                self._img_lbls[i].configure(image="")

    def _click_slot(self, i: int):
        if i < len(self._visible_paths) and self._visible_paths[i] is not None and self._on_image_click:
            self._on_image_click(self._visible_paths[i], list(self._pool_paths))

    # ── hover scrub ──
    def _hover_on(self):
        self._tint(CELL_HL)
        if self._stop_job is not None:
            self.after_cancel(self._stop_job)
            self._stop_job = None
        enabled = self._scrub_enabled is None or self._scrub_enabled()
        if enabled and self._scrub_job is None and len(self._pool_imgs) > self.n_slots:
            self._scrub_job = self.after(700, self._scrub_tick)

    def _hover_off(self):
        self._tint(CELL_BG)
        if self._stop_job is not None:
            self.after_cancel(self._stop_job)
        self._stop_job = self.after(90, self._stop_scrub)

    def _scrub_tick(self):
        try:
            n = len(self._pool_imgs)
            if n:
                self._scrub_offset = (self._scrub_offset + 1) % n
                self._render_window(self._scrub_offset)
            self._scrub_job = self.after(700, self._scrub_tick)
        except tk.TclError:        # row destroyed mid-scrub
            self._scrub_job = None

    def _stop_scrub(self):
        self._stop_job = None
        if self._scrub_job is not None:
            self.after_cancel(self._scrub_job)
            self._scrub_job = None
        if self._scrub_offset != 0:
            self._scrub_offset = 0
            try:
                self._render_window(0)
            except tk.TclError:
                pass

    def _tint(self, color: str):
        self.configure(bg=color)
        for lbl in self._img_lbls:
            lbl.configure(bg=color)


# ── Browser ──────────────────────────────────────────────────────────────────

class Item:
    """A file/folder entry in the grid.  The model — not the widgets — is the
    source of truth for selection and rendering.  Cell widgets are a recycled
    pool bound to Items on demand, so only the on-screen rows exist as widgets."""
    __slots__ = ("path", "is_folder", "kind", "photo", "queued", "display_name")

    def __init__(self, path: Path, is_folder: bool, kind: str):
        self.path         = path
        self.is_folder    = is_folder
        self.kind         = kind           # "folder" | "file"
        self.photo        = None           # ImageTk.PhotoImage once decoded (cached)
        self.queued       = False          # a load task has been enqueued
        self.display_name = ""             # non-empty in tag-view to show parent/name


class WifBrowser:
    def __init__(self, root: tk.Tk):
        self.root   = root
        root.title("Weird Traveler")
        try:
            _icon = Path(__file__).parent / "wif_icon.ico"
            if _icon.exists():
                root.iconbitmap(default=str(_icon))
        except Exception:
            pass
        self._cwd        = Path.cwd()
        self._items: list = []             # model: every grid entry (folders + files)
        self._view:  list = []             # items passing the filename filter
        self._rows:  list = []             # PreviewRow widgets (preview mode only)
        self._pool:  list = []             # recycled Cell widgets for the visible rows
        self._pool_dims = None             # cell dims the pool was built for
        self._visible = {}                 # item -> Cell currently displaying it
        self._size_preset  = SIZE_SMALL   # current (thumb_w, thumb_h, cell_w, cell_h)
        self._size_mode    = "small"      # cycles: small -> large -> hover
        self._filter_var      = tk.StringVar(value=FILTER_MEDIA)
        self._gen             = 0            # incremented on every navigation
        self._preview_mode    = False        # show folder content previews
        self._thumb_count_var = tk.StringVar(value="4")
        self._selected_items: set = set()    # currently selected Cell widgets
        self._anchor_item         = None     # last plain-click cell (shift-range start)
        self._lead_item           = None     # moving end of the selection (keyboard nav)
        self._marquee_item        = None     # canvas rect id while drag-selecting
        self._marquee_base: set   = set()    # selection to merge into (Ctrl-marquee)
        self._marquee_x0 = self._marquee_y0 = 0
        self._marquee_add         = False
        # File clipboard (Copy/Cut/Paste) + drag-to-move state
        self._clipboard: list     = []       # paths copied/cut, pasted with Ctrl+V
        self._clipboard_cut       = False    # True = cut (move on paste); False = copy
        self._pending_collapse    = None     # plain-click on a group: collapse on release
        self._drag_candidate      = None     # cell pressed; may become a drag
        self._drag_start          = (0, 0)
        self._dragging            = False
        self._drag_paths: set     = set()    # paths currently being dragged
        self._drag_indicator      = None     # floating "Move N items" label
        self._drop_cell           = None     # folder cell highlighted as drop target
        self._sort_var        = tk.StringVar(value=SORT_NAME)
        self._sort_reverse    = False        # ascending by default
        self._sort_desc_var   = tk.BooleanVar(value=False)   # menu checkbutton mirror
        self._theme_var       = tk.StringVar(value=DEFAULT_THEME)
        self._remember_var    = tk.BooleanVar(value=False)
        self._scrub_var       = tk.BooleanVar(value=False)   # hover-to-scrub previews (off by default)
        self._toolbar_frame   = None         # stored so the UI can be rebuilt on theme change
        self._grid_container  = None
        self._status_frame    = None
        self._resize_job      = None         # debounce id for canvas-resize relayout
        self._filter_text_var = tk.StringVar()   # live filename filter
        self._converting      = False        # guards against overlapping batch conversions
        self._passwords: list = []           # the one session password (0 or 1 entry)
        # Decrypted .wvf temp files (the no-ffplay fallback path): tracked so they
        # can be scrubbed when the app exits.  ffplay streaming leaves none behind.
        self._wvf_tempfiles: list = []
        atexit.register(self._cleanup_wvf_tempfiles)
        # Tags — loaded lazily (None = not yet attempted, {} = loaded/empty)
        self._tags: dict | None = None       # path_str → [tag, …]
        self._tags_locked       = False      # True = on-disk file couldn't be decrypted; don't overwrite
        self._tags_pw: str | None = None     # password that successfully decrypted the tags file
        self._tag_filter_var    = tk.StringVar()   # toolbar tag-filter entry
        self._show_tags_var     = tk.BooleanVar(value=True)   # toggle tag badge in cells
        self._recursive_var     = tk.BooleanVar(value=False)  # recursive (subfolder) search
        self._tag_view: str | None = None          # tag query shown in global-view mode
        self._search_view: str | None = None       # recursive filename-search query (or None)
        self._searching         = False            # True while a background search streams in
        self._dup_view: str | None = None          # duplicate-finder root shown in view mode (or None)
        self._scanning_dups     = False            # True while the dup-scan thread runs
        self._dup_group_count   = 0                # number of duplicate groups found
        self._recycle_var       = tk.BooleanVar(value=False)  # delete to Recycle Bin (vs secure-wipe)
        self._ac_popup: tk.Toplevel | None = None  # floating autocomplete for tag filter
        self._ac_lb:    tk.Listbox  | None = None
        # In-browser image overlay ("lightbox")
        self._overlay        = None
        self._overlay_img    = None          # current full-res PIL image
        self._overlay_photo  = None          # keeps a ref so tk doesn't GC it
        self._overlay_paths  = []            # viewable files to arrow through
        self._overlay_index  = 0
        self._overlay_frames    = []         # animated-GIF frames in the lightbox
        self._overlay_durations = []
        self._overlay_frame_idx = 0
        self._overlay_anim_job  = None
        self._overlay_hint      = None       # bottom hint label (repositioned by the filmstrip)
        # Lightbox filmstrip
        self._film_strip   = None            # strip Frame inside the overlay
        self._film_photos  = []              # PhotoImages currently shown (GC refs)
        self._film_cache   = {}              # path_str -> small PhotoImage thumbnail
        # Debounce jobs
        self._overlay_resize_job = None
        self._tag_filter_job     = None

        # Hover-zoom popup state
        self._hover_popup    = None
        self._hover_lbl      = None
        self._hover_photo    = None
        self._hover_widget   = None   # widget (Cell or slot Label) whose hover is active
        self._hover_show_job = None
        self._hover_hide_job = None
        self._hover_pcache   = {}            # path -> enlarged PIL preview (cache)

        # Two queues: work for the loader threads, results back to main thread
        self._load_q:   Queue = Queue()
        self._result_q: Queue = Queue()
        self._ui_q:     Queue = Queue()   # callbacks posted from worker threads

        self._load_settings()             # may override vars + theme from saved config
        self._ensure_tags_loaded(allow_prompt=False)   # silent — no prompt at startup
        root.configure(bg=BG)
        self._build_toolbar()
        self._build_grid_area()
        self._build_statusbar()

        # Guard: only act when the event came from the main window, not from a
        # modal dialog (tag search, edit-tags, etc.) whose key events propagate
        # up through the Toplevel hierarchy and reach the root bindings.
        def _main(fn):
            def wrapped(e):
                # Only fire root shortcuts when keyboard focus is inside the main
                # browser window.  focus_get() returns None when no widget is focused
                # (e.g. the Toplevel window itself has focus) — treat that as "not
                # the main window" so dialogs can't leak key events into root handlers.
                w = self.root.focus_get()
                if w is None or w.winfo_toplevel() is not self.root:
                    return None
                return fn(e)
            return wrapped

        root.bind("<BackSpace>",  _main(lambda e: None if (self._focus_in_entry() or self._overlay) else self._go_up()))
        root.bind("<Alt-Left>",   _main(lambda e: self._overlay_close() if self._overlay else self._go_up()))
        root.bind("<Delete>",     _main(lambda e: None if (self._focus_in_entry() or self._overlay) else self._delete_selected()))
        root.bind("<Control-f>",  _main(lambda e: self._filter_entry.focus_set()))
        root.bind("<Control-a>",  _main(lambda e: None if (self._focus_in_entry() or self._overlay) else self._select_all()))
        root.bind("<Control-c>",  _main(lambda e: None if (self._focus_in_entry() or self._overlay) else self._copy_to_clipboard()))
        root.bind("<Control-x>",  _main(lambda e: None if (self._focus_in_entry() or self._overlay) else self._cut_to_clipboard()))
        root.bind("<Control-v>",  _main(lambda e: None if (self._focus_in_entry() or self._overlay) else self._paste_from_clipboard()))
        root.bind("<Escape>",     _main(lambda e: self._overlay_close() if self._overlay else None))
        root.bind("<Left>",       _main(lambda e: self._overlay_nav(-1) if self._overlay else self._grid_key(-1, 0, e)))
        root.bind("<Right>",      _main(lambda e: self._overlay_nav(1)  if self._overlay else self._grid_key(1, 0, e)))
        root.bind("<Up>",         _main(lambda e: None if self._overlay else self._grid_key(0, -1, e)))
        root.bind("<Down>",       _main(lambda e: None if self._overlay else self._grid_key(0, 1, e)))
        root.bind("<Home>",       _main(lambda e: None if self._overlay else self._grid_jump(0, e)))
        root.bind("<End>",        _main(lambda e: None if self._overlay else self._grid_jump(-1, e)))
        root.bind("<Return>",     _main(lambda e: self._grid_open()))
        root.bind("<KP_Enter>",   _main(lambda e: self._grid_open()))

        # Start a pool of background loaders so thumbnails load faster
        for _ in range(4):
            threading.Thread(target=self._loader_worker, daemon=True).start()
        # Poll for finished thumbnails
        self.root.after(40, self._apply_thumbnails)

        self._navigate(self._cwd)

    # ── UI construction ───────────────────────────────────────────────────

    def _build_toolbar(self):
        tb = tk.Frame(self.root, bg=TOOLBAR_BG, pady=5)
        tb.pack(side=tk.TOP, fill=tk.X)
        self._toolbar_frame = tb

        btn = dict(bg=BTN_BG, fg=FG, relief=tk.FLAT,
                   activebackground=BTN_ACTIVE, activeforeground=ACTIVE_FG,
                   padx=10, pady=3, font=("Segoe UI", 9))

        # ☰ Settings menu — far left.  Holds Unlock, Sort by, theme, etc.
        self._build_settings_menu(tb, btn)

        tk.Button(tb, text="↑  Up", command=self._go_up, **btn).pack(side=tk.LEFT, padx=(0, 4))

        self._size_btn = tk.Button(tb, text=self._size_label(), command=self._toggle_size, **btn)
        self._size_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._preview_btn = tk.Button(
            tb, text=("⧉  Preview  ✓" if self._preview_mode else "⧉  Preview"),
            command=self._toggle_preview, **btn)
        self._preview_btn.pack(side=tk.LEFT, padx=(0, 4))

        # Thumbnail-count control — only visible in preview mode
        self._thumb_count_frame = tk.Frame(tb, bg=TOOLBAR_BG)
        tk.Label(self._thumb_count_frame, text="Thumbnails:", bg=TOOLBAR_BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(4, 2))
        thumb_menu = tk.OptionMenu(
            self._thumb_count_frame, self._thumb_count_var,
            "1", "2", "3", "4", "5", "6", "7", "8",
            command=lambda _: self._navigate(self._cwd)
        )
        thumb_menu.configure(
            bg=BTN_BG, fg=FG, relief=tk.FLAT, activebackground=BTN_ACTIVE,
            activeforeground=ACTIVE_FG, font=("Segoe UI", 9), highlightthickness=0, width=2
        )
        thumb_menu["menu"].configure(bg=BTN_BG, fg=FG,
                                     activebackground=BTN_ACTIVE, activeforeground=ACTIVE_FG)
        thumb_menu.pack(side=tk.LEFT)
        if self._preview_mode:
            self._thumb_count_frame.pack(side=tk.LEFT, padx=(0, 6))
        else:
            self._thumb_count_frame.pack_forget()

        filter_menu = tk.OptionMenu(tb, self._filter_var, *FILTERS,
                                    command=lambda _: self._on_filter_changed())
        filter_menu.configure(
            bg=BTN_BG, fg=FG, relief=tk.FLAT, activebackground=BTN_ACTIVE,
            activeforeground=ACTIVE_FG, font=("Segoe UI", 9), highlightthickness=0
        )
        filter_menu["menu"].configure(bg=BTN_BG, fg=FG, activebackground=BTN_ACTIVE,
                                      activeforeground=ACTIVE_FG)
        filter_menu.pack(side=tk.LEFT, padx=(0, 6))

        # Filename filter box — Ctrl+F focuses it, Esc clears it
        tk.Label(tb, text="Filter:", bg=TOOLBAR_BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 2))
        self._filter_entry = tk.Entry(
            tb, textvariable=self._filter_text_var, width=16,
            bg=BTN_BG, fg=FG, insertbackground=FG, relief=tk.FLAT, font=("Segoe UI", 10)
        )
        self._filter_entry.pack(side=tk.LEFT, padx=(0, 6), ipady=3)
        self._filter_entry.bind("<KeyRelease>", self._on_filter_key)
        self._filter_entry.bind("<Return>",     lambda e: self._filter_enter())
        self._filter_entry.bind("<KP_Enter>",   lambda e: self._filter_enter())
        self._filter_entry.bind("<Escape>",     lambda e: self._filter_escape())

        tk.Label(tb, text="Tags:", bg=TOOLBAR_BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 2))
        self._tag_filter_entry = tk.Entry(
            tb, textvariable=self._tag_filter_var, width=12,
            bg=BTN_BG, fg=FG, insertbackground=FG, relief=tk.FLAT, font=("Segoe UI", 10)
        )
        self._tag_filter_entry.pack(side=tk.LEFT, padx=(0, 6), ipady=3)
        # KeyRelease drives live filtering + refreshing the dropdown contents.
        self._tag_filter_entry.bind("<KeyRelease>", self._on_tag_filter_key)
        # Navigation keys are consumed on KeyPress and return "break" so they never
        # reach the main-window root bindings (which would select a grid item and
        # steal focus, tearing the dropdown down).
        self._tag_filter_entry.bind("<Down>",     lambda e: self._tag_filter_move(1))
        self._tag_filter_entry.bind("<Up>",       lambda e: self._tag_filter_move(-1))
        self._tag_filter_entry.bind("<Return>",   self._tag_filter_accept)
        self._tag_filter_entry.bind("<KP_Enter>", self._tag_filter_accept)
        self._tag_filter_entry.bind("<Escape>",   self._tag_filter_escape)
        self._tag_filter_entry.bind("<FocusOut>",
                                   lambda e: self.root.after(150, self._ac_maybe_destroy))
        # Load (and unlock) tags the first time the user clicks into the tag field
        self._tag_filter_entry.bind("<FocusIn>",
                                   lambda e: self.root.after(10, self._ensure_tags_loaded))

        self._path_var = tk.StringVar()
        entry = tk.Entry(
            tb, textvariable=self._path_var,
            bg=BTN_BG, fg=FG, insertbackground=FG,
            relief=tk.FLAT, font=("Segoe UI", 10)
        )
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, ipady=4)
        entry.bind("<Return>", lambda e: self._go_to_typed())

        # Right-click context menu on the path bar: Copy / Paste.
        # <<Copy>>/<<Paste>> are Tk's built-in virtual events for entry widgets.
        path_menu = tk.Menu(entry, tearoff=0, bg=BTN_BG, fg=FG,
                            activebackground=BTN_ACTIVE, activeforeground=ACTIVE_FG,
                            relief=tk.FLAT, bd=0)
        path_menu.add_command(label="Copy",
                             command=lambda: entry.event_generate("<<Copy>>"))
        path_menu.add_command(label="Paste",
                             command=lambda: entry.event_generate("<<Paste>>"))

        def _show_path_menu(event):
            entry.focus_set()
            try:
                path_menu.tk_popup(event.x_root, event.y_root)
            finally:
                path_menu.grab_release()

        entry.bind("<Button-3>", _show_path_menu)

        tk.Button(tb, text="Go", command=self._go_to_typed, **btn).pack(side=tk.LEFT, padx=(0, 8))

    def _build_settings_menu(self, tb, btn):
        """The ☰ dropdown (box with three lines) at the far left: Unlock, Sort by,
        theme, and remember-settings."""
        mcfg = dict(bg=BTN_BG, fg=FG, activebackground=BTN_ACTIVE,
                    activeforeground=ACTIVE_FG, relief=tk.FLAT, bd=0)
        mb = tk.Menubutton(tb, text="☰", **btn)
        mb.configure(font=("Segoe UI", 12))
        mb.pack(side=tk.LEFT, padx=(8, 4))

        menu = tk.Menu(mb, tearoff=0)
        menu.configure(**mcfg)
        mb.configure(menu=menu)

        menu.add_command(label="🔑  Unlock…", command=self._add_password)
        menu.add_command(label="🔓  Clear session password", command=self._clear_session_password)
        menu.add_command(label="📋  Append to txt", command=self._append_selected_to_txt)
        menu.add_separator()

        sort_sub = tk.Menu(menu, tearoff=0)
        sort_sub.configure(**mcfg)
        for opt in SORT_OPTIONS:
            sort_sub.add_radiobutton(label=opt, value=opt, variable=self._sort_var,
                                     command=self._on_sort_changed)
        menu.add_cascade(label="Sort by", menu=sort_sub)

        menu.add_checkbutton(label="Descending order", variable=self._sort_desc_var,
                             command=self._on_sort_dir_changed)
        menu.add_separator()

        theme_sub = tk.Menu(menu, tearoff=0)
        theme_sub.configure(**mcfg)
        for name in THEME_NAMES:
            theme_sub.add_radiobutton(label=name, value=name, variable=self._theme_var,
                                      command=self._on_theme_changed)
        menu.add_cascade(label="Theme", menu=theme_sub)

        menu.add_separator()
        menu.add_checkbutton(label="Hover-to-scrub previews", variable=self._scrub_var,
                             command=self._on_scrub_changed)
        menu.add_checkbutton(label="Remember settings", variable=self._remember_var,
                             command=self._on_remember_changed)
        menu.add_checkbutton(label="Show tags in cells", variable=self._show_tags_var,
                             command=self._reflow)
        menu.add_checkbutton(label="Search subfolders  (Enter in Filter)",
                             variable=self._recursive_var, command=self._on_recursive_changed)
        menu.add_checkbutton(label="Delete to Recycle Bin", variable=self._recycle_var,
                             command=self._on_recycle_changed)
        menu.add_separator()
        menu.add_command(label="⧉  Find duplicates…", command=self._find_duplicates)
        menu.add_command(label="🏷  Tag search…", command=self._show_tag_search)
        menu.add_command(label="🧹  Remove orphaned tags…", command=self._prune_orphaned_tags)
        menu.add_separator()
        menu.add_command(label="❔  Help / shortcuts", command=self._show_help)

    def _build_grid_area(self):
        # A theme rebuild destroys the old canvas (and its child cell widgets), so
        # drop the recycled pool and preview rows that lived inside it.
        self._pool = []
        self._pool_dims = None
        self._visible = {}
        self._rows = []

        container = tk.Frame(self.root, bg=BG)
        container.pack(fill=tk.BOTH, expand=True)
        self._grid_container = container

        self._canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        vbar = tk.Scrollbar(container, orient=tk.VERTICAL, command=self._yview_render)
        self._canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Cells are placed as individual canvas window items (not inside one big
        # frame) so the total height can exceed the ~32767-px Tk widget limit, and
        # only the rows in view are actually created (see _reflow).
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

        # Rubber-band marquee — these fire only when the press lands on empty
        # canvas; a press on a cell is handled by the cell's own bindings.
        self._canvas.bind("<ButtonPress-1>",   self._marquee_press)
        self._canvas.bind("<B1-Motion>",       self._marquee_drag)
        self._canvas.bind("<ButtonRelease-1>", self._marquee_release)
        self._canvas.bind("<Button-3>",        self._canvas_right_click)

    def _build_statusbar(self):
        if not hasattr(self, "_status_var"):
            self._status_var = tk.StringVar()
        self._status_frame = tk.Label(
            self.root, textvariable=self._status_var,
            bg=TOOLBAR_BG, fg=FG_DIM, anchor="w", padx=10, pady=4,
            font=("Segoe UI", 9))
        self._status_frame.pack(side=tk.BOTTOM, fill=tk.X)

    # ── navigation ────────────────────────────────────────────────────────

    def _navigate(self, path: Path):
        self._tag_view = None
        self._search_view = None
        self._searching = False
        self._dup_view = None
        self._scanning_dups = False
        try:
            entries = list(path.iterdir())
        except PermissionError:
            messagebox.showerror("Access denied", f"Cannot open:\n{path}")
            return

        self._cwd = path
        self._gen += 1          # invalidate all in-flight tasks from previous folders
        gen = self._gen
        self._path_var.set(str(path))
        self.root.title(f"Weird Traveler — {path}")

        # Clear selection and tear down the previous folder's preview rows.
        # Pool cells are kept and re-bound by _reflow.
        self._selected_items.clear()
        self._anchor_item = None
        self._lead_item = None
        self._hover_reset()          # drop any hover popup + stale preview cache
        for r in self._rows:
            try:
                r.destroy()
            except tk.TclError:
                pass
        self._rows = []

        # Wrap is_dir/is_file in try/except — Windows drive roots have
        # junction points and protected folders that raise OSError
        f_mode = self._filter_var.get()
        folders, wif_files = [], []
        for e in entries:
            try:
                if e.is_dir():
                    folders.append(e)
                elif e.is_file():
                    ext = e.suffix.lower()
                    if f_mode == FILTER_WIF   and ext in WEIRD_EXTS:
                        wif_files.append(e)
                    elif f_mode == FILTER_MEDIA and ext in MEDIA_EXTS:
                        wif_files.append(e)
                    elif f_mode == FILTER_ALL:
                        wif_files.append(e)
            except OSError:
                pass
        folders.sort(key=self._sort_key, reverse=self._sort_reverse)
        wif_files.sort(key=self._sort_key, reverse=self._sort_reverse)

        tw, th, cw, ch = self._size_preset

        # Build the model.  In preview mode folders become PreviewRow widgets
        # (rendered above the grid); files are Items either way.  Cell thumbnails
        # are decoded lazily by _reflow as items scroll into view.
        self._items = []
        if self._preview_mode:
            n_thumbs = int(self._thumb_count_var.get())
            p_img_w = max(100, tw * 9 // 10)
            p_img_h = max(75,  th * 9 // 10)
            for p in folders:
                row = PreviewRow(self._canvas, p, self._on_double_click,
                                 p_img_w, p_img_h, n_slots=n_thumbs,
                                 on_image_click=self._open_preview_image,
                                 scrub_enabled=self._scrub_var.get,
                                 on_slot_hover=self._hover_enter_slot,
                                 on_slot_unhover=self._hover_leave)
                self._rows.append(row)
                self._load_q.put(("row", row, tw, th, gen))
        else:
            for p in folders:
                self._items.append(Item(p, True, "folder"))
        for p in wif_files:
            self._items.append(Item(p, False, "file"))

        self._recompute_view()
        self._update_status()

        # Hide recycled tiles so the previous folder can't flash during the idle
        # redraw below; _reflow re-shows the ones now in view.
        for c in self._pool:
            wid = getattr(c, "_winid", None)
            if wid is not None:
                self._canvas.itemconfigure(wid, state="hidden")

        self.root.update_idletasks()
        self._canvas.yview_moveto(0)
        self._reflow()
        self._canvas.focus_set()   # so arrow-key navigation works right away

    def _toggle_preview(self):
        self._preview_mode = not self._preview_mode
        if self._preview_mode:
            self._preview_btn.configure(text="⧉  Preview  ✓")
            self._thumb_count_frame.pack(side=tk.LEFT, padx=(0, 6))
        else:
            self._preview_btn.configure(text="⧉  Preview")
            self._thumb_count_frame.pack_forget()
        self._navigate(self._cwd)

    def _size_label(self) -> str:
        return {"small": "⊟  Small", "large": "⊞  Large", "hover": "🔍  Hover zoom"}[self._size_mode]

    def _toggle_size(self):
        # Cycle: small -> large -> hover-zoom -> small.  The button shows the active mode.
        self._size_mode = {"small": "large", "large": "hover", "hover": "small"}[self._size_mode]
        self._size_preset = SIZE_LARGE if self._size_mode == "large" else SIZE_SMALL
        self._size_btn.configure(text=self._size_label())
        self._hover_reset()
        self._navigate(self._cwd)
        self._save_if_remember()

    # ── hover-zoom preview ────────────────────────────────────────────────

    def _hover_enter(self, cell, event):
        """Hover on a Cell thumbnail (regular grid mode)."""
        if self._size_mode != "hover" or cell.is_folder or self._overlay is not None:
            return
        self._hover_show(cell, cell.path)

    def _hover_enter_slot(self, label, path, event):
        """Hover on an individual preview-row slot label (preview + hover mode)."""
        if self._size_mode != "hover" or self._overlay is not None or not path:
            return
        self._hover_show(label, path)

    def _hover_show(self, widget, path):
        """Shared entry point: schedule the popup for (widget, path)."""
        if self._hover_hide_job:
            self.root.after_cancel(self._hover_hide_job)
            self._hover_hide_job = None
        if self._hover_widget is widget and self._hover_popup is not None:
            return
        if self._hover_show_job:
            self.root.after_cancel(self._hover_show_job)
        self._hover_show_job = self.root.after(120, lambda: self._hover_render(widget, path))

    def _hover_leave(self, cell, event):
        if self._hover_show_job:
            self.root.after_cancel(self._hover_show_job)
            self._hover_show_job = None
        if self._hover_hide_job:
            self.root.after_cancel(self._hover_hide_job)
        self._hover_hide_job = self.root.after(60, self._hover_hide)

    def _hover_hide(self):
        self._hover_hide_job = None
        if self._hover_popup is not None:
            self._hover_popup.destroy()
            self._hover_popup = None
            self._hover_photo = None
            self._hover_widget = None

    def _hover_reset(self):
        """Cancel pending hover jobs, drop the popup, and clear the cache."""
        for attr in ("_hover_show_job", "_hover_hide_job"):
            job = getattr(self, attr)
            if job:
                self.root.after_cancel(job)
                setattr(self, attr, None)
        self._hover_hide()
        self._hover_pcache.clear()

    def _hover_render(self, widget, path):
        self._hover_show_job = None
        if self._size_mode != "hover" or self._overlay is not None or not widget.winfo_exists():
            return
        img = self._hover_preview_image(path)
        if img is None:
            return
        if self._hover_popup is None:
            self._hover_popup = tk.Frame(self.root, bg="#000000")
            self._hover_lbl = tk.Label(self._hover_popup, bg="#111111", bd=0)
            self._hover_lbl.pack(padx=2, pady=2)
        self._hover_photo = ImageTk.PhotoImage(img)
        self._hover_lbl.configure(image=self._hover_photo)
        self._hover_widget = widget

        # Place beside the widget, flipping left / clamping to stay on-screen
        self._hover_popup.update_idletasks()
        pw, ph = self._hover_popup.winfo_reqwidth(), self._hover_popup.winfo_reqheight()
        cx = widget.winfo_rootx() - self.root.winfo_rootx()
        cy = widget.winfo_rooty() - self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        px = cx + widget.winfo_width() + 8
        if px + pw > rw:
            px = cx - pw - 8
        px = max(0, min(px, rw - pw))
        py = max(0, min(cy, rh - ph))
        self._hover_popup.place(x=px, y=py)
        self._hover_popup.lift()

    def _hover_preview_image(self, path: Path):
        cached = self._hover_pcache.get(path)
        if cached is not None:
            return cached
        try:
            ext = path.suffix.lower()
            if ext == ".wif":
                data = path.read_bytes()
                if wif_format.is_encrypted(data):
                    try:
                        img, _, _ = wif_format.decode_try(data, self._passwords)
                    except wif_format.WrongPassword:
                        img = make_locked_icon(*HOVER_PREVIEW)
                else:
                    img, _ = wif_format.decode(data)
            elif ext == ".wvf":
                # No frame to show for a sealed video — a big sealed-video tile.
                locked = wvf_matching_password(path, self._passwords) is None
                img = make_wvf_icon(*HOVER_PREVIEW, locked=locked)
            elif ext in IMAGE_EXTS:
                img = Image.open(path)
                # JPEG draft mode downscales while decoding — far faster on big photos.
                if ext in (".jpg", ".jpeg"):
                    img.draft("RGB", HOVER_PREVIEW)
            else:
                return None
        except Exception:
            return None
        img = img.copy()
        img.thumbnail(HOVER_PREVIEW, Image.LANCZOS)
        if len(self._hover_pcache) >= 60:
            self._hover_pcache.pop(next(iter(self._hover_pcache)))
        self._hover_pcache[path] = img
        return img

    def _go_up(self):
        # Go-to-parent first clears an active tag/search view: a populated global
        # view returns to the real folder; a toolbar tag filter is emptied.
        if self._tag_view is not None or self._search_view is not None or self._dup_view is not None:
            self._navigate(self._cwd)   # exit tag/search/duplicate view, return to real folder
            return
        if self._tag_filter_var.get().strip():
            self._tag_filter_var.set("")
            self._ac_destroy()
            self._apply_filter()
            return
        parent = self._cwd.parent
        if parent != self._cwd:
            self._navigate(parent)

    def _go_to_typed(self):
        text = self._path_var.get().strip()
        # Bare drive letter e.g. "C:" → "C:\" so Path resolves to the root
        if len(text) == 2 and text[1] == ":":
            text += "\\"
        p = Path(text)
        if p.is_dir():
            self._canvas.focus_set()  # move focus away from the entry field
            self._navigate(p)
        else:
            messagebox.showerror("Not found", f"Not a valid folder:\n{p}")

    # ── sort / filter / status ────────────────────────────────────────────

    def _sort_key(self, p: Path):
        """Sort key for the active sort mode (natural name order is the stable
        tiebreaker, so 'img2' sorts before 'img10')."""
        mode = self._sort_var.get()
        try:
            if mode == SORT_DATE:
                return (p.stat().st_mtime, _natural_key(p.name))
            if mode == SORT_SIZE:
                return (p.stat().st_size, _natural_key(p.name))
            if mode == SORT_TYPE:
                return (p.suffix.lower(), _natural_key(p.name))
        except OSError:
            return (0, _natural_key(p.name))
        return _natural_key(p.name)

    def _on_sort_dir_changed(self):
        self._sort_reverse = self._sort_desc_var.get()
        self._navigate(self._cwd)
        self._save_if_remember()

    def _on_sort_changed(self):
        self._navigate(self._cwd)
        self._save_if_remember()

    def _on_filter_changed(self):
        self._navigate(self._cwd)
        self._save_if_remember()

    def _on_scrub_changed(self):
        # rows check self._scrub_var live, so no re-navigate is needed
        self._save_if_remember()

    def _on_recursive_changed(self):
        # only changes what Enter in the Filter box does — no re-navigate needed
        self._save_if_remember()

    # ── theme & saved settings ────────────────────────────────────────────

    def _on_theme_changed(self):
        self._apply_theme(self._theme_var.get())

    def _apply_theme(self, name: str):
        _set_theme(name)
        self.root.configure(bg=BG)
        self._rebuild_ui()
        self._save_if_remember()

    def _rebuild_ui(self):
        """Tear down and rebuild the toolbar/grid/statusbar so every widget picks
        up the new theme, then re-list the current folder."""
        self._overlay_close()
        self._hover_reset()
        for frame in (self._toolbar_frame, self._grid_container, self._status_frame):
            try:
                if frame is not None:
                    frame.destroy()
            except tk.TclError:
                pass
        self._build_toolbar()
        self._build_grid_area()
        self._build_statusbar()
        self._navigate(self._cwd)

    def _on_recycle_changed(self):
        if self._recycle_var.get() and not _TRASH:
            messagebox.showinfo(
                "Recycle Bin",
                "Install the 'send2trash' package to delete to the Recycle Bin:\n\n"
                "    pip install send2trash\n\n"
                "Until then, Delete still securely overwrites files.",
                parent=self.root)
        self._save_if_remember()

    def _on_remember_changed(self):
        self._save_config()      # writes the flag either way (and current settings)

    def _save_if_remember(self):
        if self._remember_var.get():
            self._save_config()

    def _save_config(self):
        cfg = {
            "remember":   self._remember_var.get(),
            "theme":      self._theme_var.get(),
            "sort":       self._sort_var.get(),
            "descending": self._sort_reverse,
            "size_mode":  self._size_mode,
            "filter":     self._filter_var.get(),
            "scrub":      self._scrub_var.get(),
            "show_tags":  self._show_tags_var.get(),
            "recursive":  self._recursive_var.get(),
            "recycle":    self._recycle_var.get(),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_settings(self):
        """Apply saved settings at startup when 'Remember settings' was on.
        Always ends by setting the theme globals for the initial build."""
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

        if cfg.get("remember"):
            self._remember_var.set(True)
            if cfg.get("theme") in THEMES:
                self._theme_var.set(cfg["theme"])
            if cfg.get("sort") in SORT_OPTIONS:
                self._sort_var.set(cfg["sort"])
            self._sort_reverse = bool(cfg.get("descending", False))
            self._sort_desc_var.set(self._sort_reverse)
            if cfg.get("filter") in FILTERS:
                self._filter_var.set(cfg["filter"])
            sm = cfg.get("size_mode")
            if sm in ("small", "large", "hover"):
                self._size_mode = sm
                self._size_preset = SIZE_LARGE if sm == "large" else SIZE_SMALL
            self._scrub_var.set(bool(cfg.get("scrub", False)))
            self._show_tags_var.set(bool(cfg.get("show_tags", True)))
            self._recursive_var.set(bool(cfg.get("recursive", False)))
            self._recycle_var.set(bool(cfg.get("recycle", False)))

        _set_theme(self._theme_var.get())

    def _focus_in_entry(self) -> bool:
        """True when a text Entry holds focus — global single-key shortcuts defer."""
        return isinstance(self.root.focus_get(), tk.Entry)

    def _recompute_view(self):
        """Rebuild the filtered view list from the model + current filename/tag filter."""
        flt     = self._filter_text_var.get().strip().lower()
        tag_flt = self._tag_filter_var.get().strip().lower()
        self._view = [
            it for it in self._items
            if ((not flt)     or flt     in it.path.name.lower())
            and ((not tag_flt) or any(tag_flt in t for t in self._get_tags(it.path)))
        ]

    def _apply_filter(self):
        """Recompute which items match the filter, then re-render from the top."""
        self._recompute_view()
        self._canvas.yview_moveto(0)
        self._reflow()
        self._update_status()

    def _on_filter_key(self, event):
        """KeyRelease in the Filter box drives the live (current-folder) filter;
        Enter / Escape are handled by their own bindings."""
        if event.keysym in ("Return", "KP_Enter", "Escape"):
            return
        self._apply_filter()

    def _filter_enter(self):
        """Enter in the Filter box: run a recursive search when 'Search subfolders'
        is on; otherwise the live filter already covers the current folder."""
        q = self._filter_text_var.get().strip()
        if q and self._recursive_var.get():
            self._enter_search_view(q)

    def _filter_escape(self):
        """Escape in the Filter box: clear it and leave any search/tag view."""
        self._filter_text_var.set("")
        if self._search_view is not None or self._tag_view is not None:
            self._navigate(self._cwd)
        else:
            self._apply_filter()

    def _update_status(self):
        """Refresh the status bar.  A single selected file shows its resolution
        and size; otherwise folder/file counts (and any active filter)."""
        sel = list(self._selected_items)
        if len(sel) == 1 and not sel[0].is_folder:
            self._status_var.set(self._single_file_status(sel[0].path))
            return
        if len(sel) >= 2:
            n_files   = sum(1 for c in sel if not c.is_folder)
            n_folders = len(sel) - n_files
            if n_folders == 0:
                self._status_var.set(f"{n_files} files selected")
            else:
                parts = []
                if n_folders:
                    parts.append(f"{n_folders} folder{'s' if n_folders > 1 else ''}")
                if n_files:
                    parts.append(f"{n_files} file{'s' if n_files > 1 else ''}")
                self._status_var.set(f"{len(sel)} selected  ({', '.join(parts)})")
            return

        # Recursive-search view has its own status line.
        if self._search_view is not None:
            n = len(self._items)
            tail = "   (searching…)" if self._searching else ""
            self._status_var.set(
                f"{n} match{'es' if n != 1 else ''} for “{self._search_view}”"
                f"   —   under {self._cwd}{tail}")
            return

        # Duplicate-finder view has its own status line.
        if self._dup_view is not None:
            n = len(self._items)
            if self._scanning_dups:
                self._status_var.set(f"Scanning for duplicates…   —   under {self._cwd}")
            elif n:
                self._status_var.set(
                    f"{n} duplicate file{'s' if n != 1 else ''} in "
                    f"{self._dup_group_count} group{'s' if self._dup_group_count != 1 else ''}"
                    f"   —   under {self._cwd}")
            else:
                self._status_var.set(f"No duplicates found   —   under {self._cwd}")
            return

        # Folders live in _items (grid mode) or _rows (preview mode); files in _items.
        total_f = sum(1 for it in self._items if it.is_folder) + len(self._rows)
        total_w = sum(1 for it in self._items if not it.is_folder)
        flt = self._filter_text_var.get().strip().lower()
        if flt:
            shown = (sum(1 for it in self._items if flt in it.path.name.lower())
                     + sum(1 for r in self._rows if flt in r.path.name.lower()))
            self._status_var.set(
                f"Showing {shown} of {total_f + total_w}   "
                f"(filter: \"{flt}\")   —   {self._cwd}"
            )
        else:
            self._status_var.set(
                f"{total_f} folder{'s' if total_f != 1 else ''}   "
                f"{total_w} file{'s' if total_w != 1 else ''}   —   {self._cwd}"
            )

    @staticmethod
    def _fmt_size(n: int) -> str:
        f = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
            if f < 1024 or unit == "PB":
                return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
            f /= 1024

    def _single_file_status(self, path: Path) -> str:
        """Status line for one selected file: name · resolution · size · EXIF · tags."""
        try:
            size = self._fmt_size(path.stat().st_size)
        except OSError:
            size = "?"
        res, exif_line = "", ""
        ext = path.suffix.lower()
        try:
            if ext == ".wif":
                data = path.read_bytes()
                if wif_format.is_encrypted(data):
                    try:
                        _img, meta, _ = wif_format.decode_try(data, self._passwords)
                        res = f"{meta['width']} × {meta['height']} px  🔒"
                    except wif_format.WrongPassword:
                        res = "🔒 encrypted (locked)"
                else:
                    info = wif_format.peek(data)
                    res = f"{info['width']} × {info['height']} px"
            elif ext == ".wvf":
                res = self._wvf_info(path)
            elif ext in IMAGE_EXTS:
                with Image.open(path) as im:
                    res = f"{im.size[0]} × {im.size[1]} px"
                    exif_line = self._exif_summary(im)
        except Exception:
            res = ""
        parts = [path.name] + ([res] if res else []) + [size]
        if exif_line:
            parts.append(exif_line)
        tags = self._get_tags(path)
        if tags:
            parts.append("🏷 " + ", ".join(tags))
        return "      ".join(parts)

    @staticmethod
    def _exif_summary(im) -> str:
        """Compact EXIF line (camera · date · ƒ/shutter/ISO) for the status bar,
        or '' when the image carries no usable EXIF (most PNG/BMP/GIF)."""
        try:
            exif = im.getexif()
        except Exception:
            return ""
        if not exif:
            return ""
        parts = []
        make  = str(exif.get(0x010F) or "").strip()
        model = str(exif.get(0x0110) or "").strip()
        cam = model if (model and model.startswith(make)) else f"{make} {model}".strip()
        if cam:
            parts.append(cam)
        try:
            sub = exif.get_ifd(0x8769)        # Exif sub-IFD — where the shot data lives
        except Exception:
            sub = {}
        dt = str(sub.get(0x9003) or exif.get(0x0132) or "").strip()
        if dt:
            parts.append(dt.split(" ")[0].replace(":", "-"))   # date as YYYY-MM-DD
        shot = []
        fno = sub.get(0x829D)                 # FNumber
        if fno:
            try:
                shot.append(f"ƒ/{float(fno):g}")
            except Exception:
                pass
        exp = sub.get(0x829A)                 # ExposureTime
        if exp:
            try:
                v = float(exp)
                shot.append(f"{v:g}s" if v >= 1 else f"1/{round(1 / v)}s")
            except Exception:
                pass
        iso = sub.get(0x8827)                 # ISOSpeedRatings
        if iso:
            try:
                iso_v = iso[0] if isinstance(iso, (tuple, list)) else iso
                shot.append(f"ISO{int(iso_v)}")
            except Exception:
                pass
        if shot:
            parts.append(" ".join(shot))
        return "   ·   ".join(parts)

    # ── passwords ─────────────────────────────────────────────────────────

    def _set_session_password(self, pw: str):
        """Make `pw` the single session password, replacing any previous one.
        Only one session password is ever held; every place that "remembers" a
        password the user typed routes through here so they can't accumulate."""
        if pw:
            self._passwords = [pw]

    def _add_password(self):
        """Set (or replace) the one session password, then refresh so newly
        unlockable thumbnails decrypt.  Only a single session password is kept:
        entering a new one replaces the old (use 'Clear session password' to
        forget it without setting a new one)."""
        if self._passwords and not messagebox.askyesno(
                "Session password",
                "A session password is already set.\n\nReplace it with a new one?",
                parent=self.root):
            return
        pw = simpledialog.askstring("Session password", "Enter a password to try:",
                                    show="*", parent=self.root)
        if not pw:
            return
        self._set_session_password(pw)
        # Also try to unlock the tag database with the new password
        if self._tags is None:
            self._ensure_tags_loaded(allow_prompt=False)
        self._navigate(self._cwd)   # re-render: locked files may now unlock

    def _clear_session_password(self):
        """Forget the session password.  Encrypted .wif / .wvf thumbnails re-lock
        until a new one is entered.  (Tags already decrypted this session stay
        loaded — clearing only affects which files unlock from here on.)"""
        if not self._passwords:
            messagebox.showinfo("Session password", "No session password is set.",
                                parent=self.root)
            return
        self._passwords = []
        self._navigate(self._cwd)   # re-render: previously unlocked files re-lock

    def _prompt_new_password(self):
        """Prompt with confirmation for a password to encrypt new v2 files.
        Returns the password, or None if cancelled / mismatched."""
        pw = simpledialog.askstring("Encryption password",
                                    "Password to encrypt the file(s) with:",
                                    show="*", parent=self.root)
        if not pw:
            return None
        if pw != simpledialog.askstring("Confirm password", "Re-enter the password:",
                                        show="*", parent=self.root):
            messagebox.showerror("Password mismatch", "The passwords didn't match.")
            return None
        self._set_session_password(pw)
        return pw

    # ── tags ──────────────────────────────────────────────────────────────

    def _ensure_tags_loaded(self, allow_prompt: bool = True) -> bool:
        """Load the tag store from disk (once per session).
        Returns True when the store is ready (possibly empty).
        If the file is encrypted and no known password works, prompts the user
        *once* when allow_prompt=True; returns False without prompting otherwise.
        After one failed/cancelled prompt self._tags_locked is set so we never
        overwrite a file we couldn't decrypt."""
        if self._tags is not None:
            return True          # already loaded (or deliberately left empty after failed unlock)

        if not TAGS_PATH.exists():
            self._tags = {}
            return True

        try:
            raw = TAGS_PATH.read_bytes()
        except Exception:
            self._tags = {}
            return True

        # Plain JSON (no encryption)?
        if not _wtag_is_encrypted(raw):
            try:
                self._tags = json.loads(raw.decode("utf-8"))
            except Exception:
                self._tags = {}
            return True

        # Encrypted — try every known session password first
        for pw in self._passwords:
            try:
                plain = _wtag_decrypt(raw, pw)
            except wif_format.WrongPassword:
                continue                         # this password isn't it — try the next
            # Right password: decryption succeeded.  A JSON error now means the
            # store is corrupt, not locked — keep features working (empty) but
            # block saves so we never overwrite the file we couldn't parse.
            self._tags_pw = pw
            try:
                self._tags = json.loads(plain)
            except Exception:
                self._tags = {}
                self._tags_locked = True
            return True

        if not allow_prompt:
            return False        # still None — caller skips tag features silently

        # Ask the user once
        pw = simpledialog.askstring(
            "Tags password",
            "The tag database is encrypted.\nEnter password to unlock tags:",
            show="*", parent=self.root,
        )
        if not pw:
            # User cancelled — keep _tags = None so the next access re-prompts;
            # set _tags_locked only to prevent accidentally overwriting the file.
            self._tags_locked = True
            return False

        try:
            plain = _wtag_decrypt(raw, pw)
        except wif_format.WrongPassword:
            messagebox.showerror(
                "Tags", "Wrong password — please try again.",
                parent=self.root)
            # Keep _tags = None so the user is prompted again next time
            self._tags_locked = True
            return False
        # Decryption succeeded — this is the right password.
        self._tags_pw     = pw
        self._tags_locked = False   # saves are now allowed
        self._set_session_password(pw)
        try:
            self._tags = json.loads(plain)
        except Exception:
            self._tags = {}             # decrypted but unparseable — keep file intact
            self._tags_locked = True
        return True

    def _save_tags(self):
        """Persist the in-memory tag store to disk.
        Encrypts if a tags password is known; otherwise plain JSON.
        Refuses to write if the on-disk file was locked (wrong/unknown password)."""
        if self._tags is None or self._tags_locked:
            return
        try:
            raw = json.dumps(self._tags, indent=2, sort_keys=True).encode("utf-8")
            if self._tags_pw:
                raw = _wtag_encrypt(raw, self._tags_pw)
            elif self._passwords and _WTAG_CRYPTO:
                # First time saving with encryption: use the first session password
                self._tags_pw = self._passwords[0]
                raw = _wtag_encrypt(raw, self._tags_pw)
            TAGS_PATH.write_bytes(raw)
        except Exception:
            pass

    def _get_tags(self, path: Path) -> list:
        if not self._tags:
            return []
        return self._tags.get(str(path), [])

    def _set_tags(self, path: Path, tags: list):
        if self._tags is None:
            self._tags = {}
        key     = str(path)
        cleaned = sorted({t.strip().lower() for t in tags if t.strip()})
        if cleaned:
            self._tags[key] = cleaned
        else:
            self._tags.pop(key, None)
        self._save_tags()

    def _prune_orphaned_tags(self):
        """Drop tag entries whose file/folder no longer exists on disk (e.g. items
        moved or renamed outside the browser)."""
        if not self._ensure_tags_loaded():
            return
        if not self._tags:
            messagebox.showinfo("Remove orphaned tags", "No tags are stored yet.",
                                parent=self.root)
            return

        def _missing(path_str: str) -> bool:
            try:
                return not Path(path_str).exists()
            except OSError:
                return False     # unreachable drive etc. — keep it, don't prune

        orphaned = [ps for ps in self._tags if _missing(ps)]
        if not orphaned:
            messagebox.showinfo("Remove orphaned tags",
                                "Nothing to remove — every tagged item still exists.",
                                parent=self.root)
            return
        preview = "\n".join(Path(ps).name for ps in orphaned[:15])
        if len(orphaned) > 15:
            preview += f"\n… and {len(orphaned) - 15} more"
        if not messagebox.askyesno(
                "Remove orphaned tags",
                f"Remove tags for {len(orphaned)} item(s) that no longer exist?\n\n{preview}",
                parent=self.root):
            return
        for ps in orphaned:
            self._tags.pop(ps, None)
        self._save_tags()
        self._reflow()
        n = len(orphaned)
        messagebox.showinfo("Remove orphaned tags",
                            f"Removed {n} orphaned entr{'y' if n == 1 else 'ies'}.",
                            parent=self.root)

    def _show_help(self):
        """A themed, scrollable cheat-sheet of controls (☰ → Help / shortcuts)."""
        win = tk.Toplevel(self.root)
        win.title("Weird Traveler — Help & shortcuts")
        win.configure(bg=BG)
        win.geometry("580x600")
        win.transient(self.root)
        win.bind("<Escape>", lambda e: win.destroy())

        frm = tk.Frame(win, bg=BG)
        frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        vsb = tk.Scrollbar(frm, orient=tk.VERTICAL)
        txt = tk.Text(frm, bg=CELL_BG, fg=FG, relief=tk.FLAT, wrap=tk.WORD,
                      font=("Segoe UI", 9), padx=14, pady=10,
                      yscrollcommand=vsb.set, highlightthickness=0, cursor="arrow")
        vsb.config(command=txt.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        txt.tag_configure("h", font=("Segoe UI", 10, "bold"), spacing1=10, spacing3=4)
        txt.tag_configure("k", font=("Consolas", 9, "bold"))
        txt.tag_configure("d", foreground=FG_DIM)

        sections = [
            ("Navigation", [
                ("Double-click folder", "enter it"),
                ("Backspace / ↑ Up", "parent folder (also exits tag/search view)"),
                ("Enter in path bar", "go to the typed path"),
                ("Arrow keys", "move selection · Shift extends · Home/End = first/last"),
                ("Mouse wheel", "scroll the grid"),
            ]),
            ("Selection", [
                ("Click", "select · Ctrl-click toggles · Shift-click range"),
                ("Drag empty space", "rubber-band select (Ctrl-drag adds)"),
                ("Ctrl+A", "select all files"),
                ("Right-click", "context menu · right-click empty space = New folder"),
            ]),
            ("Viewing", [
                ("Double-click image/.wif", "open the in-browser lightbox"),
                ("Double-click .wvf", "play the encrypted video (streamed via ffplay — no plaintext on disk)"),
                ("←/→ or wheel", "previous / next image in the lightbox"),
                ("Filmstrip", "click a thumbnail at the bottom to jump to it"),
                ("Esc / background / ✕", "close the lightbox"),
                ("Size button", "Small → Large → Hover-zoom"),
                ("⧉ Preview", "folder-content preview rows"),
            ]),
            ("Search & tags", [
                ("Ctrl+F", "focus the Filter box"),
                ("Filter box", "filter the current folder by name"),
                ("Search subfolders + Enter", "recursive name search (toggle in ☰)"),
                ("Tags box", "filter by tag (autocompletes)"),
                ("🏷 Tag search", "overview · query: a,b (and)  a|b (or)  -c (not)"),
            ]),
            ("Files", [
                ("Rename… / Move… / New folder…", "right-click menu — tags follow renames"),
                ("Batch rename…", "right-click 2+ files — pattern with {name}, {n}, {ext}"),
                ("Ctrl+C / Ctrl+X / Ctrl+V", "copy / cut / paste files (paste into the open folder)"),
                ("Drag onto a folder", "move the selection into it (tags follow)"),
                ("Convert to WIF v2/v3", "encode images (never overwrites an existing .wif)"),
                ("Export to image…", "decode .wif back to PNG/JPEG/BMP/WEBP"),
                ("Seal to WVF 🔒…", "encrypt a video into a .wvf container"),
                ("Decrypt to video…", "restore a .wvf back to its original video"),
                ("Delete", "secure-overwrite, then delete (or Recycle Bin — see ☰)"),
            ]),
            ("Encryption & upkeep", [
                ("🔑 Unlock", "set the one session password for locked .wif / .wvf files"),
                ("🔓 Clear session password", "forget it (files re-lock); then Unlock to set a new one"),
                ("Encryption ▸", "encrypt / change password / remove (per .wif)"),
                ("⧉ Find duplicates", "group byte-identical files under this folder (☰)"),
                ("🧹 Remove orphaned tags", "drop tags for files that no longer exist"),
            ]),
        ]
        for head, rows in sections:
            txt.insert(tk.END, head + "\n", "h")
            for key, desc in rows:
                txt.insert(tk.END, "   " + key, "k")
                txt.insert(tk.END, "   " + desc + "\n", "d")
        txt.configure(state=tk.DISABLED)

    # ── tag query helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_tag_query(raw: str):
        """Parse a tag query into (pos_groups, neg_terms, is_multi).

        Comma = AND within a group; pipe = OR between groups; -tag = exclude.
        pos_groups is a list of AND-groups; groups are OR-ed together."""
        or_groups_raw = [g.strip() for g in raw.lower().split("|") if g.strip()]
        neg, pos_groups = [], []
        for grp in or_groups_raw:
            tokens = [t.strip() for t in grp.split(",") if t.strip()]
            pos_in = [t     for t in tokens if not t.startswith("-")]
            neg_in = [t[1:] for t in tokens if t.startswith("-") and len(t) > 1]
            neg.extend(neg_in)
            if pos_in:
                pos_groups.append(pos_in)
        multi = "," in raw or "|" in raw or bool(neg)
        return pos_groups, neg, multi

    @staticmethod
    def _tag_query_matches(file_tags: list, pos_groups: list, neg: list) -> bool:
        """Return True when file_tags satisfies the compiled query."""
        if neg and any(any(n in t for t in file_tags) for n in neg):
            return False
        if not pos_groups:
            return True
        return any(
            all(any(p in t for t in file_tags) for p in grp)
            for grp in pos_groups
        )

    # ── tag autocomplete helpers ───────────────────────────────────────────────

    def _all_known_tags(self) -> list:
        """Sorted list of every unique tag in the store."""
        tags: set = set()
        for lst in (self._tags or {}).values():
            tags.update(lst)
        return sorted(tags)

    def _on_tag_filter_key(self, event):
        """KeyRelease on the toolbar tag-filter entry: schedule debounced update."""
        if event.keysym in ("Down", "Up", "Escape", "Return", "KP_Enter"):
            return
        if self._tag_filter_job:
            self.root.after_cancel(self._tag_filter_job)
        self._tag_filter_job = self.root.after(120, self._apply_tag_filter_update)

    def _apply_tag_filter_update(self):
        """Debounced: apply filter + refresh autocomplete dropdown."""
        self._tag_filter_job = None
        self._apply_filter()
        text   = self._tag_filter_var.get().strip().lower()
        known  = self._all_known_tags()
        matches = [t for t in known if not text or text in t]
        if matches:
            self._ac_show(matches)
        else:
            self._ac_destroy()

    def _tag_filter_move(self, delta: int):
        """Up/Down in the tag-filter entry: move the dropdown highlight while focus
        stays in the entry (overrideredirect popups don't take keyboard focus on
        Windows).  Returns 'break' so the key never reaches the grid bindings."""
        lb = self._ac_lb
        if lb is None or not lb.winfo_exists() or lb.size() == 0:
            return "break"
        cur = lb.curselection()
        i   = cur[0] if cur else (-1 if delta > 0 else 0)
        i   = max(0, min(lb.size() - 1, i + delta))
        lb.selection_clear(0, tk.END)
        lb.selection_set(i)
        lb.activate(i)
        lb.see(i)
        return "break"

    def _tag_filter_accept(self, event):
        """Enter in the tag-filter entry: accept the highlighted suggestion if the
        dropdown is open; otherwise let the keypress fall through normally."""
        lb = self._ac_lb
        if lb is not None and lb.winfo_exists() and lb.curselection():
            t = lb.get(lb.curselection()[0]).strip()
            self._tag_filter_var.set(t)
            self._tag_filter_entry.icursor(tk.END)
            self._ac_destroy()
            self._apply_filter()
            return "break"
        return None

    def _tag_filter_escape(self, event):
        """Escape in the tag-filter entry: clear filter + close dropdown.
        Returns 'break' so it doesn't reach the root Escape binding (overlay close)."""
        self._tag_filter_var.set("")
        self._ac_destroy()
        self._apply_filter()
        return "break"

    def _ac_show(self, matches: list):
        """Create or refresh the floating autocomplete dropdown below the tag filter entry."""
        e = self._tag_filter_entry
        if not e.winfo_exists():
            return
        if self._ac_popup is None or not self._ac_popup.winfo_exists():
            self._ac_popup = tk.Toplevel(self.root)
            self._ac_popup.wm_overrideredirect(True)
            self._ac_popup.configure(bg=CELL_BG)
            self._ac_popup.attributes("-topmost", True)
            self._ac_lb = tk.Listbox(
                self._ac_popup, bg=CELL_BG, fg=FG,
                selectbackground=CELL_HL, selectforeground=ACTIVE_FG,
                relief=tk.FLAT, font=("Segoe UI", 9), activestyle="none",
                borderwidth=0, highlightthickness=1, highlightbackground=CELL_HL,
            )
            self._ac_lb.pack(fill=tk.BOTH, expand=True)
            # Focus stays in the entry; the dropdown is mouse-clickable only.
            self._ac_lb.bind("<ButtonRelease-1>", self._ac_select)
        self._ac_lb.delete(0, tk.END)
        for t in matches[:8]:
            self._ac_lb.insert(tk.END, f"  {t}")
        n = min(8, len(matches))
        x = e.winfo_rootx()
        y = e.winfo_rooty() + e.winfo_height() + 1
        w = max(e.winfo_width(), 100)
        self._ac_popup.geometry(f"{w}x{n * 20 + 2}+{x}+{y}")

    def _ac_destroy(self):
        if self._ac_popup is not None:
            try:
                self._ac_popup.destroy()
            except tk.TclError:
                pass
        self._ac_popup = None
        self._ac_lb    = None

    def _ac_maybe_destroy(self):
        """Close the dropdown only if focus left it entirely — keeps it alive when
        the user steps from the entry into the dropdown (or back)."""
        if self._ac_popup is None:
            return
        w = self.root.focus_get()
        if w is not None and w.winfo_toplevel() is self._ac_popup:
            return   # focus moved into the dropdown — keep it open
        self._ac_destroy()

    def _ac_select(self, event=None):
        if self._ac_lb is None:
            return
        sel = self._ac_lb.curselection()
        if sel:
            t = self._ac_lb.get(sel[0]).strip()
            self._tag_filter_var.set(t)
            self._tag_filter_entry.icursor(tk.END)
            self._apply_filter()
        self._ac_destroy()
        self._tag_filter_entry.focus_set()

    # ── tag edit dialog ────────────────────────────────────────────────────────

    def _edit_tags_dialog(self, cells):
        """Right-click → Edit tags with live autocomplete suggestions."""
        if not cells:
            return
        self._ensure_tags_loaded()

        if len(cells) == 1:
            path     = cells[0].path
            existing = ", ".join(self._get_tags(path))
            label    = "Folder" if cells[0].is_folder else "File"
            prompt   = f"Tags for  {path.name}  ({label}):"
        else:
            all_sets  = [set(self._get_tags(c.path)) for c in cells]
            common    = sorted(set.intersection(*all_sets)) if all_sets else []
            existing  = ", ".join(common)
            n_folders = sum(1 for c in cells if c.is_folder)
            n_files   = len(cells) - n_folders
            desc = ", ".join(filter(None, [
                f"{n_folders} folder{'s' if n_folders > 1 else ''}" if n_folders else "",
                f"{n_files} file{'s' if n_files > 1 else ''}"       if n_files   else "",
            ]))
            prompt = f"Tags for {len(cells)} items  ({desc})\n(replaces existing on all):"

        known = self._all_known_tags()

        dlg = tk.Toplevel(self.root)
        dlg.title("Edit tags")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        for _k in ("<Down>", "<Up>", "<Left>", "<Right>",
                   "<Delete>", "<BackSpace>", "<Home>", "<End>"):
            dlg.bind(_k, lambda e: "break")

        frm = tk.Frame(dlg, bg=BG)
        frm.pack(fill=tk.BOTH, expand=True, padx=14, pady=(12, 8))

        tk.Label(frm, text=prompt, bg=BG, fg=FG, justify=tk.LEFT,
                 font=("Segoe UI", 9), wraplength=340).pack(anchor=tk.W, pady=(0, 4))

        entry_var = tk.StringVar(value=existing)
        entry = tk.Entry(frm, textvariable=entry_var, width=42, bg=BTN_BG, fg=FG,
                         insertbackground=FG, relief=tk.FLAT, font=("Segoe UI", 10))
        entry.pack(fill=tk.X, ipady=3)

        tk.Label(frm, text="Comma-separated  •  Tab or click suggestion to insert",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8)).pack(anchor=tk.W, pady=(2, 4))

        sug_frm = tk.Frame(frm, bg=BG)
        sug_frm.pack(fill=tk.X)
        vsb = tk.Scrollbar(sug_frm, orient=tk.VERTICAL)
        lb  = tk.Listbox(sug_frm, yscrollcommand=vsb.set, height=5,
                         bg=CELL_BG, fg=FG, selectbackground=CELL_HL,
                         selectforeground=ACTIVE_FG, relief=tk.FLAT,
                         font=("Segoe UI", 9), activestyle="none")
        vsb.config(command=lb.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        lb.pack(fill=tk.X)

        result = [None]

        def _partial():
            return entry_var.get().split(",")[-1].strip().lower()

        def _used():
            parts = entry_var.get().split(",")
            return {p.strip().lower() for p in parts[:-1] if p.strip()}

        def update_suggestions(*_):
            partial = _partial()
            used    = _used()
            lb.delete(0, tk.END)
            for t in known:
                if t in used:
                    continue
                if not partial or partial in t:
                    lb.insert(tk.END, f"  {t}")

        def insert_tag(tag: str):
            parts = entry_var.get().split(",")
            parts[-1] = " " + tag
            entry_var.set(",".join(parts) + ", ")
            entry.icursor(tk.END)
            entry.focus_set()

        def on_lb_pick(*_):
            sel = lb.curselection()
            if sel:
                insert_tag(lb.get(sel[0]).strip())

        def on_tab(event):
            sel = lb.curselection()
            if sel:
                on_lb_pick()
            elif lb.size() > 0:
                lb.selection_set(0)
                on_lb_pick()
            return "break"

        def confirm(*_):
            result[0] = entry_var.get()
            dlg.destroy()

        def cancel(*_):
            dlg.destroy()

        entry_var.trace_add("write", update_suggestions)
        lb.bind("<ButtonRelease-1>", on_lb_pick)
        lb.bind("<Double-Button-1>", on_lb_pick)
        lb.bind("<Return>",          on_lb_pick)
        entry.bind("<Tab>",    on_tab)
        entry.bind("<Return>", confirm)
        entry.bind("<Escape>", cancel)
        dlg.bind("<Escape>",   cancel)

        btn_frm = tk.Frame(dlg, bg=TOOLBAR_BG, pady=6)
        btn_frm.pack(fill=tk.X)
        bcfg = dict(bg=BTN_BG, fg=FG, relief=tk.FLAT, font=("Segoe UI", 9),
                    activebackground=BTN_ACTIVE, activeforeground=ACTIVE_FG, padx=12, pady=3)
        tk.Button(btn_frm, text="OK",     command=confirm, **bcfg).pack(side=tk.LEFT, padx=(10, 4))
        tk.Button(btn_frm, text="Cancel", command=cancel,  **bcfg).pack(side=tk.LEFT, padx=4)

        update_suggestions()
        dlg.after(50, lambda: (entry.focus_set(), entry.icursor(tk.END)))
        dlg.wait_window()

        if result[0] is None:
            return
        cleaned = sorted({t.strip().lower() for t in result[0].split(",") if t.strip()})
        n = len(cells)
        if n > 1:
            self._status_var.set(f"Tagging {n} files…")
            self.root.update_idletasks()
        if self._tags is None:
            self._tags = {}
        for c in cells:
            key = str(c.path)
            if cleaned:
                self._tags[key] = cleaned
            else:
                self._tags.pop(key, None)
        self._save_tags()
        self._reflow()
        self._status_var.set(f"Tagged {n} file{'s' if n != 1 else ''}")
        self.root.after(2000, self._update_status)

    def _show_tag_search(self):
        """Tag overview: lists every unique tag with its file count; double-click to browse."""
        self._ensure_tags_loaded()

        win = tk.Toplevel(self.root)
        win.title("Tags")
        win.configure(bg=BG)
        win.geometry("430x520")
        win.transient(self.root)
        win.grab_set()

        for _k in ("<Down>", "<Up>", "<Left>", "<Right>",
                   "<Return>", "<KP_Enter>", "<Delete>", "<BackSpace>",
                   "<Home>", "<End>"):
            win.bind(_k, lambda e: "break")
        win.bind("<Escape>", lambda e: win.destroy() or "break")

        # ── filter bar ──
        bar = tk.Frame(win, bg=TOOLBAR_BG, pady=6)
        bar.pack(fill=tk.X)
        tk.Label(bar, text="Filter:", bg=TOOLBAR_BG, fg=FG,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(10, 4))
        filter_var   = tk.StringVar()
        filter_entry = tk.Entry(bar, textvariable=filter_var, bg=BTN_BG, fg=FG,
                                insertbackground=FG, relief=tk.FLAT,
                                font=("Segoe UI", 10), width=26)
        filter_entry.pack(side=tk.LEFT, padx=4, ipady=3)

        # ── cheat sheet ──
        cheat_frm = tk.Frame(win, bg=BG, pady=4)
        cheat_frm.pack(fill=tk.X, padx=10)
        cheat = [
            ("tag",            "filter tag list"),
            ("tag1, tag2",     "files with BOTH tags (AND)"),
            ("tag1 | tag2",    "files with EITHER tag (OR)"),
            ("-tag",           "exclude tag from results"),
            ("landscape, -bw", "combine freely"),
        ]
        for syntax, desc in cheat:
            row = tk.Frame(cheat_frm, bg=BG)
            row.pack(fill=tk.X)
            tk.Label(row, text=syntax, bg=BG, fg=FG,
                     font=("Courier New", 8), width=20, anchor=tk.W).pack(side=tk.LEFT)
            tk.Label(row, text=desc,   bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 8),    anchor=tk.W).pack(side=tk.LEFT)
        tk.Label(cheat_frm,
                 text="Double-click or Enter → browse in grid  •  right-click tag → rename",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 7), anchor=tk.W).pack(fill=tk.X, pady=(2, 0))

        # ── tag list ──
        lst_frm = tk.Frame(win, bg=BG)
        lst_frm.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))
        vsb = tk.Scrollbar(lst_frm, orient=tk.VERTICAL)
        lb  = tk.Listbox(lst_frm, yscrollcommand=vsb.set, bg=CELL_BG, fg=FG,
                         selectbackground=CELL_HL, selectforeground=ACTIVE_FG,
                         relief=tk.FLAT, font=("Segoe UI", 10), activestyle="none", width=50)
        vsb.config(command=lb.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        lb.pack(fill=tk.BOTH, expand=True)

        # ── button bar ──
        btn_frm = tk.Frame(win, bg=TOOLBAR_BG, pady=6)
        btn_frm.pack(fill=tk.X)

        shown_tags:  list = []
        shown_paths: list = []

        def _build_counts():
            from collections import Counter
            c: Counter = Counter()
            for tags in (self._tags or {}).values():
                c.update(tags)
            return c

        def _is_multi():
            _, _, multi = WifBrowser._parse_tag_query(filter_var.get())
            return multi

        def refresh(*_):
            raw = filter_var.get()
            lb.delete(0, tk.END)
            shown_tags.clear()
            shown_paths.clear()
            pos_groups, neg, multi = WifBrowser._parse_tag_query(raw)

            if multi:
                store   = self._tags or {}
                matches = [(Path(ps), ft) for ps, ft in store.items()
                           if WifBrowser._tag_query_matches(ft, pos_groups, neg)]
                matches.sort(key=lambda x: x[0].name.lower())
                for p, ft in matches:
                    lb.insert(tk.END, f"  {p.name}  [{', '.join(ft)}]")
                    shown_paths.append(p)
                n       = len(shown_paths)
                pos_str = " | ".join(" + ".join(grp) for grp in pos_groups)
                neg_str = "  ".join(f"−{t}" for t in neg)
                label   = "  ".join(filter(None, [pos_str, neg_str]))
                win.title(f"Tags — {n} file{'s' if n != 1 else ''}  [{label}]")
            else:
                q      = raw.strip().lower()
                counts = _build_counts()
                for tag in sorted(counts):
                    if not q or q in tag:
                        lb.insert(tk.END, f"  {tag}  ({counts[tag]})")
                        shown_tags.append(tag)
                total   = len(counts)
                showing = len(shown_tags)
                if q:
                    win.title(f"Tags — {showing} of {total}")
                else:
                    win.title(f"Tags — {total} tag{'s' if total != 1 else ''}")

        def rename_tag(*_):
            sel = lb.curselection()
            if not sel or _is_multi():
                return
            old = shown_tags[sel[0]]
            new = simpledialog.askstring(
                "Rename tag", f"Rename  \"{old}\"  to:",
                initialvalue=old, parent=win)
            if not new:
                return
            new = new.strip().lower()
            if not new or new == old:
                return
            for path_str, tags in list((self._tags or {}).items()):
                if old in tags:
                    self._set_tags(Path(path_str), [new if t == old else t for t in tags])
            refresh()
            self._reflow()

        def show_in_browser(*_):
            raw = filter_var.get()
            _, _, multi = WifBrowser._parse_tag_query(raw)
            if multi:
                if not raw.strip():
                    return
                win.destroy()
                self._enter_tag_view(raw.strip())
            else:
                sel = lb.curselection()
                if not sel:
                    return
                win.destroy()
                self._enter_tag_view(shown_tags[sel[0]])

        def on_lb_rclick(event):
            if _is_multi():
                return
            idx = lb.nearest(event.y)
            if idx < 0 or idx >= len(shown_tags):
                return
            lb.selection_clear(0, tk.END)
            lb.selection_set(idx)
            ctx = tk.Menu(win, tearoff=0)
            ctx.configure(bg=TOOLBAR_BG, fg=FG, activebackground=CELL_HL,
                          activeforeground=ACTIVE_FG, relief=tk.FLAT, bd=0)
            ctx.add_command(label=f"Rename \"{shown_tags[idx]}\"…", command=rename_tag)
            ctx.tk_popup(event.x_root, event.y_root)

        filter_var.trace_add("write", refresh)
        lb.bind("<Double-Button-1>", show_in_browser)
        lb.bind("<Button-3>",        on_lb_rclick)
        win.bind("<Return>",    show_in_browser)
        win.bind("<KP_Enter>",  show_in_browser)

        bcfg = dict(bg=BTN_BG, fg=FG, relief=tk.FLAT, font=("Segoe UI", 9),
                    activebackground=BTN_ACTIVE, activeforeground=ACTIVE_FG, padx=10, pady=3)
        tk.Button(btn_frm, text="Show in browser", command=show_in_browser, **bcfg).pack(side=tk.LEFT, padx=(10, 4))
        tk.Button(btn_frm, text="Rename tag…",     command=rename_tag,      **bcfg).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_frm, text="Close",           command=win.destroy,     **bcfg).pack(side=tk.LEFT, padx=4)

        refresh()
        win.after(50, filter_entry.focus_set)

    def _enter_tag_view(self, query: str):
        """Populate the browser grid with every tagged item matching *query* across all folders."""
        if not self._ensure_tags_loaded():
            return
        q = query.strip().lower()
        if not q:
            return

        self._tag_view = q
        self._search_view = None
        self._tag_filter_var.set("")   # toolbar tag filter is redundant in this mode
        self._gen += 1

        self._selected_items.clear()
        self._anchor_item = None
        self._lead_item   = None
        self._hover_reset()
        for r in self._rows:
            try:
                r.destroy()
            except tk.TclError:
                pass
        self._rows = []

        self._items = []
        store      = self._tags or {}
        pos_groups, neg, _ = self._parse_tag_query(q)

        pos_str   = " | ".join(" + ".join(grp) for grp in pos_groups)
        neg_str   = "  ".join(f"−{t}" for t in neg)
        display_q = "  ".join(filter(None, [pos_str, neg_str])) or q

        for path_str, tags in store.items():
            if self._tag_query_matches(tags, pos_groups, neg):
                p      = Path(path_str)
                is_dir = p.is_dir()
                kind   = "folder" if is_dir else "file"
                it     = Item(p, is_dir, kind)
                parent = p.parent.name or str(p.parent)
                it.display_name = f"{parent}/{p.name}"
                self._items.append(it)
        self._items.sort(key=lambda it: _natural_key(it.path.name))

        self._path_var.set(f"🏷  {display_q}")
        self.root.title(f"Weird Traveler — Tag: {display_q}")

        self._recompute_view()
        self._update_status()

        for c in self._pool:
            wid = getattr(c, "_winid", None)
            if wid is not None:
                self._canvas.itemconfigure(wid, state="hidden")

        self.root.update_idletasks()
        self._canvas.yview_moveto(0)
        self._reflow()
        self._canvas.focus_set()

    # ── recursive (subfolder) search ───────────────────────────────────────

    def _enter_search_view(self, query: str):
        """Fill the grid with every file under the current folder whose name
        contains *query*, walking subfolders on a background thread."""
        q = query.strip().lower()
        if not q:
            return
        root_dir = self._cwd
        self._tag_view = None
        self._search_view = query.strip()
        self._searching = True
        self._gen += 1
        gen = self._gen

        self._selected_items.clear()
        self._anchor_item = None
        self._lead_item   = None
        self._hover_reset()
        for r in self._rows:
            try:
                r.destroy()
            except tk.TclError:
                pass
        self._rows = []
        self._items = []

        self._path_var.set(f"🔍  {query.strip()}")
        self.root.title(f"Weird Traveler — Search: {query.strip()}")
        self._recompute_view()
        self._update_status()

        for c in self._pool:
            wid = getattr(c, "_winid", None)
            if wid is not None:
                self._canvas.itemconfigure(wid, state="hidden")

        self.root.update_idletasks()
        self._canvas.yview_moveto(0)
        self._reflow()
        threading.Thread(target=self._search_worker,
                         args=(root_dir, q, gen), daemon=True).start()

    def _search_worker(self, root_dir: Path, q: str, gen: int):
        """Walk root_dir on a background thread; stream matches back in batches.
        Respects the active file-type filter and stops if the user navigates away."""
        f_mode = self._filter_var.get()
        batch: list = []
        found = 0

        def flush():
            if batch and gen == self._gen:
                self._ui_q.put((self._add_search_results, (list(batch), gen)))
            batch.clear()

        try:
            for dirpath, _dirnames, filenames in os.walk(root_dir):
                if gen != self._gen:
                    return                      # navigated away — abandon the walk
                for fn in filenames:
                    if q not in fn.lower():
                        continue
                    ext = os.path.splitext(fn)[1].lower()
                    if f_mode == FILTER_WIF and ext not in WEIRD_EXTS:
                        continue
                    if f_mode == FILTER_MEDIA and ext not in MEDIA_EXTS:
                        continue
                    batch.append(Path(dirpath) / fn)
                    found += 1
                    if len(batch) >= 60:
                        flush()
                if found >= 5000:               # soft cap — keep memory/UI sane
                    break
        except Exception:
            pass
        flush()
        if gen == self._gen:
            self._ui_q.put((self._search_done, (gen,)))

    def _add_search_results(self, paths: list, gen: int):
        """Main thread: append a batch of search hits and re-render."""
        if gen != self._gen or self._search_view is None:
            return
        for p in paths:
            it = Item(p, False, "file")
            parent = p.parent.name or str(p.parent)
            it.display_name = f"{parent}/{p.name}"
            self._items.append(it)
        self._recompute_view()
        self._reflow()
        self._update_status()

    def _search_done(self, gen: int):
        """Main thread: search finished — final sort + status."""
        if gen != self._gen or self._search_view is None:
            return
        self._searching = False
        self._items.sort(key=lambda it: _natural_key(it.path.name))
        self._recompute_view()
        self._reflow()
        self._update_status()

    # ── duplicate finder ──────────────────────────────────────────────────────

    def _find_duplicates(self):
        """Scan the current folder tree for files with identical contents and
        show them grouped together in a global view (☰ → Find duplicates)."""
        root_dir = self._cwd
        if not messagebox.askyesno(
                "Find duplicates",
                f"Scan for duplicate files under:\n{root_dir}\n\n"
                "Files with byte-for-byte identical contents are grouped together.\n"
                "The active file-type filter applies.",
                parent=self.root):
            return

        self._tag_view = None
        self._search_view = None
        self._searching = False
        self._dup_view = str(root_dir)
        self._scanning_dups = True
        self._dup_group_count = 0
        self._gen += 1
        gen = self._gen

        self._selected_items.clear()
        self._anchor_item = None
        self._lead_item   = None
        self._hover_reset()
        for r in self._rows:
            try:
                r.destroy()
            except tk.TclError:
                pass
        self._rows = []
        self._items = []

        self._path_var.set(f"⧉  Duplicates under {root_dir}")
        self.root.title(f"Weird Traveler — Duplicates: {root_dir}")
        self._recompute_view()
        self._update_status()

        for c in self._pool:
            wid = getattr(c, "_winid", None)
            if wid is not None:
                self._canvas.itemconfigure(wid, state="hidden")

        self.root.update_idletasks()
        self._canvas.yview_moveto(0)
        self._reflow()
        threading.Thread(target=self._dup_worker, args=(root_dir, gen), daemon=True).start()

    @staticmethod
    def _hash_file(path: Path):
        """SHA-256 of a file's bytes (streamed), or None if it can't be read."""
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            return None
        return h.digest()

    def _dup_worker(self, root_dir: Path, gen: int):
        """Walk root_dir on a background thread, group files by size, then hash
        only the size-collisions to find exact duplicates.  Posts the result back."""
        f_mode = self._filter_var.get()
        by_size: dict = {}
        try:
            for dirpath, _dirnames, filenames in os.walk(root_dir):
                if gen != self._gen:
                    return                      # navigated away — abandon the walk
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if f_mode == FILTER_WIF and ext not in WEIRD_EXTS:
                        continue
                    if f_mode == FILTER_MEDIA and ext not in MEDIA_EXTS:
                        continue
                    p = Path(dirpath) / fn
                    try:
                        size = p.stat().st_size
                    except OSError:
                        continue
                    by_size.setdefault(size, []).append(p)
        except Exception:
            pass

        # Only files that share a size with another file can be duplicates — hash those.
        by_hash: dict = {}
        for size, paths in by_size.items():
            if len(paths) < 2:
                continue
            for p in paths:
                if gen != self._gen:
                    return
                digest = self._hash_file(p)
                if digest is not None:
                    by_hash.setdefault(digest, []).append(p)

        groups = [sorted(paths, key=lambda p: _natural_key(p.name))
                  for paths in by_hash.values() if len(paths) > 1]
        groups.sort(key=lambda g: (-len(g), _natural_key(g[0].name)))   # biggest first
        if gen == self._gen:
            self._ui_q.put((self._dup_done, (groups, gen)))

    def _dup_done(self, groups: list, gen: int):
        """Main thread: build the grid from duplicate groups, numbered so members
        of the same group read together."""
        if gen != self._gen or self._dup_view is None:
            return
        self._scanning_dups = False
        self._dup_group_count = len(groups)
        self._items = []
        for gi, paths in enumerate(groups, 1):
            for p in paths:
                it = Item(p, False, "file")
                parent = p.parent.name or str(p.parent)
                it.display_name = f"[{gi}]  {parent}/{p.name}"
                self._items.append(it)
        self._recompute_view()
        self._reflow()
        self._update_status()

    def _viewer_env(self):
        """Environment for a spawned viewer, carrying session passwords so it
        reuses them instead of re-prompting."""
        env = os.environ.copy()
        env["WIF_PW"] = json.dumps(self._passwords)
        return env

    def _open_item(self, item):
        """Open an Item: enter folders, preview viewable files in the lightbox,
        hand everything else to the OS."""
        if item.is_folder:
            self._navigate(item.path)
        elif item.path.suffix.lower() == ".wvf":
            self._play_wvf(item.path)            # encrypted video → decrypt & play
        elif item.path.suffix.lower() in VIEWER_EXTS:
            self._show_overlay(item.path)        # quick in-browser preview
        else:
            os.startfile(item.path)

    def _on_double_click(self, widget):
        item = getattr(widget, "item", None)
        if item is not None:                       # a grid Cell
            self._open_item(item)
        elif getattr(widget, "is_folder", False):  # a PreviewRow (folder)
            self._navigate(widget.path)

    def _on_cell_select(self, cell, event):
        """Left-click: single select.  Ctrl+click: toggle.  Shift+click: range.
        A plain click on an item already part of a multi-selection defers to the
        release (see _cell_drag_release) so the whole group can be dragged."""
        item = cell.item
        if item is None:
            return
        self._pending_collapse = None
        # Record a possible drag start regardless of modifiers.
        self._drag_candidate = cell
        self._drag_start     = (event.x_root, event.y_root)
        self._dragging       = False
        shift = bool(event.state & 0x0001)
        ctrl  = bool(event.state & 0x0004)
        view = self._view

        if shift and self._anchor_item in view and item in view:
            a = view.index(self._anchor_item)
            b = view.index(item)
            lo, hi = min(a, b), max(a, b)
            self._selected_items = set(view[lo : hi + 1])
            # anchor stays fixed during shift-range extension
        elif ctrl:
            if item in self._selected_items:
                self._selected_items.discard(item)
            else:
                self._selected_items.add(item)
            self._anchor_item = item
        elif item in self._selected_items and len(self._selected_items) > 1:
            # Keep the group intact for a possible drag; collapse on release if not.
            self._pending_collapse = item
            self._anchor_item = item
            self._lead_item = item
            return
        else:
            self._selected_items = {item}
            self._anchor_item = item
        self._lead_item = item
        self._reflow()           # re-tint visible cells from the new selection
        self._update_status()

    def _on_cell_right_click(self, cell, event):
        """Right-click: select the item (if not already selected) then show menu."""
        item = cell.item
        if item is not None and item not in self._selected_items:
            self._selected_items = {item}
            self._anchor_item = item
            self._lead_item = item
            self._reflow()
        self._update_status()
        self._show_context_menu(event)

    def _select_all(self):
        """Ctrl+A — select every file in the current (filtered) view, skipping folders."""
        self._selected_items = {it for it in self._view if not it.is_folder}
        self._anchor_item = None
        self._lead_item = None
        self._reflow()
        self._update_status()

    # ── clipboard: copy / cut / paste ──────────────────────────────────────

    def _copy_to_clipboard(self):
        paths = [it.path for it in self._selected_items]
        if not paths:
            return
        self._clipboard, self._clipboard_cut = paths, False
        self._reflow()           # clear any leftover cut-dim
        n = len(paths)
        self._status_var.set(f"Copied {n} item{'s' if n != 1 else ''} — paste with Ctrl+V")

    def _cut_to_clipboard(self):
        paths = [it.path for it in self._selected_items]
        if not paths:
            return
        self._clipboard, self._clipboard_cut = paths, True
        self._reflow()           # dim the cut items
        n = len(paths)
        self._status_var.set(f"Cut {n} item{'s' if n != 1 else ''} — paste with Ctrl+V")

    def _paste_from_clipboard(self, dest=None):
        if not self._clipboard:
            return
        if self._converting:
            messagebox.showinfo("Paste", "A file operation is already running.")
            return
        dest = Path(dest) if dest is not None else self._cwd
        if not dest.is_dir():
            return
        self._converting = True
        threading.Thread(
            target=self._paste_worker,
            args=(list(self._clipboard), self._clipboard_cut, dest),
            daemon=True).start()

    def _paste_worker(self, sources, cut, dest):
        """Off the main thread: copy or move each source into dest.  Tag migration
        and the UI refresh run back on the main thread in _paste_done."""
        done, errors, moved_pairs = [], [], []
        total = len(sources)
        for i, src in enumerate(sources, 1):
            self._ui_q.put((self._status_var.set,
                            (f"{'Moving' if cut else 'Copying'} {i}/{total}:  {src.name}",)))
            try:
                if not src.exists():
                    errors.append(f"{src.name}: no longer exists")
                    continue
                if src.is_dir():
                    # Refuse to paste a folder into itself or its own subtree.
                    try:
                        dest.relative_to(src)
                        errors.append(f"{src.name}: can't paste a folder into itself")
                        continue
                    except ValueError:
                        pass
                if cut:
                    if dest == src.parent:
                        continue                  # already here — nothing to move
                    target = dest / src.name
                    if target.exists():
                        errors.append(f"{src.name}: already exists in destination")
                        continue
                    shutil.move(str(src), str(target))
                    moved_pairs.append((src, target))
                else:
                    target = _unique_path(dest / src.name)
                    if src.is_dir():
                        shutil.copytree(str(src), str(target))
                    else:
                        shutil.copy2(str(src), str(target))
                done.append(src.name)
            except Exception as exc:
                errors.append(f"{src.name}: {exc}")
        self._ui_q.put((self._paste_done, (done, errors, cut, moved_pairs)))

    def _paste_done(self, done, errors, cut, moved_pairs):
        self._converting = False
        for src, target in moved_pairs:
            self._retag_path(src, target)
        if cut:                                  # drop the moved items from the clipboard
            moved = {src for src, _ in moved_pairs}
            self._clipboard = [p for p in self._clipboard if p not in moved]
            if not self._clipboard:
                self._clipboard_cut = False
        if errors:
            messagebox.showwarning(
                "Paste", _list_block(f"{len(done)} done.  Problems:", errors),
                parent=self.root)
        self._navigate(self._cwd)
        verb = "Moved" if cut else "Copied"
        self._status_var.set(f"{verb} {len(done)} item{'s' if len(done) != 1 else ''}")

    # ── drag-to-move ───────────────────────────────────────────────────────

    def _widget_owner(self, w):
        """Walk up from a widget to the Cell or PreviewRow that contains it."""
        while w is not None:
            if w in self._pool or w in self._rows:
                return w
            w = getattr(w, "master", None)
        return None

    def _cell_drag_motion(self, cell, event):
        if self._drag_candidate is not cell or self._overlay is not None:
            return
        if not self._dragging:
            if (abs(event.x_root - self._drag_start[0]) < 6
                    and abs(event.y_root - self._drag_start[1]) < 6):
                return                            # below the drag threshold
            # Drag is starting.  A deferred group-collapse is cancelled; if the
            # pressed item wasn't selected, select it alone.
            self._pending_collapse = None
            if cell.item is not None and cell.item not in self._selected_items:
                self._selected_items = {cell.item}
                self._reflow()
                self._update_status()
            self._drag_paths = {it.path for it in self._selected_items}
            self._dragging = True
            self._make_drag_indicator()
        self._move_drag_indicator(event.x_root, event.y_root)
        self._update_drop_target(event.x_root, event.y_root)

    def _cell_drag_release(self, cell, event):
        if not self._dragging:
            # A plain click that never became a drag: honour any deferred collapse.
            if self._pending_collapse is not None:
                item = self._pending_collapse
                self._pending_collapse = None
                self._selected_items = {item}
                self._anchor_item = self._lead_item = item
                self._reflow()
                self._update_status()
            self._drag_candidate = None
            return
        self._dragging = False
        self._drag_candidate = None
        self._destroy_drag_indicator()
        self._clear_drop_highlight()
        owner = self._widget_owner(self.root.winfo_containing(event.x_root, event.y_root))
        self._drag_paths = set()
        srcs = [it.path for it in self._selected_items]
        if (owner is not None and getattr(owner, "is_folder", False)
                and srcs and owner.path not in srcs):
            self._do_move(srcs, owner.path)      # navigates → tints reset
        else:
            self._reflow()                        # reset the source cell's hover tint

    def _make_drag_indicator(self):
        n = len(self._selected_items)
        self._drag_indicator = tk.Toplevel(self.root)
        self._drag_indicator.wm_overrideredirect(True)
        self._drag_indicator.attributes("-topmost", True)
        tk.Label(self._drag_indicator, text=f"↪  Move {n} item{'s' if n != 1 else ''}",
                 bg=CELL_HL, fg=ACTIVE_FG, font=("Segoe UI", 9),
                 padx=8, pady=3).pack()

    def _move_drag_indicator(self, x_root, y_root):
        if self._drag_indicator is not None:
            self._drag_indicator.geometry(f"+{x_root + 12}+{y_root + 16}")

    def _destroy_drag_indicator(self):
        if self._drag_indicator is not None:
            try:
                self._drag_indicator.destroy()
            except tk.TclError:
                pass
            self._drag_indicator = None

    def _update_drop_target(self, x_root, y_root):
        owner  = self._widget_owner(self.root.winfo_containing(x_root, y_root))
        target = owner if (owner is not None and getattr(owner, "is_folder", False)
                           and owner.path not in self._drag_paths) else None
        if target is self._drop_cell:
            return
        self._clear_drop_highlight()
        if target is not None:
            target._tint(CELL_HL)
            self._drop_cell = target

    def _clear_drop_highlight(self):
        c = self._drop_cell
        self._drop_cell = None
        if c is None:
            return
        try:
            if isinstance(c, Cell):
                c._tint(CELL_SEL if c._selected else CELL_BG)
            else:
                c._tint(CELL_BG)
        except tk.TclError:
            pass

    def _do_move(self, srcs, dest):
        """Move source paths into dest (a folder), migrating their tags.  Shared by
        the Move… dialog and drag-to-move."""
        dest = Path(dest)
        moved, errors = [], []
        for src in srcs:
            if dest == src.parent:
                continue                          # already here
            if src.is_dir():
                try:
                    dest.relative_to(src)
                    errors.append(f"{src.name}: can't move a folder into itself")
                    continue
                except ValueError:
                    pass
            target = dest / src.name
            if target.exists():
                errors.append(f"{src.name}: already exists in destination")
                continue
            try:
                shutil.move(str(src), str(target))
                self._retag_path(src, target)
                moved.append(src.name)
            except Exception as exc:
                errors.append(f"{src.name}: {exc}")
        if errors:
            messagebox.showwarning(
                "Move", _list_block(f"Moved {len(moved)} of {len(srcs)}.  Problems:", errors),
                parent=self.root)
        self._navigate(self._cwd)
        self._status_var.set(f"Moved {len(moved)} item{'s' if len(moved) != 1 else ''}")

    # ── keyboard navigation & grid geometry ───────────────────────────────

    def _view_items(self) -> list:
        """Items in the current filtered view, in grid order — the keyboard/marquee
        selection universe (PreviewRow folders aren't included)."""
        return self._view

    def _grid_cols(self) -> int:
        cw = self._canvas.winfo_width()
        cell_w = self._size_preset[2]
        return max(1, (cw + GAP) // (cell_w + GAP))

    def _grid_base(self) -> int:
        """Y offset (canvas content coords) where the cell grid begins — below any
        preview rows in preview mode, otherwise 0."""
        if not (self._preview_mode and self._rows):
            return 0
        flt = self._filter_text_var.get().strip().lower()
        p_img_h = max(75, self._size_preset[1] * 9 // 10)
        n_pre = sum(1 for r in self._rows if (not flt) or flt in r.path.name.lower())
        return 2 + n_pre * (p_img_h + 20)

    def _content_y_for_row(self, row: int) -> int:
        """Top y of grid row `row` in canvas content coords — mirrors _reflow so
        keyboard scroll-into-view and the marquee hit-test agree with the layout."""
        cell_h = self._size_preset[3]
        return self._grid_base() + GAP // 2 + row * (cell_h + GAP)

    def _set_selection(self, items):
        self._selected_items = set(items)
        self._reflow()           # re-tints the visible cells from the new selection
        self._update_status()

    def _scroll_cell_into_view(self, item):
        view = self._view
        if item not in view:
            return
        row = view.index(item) // self._grid_cols()
        cy = self._content_y_for_row(row)
        ch = self._size_preset[3]
        vp = self._canvas.winfo_height() or 1
        try:
            H = float(self._canvas.cget("scrollregion").split()[3])
        except (IndexError, ValueError):
            H = vp
        if H <= vp:
            return
        view_top = self._canvas.canvasy(0)
        if cy < view_top:
            self._canvas.yview_moveto(max(0.0, cy / H))
            self._reflow()
        elif cy + ch > view_top + vp:
            self._canvas.yview_moveto(min(1.0, (cy + ch - vp) / H))
            self._reflow()

    def _grid_key(self, dx: int, dy: int, event):
        """Arrow keys move the selection through the grid; Shift extends the range."""
        if self._overlay is not None or self._focus_in_entry():
            return
        vis = self._view_items()
        if not vis:
            return
        cols = self._grid_cols()
        lead = self._lead_item if self._lead_item in vis else (
               self._anchor_item if self._anchor_item in vis else None)
        if lead is None:
            self._anchor_item = self._lead_item = vis[0]
            self._set_selection([vis[0]])
            self._scroll_cell_into_view(vis[0])
            return
        ni = max(0, min(len(vis) - 1, vis.index(lead) + dx + dy * cols))
        target = vis[ni]
        if bool(event.state & 0x0001) and self._anchor_item in vis:   # Shift = extend
            a = vis.index(self._anchor_item)
            lo, hi = min(a, ni), max(a, ni)
            self._set_selection(vis[lo:hi + 1])
        else:
            self._anchor_item = target
            self._set_selection([target])
        self._lead_item = target
        self._scroll_cell_into_view(target)

    def _grid_jump(self, which: int, event):
        """Home / End → first / last cell."""
        if self._overlay is not None or self._focus_in_entry():
            return
        vis = self._view_items()
        if not vis:
            return
        target = vis[0] if which == 0 else vis[-1]
        self._anchor_item = self._lead_item = target
        self._set_selection([target])
        self._scroll_cell_into_view(target)

    def _grid_open(self):
        """Enter → open the lead/selected cell (enter folder or open file)."""
        if self._overlay is not None or self._focus_in_entry():
            return
        item = self._lead_item if self._lead_item in self._items else None
        if item is None and len(self._selected_items) == 1:
            item = next(iter(self._selected_items))
        if item is not None:
            self._open_item(item)

    # ── rubber-band marquee selection ──────────────────────────────────────

    def _marquee_press(self, event):
        """Start a marquee on empty canvas space.  Ctrl adds to the current
        selection; a plain press clears it first."""
        if self._overlay is not None:
            return
        self._canvas.focus_set()
        self._marquee_add  = bool(event.state & 0x0004)     # Ctrl = add
        self._marquee_x0   = self._canvas.canvasx(event.x)
        self._marquee_y0   = self._canvas.canvasy(event.y)
        self._marquee_base = set(self._selected_items) if self._marquee_add else set()
        if not self._marquee_add:
            self._set_selection([])
        self._marquee_item = self._canvas.create_rectangle(
            self._marquee_x0, self._marquee_y0, self._marquee_x0, self._marquee_y0,
            outline=CELL_HL, width=1, fill=CELL_HL, stipple="gray25", tags="marquee")

    def _marquee_drag(self, event):
        if self._marquee_item is None:
            return
        x1 = self._canvas.canvasx(event.x)
        y1 = self._canvas.canvasy(event.y)
        self._canvas.coords(self._marquee_item, self._marquee_x0, self._marquee_y0, x1, y1)
        mx0, mx1 = sorted((self._marquee_x0, x1))
        my0, my1 = sorted((self._marquee_y0, y1))
        cols   = self._grid_cols()
        cell_w = self._size_preset[2]
        cell_h = self._size_preset[3]
        view   = self._view
        # Only test the rows the band overlaps (keeps this O(visible), not O(folder)).
        y_first = self._content_y_for_row(0)
        r0 = max(0, int((my0 - y_first) // (cell_h + GAP)))
        r1 = int((my1 - y_first) // (cell_h + GAP))
        hits = set(self._marquee_base)
        for idx in range(r0 * cols, min(len(view), (r1 + 1) * cols)):
            x0 = GAP // 2 + (idx % cols) * (cell_w + GAP)
            y0 = self._content_y_for_row(idx // cols)
            if not (x0 + cell_w < mx0 or x0 > mx1 or y0 + cell_h < my0 or y0 > my1):
                hits.add(view[idx])
        if hits != self._selected_items:
            self._selected_items = hits
            self._reflow()
            self._update_status()

    def _marquee_release(self, event):
        if self._marquee_item is None:
            return
        self._canvas.delete(self._marquee_item)
        self._marquee_item = None
        self._anchor_item = None
        self._lead_item = None

    def _canvas_right_click(self, event):
        """Right-click on empty grid space → a small folder-level menu."""
        if self._overlay is not None:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.configure(bg=TOOLBAR_BG, fg=FG, activebackground=CELL_HL,
                       activeforeground=ACTIVE_FG, relief=tk.FLAT, bd=0)
        menu.add_command(label="New folder…", command=self._new_folder)
        menu.add_command(label="Refresh", command=lambda: self._navigate(self._cwd))
        if self._clipboard:
            n = len(self._clipboard)
            menu.add_command(
                label=f"Paste {n} item{'s' if n != 1 else ''}"
                      f"  ({'move' if self._clipboard_cut else 'copy'})",
                command=lambda: self._paste_from_clipboard())
        visible = list(self._items)
        if visible:
            menu.add_separator()
            menu.add_command(
                label=f"Tag all visible…  ({len(visible)})",
                command=lambda v=visible: self._edit_tags_dialog(v))
        menu.tk_popup(event.x_root, event.y_root)

    # ── context menu ──────────────────────────────────────────────────────

    def _show_context_menu(self, event):
        sel        = list(self._selected_items)
        file_cells = [c for c in sel if not c.is_folder]
        n_sel      = len(sel)
        n_files    = len(file_cells)

        menu = tk.Menu(self.root, tearoff=0)
        menu.configure(bg=TOOLBAR_BG, fg=FG,
                       activebackground=CELL_HL, activeforeground=ACTIVE_FG,
                       relief=tk.FLAT, bd=0)

        # ── Info line — always visible, normal colour, non-functional click ──
        if n_sel == 1 and not sel[0].is_folder:
            info = self._get_image_info(sel[0].path)
        elif n_sel == 1 and sel[0].is_folder:
            info = f"Folder  —  {sel[0].path.name}"
        else:
            n_folders = n_sel - n_files
            parts = []
            if n_folders:
                parts.append(f"{n_folders} folder{'s' if n_folders > 1 else ''}")
            if n_files:
                parts.append(f"{n_files} file{'s' if n_files > 1 else ''}")
            info = f"{n_sel} selected  ({', '.join(parts)})"

        menu.add_command(label=info, command=lambda: None)
        menu.add_separator()

        # ── Actions ──
        menu.add_command(label="Open", command=lambda: self._open_selected(sel))

        viewer_cells = [c for c in sel
                        if not c.is_folder and c.path.suffix.lower() in VIEWER_EXTS]
        if viewer_cells:
            v_label = ("Open in viewer" if len(viewer_cells) == 1
                       else f"Open {len(viewer_cells)} in viewer")
            menu.add_command(label=v_label,
                             command=lambda vc=viewer_cells: self._open_in_viewer(vc))
            e_label = ("Export to image…" if len(viewer_cells) == 1
                       else f"Export {len(viewer_cells)} to images…")
            menu.add_command(label=e_label,
                             command=lambda vc=viewer_cells: self._export_selected(vc))

        # WVF — encrypted videos in the selection: play / decrypt back to video
        wvf_cells = [c for c in sel
                     if not c.is_folder and c.path.suffix.lower() == ".wvf"]
        if _WVF and wvf_cells:
            p_label = "Play" if len(wvf_cells) == 1 else f"Play {len(wvf_cells)} videos"
            menu.add_command(label=p_label,
                             command=lambda wc=wvf_cells: self._play_wvf_cells(wc))
            d_label = ("Decrypt to video…" if len(wvf_cells) == 1
                       else f"Decrypt {len(wvf_cells)} to videos…")
            menu.add_command(label=d_label,
                             command=lambda wc=wvf_cells: self._unwrap_selected(wc))

        # Convert to WIF — images only; videos (and sealed .wvf videos) can't be
        # encoded as a WIF image, so the option is hidden for them.
        convert_cells = [c for c in file_cells
                         if c.path.suffix.lower() not in VIDEO_EXTS
                         and c.path.suffix.lower() != ".wvf"]
        if convert_cells:
            n_conv = len(convert_cells)
            plural = "" if n_conv == 1 else f" ({n_conv} files)"
            menu.add_command(label=f"Convert to WIF v3{plural}",
                             command=lambda cc=convert_cells: self._convert_selected(cc, version=1))
            menu.add_command(label=f"Convert to WIF v2 — encrypted 🔒{plural}",
                             command=lambda cc=convert_cells: self._convert_selected(cc, version=2))

        # WVF — seal plain videos into encrypted .wvf containers
        video_cells = [c for c in file_cells if c.path.suffix.lower() in VIDEO_EXTS]
        if _WVF and video_cells:
            n_vid = len(video_cells)
            s_label = "Seal to WVF 🔒…" if n_vid == 1 else f"Seal {n_vid} videos to WVF 🔒…"
            menu.add_command(label=s_label,
                             command=lambda vc=video_cells: self._wrap_to_wvf(vc))

        # Encryption — operates on existing .wif files in the selection
        wif_cells = [c for c in file_cells if c.path.suffix.lower() == ".wif"]
        if wif_cells:
            enc_cells   = [c for c in wif_cells if self._is_encrypted_file(c.path)]
            plain_cells = [c for c in wif_cells if c not in enc_cells]
            enc_menu = tk.Menu(menu, tearoff=0)
            enc_menu.configure(bg=TOOLBAR_BG, fg=FG, activebackground=CELL_HL,
                               activeforeground=ACTIVE_FG, relief=tk.FLAT, bd=0)
            if plain_cells:
                lbl = "Encrypt to v2…" if len(plain_cells) == 1 else f"Encrypt to v2…  ({len(plain_cells)})"
                enc_menu.add_command(label=lbl, command=lambda c=plain_cells: self._encrypt_to_v2(c))
            if enc_cells:
                lbl = "Change password…" if len(enc_cells) == 1 else f"Change password…  ({len(enc_cells)})"
                enc_menu.add_command(label=lbl, command=lambda c=enc_cells: self._change_password(c))
                lbl = ("Remove encryption (→ v3)…" if len(enc_cells) == 1
                       else f"Remove encryption (→ v3)…  ({len(enc_cells)})")
                enc_menu.add_command(label=lbl, command=lambda c=enc_cells: self._decrypt_to_v1(c))
            menu.add_cascade(label="Encryption", menu=enc_menu)

        menu.add_separator()
        if n_sel == 1:
            menu.add_command(label="Rename…",
                             command=lambda c=sel[0]: self._rename_selected(c))
        elif n_files > 1:
            menu.add_command(label=f"Batch rename {n_files} files…",
                             command=lambda fc=file_cells: self._batch_rename(fc))
        if sel:
            mv_label = "Move…" if n_sel == 1 else f"Move {n_sel} items…"
            menu.add_command(label=mv_label,
                             command=lambda s=list(sel): self._move_selected(s))
            menu.add_command(label=("Copy" if n_sel == 1 else f"Copy {n_sel} items"),
                             command=self._copy_to_clipboard)
            menu.add_command(label=("Cut" if n_sel == 1 else f"Cut {n_sel} items"),
                             command=self._cut_to_clipboard)
        # Paste into a single selected folder when the clipboard isn't empty.
        if self._clipboard and n_sel == 1 and sel[0].is_folder:
            n_clip = len(self._clipboard)
            menu.add_command(
                label=f"Paste {n_clip} item{'s' if n_clip != 1 else ''} into "
                      f"\"{sel[0].path.name}\"",
                command=lambda d=sel[0].path: self._paste_from_clipboard(d))

        menu.add_separator()
        if sel:
            menu.add_command(label="Show in Explorer",
                             command=lambda p=sel[0].path: self._show_in_explorer(p))
        menu.add_command(label=("Copy path" if n_sel == 1 else f"Copy paths ({n_sel})"),
                         command=lambda s=list(sel): self._copy_paths(s))
        if sel:
            menu.add_command(label="Edit tags…",
                             command=lambda: self._edit_tags_dialog(sel))
        folder_cells = [c for c in sel if c.is_folder]
        if folder_cells:
            def _tag_folder(folders):
                files = [Item(f, False, "file")
                         for fc in folders
                         for f in sorted(fc.path.iterdir(), key=lambda p: p.name.lower())
                         if f.is_file()]
                if files:
                    self._edit_tags_dialog(files)
                else:
                    messagebox.showinfo("Tag folder", "No files found.", parent=self.root)
            if len(folder_cells) == 1:
                flabel = f"Tag all files in \"{folder_cells[0].path.name}\"…"
            else:
                flabel = f"Tag all files in {len(folder_cells)} folders…"
            menu.add_command(label=flabel,
                             command=lambda fc=folder_cells: _tag_folder(fc))

        if file_cells and n_files == n_sel:   # hide if any folder is in the selection
            menu.add_separator()
            if _TRASH:
                recycle_label = ("Move to Recycle Bin" if n_files == 1
                                 else f"Move {n_files} files to Recycle Bin")
                menu.add_command(label=recycle_label, command=self._recycle_selected)
                delete_label = ("Delete permanently" if n_files == 1
                                else f"Delete {n_files} files permanently")
            else:
                delete_label = "Delete" if n_files == 1 else f"Delete {n_files} files"
            menu.add_command(label=delete_label,
                             command=lambda: self._do_delete(use_trash=False))

        menu.tk_popup(event.x_root, event.y_root)

    def _get_image_info(self, path: Path) -> str:
        """Return a short resolution/info string for the status line in the menu."""
        try:
            if path.suffix.lower() == ".wif":
                data = path.read_bytes()
                info = wif_format.peek(data)
                if info["encrypted"]:
                    try:
                        _img, meta, _ = wif_format.decode_try(data, self._passwords)
                        return f"{meta['width']} × {meta['height']} px  🔒  —  {path.name}"
                    except wif_format.WrongPassword:
                        return f"🔒 encrypted (locked)  —  {path.name}"
                return f"{info['width']} × {info['height']} px  —  {path.name}"
            elif path.suffix.lower() == ".wvf":
                return self._wvf_info(path) + f"  —  {path.name}"
            elif path.suffix.lower() in IMAGE_EXTS:
                with Image.open(path) as img:
                    w, h = img.size
                return f"{w} × {h} px  —  {path.name}"
            else:
                size = path.stat().st_size
                return f"{path.name}  ({size:,} bytes)"
        except Exception:
            return path.name

    def _wvf_info(self, path: Path) -> str:
        """Short description of a .wvf for the status line / context menu.

        Only the header (version, size) and the lock state are read — the
        original name and codec stay sealed inside the ciphertext."""
        if not _WVF:
            return "🔒 WVF encrypted video"
        try:
            info = wvf.peek(path)
        except Exception:
            return "WVF (unreadable)"
        ver = f"WVF v{info['version']}"
        if not info.get("supported", True):
            ver += " (unsupported)"
        state = "🔓 unlocked" if wvf_matching_password(path, self._passwords) else "🔒 locked"
        return f"{ver} · encrypted video · {state}"

    def _open_selected(self, cells):
        for cell in cells:
            if cell.is_folder:
                subprocess.Popen(["explorer", str(cell.path)])
            elif cell.path.suffix.lower() == ".wvf":
                self._play_wvf(cell.path)
            elif cell.path.suffix.lower() in VIEWER_EXTS:
                subprocess.Popen(_viewer_cmd(cell.path), env=self._viewer_env())
            else:
                os.startfile(cell.path)

    def _open_in_viewer(self, cells):
        """Open each viewer-compatible file (.wif or image) in the Weird Viewer."""
        for cell in cells:
            if not cell.is_folder and cell.path.suffix.lower() in VIEWER_EXTS:
                subprocess.Popen(_viewer_cmd(cell.path), env=self._viewer_env())

    # ── WVF: play / seal / decrypt encrypted videos ───────────────────────

    def _cleanup_wvf_tempfiles(self):
        """Scrub any decrypted-to-temp .wvf playback files (atexit / on demand).

        Best-effort: a still-running player can hold a file open on Windows, in
        which case the unlink fails and the file stays until next time."""
        for p in list(self._wvf_tempfiles):
            try:
                if os.path.exists(p):
                    secure_delete(Path(p))
            except OSError:
                pass
        self._wvf_tempfiles = []

    def _wvf_unavailable_msg(self):
        messagebox.showerror(
            "WVF",
            "WVF video support is unavailable.\n\n"
            "It needs the 'wvf' package (in this folder) and 'cryptography':\n"
            "    pip install cryptography",
            parent=self.root)

    def _prompt_wvf_password(self, path: Path):
        """Ask for a password that unlocks `path`, remembering it for the session.
        Returns the password, or None if the user cancels."""
        prompt = f"“{path.name}” is an encrypted WVF video.\nEnter password:"
        while True:
            pw = simpledialog.askstring("Password", prompt, show="*", parent=self.root)
            if not pw:
                return None
            if wvf_matching_password(path, [pw]):
                self._set_session_password(pw)
                return pw
            prompt = (f"Wrong password — try again.\n\n"
                      f"“{path.name}” is an encrypted WVF video.\nEnter password:")

    def _play_wvf_cells(self, cells):
        for cell in cells:
            if not cell.is_folder and cell.path.suffix.lower() == ".wvf":
                self._play_wvf(cell.path)

    def _play_wvf(self, path: Path):
        """Decrypt and play a .wvf.  Prefers streaming straight into ffplay (no
        plaintext on disk); the worker thread keeps the UI responsive."""
        if not _WVF:
            self._wvf_unavailable_msg()
            return
        pw = wvf_matching_password(path, self._passwords)
        if pw is None:
            pw = self._prompt_wvf_password(path)
            if pw is None:
                return
        self._status_var.set(f"Opening {path.name} …")
        threading.Thread(target=self._play_wvf_worker,
                         args=(path, pw), daemon=True).start()

    def _play_wvf_worker(self, path: Path, pw: str):
        """Runs off the main thread.  Touches tkinter only via self._ui_q."""
        try:
            ffplay = wvf_player._find_ffplay()
            if ffplay:
                try:
                    status, meta = wvf_player._play_streamed(ffplay, str(path), pw)
                except wvf.WrongPassword:
                    self._ui_q.put((self._status_var.set, ("Wrong password.",)))
                    return
                except OSError:
                    status = None          # ffplay wouldn't start — fall back
                if status == "ok":
                    name = (meta or {}).get("name", path.name)
                    self._ui_q.put((self._status_var.set,
                                    (f"Played {name}   (streamed via ffplay — no plaintext on disk)",)))
                    return
                # "unplayable" (ffplay couldn't demux the pipe) → temp-file fallback
            self._play_wvf_tempfile(path, pw)
        except Exception as exc:
            self._ui_q.put((self._wvf_error, (f"Could not play {path.name}: {exc}",)))

    def _play_wvf_tempfile(self, path: Path, pw: str):
        """Fallback when ffplay can't stream the clip: decrypt to a temp file in
        the project folder, open it in the OS player, and scrub it on exit."""
        fd, tmp = tempfile.mkstemp(prefix="wvf_play_", dir=str(SCRIPT_DIR))
        os.close(fd)
        try:
            meta = wvf.unwrap_file(path, tmp, password=pw)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # `container` comes out of the ciphertext (untrusted) — sanitise to a bare
        # extension so it can only ever be appended, never used to escape the dir.
        container = wvf.sanitize_container(meta.get("container"))
        target = f"{tmp}.{container}"
        os.replace(tmp, target)
        self._wvf_tempfiles.append(target)
        name = meta.get("name", path.name)
        self._ui_q.put((self._status_var.set,
                        (f"Playing {name}   (decrypted to a temp file — removed when you quit)",)))
        os.startfile(target)

    def _wvf_error(self, msg: str):
        self._status_var.set(msg)
        messagebox.showerror("WVF", msg, parent=self.root)

    def _wrap_to_wvf(self, cells):
        """Seal the selected plain videos into encrypted .wvf containers."""
        if not _WVF:
            self._wvf_unavailable_msg()
            return
        files = [c.path for c in cells
                 if not c.is_folder and c.path.suffix.lower() in VIDEO_EXTS]
        if not files:
            return
        if self._converting:
            messagebox.showinfo("Seal to WVF", "A file operation is already running.",
                                parent=self.root)
            return
        # Reuse the session password if one is set, otherwise ask (with confirm).
        pw = self._passwords[0] if self._passwords else self._prompt_new_password()
        if not pw:
            return
        self._converting = True
        threading.Thread(target=self._wrap_worker, args=(files, pw), daemon=True).start()

    def _wrap_worker(self, files, pw):
        done, errors = [], []
        total = len(files)
        for i, src in enumerate(files, 1):
            self._ui_q.put((self._status_var.set, (f"Sealing {i}/{total}:  {src.name}",)))
            try:
                dst = _unique_path(src.with_suffix(".wvf"))
                wvf.wrap_file(src, dst, pw)
                done.append(dst.name)
            except Exception as exc:
                errors.append(f"{src.name}: {exc}")
        self._ui_q.put((self._wrap_done, (done, errors)))

    def _wrap_done(self, done, errors):
        self._converting = False
        parts = []
        if done:
            parts.append(_list_block("Sealed to WVF 🔒:", done))
        if errors:
            parts.append(_list_block("Errors:", errors))
        if parts:
            messagebox.showinfo("Seal to WVF", "\n\n".join(parts), parent=self.root)
        if done:
            self._navigate(self._cwd)      # show the new .wvf files
        else:
            self._update_status()

    def _unwrap_selected(self, cells):
        """Decrypt the selected .wvf files back to their original videos."""
        if not _WVF:
            self._wvf_unavailable_msg()
            return
        files = [c.path for c in cells
                 if not c.is_folder and c.path.suffix.lower() == ".wvf"]
        if not files:
            return
        if self._converting:
            messagebox.showinfo("Decrypt", "A file operation is already running.",
                                parent=self.root)
            return
        folder = filedialog.askdirectory(
            title=f"Decrypt {len(files)} video{'s' if len(files) != 1 else ''} into folder…")
        if not folder:
            return
        self._converting = True
        threading.Thread(target=self._unwrap_worker,
                         args=(files, Path(folder), list(self._passwords)),
                         daemon=True).start()

    def _unwrap_worker(self, files, folder, passwords):
        done, locked, errors = [], [], []
        total = len(files)
        for i, src in enumerate(files, 1):
            self._ui_q.put((self._status_var.set, (f"Decrypting {i}/{total}:  {src.name}",)))
            fd, tmp = tempfile.mkstemp(suffix=".part", dir=str(folder))
            os.close(fd)
            try:
                meta = wvf.unwrap_file(src, tmp, passwords=passwords)
            except wvf.WrongPassword:
                locked.append(src.name)
                self._safe_unlink(tmp)
                continue
            except Exception as exc:
                errors.append(f"{src.name}: {exc}")
                self._safe_unlink(tmp)
                continue
            # meta came out of the ciphertext (untrusted) — never let it pick a path.
            dst = _unique_path(folder / wvf.safe_output_name(meta, src.stem))
            os.replace(tmp, dst)
            done.append(dst.name)
        self._ui_q.put((self._unwrap_done, (done, locked, errors, str(folder))))

    @staticmethod
    def _safe_unlink(p):
        try:
            os.unlink(p)
        except OSError:
            pass

    def _unwrap_done(self, done, locked, errors, folder):
        self._converting = False
        parts = []
        if done:
            parts.append(_list_block("Decrypted:", done))
        if locked:
            parts.append(_list_block("Skipped (locked — unlock with 🔑 first):", locked))
        if errors:
            parts.append(_list_block("Errors:", errors))
        if parts:
            messagebox.showinfo("Decrypt to video", "\n\n".join(parts), parent=self.root)
        try:
            into_cwd = done and Path(folder).resolve() == self._cwd.resolve()
        except OSError:
            into_cwd = False
        if into_cwd:
            self._navigate(self._cwd)
        else:
            self._update_status()

    # ── in-browser image overlay (lightbox) ───────────────────────────────

    def _on_mousewheel(self, event):
        # While the overlay is open, the wheel flips through images instead of
        # scrolling the grid behind it.
        if self._overlay is not None:
            self._overlay_nav(1 if event.delta < 0 else -1)
            return "break"
        self._canvas.yview_scroll(-1 * (event.delta // 120), "units")
        self._reflow()

    def _open_preview_image(self, path: Path, paths):
        """A thumbnail in a folder-preview row was clicked — open it in the lightbox,
        with the row's other previewed images as the navigation set."""
        self._show_overlay(path, paths)

    def _show_overlay(self, path: Path, paths=None):
        """Show a full-size image overlay on top of the grid, with arrow/wheel
        navigation.  `paths`, if given, is the navigation set (else the current
        view's viewable files)."""
        self._hover_hide()   # don't leave a hover popup lurking behind the lightbox
        if paths is not None:
            self._overlay_paths = list(paths)
            self._overlay_index = self._overlay_paths.index(path) if path in self._overlay_paths else 0
        else:
            viewable = [it.path for it in self._items
                        if not it.is_folder and it.path.suffix.lower() in VIEWER_EXTS]
            if path in viewable:
                self._overlay_paths = viewable
                self._overlay_index = viewable.index(path)
            else:
                self._overlay_paths = [path]
                self._overlay_index = 0

        if self._overlay is None:
            self._overlay = tk.Frame(self.root, bg="#0b0b0b")
            self._overlay.place(x=0, y=0, relwidth=1, relheight=1)

            self._overlay_lbl = tk.Label(self._overlay, bg="#0b0b0b", bd=0)
            self._overlay_lbl.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

            self._overlay_cap = tk.Label(self._overlay, bg="#0b0b0b", fg="#dddddd",
                                         font=("Segoe UI", 10))
            self._overlay_cap.place(relx=0.5, y=10, anchor=tk.N)

            self._overlay_hint = tk.Label(
                self._overlay, bg="#0b0b0b", fg="#888888", font=("Segoe UI", 9),
                text="←/→ or wheel: navigate · click a thumbnail to jump"
                     "     click background · Esc · ✕: close")
            self._overlay_hint.place(relx=0.5, rely=1.0, y=-10, anchor=tk.S)

            # Filmstrip — a windowed row of thumbnails, (re)built per image by _film_render.
            self._film_strip = tk.Frame(self._overlay, bg="#0b0b0b")
            self._film_cache = {}

            close = tk.Label(self._overlay, text="✕", bg="#0b0b0b", fg="#dddddd",
                             font=("Segoe UI", 16), cursor="hand2")
            close.place(relx=1.0, x=-18, y=8, anchor=tk.NE)
            close.bind("<Button-1>", lambda e: self._overlay_close())

            prev = tk.Label(self._overlay, text="‹", bg="#0b0b0b", fg="#999999",
                            font=("Segoe UI", 36), cursor="hand2")
            prev.place(relx=0.0, x=18, rely=0.5, anchor=tk.W)
            prev.bind("<Button-1>", lambda e: self._overlay_nav(-1))

            nxt = tk.Label(self._overlay, text="›", bg="#0b0b0b", fg="#999999",
                           font=("Segoe UI", 36), cursor="hand2")
            nxt.place(relx=1.0, x=-18, rely=0.5, anchor=tk.E)
            nxt.bind("<Button-1>", lambda e: self._overlay_nav(1))

            # Clicking the dark background closes; clicking the image does not.
            self._overlay.bind("<Button-1>", lambda e: self._overlay_close())
            self._overlay.bind("<Configure>", lambda e: self._debounce_overlay_render())

        self._overlay.lift()
        self._overlay.focus_set()
        self._overlay_load(allow_prompt=True)

    def _overlay_load(self, allow_prompt=False):
        self._overlay_stop_anim()
        path = self._overlay_paths[self._overlay_index]
        n    = len(self._overlay_paths)

        # Animated GIF → play it in the lightbox
        if path.suffix.lower() == ".gif":
            frames, durations = self._load_gif_frames(path)
            if len(frames) > 1:
                self._overlay_frames    = frames
                self._overlay_durations = durations
                self._overlay_frame_idx = 0
                self._overlay_img       = frames[0]
                self._overlay_cap.configure(
                    text=f"{path.name}   ({self._overlay_index + 1}/{n})   ▶ GIF · {len(frames)} frames")
                self._overlay_render()
                self._film_render()
                self._overlay_anim()
                return

        img  = self._load_viewable(path, allow_prompt)
        if img is None:
            if allow_prompt:                  # explicit open the user cancelled
                self._overlay_close()
                return
            locked = path.suffix.lower() == ".wif" and self._is_encrypted_file(path)
            img = make_locked_icon(360, 270) if locked else make_error_icon(360, 270)
            tag = "locked" if locked else "can't display"
            self._overlay_cap.configure(text=f"{path.name}   ({self._overlay_index + 1}/{n}) — {tag}")
        else:
            self._overlay_cap.configure(text=f"{path.name}   ({self._overlay_index + 1}/{n})")
        self._overlay_img = img
        self._overlay_render()
        self._film_render()

    def _overlay_render(self):
        if self._overlay is None or self._overlay_img is None:
            return
        ow = self._overlay.winfo_width()  or self.root.winfo_width()
        oh = self._overlay.winfo_height() or self.root.winfo_height()
        margin = 48
        # Reserve room at the bottom for the filmstrip (and nudge the image up so
        # it stays centred in the space that's left).
        reserved = FILM_BAR_H if (self._film_strip is not None
                                  and len(self._overlay_paths) > 1) else 0
        avail_w = max(1, ow - 2 * margin)
        avail_h = max(1, oh - 2 * margin - reserved)
        img = self._overlay_img
        # Shrink images larger than the window to fit, but never enlarge a
        # smaller image past its native size (cap the scale factor at 1.0).
        ratio = min(avail_w / img.width, avail_h / img.height, 1.0)
        size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        self._overlay_photo = ImageTk.PhotoImage(img.resize(size, Image.LANCZOS))
        self._overlay_lbl.configure(image=self._overlay_photo)
        self._overlay_lbl.place_configure(y=-reserved // 2)

    def _debounce_overlay_render(self):
        if self._overlay_resize_job:
            self.root.after_cancel(self._overlay_resize_job)
        self._overlay_resize_job = self.root.after(60, self._overlay_render)

    def _overlay_nav(self, step: int):
        if self._overlay is None or not self._overlay_paths:
            return
        self._overlay_index = (self._overlay_index + step) % len(self._overlay_paths)
        self._overlay_load(allow_prompt=False)

    def _overlay_close(self):
        self._overlay_stop_anim()
        if self._overlay_resize_job:
            self.root.after_cancel(self._overlay_resize_job)
            self._overlay_resize_job = None
        if self._overlay is not None:
            self._overlay.destroy()
            self._overlay = None
            self._overlay_img = None
            self._overlay_photo = None
            self._overlay_hint = None
            self._film_strip = None
            self._film_photos = []
            self._film_cache = {}

    def _film_thumb(self, path: Path) -> ImageTk.PhotoImage:
        """Small cover-cropped thumbnail for the filmstrip, cached per path.
        Locked/corrupt files fall back to the error icon."""
        key = str(path)
        cached = self._film_cache.get(key)
        if cached is not None:
            return cached
        tw, th = FILM_THUMB
        try:
            if path.suffix.lower() == ".wif":
                img, _m, _pw = wif_format.decode_try(path.read_bytes(), self._passwords)
            else:
                img = Image.open(path)
            thumb = _cover_crop(img, tw, th)
        except Exception:
            thumb = make_error_icon(tw, th)
        photo = ImageTk.PhotoImage(thumb)
        self._film_cache[key] = photo
        return photo

    def _film_render(self):
        """(Re)build the filmstrip as a window of thumbnails centred on the current
        image, with the active one highlighted.  Hidden when there's only one image."""
        if self._overlay is None or self._film_strip is None:
            return
        for w in self._film_strip.winfo_children():
            w.destroy()
        self._film_photos = []

        n = len(self._overlay_paths)
        if n <= 1:
            self._film_strip.place_forget()
            if self._overlay_hint is not None:
                self._overlay_hint.place_configure(y=-10)
            return

        self._film_strip.place(relx=0.5, rely=1.0, y=-8, anchor=tk.S)
        if self._overlay_hint is not None:
            self._overlay_hint.place_configure(y=-(FILM_BAR_H + 6))

        # Window of FILM_MAX indices centred on the current one.
        half = FILM_MAX // 2
        hi   = min(n, max(self._overlay_index + half + 1, FILM_MAX))
        lo   = max(0, hi - FILM_MAX)
        for idx in range(lo, hi):
            photo = self._film_thumb(self._overlay_paths[idx])
            self._film_photos.append(photo)
            border = "#e8a020" if idx == self._overlay_index else "#0b0b0b"
            cell = tk.Frame(self._film_strip, bg=border, padx=2, pady=2)
            cell.pack(side=tk.LEFT, padx=2)
            lbl = tk.Label(cell, image=photo, bg="#1a1a1a", cursor="hand2",
                           bd=0, highlightthickness=0)
            lbl.pack()
            lbl.bind("<Button-1>", lambda e, i=idx: self._film_jump(i))

    def _film_jump(self, idx: int):
        if self._overlay is None or not self._overlay_paths:
            return
        self._overlay_index = idx % len(self._overlay_paths)
        self._overlay_load(allow_prompt=False)

    def _load_gif_frames(self, path: Path):
        """Return (frames, durations) for a GIF, or ([], []) on failure."""
        frames, durations = [], []
        try:
            raw = Image.open(path)
            for frame in ImageSequence.Iterator(raw):
                frames.append(frame.convert("RGBA"))
                durations.append(max(20, frame.info.get("duration", 100)))
        except Exception:
            return [], []
        return frames, durations

    def _overlay_anim(self):
        if self._overlay is None or not self._overlay_frames:
            return
        self._overlay_frame_idx = (self._overlay_frame_idx + 1) % len(self._overlay_frames)
        self._overlay_img = self._overlay_frames[self._overlay_frame_idx]
        self._overlay_render()
        delay = self._overlay_durations[self._overlay_frame_idx]
        self._overlay_anim_job = self.root.after(delay, self._overlay_anim)

    def _overlay_stop_anim(self):
        if self._overlay_anim_job is not None:
            self.root.after_cancel(self._overlay_anim_job)
            self._overlay_anim_job = None
        self._overlay_frames = []
        self._overlay_durations = []

    def _is_encrypted_file(self, path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                return wif_format.is_encrypted(f.read(64))
        except Exception:
            return False

    def _load_viewable(self, path: Path, allow_prompt: bool):
        """Full-res PIL image for the overlay, or None if it can't be shown
        (unsupported/corrupt, or encrypted-and-not-unlocked)."""
        if path.suffix.lower() != ".wif":
            try:
                return Image.open(path).convert("RGBA")
            except Exception:
                return None
        try:
            data = path.read_bytes()
        except Exception:
            return None
        if not wif_format.is_encrypted(data):
            try:
                img, _ = wif_format.decode(data)
                return img
            except Exception:
                return None
        # Encrypted: try known passwords, then prompt (only on an explicit open)
        try:
            img, _, _ = wif_format.decode_try(data, self._passwords)
            return img
        except wif_format.WrongPassword:
            pass
        if not allow_prompt:
            return None
        prompt = f"“{path.name}” is encrypted.\nEnter password:"
        while True:
            pw = simpledialog.askstring("Password", prompt, show="*", parent=self.root)
            if not pw:
                return None
            try:
                img, _ = wif_format.decode(data, pw)
                self._set_session_password(pw)
                return img
            except wif_format.WrongPassword:
                prompt = (f"Wrong password — try again.\n\n"
                          f"“{path.name}” is encrypted.\nEnter password:")

    def _convert_selected(self, file_cells, version=1):
        """Encode the selected files to .wif (v1 plain or v2 encrypted) on a
        background thread so the UI stays responsive during large batches."""
        if self._converting:
            messagebox.showinfo("Convert to WIF", "A conversion is already running.")
            return
        paths = [c.path for c in file_cells]
        if not paths:
            return
        password = None
        if version == 2:
            # Encrypt with the session password, or ask for one if none is set
            password = self._passwords[0] if self._passwords else self._prompt_new_password()
            if not password:
                return   # cancelled
        self._converting = True
        threading.Thread(target=self._convert_worker,
                         args=(paths, version, password), daemon=True).start()

    def _convert_worker(self, paths, version, password):
        """Runs off the main thread — touches tkinter only via root.after()."""
        converted, skipped, errors = [], [], []
        total = len(paths)
        for i, path in enumerate(paths, 1):
            self._ui_q.put((self._status_var.set, (f"Converting {i}/{total}:  {path.name}",)))
            if path.suffix.lower() == ".wif":
                skipped.append(path.name)
                continue
            try:
                data = wif_format.encode(Image.open(path), version=version,
                                         compress=True, password=password, filtered=True)
                # Never clobber an existing .wif — fall back to a free "(2)" name.
                target = _unique_path(path.with_suffix(".wif"))
                target.write_bytes(data)
                converted.append(target.name)
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
        self._ui_q.put((self._convert_done, (converted, skipped, errors)))

    def _convert_done(self, converted, skipped, errors):
        self._converting = False
        parts = []
        if converted:
            parts.append(_list_block("Converted:", converted))
        if skipped:
            parts.append(_list_block("Skipped (already .wif):", skipped))
        if errors:
            parts.append(_list_block("Errors:", errors))
        if parts:
            messagebox.showinfo("Convert to WIF", "\n\n".join(parts))

        # New .wif files were written next to their sources — refresh to show them
        if converted:
            self._navigate(self._cwd)
        else:
            self._update_status()

    # ── export .wif → standard image ──────────────────────────────────────

    def _export_selected(self, cells):
        """Decode the selected viewable files and save them as standard images."""
        files = [c.path for c in cells
                 if not c.is_folder and c.path.suffix.lower() in VIEWER_EXTS]
        if not files:
            return
        if self._converting:
            messagebox.showinfo("Export", "A file operation is already running.")
            return

        if len(files) == 1:
            src = files[0]
            dst = filedialog.asksaveasfilename(
                title="Export image", initialfile=src.stem + ".png", defaultextension=".png",
                filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"), ("BMP", "*.bmp"), ("WEBP", "*.webp")])
            if not dst:
                return
            jobs = [(src, Path(dst))]
        else:
            folder = filedialog.askdirectory(title=f"Export {len(files)} images to folder…")
            if not folder:
                return
            # Never overwrite an existing PNG, and don't let two sources with the
            # same stem collide within this batch — reserve each chosen name.
            folder = Path(folder)
            jobs, taken = [], set()
            for p in files:
                dst = folder / (p.stem + ".png")
                i = 2
                while dst.exists() or dst in taken:
                    dst = folder / f"{p.stem} ({i}).png"
                    i += 1
                taken.add(dst)
                jobs.append((p, dst))

        self._converting = True
        threading.Thread(target=self._export_worker,
                         args=(jobs, list(self._passwords)), daemon=True).start()

    def _export_worker(self, jobs, passwords):
        done, locked, errors = [], [], []
        total = len(jobs)
        for i, (src, dst) in enumerate(jobs, 1):
            self._ui_q.put((self._status_var.set, (f"Exporting {i}/{total}:  {src.name}",)))
            try:
                if src.suffix.lower() == ".wif":
                    img, _, _ = wif_format.decode_try(src.read_bytes(), passwords)
                else:
                    img = Image.open(src)
                if dst.suffix.lower() in (".jpg", ".jpeg", ".bmp") and img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                img.save(dst)
                done.append(dst.name)
            except wif_format.WrongPassword:
                locked.append(src.name)
            except Exception as exc:
                errors.append(f"{src.name}: {exc}")
        self._ui_q.put((self._export_done, (done, locked, errors)))

    def _export_done(self, done, locked, errors):
        self._converting = False
        parts = []
        if done:
            parts.append(_list_block("Exported:", done))
        if locked:
            parts.append(_list_block("Skipped (locked — unlock with 🔑 first):", locked))
        if errors:
            parts.append(_list_block("Errors:", errors))
        if parts:
            messagebox.showinfo("Export to image", "\n\n".join(parts))
        self._update_status()

    # ── re-encrypt / change password / decrypt in place ───────────────────

    def _encrypt_to_v2(self, cells):
        pw = self._prompt_new_password()
        if not pw:
            return
        self._reencode_selected([c.path for c in cells], target_version=2, new_password=pw)

    def _change_password(self, cells):
        pw = self._prompt_new_password()
        if not pw:
            return
        self._reencode_selected([c.path for c in cells], target_version=2, new_password=pw)

    def _decrypt_to_v1(self, cells):
        if not messagebox.askyesno(
                "Remove encryption",
                f"Store {'this file' if len(cells) == 1 else f'these {len(cells)} files'} "
                "UNENCRYPTED (WIF v1)?\n\nAnyone with the file will be able to open it.",
                icon="warning"):
            return
        self._reencode_selected([c.path for c in cells], target_version=1, new_password=None)

    def _reencode_selected(self, paths, target_version, new_password):
        if self._converting:
            messagebox.showinfo("Encryption", "A file operation is already running.")
            return
        if not paths:
            return
        self._converting = True
        threading.Thread(target=self._reencode_worker,
                         args=(paths, target_version, new_password, list(self._passwords)),
                         daemon=True).start()

    def _reencode_worker(self, paths, target_version, new_password, passwords):
        done, locked, errors = [], [], []
        total = len(paths)
        for i, path in enumerate(paths, 1):
            self._ui_q.put((self._status_var.set, (f"Processing {i}/{total}:  {path.name}",)))
            try:
                img, meta, _ = wif_format.decode_try(path.read_bytes(), passwords)
            except wif_format.WrongPassword:
                locked.append(path.name)
                continue
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            # Re-encode atomically (temp file + replace) so a failure can't corrupt the original
            tmp = path.with_name(path.name + ".tmp")
            try:
                data = wif_format.encode(img, version=target_version,
                                         compress=meta["compressed"], password=new_password,
                                         filtered=meta["filtered"])
                tmp.write_bytes(data)
                os.replace(tmp, path)
                done.append(path.name)
            except Exception as exc:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                errors.append(f"{path.name}: {exc}")
        self._ui_q.put((self._reencode_done, (done, locked, errors)))

    def _reencode_done(self, done, locked, errors):
        self._converting = False
        parts = []
        if done:
            parts.append(_list_block("Done:", done))
        if locked:
            parts.append(_list_block("Skipped (locked — unlock with 🔑 first):", locked))
        if errors:
            parts.append(_list_block("Errors:", errors))
        if parts:
            messagebox.showinfo("Encryption", "\n\n".join(parts))
        if done:
            self._navigate(self._cwd)   # versions/badges changed — refresh
        else:
            self._update_status()

    def _append_selected_to_txt(self):
        """☰ menu: append the current selection's name + path to temp.txt."""
        sel = list(self._selected_items)
        if not sel:
            messagebox.showinfo("Append to txt", "Select one or more items first.",
                                parent=self.root)
            return
        self._append_to_txt(sel)

    def _append_to_txt(self, cells):
        try:
            with open(TEMP_TXT, "a", encoding="utf-8") as fh:
                for cell in cells:
                    fh.write(f"{cell.path.name}\t{cell.path}\n")
            messagebox.showinfo("Appended",
                                f"Added {len(cells)} item(s) to:\n{TEMP_TXT}")
        except Exception as exc:
            messagebox.showerror("Write failed", str(exc))

    def _show_in_explorer(self, path: Path):
        """Open File Explorer with the file/folder highlighted."""
        try:
            subprocess.Popen(["explorer", f"/select,{path}"])
        except Exception as exc:
            messagebox.showerror("Show in Explorer", str(exc))

    def _copy_paths(self, cells):
        """Copy the full path(s) of the selection to the clipboard, one per line."""
        text = "\n".join(str(c.path) for c in cells)
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        n = len(cells)
        self._status_var.set(f"Copied {n} path{'s' if n != 1 else ''} to clipboard")

    # ── rename / move / new folder ─────────────────────────────────────────

    def _retag_path(self, old: Path, new: Path):
        """Migrate tag entries from `old` to `new` after a rename/move so tags
        follow the file.  For a folder, every tagged item beneath it moves too."""
        if not self._tags:
            return
        old_s, new_s = str(old), str(new)
        old_prefix = old_s + os.sep
        remapped = {}
        for key, tags in list(self._tags.items()):
            if key == old_s:
                remapped[new_s] = tags
                del self._tags[key]
            elif key.startswith(old_prefix):
                remapped[new_s + os.sep + key[len(old_prefix):]] = tags
                del self._tags[key]
        if remapped:
            self._tags.update(remapped)
            self._save_tags()

    def _rename_selected(self, cell):
        old = cell.path
        new_name = simpledialog.askstring("Rename", "New name:",
                                          initialvalue=old.name, parent=self.root)
        if not new_name:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old.name:
            return
        new = old.with_name(new_name)
        if new.exists():
            messagebox.showerror("Rename", f"“{new_name}” already exists.", parent=self.root)
            return
        try:
            old.rename(new)
        except Exception as exc:
            messagebox.showerror("Rename failed", str(exc), parent=self.root)
            return
        self._retag_path(old, new)
        self._navigate(self._cwd)
        self._status_var.set(f"Renamed to {new_name}")

    def _batch_rename(self, cells):
        """Rename several files at once from a numbered pattern, with a live preview.
        Tokens: {name} original stem · {n} sequence number · {ext} extension."""
        files = [c for c in cells if not c.is_folder]
        if len(files) < 2:
            return
        # Rename in the order the files appear in the current view (natural-name fallback).
        order = {it: i for i, it in enumerate(self._view)}
        files.sort(key=lambda c: (order.get(c, 1 << 30), _natural_key(c.path.name)))

        dlg = tk.Toplevel(self.root)
        dlg.title("Batch rename")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        frm = tk.Frame(dlg, bg=BG)
        frm.pack(fill=tk.BOTH, expand=True, padx=14, pady=(12, 8))

        tk.Label(frm, text=f"Rename {len(files)} files — pattern:", bg=BG, fg=FG,
                 font=("Segoe UI", 9)).pack(anchor=tk.W, pady=(0, 4))

        pat_var = tk.StringVar(value="{name}")
        pat = tk.Entry(frm, textvariable=pat_var, width=46, bg=BTN_BG, fg=FG,
                       insertbackground=FG, relief=tk.FLAT, font=("Segoe UI", 10))
        pat.pack(fill=tk.X, ipady=3)

        tk.Label(frm, text="{name} original name  ·  {n} number  ·  {ext} extension\n"
                           "The extension is kept automatically unless you use {ext}.",
                 bg=BG, fg=FG_DIM, justify=tk.LEFT, font=("Segoe UI", 8)).pack(
            anchor=tk.W, pady=(2, 6))

        opts = tk.Frame(frm, bg=BG)
        opts.pack(fill=tk.X)
        tk.Label(opts, text="Start number:", bg=BG, fg=FG,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        start_var = tk.StringVar(value="1")
        tk.Spinbox(opts, from_=0, to=999999, textvariable=start_var, width=6,
                   bg=BTN_BG, fg=FG, insertbackground=FG, relief=tk.FLAT,
                   buttonbackground=BTN_BG, font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(4, 0))

        tk.Label(frm, text="Preview:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor=tk.W, pady=(8, 2))
        prev_box = tk.Frame(frm, bg=BG)
        prev_box.pack(fill=tk.BOTH, expand=True)
        pvsb = tk.Scrollbar(prev_box, orient=tk.VERTICAL)
        prev = tk.Listbox(prev_box, yscrollcommand=pvsb.set, height=8, width=54,
                          bg=CELL_BG, fg=FG, relief=tk.FLAT, font=("Consolas", 9),
                          activestyle="none", highlightthickness=0)
        pvsb.config(command=prev.yview)
        pvsb.pack(side=tk.RIGHT, fill=tk.Y)
        prev.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def _compute():
            try:
                start = int(start_var.get())
            except ValueError:
                start = 1
            pattern = pat_var.get()
            pad = max(len(str(start + len(files) - 1)), 1)
            out = []
            for i, c in enumerate(files):
                src = c.path
                num = str(start + i).zfill(pad)
                base = (pattern.replace("{name}", src.stem)
                               .replace("{n}", num)
                               .replace("{ext}", src.suffix.lstrip(".")))
                new_name = base if "{ext}" in pattern else base + src.suffix
                out.append((src, new_name))
            return out

        def _refresh(*_):
            prev.delete(0, tk.END)
            for src, new_name in _compute():
                prev.insert(tk.END, f"  {src.name}   →   {new_name or '⟨empty⟩'}")

        plan = [None]

        def confirm(*_):
            plan[0] = _compute()
            dlg.destroy()

        def cancel(*_):
            dlg.destroy()

        pat_var.trace_add("write", _refresh)
        start_var.trace_add("write", _refresh)
        pat.bind("<Return>", confirm)
        pat.bind("<Escape>", cancel)
        dlg.bind("<Escape>", cancel)

        btn_frm = tk.Frame(dlg, bg=TOOLBAR_BG, pady=6)
        btn_frm.pack(fill=tk.X)
        bcfg = dict(bg=BTN_BG, fg=FG, relief=tk.FLAT, font=("Segoe UI", 9),
                    activebackground=BTN_ACTIVE, activeforeground=ACTIVE_FG, padx=12, pady=3)
        tk.Button(btn_frm, text="Rename", command=confirm, **bcfg).pack(side=tk.LEFT, padx=(10, 4))
        tk.Button(btn_frm, text="Cancel", command=cancel,  **bcfg).pack(side=tk.LEFT, padx=4)

        _refresh()
        dlg.after(50, lambda: (pat.focus_set(), pat.icursor(tk.END)))
        dlg.wait_window()

        if plan[0]:
            self._run_batch_rename(plan[0])

    def _run_batch_rename(self, plan: list):
        """Apply a [(src_path, new_name), …] plan.  Validates first (refusing any
        collision so nothing is renamed on conflict), then renames via temp names
        so files can swap names safely.  Tags follow each file."""
        finals: dict = {}
        errors = []
        for src, new_name in plan:
            new_name = (new_name or "").strip()
            if not new_name:
                errors.append(f"{src.name}: empty name")
            elif "/" in new_name or "\\" in new_name or new_name in (".", ".."):
                errors.append(f"{src.name}: illegal name “{new_name}”")
            else:
                finals[src] = src.with_name(new_name)
        if errors:
            messagebox.showerror("Batch rename",
                                 "Nothing renamed:\n\n" + "\n".join(errors[:15]), parent=self.root)
            return

        src_set = set(finals)
        counts, blocked = {}, []
        for src, dst in finals.items():
            counts[dst] = counts.get(dst, 0) + 1
            if dst.exists() and dst not in src_set:
                blocked.append(f"{src.name} → {dst.name} (already exists)")
        clashing = [d.name for d, c in counts.items() if c > 1]
        if clashing:
            messagebox.showerror("Batch rename",
                "Nothing renamed — the pattern produces duplicate names:\n\n"
                + "\n".join(clashing[:15]), parent=self.root)
            return
        if blocked:
            messagebox.showerror("Batch rename",
                "Nothing renamed — these targets already exist:\n\n"
                + "\n".join(blocked[:15]), parent=self.root)
            return

        # Two-phase: everything → unique temp name, then temp → final name.
        srcs = list(finals)
        staged, done = [], 0
        try:
            for i, src in enumerate(srcs):
                tmp = _unique_path(src.with_name(f"__wifrename_{i}__{src.name}"))
                src.rename(tmp)
                staged.append((tmp, finals[src], src))
            for tmp, final, orig in staged:
                final = _unique_path(final)
                tmp.rename(final)
                self._retag_path(orig, final)
                done += 1
        except Exception as exc:
            messagebox.showerror("Batch rename failed",
                                 f"{exc}\n\nSome files may keep a temporary name.",
                                 parent=self.root)

        self._navigate(self._cwd)
        self._status_var.set(f"Renamed {done} file{'s' if done != 1 else ''}")
        self.root.after(2500, self._update_status)

    def _move_selected(self, cells):
        srcs = [c.path for c in cells]
        if not srcs:
            return
        dest = filedialog.askdirectory(
            title=f"Move {len(srcs)} item{'s' if len(srcs) != 1 else ''} to…")
        if not dest:
            return
        self._do_move(srcs, dest)

    def _new_folder(self):
        name = simpledialog.askstring("New folder", "Folder name:",
                                      initialvalue="New folder", parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        target = self._cwd / name
        if target.exists():
            messagebox.showerror("New folder", f"“{name}” already exists.", parent=self.root)
            return
        try:
            target.mkdir()
        except Exception as exc:
            messagebox.showerror("New folder failed", str(exc), parent=self.root)
            return
        self._navigate(self._cwd)

    def _delete_selected(self):
        """Delete key / default delete — honors the 'Delete to Recycle Bin' setting."""
        self._do_delete(use_trash=self._recycle_var.get() and _TRASH)

    def _recycle_selected(self):
        """Explicit 'Move to Recycle Bin' (context menu)."""
        self._do_delete(use_trash=True)

    def _do_delete(self, use_trash: bool):
        file_items = [it for it in self._selected_items if not it.is_folder]
        if not file_items:
            return
        if use_trash and not _TRASH:
            messagebox.showinfo(
                "Recycle Bin",
                "Install the 'send2trash' package to delete to the Recycle Bin.",
                parent=self.root)
            return

        n = len(file_items)
        names = "\n".join(it.path.name for it in file_items[:10])
        if n > 10:
            names += f"\n… and {n - 10} more"

        if use_trash:
            title = f"Recycle {'file' if n == 1 else f'{n} files'}"
            body  = (f"Move {'this file' if n == 1 else f'these {n} files'} "
                     f"to the Recycle Bin?\n\n{names}")
        else:
            title = f"Delete {'file' if n == 1 else f'{n} files'}"
            body  = (f"{'Delete' if n == 1 else f'Delete {n} files'}?\n\n{names}"
                     f"\n\nFiles will be overwritten with zeros before deletion.")

        if not messagebox.askyesno(title, body, icon="warning"):
            return

        errors, deleted = [], []
        for it in file_items:
            try:
                if use_trash:
                    _send2trash(str(it.path))
                else:
                    secure_delete(it.path)
                deleted.append(it)
            except Exception as exc:
                errors.append(f"{it.path.name}: {exc}")

        if deleted and self._tags:
            changed = any(self._tags.pop(str(it.path), None) is not None for it in deleted)
            if changed:
                self._save_tags()
        dset = set(deleted)
        self._items = [it for it in self._items if it not in dset]
        self._selected_items -= dset
        if self._anchor_item in dset:
            self._anchor_item = None
        if self._lead_item in dset:
            self._lead_item = None
        self._recompute_view()
        self._reflow()
        self._update_status()
        if deleted:
            verb = "Moved to Recycle Bin" if use_trash else "Deleted"
            self._status_var.set(f"{verb} {len(deleted)} file{'s' if len(deleted) != 1 else ''}")
            self.root.after(2500, self._update_status)
        if errors:
            messagebox.showerror("Delete errors", "\n".join(errors))

    # ── grid layout ───────────────────────────────────────────────────────

    def _pool_cell(self, k: int):
        """Return pool slot k, creating the tile (and its persistent, initially
        hidden canvas window) on demand.  The window is reused for the life of the
        tile — moved and shown/hidden rather than recreated — so scrolling is smooth."""
        while len(self._pool) <= k:
            tw, th, cw, ch = self._pool_dims
            c = Cell(self._canvas, self._on_double_click, self._on_cell_select,
                     self._on_cell_right_click, cw, ch, tw, th,
                     self._hover_enter, self._hover_leave,
                     self._cell_drag_motion, self._cell_drag_release)
            c._winid = self._canvas.create_window(0, 0, window=c, anchor=tk.NW,
                                                  state="hidden")
            self._pool.append(c)
        return self._pool[k]

    def _yview_render(self, *args):
        """Scrollbar command: scroll, then re-render the now-visible rows."""
        self._canvas.yview(*args)
        self._reflow()

    def _reflow(self):
        """Render only the rows in view (plus a small buffer) by recycling a small
        pool of Cell widgets.  Tiles keep a persistent canvas window that is moved
        and shown/hidden — never destroyed — so scrolling doesn't flicker."""
        cw = self._canvas.winfo_width()
        if cw < 2:
            return                       # canvas not realised yet
        tw, th, cell_w, cell_h = self._size_preset

        # Rebuild the pool if the cell size changed (Small <-> Large).
        if self._pool_dims != (tw, th, cell_w, cell_h):
            for c in self._pool:
                c.destroy()              # destroys its canvas window too
            self._pool = []
            self._visible = {}
            self._pool_dims = (tw, th, cell_w, cell_h)

        cols = max(1, (cw + GAP) // (cell_w + GAP))
        view = self._view

        # Preview rows (folders) above the grid — persistent windows that are
        # repositioned/hidden (not recreated) so they don't flicker either.
        base = 0
        if self._preview_mode and self._rows:
            flt = self._filter_text_var.get().strip().lower()
            y = 2
            for row in self._rows:
                wid = getattr(row, "_winid", None)
                if flt and flt not in row.path.name.lower():
                    if wid is not None:
                        self._canvas.itemconfigure(wid, state="hidden")
                    continue
                if wid is None:
                    row._winid = self._canvas.create_window(
                        4, y, window=row, anchor=tk.NW,
                        width=max(cell_w, cw - 8))
                else:
                    self._canvas.coords(wid, 4, y)
                    self._canvas.itemconfigure(wid, state="normal",
                                               width=max(cell_w, cw - 8))
                y += row.img_h + 20
            base = y

        n_rows = (len(view) + cols - 1) // cols
        total_h = base + (n_rows * (cell_h + GAP) + GAP if view else 0)
        canvas_h = self._canvas.winfo_height() or 1
        self._canvas.configure(scrollregion=(0, 0, cw, max(total_h, canvas_h)))

        # Which grid rows are on screen (+ a buffer above/below for smooth scroll).
        top = self._canvas.canvasy(0)
        BUF = 2
        first_row = max(0, int((top - base) // (cell_h + GAP)) - BUF)
        last_row  = int((top + canvas_h - base) // (cell_h + GAP)) + BUF
        first = max(0, first_row * cols)
        last  = min(len(view), (last_row + 1) * cols)

        self._visible = {}
        cut_paths = set(self._clipboard) if self._clipboard_cut else set()
        k = 0
        for idx in range(first, last):
            item = view[idx]
            cell = self._pool_cell(k); k += 1
            tags_str = ""
            if self._tags and self._show_tags_var.get():
                t = self._get_tags(item.path)
                if t:
                    tags_str = "🏷 " + ", ".join(t)
            cell.rebind(item, item in self._selected_items, tags=tags_str,
                        cut=item.path in cut_paths)
            x  = GAP // 2 + (idx % cols) * (cell_w + GAP)
            yy = base + GAP // 2 + (idx // cols) * (cell_h + GAP)
            self._canvas.coords(cell._winid, x, yy)          # move, don't recreate
            self._canvas.itemconfigure(cell._winid, state="normal")
            self._visible[item] = cell
            if item.photo is None and not item.queued:       # decode lazily, once
                item.queued = True
                self._load_q.put(("cell", item, tw, th, self._gen))

        # Hide the pool tiles we didn't use this pass.
        for j in range(k, len(self._pool)):
            self._canvas.itemconfigure(self._pool[j]._winid, state="hidden")

        if total_h < canvas_h:
            self._canvas.yview_moveto(0)

    def _on_canvas_resize(self, event):
        # Debounce — relaying hundreds of canvas items on every resize tick is wasteful.
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(60, self._do_resize_reflow)

    def _do_resize_reflow(self):
        self._resize_job = None
        self._reflow()

    # ── thumbnail loading (background thread + main-thread apply) ─────────

    def _loader_worker(self):
        """Runs in a background thread. Loads PIL Images, never touches tkinter."""
        while True:
            job = self._load_q.get()
            kind = job[0]
            if kind == "row":
                _, row, tw, th, gen = job
                if gen != self._gen:
                    continue
                passwords = list(self._passwords)
                try:
                    result = load_preview_data(row.path, row.img_w, row.img_h,
                                               row.n_slots, passwords)
                except Exception:
                    result = {"imgs": [], "paths": [], "subtitle": ""}
                if gen == self._gen:
                    self._result_q.put(("row", row, result))
                continue

            # kind == "cell"
            _, item, tw, th, gen = job
            if gen != self._gen:
                continue
            passwords = list(self._passwords)   # snapshot of known passwords
            path = item.path
            try:
                if item.is_folder:
                    result = make_folder_icon(tw, th)
                else:
                    ext = path.suffix.lower()
                    if ext == ".wif":
                        result = load_wif_thumbnail(path, tw, th, passwords)
                    elif ext == ".wvf":
                        result = load_wvf_thumbnail(path, tw, th, passwords)
                    elif ext in IMAGE_EXTS:
                        result = load_image_thumbnail(path, tw, th)
                    elif ext in VIDEO_EXTS:
                        result = load_video_thumbnail(path, tw, th)
                    elif ext in AUDIO_EXTS:
                        result = make_audio_icon(tw, th)
                    else:
                        result = make_error_icon(tw, th)
            except wif_format.WrongPassword:
                result = make_locked_icon(tw, th)     # encrypted, no known password
            except Exception:
                result = make_error_icon(tw, th)
            if gen == self._gen:
                self._result_q.put(("cell", item, result))

    def _apply_thumbnails(self):
        """Runs in the main thread. Converts PIL Images to PhotoImages and sets them,
        and runs any UI callbacks posted by worker threads."""
        try:
            while True:                      # worker-thread callbacks (status, done)
                func, args = self._ui_q.get_nowait()
                func(*args)
        except Empty:
            pass
        try:
            while True:
                kind, target, result = self._result_q.get_nowait()
                if kind == "row":
                    if target.winfo_exists():
                        target.set_preview(result)
                else:  # "cell"
                    item = target
                    item.photo = ImageTk.PhotoImage(result)
                    cell = self._visible.get(item)
                    if cell is not None and cell.winfo_exists() and cell.item is item:
                        cell.set_image(item.photo)
        except Empty:
            pass
        self.root.after(40, self._apply_thumbnails)


# ── entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    root.state("zoomed")
    app = WifBrowser(root)

    # Optional: start in a specific folder passed as argument
    if len(sys.argv) > 1:
        start = Path(sys.argv[1])
        if start.is_dir():
            app._navigate(start)

    root.mainloop()
