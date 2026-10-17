"""Make data sent to the browser readable when the server is big-endian (IBM LinuxONE / s390x).

Browsers are little-endian. Plotly sends numeric arrays as base64 in the server's native
byte order ("bdata"); on s390x plotly.js would read them backwards (route lines vanish,
charts show garbage). We re-encode those arrays as little-endian. Tables avoid the same
problem by being rendered as HTML (see tables.py) instead of Streamlit's binary Arrow tables.
"""
from __future__ import annotations

import base64
import sys

import numpy as np

_PATCHED = False


def to_little_endian_spec(spec: dict, source_big_endian: bool = sys.byteorder == "big") -> dict:
    """Re-encode a plotly typed-array spec {"dtype": "f8", "bdata": ...} as little-endian."""
    if not source_big_endian or not isinstance(spec, dict) or "bdata" not in spec:
        return spec
    dt = np.dtype(spec["dtype"])
    if dt.itemsize == 1:
        return spec
    raw = base64.b64decode(spec["bdata"])
    arr = np.frombuffer(raw, dtype=dt.newbyteorder(">"))
    out = dict(spec)
    out["bdata"] = base64.b64encode(arr.astype(dt.newbyteorder("<")).tobytes()).decode("ascii")
    return out


def patch_plotly(force: bool = False) -> bool:
    """Patch plotly's typed-array encoder on big-endian machines (idempotent)."""
    global _PATCHED
    if _PATCHED or not (force or sys.byteorder == "big"):
        return _PATCHED
    import _plotly_utils.utils as pu
    original = pu.to_typed_array_spec

    def to_typed_array_spec(v):
        return to_little_endian_spec(original(v), source_big_endian=True)

    pu.to_typed_array_spec = to_typed_array_spec
    _PATCHED = True
    return True
