"""IBM Telum on-chip AI accelerator path for tree-ensemble inference.

The forecaster's gradient-boosted trees (LightGBM text / XGBoost JSON) are
imported into IBM Snap ML. With tree_format='zdnn_tensors' Snap ML runs them
through IBM zDNN on the Integrated Accelerator for AI (NNPA) of IBM z16/z17
(Telum / Telum II); with 'compress_trees' it runs them on the CPU.
Settings > AI chooses auto / cpu / accelerator; the header shows what is used.
If the machine has no accelerator (or Snap ML is not installed) the native
library predicts on the CPU and the UI says so.
"""
from __future__ import annotations

import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import platform_info


@dataclass
class Engine:
    device: str
    tree_format: str
    model: object

    def predict(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(self.model.predict(np.ascontiguousarray(X, dtype=np.float32)))


def snapml_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("snapml")
    except Exception:
        return None


def accelerator_available() -> bool:
    p = platform_info.detect()
    return bool(p.get("is_s390x") and p.get("nnpa_usable"))


def load_snapml(path: Path, kind: str, tree_format: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import snapml
    m = snapml.BoostingMachineRegressor()
    m.import_model(str(path), kind, tree_format=tree_format)
    return m


def engine_for(model, model_dir: Path, mode: str = "auto") -> Engine | None:
    """Engine for the boosted trees, or None to use the native library on the CPU."""
    if getattr(model, "gbt", None) is None or mode == "cpu" or snapml_version() is None:
        return None
    path, kind = model.export_gbt(model_dir)
    if mode in ("auto", "accelerator") and accelerator_available():
        try:
            return Engine("IBM Telum AI accelerator (Snap ML, zDNN)", "zdnn_tensors",
                          load_snapml(path, kind, "zdnn_tensors"))
        except Exception:
            if mode == "accelerator":
                raise
    try:
        return Engine("CPU (Snap ML compressed trees)", "compress_trees", load_snapml(path, kind, "compress_trees"))
    except Exception:
        return None


def benchmark(model, model_dir: Path, X: np.ndarray, repeats: int = 7, batch_sizes=(720, 20000)) -> dict:
    """Time the same boosted trees on: native library (CPU), Snap ML CPU, Snap ML accelerator."""
    from .forecast import _fill_nan
    Xf = np.ascontiguousarray(_fill_nan(X), dtype=np.float32)
    ref = model.gbt_predict(Xf.astype(np.float64))
    path, kind = model.export_gbt(model_dir)
    engines = {f"native {model.gbt_lib} (CPU)": model.gbt_predict}
    info = {"snapml": snapml_version(), "accelerator_available": accelerator_available()}
    if info["snapml"]:
        try:
            m = load_snapml(path, kind, "compress_trees")
            engines["Snap ML compress_trees (CPU)"] = lambda A, m=m: m.predict(A)
        except Exception as exc:
            info["compress_trees_error"] = str(exc)
        if info["accelerator_available"]:
            try:
                m = load_snapml(path, kind, "zdnn_tensors")
                engines["Snap ML zdnn_tensors (Telum accelerator)"] = lambda A, m=m: m.predict(A)
            except Exception as exc:
                info["zdnn_error"] = str(exc)
    results = {}
    for name, fn in engines.items():
        row = {}
        for n in batch_sizes:
            A = Xf[:n] if n <= len(Xf) else np.resize(Xf, (n, Xf.shape[1]))
            fn(A)                                  # warm-up
            times = []
            for _ in range(repeats):
                t = time.perf_counter()
                p = fn(A)
                times.append(time.perf_counter() - t)
            row[f"batch_{n}_ms_median"] = round(1000 * float(np.median(times)), 3)
            row[f"batch_{n}_rows_per_s"] = int(n / float(np.median(times)))
        p = np.asarray(fn(Xf))
        row["max_abs_diff_vs_native"] = round(float(np.abs(p - ref).max()), 5)
        row["mean_abs_diff_vs_native"] = round(float(np.abs(p - ref).mean()), 6)
        results[name] = row
    return {**info, "rows_compared": int(len(Xf)), "trees": model.meta.get("gbt_iterations"),
            "machine": platform_info.detect(), "engines": results}
