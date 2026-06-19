"""
wvf.player — play an encrypted .wvf video.

    python -m wvf.player clip.wvf
    python -m wvf.player clip.wvf --password pw

(The top-level ``wvf_player.py`` launcher runs this same code.)

Playback path:

* If an ``ffplay`` binary is found (next to the script/exe, or on PATH), the
  decrypted video is streamed straight into ``ffplay`` over stdin — **nothing
  is written to disk**.  This is the preferred path.
* Otherwise (or if ffplay can't demux the piped stream — e.g. a non-faststart
  MP4 whose moov atom needs a seek), it falls back to the original method:
  decrypt to a temp file and open it in your OS default player.

Security note: on the *fallback* path the decrypted video exists as an ordinary
file in your temp folder while it plays (your OS player needs a real file to
open).  That file is overwritten with zeros before deletion to reduce recovery
risk — best effort only; on SSDs / copy-on-write filesystems wear-levelling can
leave the original blocks behind.  Cleanup also runs on Ctrl+C and on normal
process exit, not only when you press Enter.  The ffplay streaming path avoids
this entirely.
"""
import argparse
import atexit
import getpass
import os
import shutil
import signal
import subprocess
import sys
import tempfile

from . import core as wvf_format

# Absolute path of the active temp file; shared with atexit / signal handlers.
_cleanup_target: "str | None" = None


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def _zero_overwrite(path: str) -> None:
    """Best-effort zero-fill before unlink to hamper file recovery."""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as fh:
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def _force_delete(path: str) -> None:
    """Zero-fill then unlink, swallowing all errors (used in atexit/signal)."""
    _zero_overwrite(path)
    try:
        os.unlink(path)
    except OSError:
        pass


def _cleanup() -> None:
    """Called by atexit and signal handlers — silently removes the temp file."""
    global _cleanup_target
    target = _cleanup_target
    _cleanup_target = None
    if target and os.path.exists(target):
        _force_delete(target)


def _signal_handler(sig: int, frame: object) -> None:  # noqa: ANN001
    _cleanup()
    sys.exit(128 + sig)


# Register once at import time so the handler survives any code path.
atexit.register(_cleanup)
for _sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(_sig, _signal_handler)
    except (OSError, ValueError):
        pass   # not available in all environments (e.g. non-main thread)


# ---------------------------------------------------------------------------
# ffplay discovery + streaming playback (preferred: no plaintext on disk)
# ---------------------------------------------------------------------------

def _find_ffplay() -> "str | None":
    """Locate an `ffplay` binary, preferring the launch folder, then PATH.

    "Launch folder" = next to the running script/exe (and, when frozen, the
    PyInstaller bundle dir) plus the current working directory.  Returns the
    full path, or None if ffplay isn't anywhere we can see."""
    exe = "ffplay.exe" if os.name == "nt" else "ffplay"

    launch_dirs = []
    if getattr(sys, "frozen", False):                # PyInstaller single-exe
        launch_dirs.append(os.path.dirname(sys.executable))
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            launch_dirs.append(meipass)
    if sys.argv and sys.argv[0]:
        launch_dirs.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    launch_dirs.append(os.getcwd())

    for d in launch_dirs:
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            return cand
    return shutil.which("ffplay")


def _close(stream) -> None:
    try:
        stream.close()
    except OSError:
        pass


def _play_streamed(ffplay: str, path: str, password: str):
    """Decrypt straight into ffplay's stdin — the plaintext never hits disk.

    Returns ('ok', meta) if ffplay played the stream, or ('unplayable', meta)
    if ffplay rejected it (e.g. a non-faststart MP4 whose moov atom needs a
    seek a pipe can't do).  Raises WrongPassword for a bad password."""
    cmd = [ffplay, "-hide_banner", "-loglevel", "error", "-autoexit",
           "-window_title", "WVF (encrypted)", "pipe:0"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    meta = None
    try:
        try:
            meta = wvf_format.unwrap_stream(path, proc.stdin, password=password)
        except OSError:
            # ffplay closed the read end: it either couldn't demux the stream or
            # the user quit early.  The return code disambiguates (0 = quit).
            pass
    except wvf_format.WrongPassword:
        _close(proc.stdin)
        proc.wait()
        raise
    finally:
        _close(proc.stdin)
    rc = proc.wait()
    return ("ok" if rc == 0 else "unplayable"), meta


# ---------------------------------------------------------------------------
# Temp-file playback (fallback: OS default player, plaintext on disk)
# ---------------------------------------------------------------------------

def _open(path: str) -> None:
    """Open *path* in the OS default player (non-blocking)."""
    if sys.platform.startswith("win"):
        os.startfile(path)                          # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _play_via_tempfile(path: str, password: str) -> int:
    """The original method: decrypt to a temp file and open it in the system
    player, scrubbing the file afterwards.  Used when ffplay isn't available
    (or couldn't play a piped stream)."""
    global _cleanup_target

    fd, tmp = tempfile.mkstemp(prefix="wvf_play_", dir=os.getcwd())
    os.close(fd)

    try:
        meta = wvf_format.unwrap_file(path, tmp, password=password)
    except wvf_format.WrongPassword:
        os.unlink(tmp)
        print("Wrong password.")
        return 1
    except Exception:
        os.unlink(tmp)
        raise

    # `container` comes out of the ciphertext (untrusted) — sanitise it to a bare
    # extension so it can only ever be appended, never used to escape the temp dir.
    container = wvf_format.sanitize_container(meta.get("container"))
    target = tmp + "." + container
    os.replace(tmp, target)
    _cleanup_target = target                         # arm atexit / signal handler

    print(f"Playing: {meta.get('name', '?')}")
    print(f"  (temp file: {target})")
    _open(target)

    # Retry loop: on Windows a player can hold the file open.  Warn and let the
    # user close it rather than silently leaving the decrypted file behind.
    while True:
        try:
            input("Press Enter when done to delete the decrypted file… ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        _zero_overwrite(target)
        try:
            os.unlink(target)
            _cleanup_target = None
            print("Decrypted file deleted.")
            break
        except PermissionError:
            print("  ⚠  Your player still has the file open.")
            print("     Close the player, then press Enter again.")
        except FileNotFoundError:
            _cleanup_target = None
            break                                    # already gone — fine
        except OSError as exc:
            print(f"  Warning: could not delete temp file: {exc}")
            break

    return 0


# ---------------------------------------------------------------------------
# Entry point: prefer ffplay streaming, fall back to a temp file
# ---------------------------------------------------------------------------

def play(path: str, password: "str | None" = None) -> int:
    wvf_format.peek(path)                            # fail fast if not a .wvf
    if password is None:
        password = getpass.getpass("Password: ")

    ffplay = _find_ffplay()
    if ffplay:
        try:
            status, meta = _play_streamed(ffplay, path, password)
        except wvf_format.WrongPassword:
            print("Wrong password.")
            return 1
        except OSError as exc:                        # ffplay wouldn't even start
            print(f"  (couldn't start ffplay: {exc}; using the default player)")
            status = None
        if status == "ok":
            name = (meta or {}).get("name", "?")
            print(f"Played: {name}  (streamed via ffplay — no decrypted file on disk)")
            return 0
        if status == "unplayable":
            print("ffplay couldn't play the piped stream; "
                  "falling back to a temp file…")

    return _play_via_tempfile(path, password)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Play an encrypted .wvf video")
    ap.add_argument("file")
    ap.add_argument("--password")
    args = ap.parse_args(argv)
    return play(args.file, args.password)


if __name__ == "__main__":
    raise SystemExit(main())
