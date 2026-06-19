"""
wvf.converter — wrap a video into an encrypted .wvf, or unwrap it back.

    python -m wvf.converter wrap clip.mp4                 -> clip.wvf  (prompts for a password)
    python -m wvf.converter wrap clip.mp4 -o secret.wvf --password pw
    python -m wvf.converter wrap big.mov --transcode h264 --crf 24    (shrink first; needs imageio-ffmpeg)
    python -m wvf.converter unwrap clip.wvf               -> restores the original file
    python -m wvf.converter unwrap clip.wvf -o out.mp4 --password pw
    python -m wvf.converter info clip.wvf

(The top-level ``wvf_converter.py`` launcher runs this same code, so the original
``python wvf_converter.py …`` commands keep working.)

WVF is an *encrypted container*: it doesn't recompress your video, it seals the
already-compressed bytes with AES-256-GCM.  Use --transcode only when you want a
smaller / normalised codec before sealing.
"""
import argparse
import getpass
import os
import sys
import tempfile
from pathlib import Path

from . import core as wvf_format


def _ask_new_password():
    pw = getpass.getpass("Set password: ")
    if pw != getpass.getpass("Confirm password: "):
        sys.exit("Passwords did not match.")
    if not pw:
        sys.exit("An empty password is not allowed.")
    return pw


def _mb(n):
    return f"{n / 1e6:.1f} MB"


def cmd_wrap(a):
    src = Path(a.input)
    if not src.is_file():
        sys.exit(f"Not found: {src}")
    pw = a.password or _ask_new_password()
    dst = Path(a.output) if a.output else src.with_suffix(".wvf")

    to_seal = src
    container = src.suffix.lstrip(".").lower() or "bin"
    restore_name = src.name
    tmp = None
    if a.transcode:
        from . import codec as wvf_codec
        if not wvf_codec.available():
            sys.exit("--transcode needs imageio-ffmpeg:  pip install imageio-ffmpeg")
        fd, tmp = tempfile.mkstemp(suffix=".mp4"); os.close(fd)
        print(f"Transcoding to {a.transcode} (crf {a.crf}) …")
        wvf_codec.transcode(src, tmp, codec=a.transcode, crf=a.crf)
        to_seal, container, restore_name = Path(tmp), "mp4", src.stem + ".mp4"

    try:
        size = to_seal.stat().st_size
        def prog(done):
            print(f"\rEncrypting {_mb(done)} / {_mb(size)}", end="", flush=True)
        wvf_format.wrap_file(to_seal, dst, pw, name=restore_name,
                             container=container, progress=prog)
        print()
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    print(f"Wrapped: {src.name} -> {dst}  ({_mb(dst.stat().st_size)} encrypted)")


def cmd_unwrap(a):
    src = Path(a.input)
    if not src.is_file():
        sys.exit(f"Not found: {src}")
    pw = a.password or getpass.getpass("Password: ")
    fd, tmp = tempfile.mkstemp(suffix=".part", dir=str(src.parent)); os.close(fd)
    try:
        meta = wvf_format.unwrap_file(src, tmp, password=pw)
    except wvf_format.WrongPassword:
        os.unlink(tmp); sys.exit("Wrong password.")
    except Exception:
        os.unlink(tmp); raise

    if a.output:
        dst = Path(a.output)
    else:
        # meta came out of the ciphertext (untrusted); never let it pick a path.
        dst = src.with_name(wvf_format.safe_output_name(meta, src.stem))
    if dst.exists() and dst.resolve() != src.resolve():
        os.unlink(tmp)
        sys.exit(f"Refusing to overwrite {dst} (use -o to choose another path).")
    os.replace(tmp, dst)
    print(f"Unwrapped: {src.name} -> {dst}")


def cmd_info(a):
    info = wvf_format.peek(a.input)
    flag = "" if info["supported"] else "  (UNSUPPORTED VERSION)"
    print(f"{a.input}: WVF v{info['version']}  encrypted={info['encrypted']}  "
          f"{_mb(info['file_size'])}{flag}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wrap/unwrap encrypted .wvf video containers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("wrap", help="encrypt a video into a .wvf")
    w.add_argument("input")
    w.add_argument("-o", "--output")
    w.add_argument("--password")
    w.add_argument("--transcode", choices=["h264", "h265", "av1"],
                   help="re-encode with ffmpeg before sealing (smaller files)")
    w.add_argument("--crf", type=int, default=23, help="transcode quality, lower = better (default 23)")
    w.set_defaults(func=cmd_wrap)

    u = sub.add_parser("unwrap", help="decrypt a .wvf back to the original video")
    u.add_argument("input")
    u.add_argument("-o", "--output")
    u.add_argument("--password")
    u.set_defaults(func=cmd_unwrap)

    i = sub.add_parser("info", help="show header info (no password needed)")
    i.add_argument("input")
    i.set_defaults(func=cmd_info)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
