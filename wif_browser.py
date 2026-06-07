"""
WIF Browser — navigate folders and browse .wif files as thumbnails.

Controls:
  Double-click folder    → enter folder
  Double-click .wif      → open in wif_viewer.py
  Backspace / Up button  → parent folder
  Enter in path bar      → navigate to typed path
  Mouse wheel            → scroll grid
  Arrow keys             → move selection (Shift extends; Home/End = first/last)
  Enter (in grid)        → open the selected item
  Drag on empty space    → rubber-band select (Ctrl-drag adds to selection)
  Right-click            → menu (Open, Show in Explorer, Copy path, …)
"""

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from tkinter import filedialog, messagebox, simpledialog
from PIL import Image, ImageTk, ImageDraw, ImageSequence

import wif_format

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

# ── constants ────────────────────────────────────────────────────────────────

GAP = 10

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".ico", ".avif"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"}
MEDIA_EXTS = {".wif"} | IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

VIEWER_EXTS = {".wif"} | IMAGE_EXTS   # file types the viewer can open

FILTER_WIF   = "Folders + WIF"
FILTER_MEDIA = "Folders + Media"
FILTER_ALL   = "Folders + All files"
FILTERS      = [FILTER_WIF, FILTER_MEDIA, FILTER_ALL]

SORT_NAME, SORT_DATE, SORT_SIZE, SORT_TYPE = "Name", "Date", "Size", "Type"
SORT_OPTIONS = [SORT_NAME, SORT_DATE, SORT_SIZE, SORT_TYPE]

# (thumb_w, thumb_h, cell_w, cell_h)
SIZE_SMALL = (140, 110, 164, 158)
SIZE_LARGE = (260, 200, 300, 248)
HOVER_PREVIEW = (520, 460)   # max size of the hover-zoom popup image

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
    if path.suffix.lower() == ".avif" and not _AVIF:
        raise ValueError("AVIF not supported — run: pip install pillow-avif-plugin")
    img = Image.open(path).convert("RGBA")
    img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
    if path.suffix.lower() == ".gif":
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
    # draw a small play icon overlay
    d = ImageDraw.Draw(img)
    iw, ih = img.size
    cx, cy = iw // 2, ih // 2
    s = int(min(iw, ih) * 0.18)
    d.ellipse([cx - s - 2, cy - s - 2, cx + s + 2, cy + s + 2], fill=(0, 0, 0, 140))
    d.polygon([cx - s // 2, cy - s // 2, cx - s // 2, cy + s // 2, cx + s, cy],
              fill=(255, 255, 255, 220))
    return img


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
    found, queue = [], []
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
        current = queue.pop(0)
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
                    if e.is_file(follow_symlinks=False) and os.path.splitext(e.name)[1].lower() in PREVIEW_EXTS:
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
    candidates = sorted(_gather_preview_media(folder), key=lambda p: p.name.lower())

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
    size = path.stat().st_size
    with open(path, "r+b") as fh:
        fh.write(b"\x00" * size)
        fh.flush()
        os.fsync(fh.fileno())
    path.unlink()


def _list_block(title: str, items: list, limit: int = 15) -> str:
    """Format a capped list for a summary message box."""
    text = f"{title}\n" + "\n".join(items[:limit])
    if len(items) > limit:
        text += f"\n… and {len(items) - limit} more"
    return text


# ── Cell widget ──────────────────────────────────────────────────────────────

class Cell(tk.Frame):
    """A recycled thumbnail tile.  Created once and re-bound to different Items as
    the grid scrolls (see WifBrowser._reflow), so a huge folder needs only enough
    tiles to cover the viewport."""
    def __init__(self, parent, on_dbl, on_select, on_right_click,
                 cell_w: int, cell_h: int, thumb_w: int, thumb_h: int,
                 on_hover=None, on_unhover=None):
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

        for w in (self, self.img_lbl, self.name_lbl):
            w.bind("<Double-Button-1>", lambda e: on_dbl(self))
            w.bind("<Button-1>",        lambda e: on_select(self, e))
            w.bind("<Button-3>",        lambda e: on_right_click(self, e))
            w.bind("<Enter>",           lambda e: self._tint(CELL_HL))
            w.bind("<Leave>",           lambda e: self._tint(CELL_SEL if self._selected else CELL_BG))

        # Hover-zoom — fires on the thumbnail image; the browser ignores it
        # unless it's in "hover" size mode.
        if on_hover is not None:
            self.img_lbl.bind("<Enter>", lambda e: on_hover(self, e),   add="+")
            self.img_lbl.bind("<Leave>", lambda e: on_unhover(self, e), add="+")

    def rebind(self, item, selected: bool):
        """Point this recycled tile at `item` and refresh its name, image and tint."""
        self.item      = item
        self.path      = item.path
        self.is_folder = item.is_folder
        name = item.path.name or str(item.path)
        max_chars = ((self._cell_w - 12) // 7) * 2   # ~7px/char, two lines
        if len(name) > max_chars:
            name = name[:max_chars - 3] + "..."
        self.name_lbl.configure(text=name)
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
    __slots__ = ("path", "is_folder", "kind", "photo", "queued")

    def __init__(self, path: Path, is_folder: bool, kind: str):
        self.path      = path
        self.is_folder = is_folder
        self.kind      = kind          # "folder" | "file"
        self.photo     = None          # ImageTk.PhotoImage once decoded (cached)
        self.queued    = False         # a load task has been enqueued


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
        self._passwords: list = []           # session passwords for encrypted files
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
        root.configure(bg=BG)
        self._build_toolbar()
        self._build_grid_area()
        self._build_statusbar()

        root.bind("<BackSpace>",  lambda e: None if (self._focus_in_entry() or self._overlay) else self._go_up())
        root.bind("<Alt-Left>",   lambda e: self._overlay_close() if self._overlay else self._go_up())
        root.bind("<Delete>",     lambda e: None if (self._focus_in_entry() or self._overlay) else self._delete_selected())
        root.bind("<Control-f>",  lambda e: self._filter_entry.focus_set())
        root.bind("<Control-a>",  lambda e: None if (self._focus_in_entry() or self._overlay) else self._select_all())
        root.bind("<Escape>",     lambda e: self._overlay_close() if self._overlay else None)
        root.bind("<Left>",       lambda e: self._overlay_nav(-1) if self._overlay else self._grid_key(-1, 0, e))
        root.bind("<Right>",      lambda e: self._overlay_nav(1)  if self._overlay else self._grid_key(1, 0, e))
        root.bind("<Up>",         lambda e: None if self._overlay else self._grid_key(0, -1, e))
        root.bind("<Down>",       lambda e: None if self._overlay else self._grid_key(0, 1, e))
        root.bind("<Home>",       lambda e: None if self._overlay else self._grid_jump(0, e))
        root.bind("<End>",        lambda e: None if self._overlay else self._grid_jump(-1, e))
        root.bind("<Return>",     lambda e: self._grid_open())
        root.bind("<KP_Enter>",   lambda e: self._grid_open())

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
        self._filter_entry.bind("<KeyRelease>", lambda e: self._apply_filter())
        self._filter_entry.bind("<Escape>",
                                lambda e: (self._filter_text_var.set(""), self._apply_filter()))

        self._path_var = tk.StringVar()
        entry = tk.Entry(
            tb, textvariable=self._path_var,
            bg=BTN_BG, fg=FG, insertbackground=FG,
            relief=tk.FLAT, font=("Segoe UI", 10)
        )
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, ipady=4)
        entry.bind("<Return>", lambda e: self._go_to_typed())

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
                    if f_mode == FILTER_WIF   and ext == ".wif":
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
            elif ext in IMAGE_EXTS:
                img = Image.open(path)
            else:
                return None
        except Exception:
            return None
        img = img.copy()
        img.thumbnail(HOVER_PREVIEW, Image.LANCZOS)
        if len(self._hover_pcache) > 60:
            self._hover_pcache.clear()
        self._hover_pcache[path] = img
        return img

    def _go_up(self):
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
        """Sort key for the active sort mode (name is the stable tiebreaker)."""
        mode = self._sort_var.get()
        try:
            if mode == SORT_DATE:
                return (p.stat().st_mtime, p.name.lower())
            if mode == SORT_SIZE:
                return (p.stat().st_size, p.name.lower())
            if mode == SORT_TYPE:
                return (p.suffix.lower(), p.name.lower())
        except OSError:
            return (0, p.name.lower())
        return p.name.lower()

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

        _set_theme(self._theme_var.get())

    def _focus_in_entry(self) -> bool:
        """True when a text Entry holds focus — global single-key shortcuts defer."""
        return isinstance(self.root.focus_get(), tk.Entry)

    def _recompute_view(self):
        """Rebuild the filtered view list from the model + current filename filter."""
        flt = self._filter_text_var.get().strip().lower()
        self._view = [it for it in self._items
                      if (not flt) or flt in it.path.name.lower()]

    def _apply_filter(self):
        """Recompute which items match the filter, then re-render from the top."""
        self._recompute_view()
        self._canvas.yview_moveto(0)
        self._reflow()
        self._update_status()

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
        for unit in ("B", "KB", "MB", "GB"):
            if f < 1024 or unit == "GB":
                return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
            f /= 1024

    def _single_file_status(self, path: Path) -> str:
        """Status line for one selected file: name · resolution · size."""
        try:
            size = self._fmt_size(path.stat().st_size)
        except OSError:
            size = "?"
        res = ""
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
            elif ext in IMAGE_EXTS:
                with Image.open(path) as im:
                    res = f"{im.size[0]} × {im.size[1]} px"
        except Exception:
            res = ""
        parts = [path.name] + ([res] if res else []) + [size]
        return "      ".join(parts)

    # ── passwords ─────────────────────────────────────────────────────────

    def _add_password(self):
        """Prompt for another password and refresh so newly-unlockable
        thumbnails decrypt."""
        pw = simpledialog.askstring("Add password", "Enter a password to try:",
                                    show="*", parent=self.root)
        if pw and pw not in self._passwords:
            self._passwords.append(pw)
            self._navigate(self._cwd)   # re-render: locked files may now unlock

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
        if pw not in self._passwords:
            self._passwords.append(pw)
        return pw

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
        """Left-click: single select.  Ctrl+click: toggle.  Shift+click: range."""
        item = cell.item
        if item is None:
            return
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

        if file_cells:
            plural = "" if n_files == 1 else f" ({n_files} files)"
            menu.add_command(label=f"Convert to WIF v1{plural}",
                             command=lambda: self._convert_selected(file_cells, version=1))
            menu.add_command(label=f"Convert to WIF v2 — encrypted 🔒{plural}",
                             command=lambda: self._convert_selected(file_cells, version=2))

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
                lbl = ("Remove encryption (→ v1)…" if len(enc_cells) == 1
                       else f"Remove encryption (→ v1)…  ({len(enc_cells)})")
                enc_menu.add_command(label=lbl, command=lambda c=enc_cells: self._decrypt_to_v1(c))
            menu.add_cascade(label="Encryption", menu=enc_menu)

        menu.add_separator()
        if sel:
            menu.add_command(label="Show in Explorer",
                             command=lambda p=sel[0].path: self._show_in_explorer(p))
        menu.add_command(label=("Copy path" if n_sel == 1 else f"Copy paths ({n_sel})"),
                         command=lambda s=list(sel): self._copy_paths(s))
        menu.add_command(label="Append to temp.txt",
                         command=lambda: self._append_to_txt(sel))

        if file_cells and n_files == n_sel:   # hide if any folder is in the selection
            menu.add_separator()
            delete_label = "Delete" if n_files == 1 else f"Delete {n_files} files"
            menu.add_command(label=delete_label, command=self._delete_selected)

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
            elif path.suffix.lower() in IMAGE_EXTS:
                with Image.open(path) as img:
                    w, h = img.size
                return f"{w} × {h} px  —  {path.name}"
            else:
                size = path.stat().st_size
                return f"{path.name}  ({size:,} bytes)"
        except Exception:
            return path.name

    def _open_selected(self, cells):
        for cell in cells:
            if cell.is_folder:
                subprocess.Popen(["explorer", str(cell.path)])
            elif cell.path.suffix.lower() in VIEWER_EXTS:
                subprocess.Popen(_viewer_cmd(cell.path), env=self._viewer_env())
            else:
                os.startfile(cell.path)

    def _open_in_viewer(self, cells):
        """Open each viewer-compatible file (.wif or image) in the Weird Viewer."""
        for cell in cells:
            if not cell.is_folder and cell.path.suffix.lower() in VIEWER_EXTS:
                subprocess.Popen(_viewer_cmd(cell.path), env=self._viewer_env())

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

            tk.Label(self._overlay, bg="#0b0b0b", fg="#888888", font=("Segoe UI", 9),
                     text="←/→ or wheel: navigate     click background · Esc · ✕: close"
                     ).place(relx=0.5, rely=1.0, y=-10, anchor=tk.S)

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
            self._overlay.bind("<Configure>", lambda e: self._overlay_render())

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

    def _overlay_render(self):
        if self._overlay is None or self._overlay_img is None:
            return
        ow = self._overlay.winfo_width()  or self.root.winfo_width()
        oh = self._overlay.winfo_height() or self.root.winfo_height()
        margin = 48
        avail_w, avail_h = max(1, ow - 2 * margin), max(1, oh - 2 * margin)
        img = self._overlay_img
        ratio = min(avail_w / img.width, avail_h / img.height)
        size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        self._overlay_photo = ImageTk.PhotoImage(img.resize(size, Image.LANCZOS))
        self._overlay_lbl.configure(image=self._overlay_photo)

    def _overlay_nav(self, step: int):
        if self._overlay is None or not self._overlay_paths:
            return
        self._overlay_index = (self._overlay_index + step) % len(self._overlay_paths)
        self._overlay_load(allow_prompt=False)

    def _overlay_close(self):
        self._overlay_stop_anim()
        if self._overlay is not None:
            self._overlay.destroy()
            self._overlay = None
            self._overlay_img = None
            self._overlay_photo = None

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
            return wif_format.is_encrypted(path.read_bytes())
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
                if pw not in self._passwords:
                    self._passwords.append(pw)
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
                                         compress=True, password=password)
                path.with_suffix(".wif").write_bytes(data)
                converted.append(path.name)
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
            jobs = [(p, Path(folder) / (p.stem + ".png")) for p in files]

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
                                         compress=meta["compressed"], password=new_password)
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
            subprocess.Popen(f'explorer /select,"{path}"')
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

    def _delete_selected(self):
        file_items = [it for it in self._selected_items if not it.is_folder]
        if not file_items:
            return

        n = len(file_items)
        names = "\n".join(it.path.name for it in file_items[:10])
        if n > 10:
            names += f"\n… and {n - 10} more"

        confirmed = messagebox.askyesno(
            f"Delete {'file' if n == 1 else f'{n} files'}",
            f"{'Delete' if n == 1 else f'Delete {n} files'}?\n\n{names}"
            f"\n\nFiles will be overwritten with zeros before deletion.",
            icon="warning",
        )
        if not confirmed:
            return

        errors, deleted = [], []
        for it in file_items:
            try:
                secure_delete(it.path)
                deleted.append(it)
            except Exception as exc:
                errors.append(f"{it.path.name}: {exc}")

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
                     self._hover_enter, self._hover_leave)
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
        k = 0
        for idx in range(first, last):
            item = view[idx]
            cell = self._pool_cell(k); k += 1
            cell.rebind(item, item in self._selected_items)
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
