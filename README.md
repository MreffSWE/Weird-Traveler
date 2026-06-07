# Weird Traveler

An image preview program with a custom file format '.wif'.

The program is three Python scripts plus a shared core library:

| Program | Name | What it is |
|---------|------|-----------|
| `wif_converter.py` | — | Command-line converter: PNG/JPG/… → `.wif` (batch, recursive, encrypt) |
| `wif_viewer.py` | **Weird Viewer** | A GUI image viewer (zoom, rotate, flip, animation playback, image adjustments, delete, export) |
| `wif_browser.py` | **Weird Traveler** | A GUI thumbnail file browser (virtualized grid, previews, themes, keyboard nav, convert/encrypt) |
| `wif_format.py` | — | Shared library — all encode/decode/encryption logic |


## Table of contents

- [Requirements & installation](#requirements--installation)
- [Project files](#project-files)
- [The converter (`wif_converter.py`)](#the-converter-wif_converterpy)
- [The viewer — Weird Viewer (`wif_viewer.py`)](#the-viewer--weird-viewer-wif_viewerpy)
- [The browser — Weird Traveler (`wif_browser.py`)](#the-browser--weird-traveler-wif_browserpy)
- [Encryption & passwords](#encryption--passwords)
- [Settings file](#settings-file)
- [Using the library directly](#using-the-library-directly)
- [Security notes & limitations](#security-notes--limitations)

---

## Requirements & installation

Install the dependencies:

```bash
pip install Pillow cryptography
# optional extras:
pip install pillow-avif-plugin   # read .avif input images
pip install opencv-python        # real video thumbnails in the browser
```

| Package | Needed for |
|---------|-----------|
| **Pillow** | Everything (image I/O) — required |
| **cryptography** | WIF **v2** encryption/decryption — required to make or open encrypted files |
| **pillow-avif-plugin** | Reading `.avif` source images — optional |
| **opencv-python** | Thumbnailing video files in the browser — optional (falls back to a play icon) |

If `cryptography` is missing, plain (v0/v1) files still work; only encryption is
disabled.

---

## Project files

```
wif_format.py        core library (import this, don't run it)
wif_converter.py     CLI converter
wif_viewer.py        Weird Viewer (GUI)
wif_browser.py       Weird Traveler (GUI)
wif_icon.ico         app icon used by both GUIs
wif_settings.json    browser settings (auto-created when "Remember settings" is on)
temp.txt             scratch list written by the browser's "Append to temp.txt"
WIF_FORMAT.md        byte-level format specification
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
python wif_converter.py secret.png --encrypt       # write an encrypted v2 file
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
| Folders + … | Which files to show: **WIF**, **Media**, or **All files** |
| Filter: | Live filename filter (`Ctrl+F` focuses it, `Esc` clears it) |
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
| Open | Folders open in Explorer; images/`.wif` open in a **Weird Viewer** window; others in their default app |
| Open in viewer | Force the selected images/`.wif` into **Weird Viewer** windows (skips folders/other files) |
| Export to image… | Decode & save the selected files as PNG (one → Save dialog; many → pick a folder) |
| Convert to WIF v1 | Encode the selected images to plain `.wif` |
| Convert to WIF v2 — encrypted 🔒 | Encode to encrypted `.wif` (uses/asks a password) |
| Encryption ▸ | **Encrypt to v2…**, **Change password…**, **Remove encryption (→ v1)…** for existing `.wif` files |
| Show in Explorer | Open File Explorer with the item highlighted |
| Copy path | Copy the full path(s) of the selection to the clipboard |
| Append to temp.txt | Append `name path` of the selection to `temp.txt` next to the scripts |
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

All encode/decode logic is in `wif_format.py`.

---

## Security notes & limitations

- **Passwords live only in RAM.** They're never written to disk by these tools.
- **No metadata leaks** — WIF stores only raw pixels, so EXIF/GPS and other
  metadata in the source image are dropped (a privacy plus over JPEG).
- **Format limits for WIF:** dimensions are 16-bit, so max **65 535 × 65 535 px**; pixels
  are 8-bit per channel (no 16-bit/HDR); channels are 3 (RGB) or 4 (RGBA, only
  when the image is genuinely transparent).
