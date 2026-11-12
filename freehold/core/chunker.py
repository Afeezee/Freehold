"""Content-defined chunking (FastCDC-style gear hash).

Boundaries depend only on content and on GEAR, which is generated from a fixed
seed. Any Freehold build, on any platform, splits the same bytes the same way.
"""
from __future__ import annotations

from typing import Iterator, Tuple, Union

MIN_SIZE = 256 * 1024
AVG_SIZE = 1024 * 1024
MAX_SIZE = 4 * 1024 * 1024

_MASK_S = 0x0003590703530000  # denser mask, used below the average size
_MASK_L = 0x0000d90003530000  # sparser mask, used above it
_SEED = 0x9E3779B97F4A7C15
_U64 = 0xFFFFFFFFFFFFFFFF


def _gear_table() -> Tuple[int, ...]:
    # splitmix64, so the table is identical everywhere without shipping 2 KiB
    x = _SEED
    out = []
    for _ in range(256):
        x = (x + 0x9E3779B97F4A7C15) & _U64
        z = x
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _U64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _U64
        out.append(z ^ (z >> 31))
    return tuple(out)


GEAR = _gear_table()


def cut(data: Union[bytes, memoryview], min_size: int = MIN_SIZE,
        avg_size: int = AVG_SIZE, max_size: int = MAX_SIZE) -> int:
    """Return the length of the first chunk of ``data``."""
    n = len(data)
    if n <= min_size:
        return n
    limit = min(max_size, n)
    normal = min(avg_size, limit)
    gear, mask_s, mask_l = GEAR, _MASK_S, _MASK_L
    fp = 0
    i = min_size
    while i < normal:
        fp = ((fp << 1) + gear[data[i]]) & _U64
        if not fp & mask_s:
            return i + 1
        i += 1
    while i < limit:
        fp = ((fp << 1) + gear[data[i]]) & _U64
        if not fp & mask_l:
            return i + 1
        i += 1
    return limit


def chunk_stream(fh, min_size: int = MIN_SIZE, avg_size: int = AVG_SIZE,
                 max_size: int = MAX_SIZE) -> Iterator[bytes]:
    """Yield content-defined chunks from a binary file object."""
    read_size = max(max_size * 2, 8 << 20)
    buf = b""
    eof = False
    while True:
        while not eof and len(buf) < max_size * 2:
            block = fh.read(read_size)
            if block:
                buf += block
            else:
                eof = True
        if not buf:
            return
        n = cut(buf, min_size, avg_size, max_size)
        yield buf[:n]
        buf = buf[n:]
