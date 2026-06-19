"""
wvf.codec — optional ffmpeg helpers via imageio-ffmpeg's bundled static
binary.  Only `--transcode` needs this; plain wrap / unwrap / play do not.

    pip install imageio-ffmpeg

imageio-ffmpeg ships a self-contained ffmpeg (no system install, no PATH entry),
which is what keeps the single-exe goal alive: PyInstaller bundles that one
binary alongside the code.
"""
import subprocess

try:
    import imageio_ffmpeg
    _FFMPEG = True
except ImportError:
    _FFMPEG = False


def available() -> bool:
    return _FFMPEG


def ffmpeg_exe() -> str:
    if not _FFMPEG:
        raise RuntimeError("Install 'imageio-ffmpeg' to transcode (pip install imageio-ffmpeg).")
    return imageio_ffmpeg.get_ffmpeg_exe()


# {crf} is filled in per call.  libx264 ships in every imageio-ffmpeg build;
# libx265 usually does; AV1 (libsvtav1) depends on the build and errors clearly
# if that encoder isn't present.
_PRESETS = {
    "h264": ["-c:v", "libx264", "-preset", "medium", "-crf", "{crf}"],
    "h265": ["-c:v", "libx265", "-preset", "medium", "-crf", "{crf}"],
    "av1":  ["-c:v", "libsvtav1", "-crf", "{crf}"],
}


def transcode(src, dst, codec="h264", crf=23, audio="aac", extra=None):
    """Re-encode `src` to `dst` with the chosen video codec (audio -> AAC)."""
    if codec not in _PRESETS:
        raise ValueError(f"unknown codec {codec!r}; choose from {list(_PRESETS)}")
    vargs = [a.format(crf=crf) for a in _PRESETS[codec]]
    cmd = [ffmpeg_exe(), "-y", "-i", str(src), *vargs,
           "-c:a", audio, "-b:a", "128k", *(extra or []), str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg transcode failed:\n" + proc.stderr[-2000:])
    return dst
