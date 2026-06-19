"""
wif_filter.py — PNG-style adaptive scanline filtering for WIF.

A *reversible* (lossless) spatial-prediction step applied to raw pixel bytes
before zlib, exactly like PNG.  It decorrelates neighbouring pixels so the
residuals cluster near zero, which zlib compresses far better than raw pixels.
Nothing is discarded: filter -> unfilter reproduces the input byte-for-byte.

Drop-in around wif_format.encode/decode (raw pixels = PIL img.tobytes()):

    raw = pil.tobytes()
    if filtered:
        raw = filter_pixels(raw, width, height, channels)   # BEFORE zlib
    if compress:
        raw = zlib.compress(raw, 6)
    ...
    # decode reverses the order:
    if compress:
        raw = zlib.decompress(raw)
    if filtered:
        raw = unfilter_pixels(raw, width, height, channels)  # AFTER zlib

Filtered stream layout:  [H filter-id bytes] + [H*W*C filtered pixel bytes].
filter ids follow the PNG spec: 0 None, 1 Sub, 2 Up, 3 Average, 4 Paeth.

`fast=True` limits the adaptive search to {None, Sub, Up}; those three reverse
in a fully-vectorised pass.  Average/Paeth squeeze a little more out but their
inverse is an inherent left-to-right recurrence (a short Python loop per row),
so prefer fast=True when decode latency matters (e.g. the viewer on 4K images).
"""

import numpy as np

NONE, SUB, UP, AVERAGE, PAETH = 0, 1, 2, 3, 4
_FAST = (NONE, SUB, UP)
_ALL = (NONE, SUB, UP, AVERAGE, PAETH)


def _paeth(a, b, c):
    """Vectorised PNG Paeth predictor.  a/b/c are int arrays (left/up/up-left)."""
    p = a + b - c
    pa, pb, pc = np.abs(p - a), np.abs(p - b), np.abs(p - c)
    return np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c))


def filter_pixels(pixels: bytes, width: int, height: int, channels: int,
                  fast: bool = False) -> bytes:
    """Adaptively filter raw interleaved pixel bytes (one best filter per row).
    Returns the filter-id table followed by the filtered pixels."""
    bpp = channels
    R = np.frombuffer(pixels, np.uint8).reshape(height, width, bpp).astype(np.int16)

    # neighbours, zero past the top/left edges (matches the decoder)
    left = np.zeros_like(R);   left[:, 1:] = R[:, :-1]
    up = np.zeros_like(R);     up[1:] = R[:-1]
    upleft = np.zeros_like(R); upleft[1:, 1:] = R[:-1, :-1]

    cand_ids = _FAST if fast else _ALL
    cands = []
    for fid in cand_ids:
        if fid == NONE:
            f = R
        elif fid == SUB:
            f = R - left
        elif fid == UP:
            f = R - up
        elif fid == AVERAGE:
            f = R - ((left + up) >> 1)
        else:  # PAETH
            f = R - _paeth(left, up, upleft)
        cands.append((f & 0xFF).astype(np.uint8))

    stack = np.stack(cands)                                   # (k, H, W, bpp)
    # min-SAD heuristic: score each row by treating filtered bytes as signed
    mag = np.minimum(stack, 256 - stack.astype(np.int16))
    sad = mag.reshape(len(cands), height, -1).sum(axis=2, dtype=np.int64)  # (k, H)
    best = sad.argmin(axis=0)                                 # (H,) -> index in cand_ids
    chosen = stack[best, np.arange(height)]                  # (H, W, bpp)
    ids = np.asarray(cand_ids, np.uint8)[best]               # (H,) real filter ids

    return ids.tobytes() + chosen.tobytes()


def unfilter_pixels(data: bytes, width: int, height: int, channels: int) -> bytes:
    """Invert filter_pixels, reproducing the original raw pixel bytes exactly."""
    bpp = channels
    ids = np.frombuffer(data[:height], np.uint8)
    F = np.frombuffer(data[height:], np.uint8).reshape(height, width, bpp)
    R = np.empty((height, width, bpp), np.uint8)

    for r in range(height):
        fid = ids[r]
        frow = F[r]
        above = R[r - 1] if r > 0 else np.zeros((width, bpp), np.uint8)
        if fid == NONE:
            R[r] = frow
        elif fid == UP:
            R[r] = frow + above                              # uint8 wraps mod 256
        elif fid == SUB:                                     # row-local cumulative sum
            R[r] = np.cumsum(frow.astype(np.int64), axis=0).astype(np.uint8)
        elif fid == AVERAGE:                                 # needs left + above -> recurrence
            a = np.zeros(bpp, np.int64)
            f_i, up_i = frow.astype(np.int64), above.astype(np.int64)
            out = np.empty((width, bpp), np.uint8)
            for j in range(width):
                a = (f_i[j] + ((a + up_i[j]) >> 1)) & 0xFF
                out[j] = a
            R[r] = out
        else:  # PAETH                                       # needs left + above + up-left
            a = np.zeros(bpp, np.int64)
            c = np.zeros(bpp, np.int64)
            f_i, up_i = frow.astype(np.int64), above.astype(np.int64)
            out = np.empty((width, bpp), np.uint8)
            for j in range(width):
                b = up_i[j]
                a = (f_i[j] + _paeth(a, b, c)) & 0xFF
                out[j] = a
                c = b
            R[r] = out
    return R.tobytes()


# ── self-test: prove losslessness and measure the win ──────────────────────────
if __name__ == "__main__":
    import zlib

    def make_images():
        rng = np.random.default_rng(0)
        H, W = 256, 384
        yy, xx = np.mgrid[0:H, 0:W]
        grad = ((xx * 256 // W + yy * 256 // H) % 256).astype(np.uint8)
        base = np.clip(128 + 100 * np.sin(xx / 40) + 80 * np.cos(yy / 30), 0, 255)
        photo = np.clip(base[..., None] + rng.normal(0, 6, (H, W, 3)), 0, 255).astype(np.uint8)
        flat = np.zeros((H, W, 3), np.uint8)
        flat[:, :W // 2] = (200, 30, 60); flat[:, W // 2:] = (20, 90, 180)
        g = grad.astype(np.int16)
        shift = lambda v: ((g + v) % 256).astype(np.uint8)   # rolled gradient channel
        return {
            "gradient_RGB":  np.dstack([grad, shift(80), shift(160)]),
            "photoish_RGB":  photo,
            "flatblocks_RGB": flat,
            "noise_RGB":     rng.integers(0, 256, (H, W, 3), dtype=np.uint8),
            "gradient_L":    grad[..., None],
            "gradient_RGBA": np.dstack([grad, shift(80), shift(160),
                                        np.full((H, W), 200, np.uint8)]),
        }

    print(f"{'image':16}{'ch':>3}{'raw+zlib':>10}{'full+zlib':>11}{'fast+zlib':>11}{'saved':>8}")
    print("-" * 59)
    all_ok = True
    for name, arr in make_images().items():
        H, W, C = arr.shape
        raw = arr.tobytes()
        for fast in (False, True):
            back = unfilter_pixels(filter_pixels(raw, W, H, C, fast=fast), W, H, C)
            if back != raw:
                all_ok = False
                print(f"  ROUND-TRIP FAILED: {name} fast={fast}")
        z_raw = len(zlib.compress(raw, 6))
        z_full = len(zlib.compress(filter_pixels(raw, W, H, C, fast=False), 6))
        z_fast = len(zlib.compress(filter_pixels(raw, W, H, C, fast=True), 6))
        saved = 100 * (1 - z_full / z_raw)
        print(f"{name:16}{C:>3}{z_raw:>10}{z_full:>11}{z_fast:>11}{saved:>7.1f}%")
    print("-" * 59)
    print("lossless round-trip:", "ALL OK" if all_ok else "*** FAILED ***")
