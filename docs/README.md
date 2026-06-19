# Weird Traveler

A self-contained image (and video) toolkit built around two custom file formats:
**WIF** (`.wif`, a simple, optionally-encrypted image format) and **WVF**
(`.wvf`, an encrypted container that seals an ordinary video file).

The suite is a CLI converter, two GUIs and shared libraries:

| Program | Name | What it is |
|---------|------|-----------|
| `wif_converter.py` | — | Command-line converter: PNG/JPG/… → `.wif` (batch, recursive, encrypt, filter) |
| `wif_viewer.py` | **Weird Viewer** | A GUI image viewer (zoom, rotate, flip, animation playback, image adjustments, delete, export) |
| `wif_browser.py` | **Weird Traveler** | A GUI thumbnail file browser (virtualized grid, previews, themes, keyboard nav, tags, convert/encrypt) — also **plays, seals and decrypts `.wvf` video** |
| `wif_format.py` | — | WIF core library — all image encode/decode/encryption logic |
| `wif_filter.py` | — | PNG-style spatial filtering used by WIF **v3** |
| `wvf/` | **Weird Video Format** | Library + CLI + player for `.wvf` — AES-256-GCM encrypted video containers |

> Everything is plain Python + tkinter. It can run from source, or ship as a
> single Windows executable — **`Weird Traveler.exe`** — that contains the
> browser and the viewer (see [Building a single `.exe`](#building-a-single-exe)).


## Table of contents

- [Requirements & installation](#requirements--installation)
- [Project files](#project-files)
- [The converter (`wif_converter.py`)](#the-converter-wif_converterpy)
- [The viewer — Weird Viewer (`wif_viewer.py`)](#the-viewer--weird-viewer-wif_viewerpy)
- [The browser — Weird Traveler (`wif_browser.py`)](#the-browser--weird-traveler-wif_browserpy)
- [WVF — encrypted video](#wvf--encrypted-video)
- [Tags](#tags)
- [Encryption & passwords](#encryption--passwords)
- [Settings file](#settings-file)
- [Using the library directly](#using-the-library-directly)
- [Building a single `.exe`](#building-a-single-exe)
- [Security notes & limitations](#security-notes--limitations)

---

## Requirements & installation

Install the dependencies:

```bash
pip install Pillow cryptography
# optional extras:
pip install pillow-avif-plugin   # read .avif input images
pip install opencv-python        # real video thumbnails (incl. .wvf) in the browser
pip install numpy                # smaller files via WIF v3 spatial filtering
pip install imageio-ffmpeg       # transcode video while sealing it into .wvf
```

| Package | Needed for |
|---------|-----------|
| **Pillow** | Everything (image I/O) — required |
| **cryptography** | WIF **v2** and **WVF** encryption/decryption — required to make or open any encrypted file |
| **pillow-avif-plugin** | Reading `.avif` source images — optional |
| **opencv-python** | Thumbnailing video files (and `.wvf` cover frames) in the browser — optional (falls back to a play icon) |
| **numpy** | WIF **v3** spatial filtering (`--filter`, *Convert to WIF v3*) — optional (plain/compressed v3 still works without it) |
| **imageio-ffmpeg** | Optional `--transcode` when sealing video into `.wvf` (shrink first) — optional |

If `cryptography` is missing, plain WIF (v1/v3) files still work; only encryption
and all `.wvf` features are disabled.

**Playing `.wvf` video** needs an **`ffplay`** binary (part of
[FFmpeg](https://ffmpeg.org/)) on your `PATH` or next to the program — the
decrypted stream is piped straight into it so no plaintext ever touches disk. If
`ffplay` isn't found, the browser falls back to decrypting to a temporary file
and opening it in your default player (the temp file is scrubbed afterwards).

---

## Project files

```
wif_format.py        WIF core library (import this, don't run it)
wif_filter.py        PNG-style spatial filtering for WIF v3
wif_converter.py     CLI converter (images → .wif)
wif_viewer.py        Weird Viewer (GUI)
wif_browser.py       Weird Traveler (GUI) — also plays/seals/decrypts .wvf
wif_main.py          single-exe entry point (dispatches to browser or viewer)
build.py             PyInstaller build script for Weird Traveler.exe
wvf/                 Weird Video Format package:
  ├─ core.py           encrypt/decrypt container logic (AES-256-GCM, STREAM)
  ├─ codec.py          container/codec sniffing & helpers
  ├─ converter.py      CLI:  python -m wvf.converter wrap|unwrap|info
  └─ player.py         ffplay streaming + temp-file fallback player
wif_icon.ico         app icon used by both GUIs
wif_settings.json    browser settings (auto-created when "Remember settings" is on)
wif_tags.json        tag database (auto-created when you first tag a file; encrypted if a password is set)
temp.txt             scratch list written by the browser's "Append to temp.txt"
WIF_FORMAT.md        byte-level WIF format specification
README.md            this file
```

The GUIs find `wif_icon.ico` next to themselves; if it's missing they simply run
without a custom icon.

---

## The converter (`wif_converter.py`)

Converts ordinary images to `.wif`. Compression is **on by default**.

```bash
python wif_converter.py photo.png                  # -> photo.wif (next to the source)
python wif_converter.py a.png b.jpg c.bmp          # several at once
python wif_converter.py photo.png -o out.wif       # choose the output name
python wif_converter.py *.jpg -d converted/        # all outputs into one folder
python wif_converter.py photo.png --no-compress    # store raw pixels
python wif_converter.py photos/ -r                 # walk subfolders, convert every image
python wif_converter.py photos/ -r -d out/         # mirror the folder tree into out/
python wif_converter.py photos/ -r --skip-existing # skip images already converted
python wif_converter.py photo.png --filter         # smaller WIF v3 (spatial filtering + zlib)
python wif_converter.py secret.png --encrypt       # write an encrypted v2 file
python wif_converter.py secret.png --encrypt --filter # encrypted AND filtered
```

### Options

| Option | Meaning |
|--------|---------|
| `images...` | One or more image files and/or folders |
| `-o, --output PATH` | Output path (single input only) |
| `-d, --output-dir DIR` | Put outputs in `DIR`; with `-r` the source tree is mirrored |
| `-r, --recursive` | Descend into subfolders and convert every supported image |
| `--skip-existing` | Skip sources whose `.wif` already exists and is newer |
| `--encrypt`, `--v2` | Write encrypted **v2** files |
| `--password PW` | Password for `--encrypt` (otherwise prompted securely with no echo) |
| `--no-compress` | Disable zlib compression |
| `--filter` | Spatially filter pixels before compression for smaller files (writes **WIF v3**; needs numpy) |
| `--filter-full` | Use the full 5-filter set: a little smaller, slower to decode. Implies `--filter` |

**Recursive conversion** accepts these source extensions: `.png .jpg .jpeg .bmp
.gif .tiff .tif .webp .ico .avif`. Without `-r`, folders are skipped.

**Encrypting from the CLI:** `--encrypt` asks for a password twice (via `getpass`,
so it isn't shown or stored in shell history). Use `--password` for scripting.

---

## The viewer — Weird Viewer (`wif_viewer.py`)

```bash
python wif_viewer.py             # opens a file picker
python wif_viewer.py image.wif   # opens that file directly
```

Opens `.wif` **and** ordinary images (`.jpg .png .gif .bmp .tiff .webp .ico
.avif`). The ◀/▶ buttons step through all viewable files in the same folder.
Ordinary photos are auto-rotated to match their EXIF orientation.

### Toolbar

| Button | Action |
|--------|--------|
| <- -> | Previous / next image in the folder |
| > Play / ⏸ Pause | Play or pause an animated image — GIF, WebP or APNG (appears only for multi-frame files) |
| Stretch | Scale the image to fill the window |
| Unstretch | Show at original size |
| ⟳ Rotate | Rotate 90° clockwise; a 4th press returns to 0° |
| ↔ Flip / ↕ Flip | Mirror horizontally / vertically (non-destructive) |
| − + 100% | Zoom out / in / reset |
| ⚖ Auto color | Toggle **auto colour balance** (per-channel auto-levels: neutralises colour casts, maximises contrast) |
| 🎚 Adjust | Show/hide the **adjustments panel** (brightness, contrast, saturation, gamma, sharpen, equalize) |
| 🗑 Delete | Securely delete the current file |

### Menus

- **File** — Open (`Ctrl+O`), Save as image (`Ctrl+S`), Delete current file (`Del`), Exit.
- **View** — Zoom in/out, Reset zoom, Stretch, Unstretch, Rotate 90° (`R`), Flip horizontal (`H`) / vertical (`V`), Auto colour balance (`B`), Equalize histogram, Adjustments panel (`A`), Reset adjustments.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+O` | Open a file |
| `Ctrl+S` | Save the current view as a standard image |
| `←` / `→` | Previous / next image in the folder |
| `+` / `-` | Zoom in / out |
| `0` | Reset zoom to 100% |
| `R` | Rotate 90° clockwise |
| `H` / `V` | Flip horizontal / vertical |
| `B` | Toggle auto colour balance |
| `A` | Show / hide the adjustments panel |
| `Space` | Play / pause animation |
| `Del` | Delete current file (with confirmation) |
| `F11` | Toggle fullscreen |
| mouse wheel | Scroll a zoomed image (Shift = horizontal) |
| `Ctrl + mouse wheel` | Zoom | 
| drag | Pan a zoomed image |

### Notes

- **Save as image** exports *what you currently see* — rotation, flips and every
  adjustment are baked into the saved PNG/JPG/BMP/WEBP. This is also how you get a
  normal image back out of a `.wif` (or decrypt a v2 file to a plain image).
- **Rotation and flips** reset each time you open a different image. They're
  lossless and non-destructive — the file on disk is unchanged.
- **Animation** — animated GIF, WebP and APNG play in the viewer; `Space` toggles.
- **Delete** confirms, overwrites the file with zeros, then unlinks it, and moves
  to the next image.
- **Encrypted files** prompt for a password when opened, re-prompt on a wrong one,
  and remember a correct password for the rest of the session. When the viewer is
  launched from the browser it inherits the browser's known passwords, so it
  usually won't ask again.

### Image adjustments

Press **🎚 Adjust** (or `A`) to open a panel of live controls below the toolbar:

| Control | Effect |
|---------|--------|
| Brightness / Contrast / Saturation | Standard tone & colour sliders (1.0 = unchanged) |
| Gamma | Lift shadows ( >1 ) or deepen them ( <1 ) without clipping highlights |
| Sharpen | Unsharp-mask strength (0 = off) |
| Equalize | One-click histogram equalisation |
| ⚖ Auto color | Per-channel auto-levels (also on the toolbar) |
| ↺ Reset | Clear every adjustment back to the original |

Adjustments are non-destructive and **persist as you step through a folder**, so a
batch shot in the same light stays corrected; **Reset** clears them. They preview
at screen resolution for speed and are applied at full resolution by **Save as**.

---

## The browser — Weird Traveler (`wif_browser.py`)

```bash
python wif_browser.py            # start in the current directory
python wif_browser.py C:\Photos  # start in a given folder
```

The grid is **virtualized** — only the rows currently on screen are built as
widgets — so folders with thousands of files open and scroll smoothly.

### Toolbar (left → right)

| Control | Action |
|---------|--------|
| ☰ | **Settings menu** (see below) |
| ↑ Up | Go to the parent folder |
| Small / Large / Hover zoom | Cycle the thumbnail size mode (the label shows the *active* mode) |
| Preview | Toggle **preview mode** (rich folder rows) |
| Thumbnails: 1–8 | (preview mode only) how many sample thumbnails per folder |
| Folders + … | Which files to show: **WIF/WVF**, **Media**, or **All files** |
| Filter: | Live filename filter (`Ctrl+F` focuses it, `Esc` clears it) |
| Tags: | Live tag filter — shows only files that have a matching tag (`Esc` clears it; clicking the field prompts for the tags password if the database is encrypted and not yet unlocked) |
| path bar + Go | Type or paste a folder path and jump to it |

### ☰ Settings menu

| Item | Action |
|------|--------|
| 🔑 Unlock… | Add a session password (decrypts matching `.wif` thumbnails) |
| Sort by ▸ | Name / Date / Size / Type |
| Descending order | Reverse the sort |
| Theme ▸ | **Light / Dark / Red** |
| Hover-to-scrub previews | Enable/disable preview auto-scrub on hover (off by default) |
| Remember settings | Persist theme, sort, size mode, filter & scrub across runs |
| Show tags in cells | Toggle the `🏷 tag1, tag2` badge under thumbnails (on by default; saved with Remember settings) |
| 🏷 Tag search… | Open the **global tag search** dialog (search all tagged files across all folders) |

### Thumbnail size modes

- **Small / Large**
- **Hover zoom** — a compact grid where hovering a thumbnail pops a large crisp
  preview beside it (decoded fresh from the file, cached per image).

### Preview mode

Each folder becomes a **rich row**: the folder name with a "*N images · date*"
subtitle, plus a strip of **cover-cropped** sample thumbnails (no stretching).
Samples are chosen deterministically (evenly spread, stable between visits).

- **Single-click a thumbnail** → open it in the lightbox (arrow through the
  folder's previewed images).
- **Double-click the name/row** → enter the folder.
- **Hover** → if "Hover-to-scrub previews" is enabled, the strip slowly cycles
  through more of the folder's images.
- Folders with no direct images fall back to previewing images from their
  subfolders ("in subfolders").

### Thumbnails

| Type | Shown as |
|------|----------|
| `.wif` (plain) | Decoded image |
| `.wif` (encrypted, unlocked) | Decoded image with a small 🔒 badge |
| `.wif` (encrypted, locked) | A padlock placeholder (until you unlock it) |
| `.wvf` (encrypted video, unlocked) | A representative decoded frame + play overlay (needs `opencv-python`), else a film/lock icon |
| `.wvf` (encrypted video, locked) | A padlock film placeholder (until a known password unlocks it) |
| Images | Normal thumbnail; GIFs get a "stacked frames" look |
| Video | First frame + play overlay (needs `opencv-python`), else a play icon |
| Audio | A note icon |

### Selection & the status bar

- **Click** selects one; **Ctrl+click** toggles; **Shift+click** selects a range;
  **Ctrl+A** selects every file in the current view (respecting the filter).
- **Drag on empty space** draws a **rubber-band marquee**; **Ctrl+drag** adds to
  the current selection.
- **Arrow keys** move the selection through the grid (**Shift** extends the range,
  **Home/End** jump to first/last); **Enter** opens the selected item.
- The status bar at the bottom shows:
  - **one file** → `name · W × H px · size`
  - **multiple files** → `N files selected` (or a folders/files breakdown)
  - **nothing / a folder** → folder & file counts
  - **with a filter** → `Showing X of Y`

### Right-click menu

| Item | Action |
|------|--------|
| *(info line)* | Resolution of one file, or a selection summary |
| Open | Folders open in Explorer; images/`.wif` open in a **Weird Viewer** window; `.wvf` plays; others in their default app |
| Open in viewer | Force the selected images/`.wif` into **Weird Viewer** windows (skips folders/other files) |
| Play (video) | Decrypt & play the selected `.wvf` file(s) — streamed via `ffplay`, no plaintext on disk |
| Seal to WVF 🔒… | Encrypt the selected plain video(s) into `.wvf` container(s) (uses/asks a password) |
| Decrypt to video… | Restore the selected `.wvf` file(s) back to their original video |
| Export to image… | Decode & save the selected files as PNG (one → Save dialog; many → pick a folder) |
| Convert to WIF v3 | Encode the selected images to a filtered, compressed `.wif` (v3; needs numpy) |
| Convert to WIF v2 — encrypted 🔒 | Encode to an encrypted (and filtered) `.wif` (uses/asks a password) |
| Encryption ▸ | **Encrypt to v2…**, **Change password…**, **Remove encryption (→ v3)…** for existing `.wif` files |
| Show in Explorer | Open File Explorer with the item highlighted |
| Copy path | Copy the full path(s) of the selection to the clipboard |
| Append to temp.txt | Append `name path` of the selection to `temp.txt` next to the scripts |
| Edit tags… | Add or edit comma-separated tags for the selected file(s) (hidden for pure folder selections) |
| Delete | Securely delete (zeros + unlink); hidden if any folder is selected |

### The lightbox

Double-click an image (or a preview thumbnail) to open a full-window overlay:

- **← / →** or **mouse wheel** — previous / next.
- Animated **GIFs play** in the overlay.
- **Esc**, **Alt+Left**, the **✕**, or clicking the dark background — close.
- Encrypted `.wif` prompt for a password if needed (locked ones show a 🔒 panel).

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| Arrow keys | Move the selection (in the grid); navigate the lightbox when it's open |
| `Shift` + arrows | Extend the selection |
| `Home` / `End` | Select the first / last item |
| `Enter` | Open the selected item (enter folder / open file) |
| `Backspace` / `Alt+Left` | Up one folder (Alt+Left also closes the lightbox) |
| `Ctrl+A` | Select all files in the view |
| `Ctrl+F` | Focus the filename filter |
| `Del` | Securely delete the selected file(s) |
| `Esc` | Close the lightbox |
| mouse wheel | Scroll the grid (or flip lightbox images when it's open) |

---

## WVF — encrypted video

**WVF** (`.wvf`) is the video sibling of WIF. Where WIF stores raw image pixels,
WVF is a thin **encrypted container** that seals an *already-compressed* video
file (H.264, H.265, AV1, …) exactly as-is. The bytes are sealed with
**AES-256-GCM** under a key derived from your password with **scrypt**, written in
chunks as a **STREAM** AEAD so arbitrarily large files encrypt and decrypt without
loading the whole thing into memory.

Nothing but the rough file size leaks without the password — not the frames, not
the original filename, not even the container type.

### In the browser (Weird Traveler)

- **Double-click a `.wvf`** (or right-click → **Play**) — it's decrypted and
  played. If `ffplay` is available the decrypted stream is piped straight into it,
  so **no plaintext is ever written to disk**; otherwise it falls back to a
  scrubbed temp file in your default player.
- **Right-click a plain video → Seal to WVF 🔒…** — encrypt it into a `.wvf`.
- **Right-click a `.wvf` → Decrypt to video…** — restore the original video.
- `.wvf` files get a **decoded cover frame** thumbnail once unlocked (needs
  `opencv-python`), or a padlock-film placeholder while locked. They share the
  browser's **session passwords**, so unlocking one `.wvf` (or `.wif`) tries that
  password on the rest.

### From the command line

The `wvf` package is also a standalone CLI:

```bash
python -m wvf.converter wrap   clip.mp4                 # -> clip.wvf (prompts for a password)
python -m wvf.converter wrap   clip.mp4 -o sealed.wvf   # choose the output name
python -m wvf.converter wrap   big.mov --transcode h264 --crf 24   # shrink first (needs imageio-ffmpeg)
python -m wvf.converter unwrap clip.wvf -o out.mp4      # decrypt back to a video
python -m wvf.converter info   clip.wvf                 # header info — no password needed
python -m wvf.player           clip.wvf                 # decrypt & play (ffplay streaming)
```

### As a library

```python
from wvf import wrap_file, unwrap_file, peek, WrongPassword

wrap_file("clip.mp4", "clip.wvf", "hunter2")
meta = unwrap_file("clip.wvf", "out.mp4", password="hunter2")
info = peek("clip.wvf")          # version / sizes — no password required
```

---

## Tags

The browser supports tagging any file with free-form labels. Tags are stored in
`wif_tags.json` next to the scripts and — if a session password is available —
**encrypted with AES-256-GCM + scrypt**, the same algorithm used by WIF v2 image
files. Without the password, the database is unreadable.

### Adding and editing tags

Right-click one or more files and choose **Edit tags…**. A dialog appears
pre-filled with the file's existing tags (or, for a multi-file selection, the tags
common to all selected files). Type any comma-separated labels and press OK.

Tags are stored **lower-case** and **deduplicated** automatically.
Removing all tags from a file (clearing the field) deletes its entry.

Tagged files show a small `🏷 tag1, tag2` line below their thumbnail and in the
**status bar** when the file is selected. The badge can be hidden via
**☰ → Show tags in cells** without losing the tags themselves.

### Local tag filter (toolbar)

The **Tags:** entry in the toolbar filters the current folder in real time —
only files whose tags contain the typed text are shown. It works alongside the
**Filter:** filename filter: both must match. Press `Esc` to clear it.

### Global tag search (☰ → 🏷 Tag search…)

Opens a dialog listing every tagged file known to the database:

| Button | Action |
|--------|--------|
| *(search box)* | Type to narrow the list to files whose tags match |
| **Go to folder** (or double-click) | Close the dialog and navigate the browser to the file's parent folder |
| **Remove tags** | Strip all tags from the selected entry |

Files that have been moved or deleted since they were tagged are shown with a
`(file not found)` note and can be cleaned up with **Remove tags**.

### Encrypted tag database

Tags are stored **encrypted** whenever a session password is in memory:

- **First launch / no file yet** — the database is created in plain JSON. As
  soon as you add a password via **☰ → 🔑 Unlock…**, all future saves are
  encrypted with that password.
- **Subsequent launches** — if `wif_tags.json` is encrypted, the browser tries
  every known session password silently. If none works, you are prompted each
  time you interact with tags (click **Tags:**, open **Tag search**, or use
  **Edit tags…**) until you either supply the correct password or the session ends.
- **Wrong or cancelled password** — the prompt re-appears on the next tag
  interaction. The encrypted file is never overwritten until you successfully
  unlock it, so your data is always safe.
- **Same password as your images** — if you unlock a `.wif` file before
  touching the tag filter, the browser automatically tries that password on the
  tag database too, so you usually won't be asked twice.

Tags are cleaned up automatically when files are **securely deleted** from the browser.

---

## Encryption & passwords

Encrypted files are **WIF v2** — the image (dimensions *and* pixels) is sealed
with AES-256-GCM using a key derived from your password with scrypt. Nothing but
the rough file size is visible without the password. See
[`WIF_FORMAT.md`](WIF_FORMAT.md) for the cryptographic details.

**The session-password model:**

- Encrypted files show a padlock until you provide a password.
- Use **☰ → 🔑 Unlock…** to add a password. The browser keeps a list of the
  passwords it has learned and tries them all against each encrypted file.
  Password list is deleted when the program closes.
- When you open an encrypted file that none of the known passwords fit, you're
  prompted; a correct entry is remembered for the rest of the session.
- The viewer behaves the same, and **inherits the browser's passwords** when the
  browser launches it (handed over through an environment variable, never the
  command line).

---

## Settings file

When **Remember settings** is enabled in the browser, choices are saved to
`wif_settings.json` next to the scripts and restored on the next launch:

```json
{
  "remember": true,
  "theme": "Dark",
  "sort": "Name",
  "descending": false,
  "size_mode": "small",
  "filter": "Folders + Media",
  "scrub": false
}
```
---

## Using the library directly

All WIF encode/decode logic is in `wif_format.py` (spatial filtering for v3 lives
in `wif_filter.py`); all WVF container logic is in the `wvf` package.

```python
import wif_format
from PIL import Image

# image -> .wif bytes  (version 1/3, optional compression / filtering / encryption)
data = wif_format.encode(Image.open("photo.png"), filtered=True)
open("photo.wif", "wb").write(data)

# .wif bytes -> (PIL.Image, meta)
img, meta = wif_format.decode(open("photo.wif", "rb").read())

wif_format.peek(open("secret.wif", "rb").read())        # version/size info, no decode
```

```python
from wvf import wrap_file, unwrap_file, peek            # see "WVF — encrypted video"
```

These libraries are designed to be imported and reused, not just driven from the
GUIs — full signatures are in the module docstrings.

---

## Building a single `.exe`

`wif_main.py` is a small entry point that dispatches to the browser or the viewer:

```bash
Weird Traveler.exe                  # Weird Traveler browser (current folder)
Weird Traveler.exe  C:\Photos       # browser, starting in that folder
Weird Traveler.exe  --viewer a.wif  # Weird Viewer for that file
```

`build.py` wraps it all into one Windows executable with **PyInstaller**:

```bash
pip install pyinstaller
python build.py                     # -> dist/Weird Traveler.exe
```

The build bundles the browser, the viewer, the WIF/WVF libraries and the app icon
into a single file. Optional dependencies that are installed at build time
(`opencv-python`, `numpy`, `pillow-avif-plugin`, …) are baked in; `ffplay` is
**not** bundled — for `.wvf` playback, keep an `ffplay` binary on `PATH` or beside
the `.exe`.

---

## Security notes & limitations

- **Passwords live only in RAM.** They're never written to disk by these tools.
- **No metadata leaks** — WIF stores only raw pixels, so EXIF/GPS and other
  metadata in the source image are dropped (a privacy plus over JPEG). WVF seals
  the video bytes whole and reveals only the rough file size — not the frames, the
  original filename, or the container type.
- **No plaintext on disk for `.wvf` playback** when `ffplay` is available: the
  decrypted stream is piped straight into the player. The temp-file fallback is
  scrubbed when the program exits.
- **Format limits for WIF:** dimensions are 16-bit, so max **65 535 × 65 535 px**; pixels
  are 8-bit per channel (no 16-bit/HDR); channels are 3 (RGB) or 4 (RGBA, only
  when the image is genuinely transparent).
- **WVF does not re-encode by default** — it seals your existing video losslessly.
  The crypto is only as strong as your password; there's no recovery if you lose
  it (that's the point).
