"""
Converts images (PNG, JPG, BMP, etc.) to the .wif format.

Usage:
    python wif_converter.py image.png
    python wif_converter.py image.png -o out.wif
    python wif_converter.py image1.png image2.jpg
    python wif_converter.py image1.png image2.jpg -d output/folder
    python wif_converter.py image.png --no-compress
    python wif_converter.py photos/ -r                  (walk subfolders)
    python wif_converter.py photos/ -r -d converted/    (mirror tree into converted/)
    python wif_converter.py secret.png --encrypt        (write encrypted WIF v2)
"""

import argparse
import struct
from pathlib import Path
from PIL import Image

try:
    import pillow_avif   # registers the AVIF codec so .avif inputs can be read
except ImportError:
    pass

import wif_format

# Extensions picked up when walking folders with --recursive
CONVERTIBLE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif",
                    ".tiff", ".tif", ".webp", ".ico", ".avif"}


def convert(src: Path, dst: Path, compress: bool, version: int = 1, password=None):
    img  = Image.open(src)
    data = wif_format.encode(img, version=version, compress=compress, password=password)
    dst.write_bytes(data)
    if version == 2:
        print(f"Converted: {src} -> {dst}  ({len(data):,} bytes, WIF v2 encrypted)")
    else:
        # Read channels back from the written header for accurate reporting
        channels = struct.unpack(">BHHBB", data[4:11])[3]
        mode_str = {1: "L", 3: "RGB", 4: "RGBA"}.get(channels, f"{channels}ch")
        raw_size = img.size[0] * img.size[1] * channels
        saved    = 100 * (1 - len(data) / raw_size)
        print(f"Converted: {src} -> {dst}  ({len(data):,} bytes, {saved:.1f}% smaller than raw, {mode_str})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert images to .wif format")
    parser.add_argument("images", nargs="+", type=Path, help="Image file(s) to convert")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="Output path (only valid when converting a single file)")
    parser.add_argument("-d", "--output-dir", type=Path, default=None,
                        help="Output folder (for batch conversions; created if it doesn't exist)")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Descend into subfolders of any folder arguments and convert every image found")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip images that already have an up-to-date .wif at the destination")
    parser.add_argument("--encrypt", "--v2", action="store_true", dest="encrypt",
                        help="Write encrypted WIF v2 files (you will be prompted for a password)")
    parser.add_argument("--password", default=None,
                        help="Password to use with --encrypt (otherwise prompted securely)")
    parser.add_argument("--no-compress", action="store_false", dest="compress",
                        help="Disable compression (compression is on by default)")
    parser.set_defaults(compress=True)
    args = parser.parse_args()

    if args.output and args.output_dir:
        parser.error("-o/--output and -d/--output-dir cannot be used together")

    # Build the work list as (source_file, base_dir) pairs.  base_dir lets -d
    # mirror a folder's structure into the output directory.
    jobs = []
    for item in args.images:
        if item.is_dir():
            if args.recursive:
                for f in sorted(item.rglob("*")):
                    if f.is_file() and f.suffix.lower() in CONVERTIBLE_EXTS:
                        jobs.append((f, item))
            else:
                print(f"Skipping folder (use -r/--recursive to descend): {item}")
        elif item.exists():
            jobs.append((item, item.parent))
        else:
            print(f"Skipping (not found): {item}")

    if args.output and len(jobs) > 1:
        parser.error("-o/--output can only be used when there is exactly one input file")

    if not jobs:
        print("Nothing to convert.")
        raise SystemExit(0)

    # Resolve the encryption password once, up front
    version, password = 1, None
    if args.encrypt:
        version  = 2
        password = args.password
        if not password:
            import getpass
            password = getpass.getpass("Encryption password: ")
            if password != getpass.getpass("Confirm password: "):
                parser.error("passwords did not match")
        if not password:
            parser.error("an empty password is not allowed for encryption")

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    n_done = n_skip = n_err = 0
    for src, base in jobs:
        if args.output:
            dst = args.output
        elif args.output_dir:
            dst = args.output_dir / src.relative_to(base).with_suffix(".wif")
            dst.parent.mkdir(parents=True, exist_ok=True)
        else:
            dst = src.with_suffix(".wif")

        if (args.skip_existing and dst.exists()
                and dst.stat().st_mtime >= src.stat().st_mtime):
            print(f"Skipping (up to date): {dst}")
            n_skip += 1
            continue

        try:
            convert(src, dst, args.compress, version, password)
            n_done += 1
        except Exception as exc:
            print(f"Error converting {src}: {exc}")
            n_err += 1

    if len(jobs) > 1:
        print(f"\nDone. {n_done} converted, {n_skip} skipped, {n_err} error(s).")
