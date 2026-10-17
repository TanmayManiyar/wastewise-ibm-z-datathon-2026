"""The UI must render correctly when the server is big-endian (IBM LinuxONE) and the browser little-endian."""
import base64
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from ui import compat  # noqa: E402


def test_big_endian_chart_arrays_become_little_endian():
    values = np.array([19.0612, 73.0101, -1.5, 1e6])
    big = values.astype(">f8")                      # what an s390x server would encode
    spec = {"dtype": "f8", "bdata": base64.b64encode(big.tobytes()).decode()}
    fixed = compat.to_little_endian_spec(spec, source_big_endian=True)
    decoded = np.frombuffer(base64.b64decode(fixed["bdata"]), dtype="<f8")   # how plotly.js reads it
    assert np.allclose(decoded, values)
    ints = np.array([1, 1100, -7], dtype=">i4")
    fixed = compat.to_little_endian_spec({"dtype": "i4", "bdata": base64.b64encode(ints.tobytes()).decode()}, True)
    assert list(np.frombuffer(base64.b64decode(fixed["bdata"]), dtype="<i4")) == [1, 1100, -7]


def test_little_endian_server_is_left_alone():
    spec = {"dtype": "f8", "bdata": base64.b64encode(np.array([1.0]).astype("<f8").tobytes()).decode()}
    assert compat.to_little_endian_spec(spec, source_big_endian=False) == spec


def test_patched_plotly_figure_round_trip():
    import json

    import plotly.graph_objects as go
    import _plotly_utils.utils as pu
    original = pu.to_typed_array_spec
    try:
        # simulate a big-endian server: encode natively big-endian, then patch
        pu.to_typed_array_spec = lambda v: {"dtype": "f8", "bdata": base64.b64encode(
            np.asarray(v, dtype=">f8").tobytes()).decode()}
        compat._PATCHED = False
        compat.patch_plotly(force=True)
        lat = np.array([19.05, 19.06, 19.07])
        fig = go.Figure(go.Scattermap(lat=lat, lon=lat + 54))
        d = json.loads(fig.to_json())
        enc = d["data"][0]["lat"]
        got = np.frombuffer(base64.b64decode(enc["bdata"]), dtype="<f8") if isinstance(enc, dict) else np.array(enc)
        assert np.allclose(got, lat)
    finally:
        pu.to_typed_array_spec = original
        compat._PATCHED = False


def test_no_arrow_tables_in_the_app():
    """st.dataframe / st.data_editor / st.table send binary Arrow in the server's byte order."""
    for f in (ROOT / "app").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"st\.(dataframe|data_editor|table)\(", text), f"{f} uses an Arrow table"
