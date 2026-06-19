"""WVF — Weird Video Format: an encrypted container around an ordinary video file.

The video is sealed with AES-256-GCM under a scrypt-derived key (chunked STREAM
AEAD), so nothing but the rough size leaks without the password — not the pixels,
not even the original file name or container type.

Library use::

    from wvf import wrap_file, unwrap_file, peek, WrongPassword

    wrap_file("clip.mp4", "clip.wvf", "hunter2")
    meta = unwrap_file("clip.wvf", "out.mp4", password="hunter2")

Command-line use::

    python -m wvf.converter wrap clip.mp4
    python -m wvf.player clip.wvf

(The thin top-level scripts ``wvf_converter.py`` / ``wvf_player.py`` call into
these and keep the original ``python wvf_converter.py …`` commands working.)
"""

from .core import (
    wrap_file, unwrap_file, unwrap_stream, wrap_bytes, unwrap_bytes, peek,
    safe_output_name, sanitize_container,
    WVFError, WrongPassword, CorruptWVF, UnsupportedVersion,
    MAGIC, CHUNK, SUPPORTED_VERSIONS,
)

__all__ = [
    "wrap_file", "unwrap_file", "unwrap_stream", "wrap_bytes", "unwrap_bytes", "peek",
    "safe_output_name", "sanitize_container",
    "WVFError", "WrongPassword", "CorruptWVF", "UnsupportedVersion",
    "MAGIC", "CHUNK", "SUPPORTED_VERSIONS",
]

__version__ = "1.0.0"
