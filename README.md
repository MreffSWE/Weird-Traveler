# Weird Traveler

A big part of this project was written with AI.

**A self-contained image & video toolkit built around two custom file formats — with a private-by-design encryption layer baked in.**

Browse, view and convert images as `.wif`, and seal videos into encrypted `.wvf`
containers — all from one tkinter app (or a single Windows `.exe`), no cloud, no
account, no telemetry.

> Pure Python + tkinter + Pillow. Optional extras (cryptography, OpenCV, numpy,
> FFmpeg) light up encryption, video thumbnails, smaller files and `.wvf`
> playback when present, and stay quietly out of the way when they aren't.

<img width="689" height="782" alt="Untitled-1" src="https://github.com/user-attachments/assets/2a89bc1f-876a-4152-9cca-14d3e9b64811" />

---

## What it does

| | |
|---|---|
| 🖼️ **Browse** | A fast, **virtualized** thumbnail file browser — folders with thousands of files scroll smoothly. Rich folder previews, themes (Light / Dark / Red), keyboard navigation, marquee selection, free-form **tags** with global search. |
| 🔍 **View** | A full image viewer: zoom, rotate, flip, animation playback (GIF/WebP/APNG), live non-destructive adjustments (brightness, contrast, gamma, sharpen, auto-colour), and export back to any standard format. |
| 🔄 **Convert** | A batch CLI that turns PNG/JPG/BMP/GIF/TIFF/WebP/ICO/AVIF into `.wif` — recursively, mirroring folder trees, with optional compression, spatial filtering and encryption. |
| 🔐 **Encrypt** | `.wif` images and `.wvf` videos can be sealed with **AES-256-GCM** under a scrypt-derived key. Passwords live only in RAM; nothing but the rough file size leaks. |
| 🎞️ **Video** | `.wvf` wraps an already-compressed video (H.264/H.265/AV1) in an encrypted container. The browser plays it by **streaming the decrypted bytes straight into `ffplay` — no plaintext ever hits disk.** |

---

## Two formats, one idea: store only what's needed, leak nothing else

**WIF (`.wif`)** — a deliberately simple image format. It stores *raw pixels*
(optionally zlib-compressed, optionally PNG-style spatially filtered for smaller
files), which means EXIF/GPS and every other source-image metadata field is
simply dropped. Encrypted **v2** files hide the dimensions *and* the pixels behind
AES-256-GCM.

**WVF (`.wvf`)** — the video sibling. Rather than re-encoding, it *seals* your
existing compressed video whole, in chunks, as a **STREAM AEAD** — so files of any
size encrypt and decrypt without loading into memory, and the container reveals
neither the frames, the original filename, nor even the codec.

Both share the same session-password model: unlock one file and the app silently
tries that password on the rest.

---

## Components

| Piece | What it is |
|-------|-----------|
| **Weird Traveler** (`wif_browser.py`) | The GUI thumbnail browser — also plays, seals and decrypts `.wvf` video |
| **Weird Viewer** (`wif_viewer.py`) | The GUI image viewer / editor / exporter |
| `wif_converter.py` | Command-line image → `.wif` converter (batch, recursive, encrypt, filter) |
| `wif_format.py` + `wif_filter.py` | WIF core library + spatial filtering |
| `wvf/` | Weird Video Format package — library, CLI (`python -m wvf.converter`), and player |
| `wif_main.py` + `build.py` | Single-exe entry point + PyInstaller build → **`Weird Traveler.exe`** |

---

## Quick start

```bash
pip install Pillow cryptography          # core
pip install opencv-python numpy          # optional: video thumbnails + smaller WIF v3

python wif_browser.py                     # launch the browser
python wif_converter.py photos/ -r --filter   # batch-convert a tree to filtered .wif
python -m wvf.converter wrap clip.mp4     # seal a video into clip.wvf
```

Or grab the prebuilt **`Weird Traveler.exe`** and just run it — the browser and
viewer are bundled into one file. (`ffplay` from [FFmpeg](https://ffmpeg.org/) is
needed for `.wvf` playback.)

See the [**README**](README.md) for the full feature reference and
[**WIF_FORMAT.md**](WIF_FORMAT.md) for the byte-level format spec.

---

## Highlights for the curious

- **Virtualized grid** — only on-screen rows are built as widgets, so huge folders
  stay responsive.
- **No-plaintext-on-disk video playback** — decrypted `.wvf` is piped into
  `ffplay`; the temp-file fallback is scrubbed on exit.
- **Encrypted tag database** — your tags are stored with the same AES-256-GCM +
  scrypt as your images once a session password is set.
- **Graceful degradation** — every heavy dependency is optional; the app detects
  what's installed and hides the features it can't run.
- **Ships from source or as one `.exe`** — same code, two ways to run it.

---

## Status & tech

- **Language:** Python 3 (tkinter GUI, Pillow imaging)
- **Crypto:** `cryptography` — AES-256-GCM, scrypt KDF, STREAM AEAD for video
- **Optional:** OpenCV (thumbnails), numpy (WIF v3 filtering), FFmpeg/`ffplay`
  (`.wvf` playback), imageio-ffmpeg (transcode-on-seal), pillow-avif-plugin (AVIF)
- **Platform:** Windows-first; the Python scripts are cross-platform
- A personal project — built for fun and for keeping a private, metadata-free
  media stash.
