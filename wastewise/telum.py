"""IBM Telum on-chip AI accelerator path for tree-ensemble inference.

The forecaster's gradient-boosted trees (LightGBM text / XGBoost JSON) are
imported into IBM Snap ML. With tree_format='zdnn_tensors' Snap ML runs them
through IBM zDNN on the Integrated Accelerator for AI (NNPA) of IBM z16/z17
(Telum / Telum II); with 'compress_trees' it runs them on the CPU.
Settings > AI chooses auto / cpu / accelerator; the header shows what is used.
"auto" times both engines on this machine when the model is loaded and uses the
faster one (measured on the LinuxONE VM: Snap ML on the CPU was faster than the
accelerator for these tree ensembles - see docs/S390X_NOTES.md).
If the machine has no accelerator (or Snap ML is not installed) the native
library predicts on the CPU and the UI says so.
"""
from __future__ import annotations

import os
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import platform_info


SMALL, LARGE = 720, 20000      # one planning run (30 bins x 24 h) / a bulk job (re-test, outlook, what-if sets)


@dataclass
class Engine:
    device: str
    tree_format: str
    model: object
    timings_ms: dict | None = None      # {"cpu": {"720": ms, "20000": ms}, "accelerator": {...}}
    bulk: "Engine | None" = None        # used for large batches when it measured faster there

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.bulk is not None and len(X) >= (SMALL + LARGE) // 2:
            return self.bulk.predict(X)
        return np.asarray(self.model.predict(np.ascontiguousarray(X, dtype=np.float32)))

    def device_for(self, rows: int) -> str:
        return self.bulk.device if self.bulk is not None and rows >= (SMALL + LARGE) // 2 else self.device

    def summary(self) -> str:
        """Plain words for the header and Settings: what runs where, with the measured times."""
        t = self.timings_ms or {}
        if not t.get("accelerator"):
            return f"forecast trees on {self.device}"
        c, a = t["cpu"], t["accelerator"]
        small = "Telum" if a[str(SMALL)] < c[str(SMALL)] else "CPU"
        large = "Telum" if a[str(LARGE)] < c[str(LARGE)] else "CPU"
        if small == large == "CPU":
            return (f"Telum tested - CPU faster for these models (planning {c[str(SMALL)]:.1f} vs "
                    f"{a[str(SMALL)]:.1f} ms, bulk {c[str(LARGE)]:.0f} vs {a[str(LARGE)]:.0f} ms)")
        return (f"planning on {small} ({min(c[str(SMALL)], a[str(SMALL)]):.1f} ms), bulk forecasts on {large} "
                f"({min(c[str(LARGE)], a[str(LARGE)]):.0f} ms) - measured here")


def _time(fn, X, repeats=3) -> float:
    fn(X)
    ts = []
    for _ in range(repeats):
        t = time.perf_counter()
        fn(X)
        ts.append(time.perf_counter() - t)
    return 1000 * float(np.median(ts))


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
    try:
        m.set_params(n_jobs=os.cpu_count())   # use all cores (predict(n_jobs=...) is deprecated)
    except Exception:
        pass
    return m


def engine_for(model, model_dir: Path, mode: str = "auto") -> Engine | None:
    """Engine for the boosted trees, or None to use the native library on the CPU."""
    if getattr(model, "gbt", None) is None or mode == "cpu" or snapml_version() is None:
        return None
    path, kind = model.export_gbt(model_dir)
    cpu = acc = None
    try:
        cpu = Engine("CPU (Snap ML compressed trees)", "compress_trees", load_snapml(path, kind, "compress_trees"))
    except Exception:
        pass
    if mode in ("auto", "accelerator") and accelerator_available():
        try:
            acc = Engine("IBM Telum AI accelerator (Snap ML, zDNN)", "zdnn_tensors",
                         load_snapml(path, kind, "zdnn_tensors"))
        except Exception:
            if mode == "accelerator":
                raise
    if mode == "accelerator" and acc is not None:
        return acc
    if mode == "auto" and acc is not None and cpu is not None:
        from .forecast import FEATURES
        rng = np.random.default_rng(0)
        t = {"cpu": {}, "accelerator": {}}
        for n in (SMALL, LARGE):
            X = rng.uniform(0, 50, (n, len(FEATURES))).astype(np.float32)
            t["cpu"][str(n)] = round(_time(cpu.predict, X), 3)
            t["accelerator"][str(n)] = round(_time(acc.predict, X), 3)
        small = acc if t["accelerator"][str(SMALL)] < t["cpu"][str(SMALL)] else cpu
        large = acc if t["accelerator"][str(LARGE)] < t["cpu"][str(LARGE)] else cpu
        small.timings_ms = t
        if large is not small:
            small.bulk = large
        return small
    return cpu or acc


def benchmark(model, model_dir: Path, X: np.ndarray, repeats: int = 7, batch_sizes=(720, 20000, 100000)) -> dict:
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


def sweep(X: np.ndarray, y: np.ndarray, configs=((4, 100), (6, 200), (6, 500), (8, 500), (10, 300)),
          rows: int = 100_000) -> list[dict]:
    """Same comparison across LightGBM model sizes (depth, trees)."""
    import tempfile
    import lightgbm as lgb
    from .forecast import _fill_nan
    X = np.ascontiguousarray(_fill_nan(X), dtype=np.float32)
    B = np.resize(X, (rows, X.shape[1]))
    out = []
    d = tempfile.mkdtemp()
    for depth, trees in configs:
        m = lgb.LGBMRegressor(n_estimators=trees, max_depth=depth, num_leaves=2 ** depth, learning_rate=0.05,
                              verbose=-1).fit(X, y)
        p = os.path.join(d, f"m{depth}_{trees}.txt")
        m.booster_.save_model(p)
        row = {"max_depth": depth, "trees": trees, "rows": rows,
               "native_lightgbm_cpu_ms": round(_time(m.predict, B), 1)}
        if snapml_version():
            s = load_snapml(p, "lightgbm", "compress_trees")
            row["snapml_cpu_ms"] = round(_time(lambda A: s.predict(A), B), 1)
            if accelerator_available():
                z = load_snapml(p, "lightgbm", "zdnn_tensors")
                row["snapml_telum_ms"] = round(_time(lambda A: z.predict(A), B), 1)
        out.append(row)
    return out