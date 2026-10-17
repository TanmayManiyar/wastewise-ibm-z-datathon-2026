"""Fill-rate forecaster (gradient-boosted trees + scikit-learn HistGradientBoosting + ensemble).

What is predicted
    For a sensor bin at an origin time t0, the average fill rate (% per hour)
    over the 3-hour window starting k hours later (k = 0..23). The planner
    turns these into hourly rates for the overflow-risk model.
Features (known at t0 only)
    lead k; hour / weekday of the window; festival (which, day of, days to);
    monsoon; the bin's own recent behaviour (same hour over the last 7 days,
    same hour and weekday over 4 weeks, last 7 days, last 24 h, last 6 h);
    current fill; hours since collection; capacity, site type, priority.
Evaluation
    Chronological split: train / validation / locked test (with gaps so no
    target window crosses a boundary). Models are compared with simple
    baselines on the locked test window; a newly trained model is promoted
    only if it beats the currently promoted one there.
IBM Telum
    The gradient-boosted model can be imported into IBM Snap ML and executed
    on the on-chip AI accelerator (tree_format='zdnn_tensors'); see telum.py.
"""
from __future__ import annotations

import json
import math
import os
import pickle
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import anomaly, calendar_in, config, db

WINDOW = 3
TRAIN_LEADS = list(range(0, 24, 3))
FEATURES = ["lead_h", "hour", "dow", "weekend", "fest_code", "fest_day", "days_to_fest", "monsoon",
            "same_hour_7d", "same_how_4w", "mean_7d", "last_24h", "last_6h", "fill_now", "hours_since_coll",
            "capacity_l", "site_code", "priority"]
FEST_CODES = {"Ganeshotsav": 1, "Navratri": 2, "Diwali": 3, "Dussehra": 4}
SITE_CODES = {k: i for i, k in enumerate(["housing", "station", "bus_stand", "supermarket", "eatery", "school",
                                          "college", "hospital", "clinic", "temple", "worship", "park", "ground",
                                          "gaothan", "public_office"], 1)}


# ------------------------------------------------------------------ series and features
@dataclass
class Series:
    key: str
    start_ts: int            # timestamp of hour 0 (aligned to the hour)
    f: np.ndarray            # (H,) fill % at each hour (NaN = missing)
    coll: np.ndarray         # (H,) bool, a collection happened during this hour
    static: dict             # capacity_l, site_code, priority
    tz: timezone
    calendar: bool = True    # Indian festival / monsoon features apply


def to_series(key: str, ts: np.ndarray, v: np.ndarray, coll_ts: np.ndarray, start_ts: int, end_ts: int,
              static: dict, tz=config.IST, calendar: bool = True, mask: np.ndarray | None = None) -> Series:
    H = (end_ts - start_ts) // 3600 + 1
    f = np.full(H, np.nan)
    if mask is not None:
        ts, v = ts[~mask], v[~mask]
    h = ((ts - start_ts) // 3600).astype(int)
    ok = (h >= 0) & (h < H)
    f[h[ok]] = v[ok]                      # last reading in each hour wins (sorted input)
    coll = np.zeros(H, dtype=bool)
    hc = ((coll_ts - start_ts) // 3600).astype(int)
    hc = hc[(hc >= 0) & (hc < H)]
    coll[hc] = True
    return Series(key, start_ts, f, coll, static, tz, calendar)


def window_rates(s: Series) -> np.ndarray:
    """inc3[h]: average %/h from hour h to h+3, NaN where not observable cleanly."""
    f, H = s.f, len(s.f)
    out = np.full(H, np.nan)
    if H <= WINDOW + 1:
        return out
    a, b = f[:-WINDOW], f[WINDOW:]
    cw = np.lib.stride_tricks.sliding_window_view(np.r_[s.coll, np.zeros(1, bool)], WINDOW + 1).any(axis=1)
    cw = cw[:H - WINDOW]
    r = (b - a) / WINDOW
    good = ~np.isnan(a) & ~np.isnan(b) & (a < 97) & (b < 97) & ~cw & (r > -1.0) & (r < 20)
    out[:H - WINDOW] = np.where(good, r, np.nan)
    return out


def _nanmean_stack(arrs: list[np.ndarray]) -> np.ndarray:
    st = np.vstack(arrs)
    cnt = (~np.isnan(st)).sum(axis=0)
    with np.errstate(invalid="ignore"):
        m = np.nansum(st, axis=0) / np.maximum(cnt, 1)
    return np.where(cnt > 0, m, np.nan)


def _shift(a: np.ndarray, k: int) -> np.ndarray:
    """out[i] = a[i - k] (NaN outside)."""
    out = np.full(len(a), np.nan)
    if k >= 0:
        out[k:] = a[:len(a) - k] if k else a
    else:
        out[:k] = a[-k:]
    return out


def _calendar_feats(d: date) -> tuple[int, int, int, int]:
    fz = calendar_in.festival_on(d)
    code = FEST_CODES.get(fz[0], 5) if fz else 0
    day_in = 0
    if fz:
        start = next(s for n, s, e, _ in calendar_in.FESTIVALS if s <= d <= e)
        day_in = (d - start).days + 1
    return code, day_in, min(calendar_in.days_to_next_festival(d, 30), 31), int(calendar_in.is_monsoon(d))


def feature_rows(s: Series, origins: np.ndarray, leads: list[int], with_target: bool = True):
    """Rows for (origin, lead) pairs. Returns X (n, F), y (n,) or None, origin hour idx, lead."""
    inc3 = window_rates(s)
    H = len(s.f)
    # history aggregates aligned to "window start" s = o + k
    same7 = {}
    for k in sorted(set(leads)):
        j0 = 1 if k + WINDOW <= 24 else 2
        same7[k] = _nanmean_stack([_shift(inc3, 24 * j) for j in range(j0, j0 + 7)])
    same4w = _nanmean_stack([_shift(inc3, 168 * j) for j in range(1, 5)])
    cs = np.r_[0.0, np.nancumsum(inc3)]
    cn = np.r_[0, np.cumsum(~np.isnan(inc3))]

    def win_mean(a, b):   # mean of inc3[a:b] (windows ending by origin)
        a = np.clip(a, 0, H)
        b = np.clip(b, 0, H)
        n = cn[b] - cn[a]
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(n > 0, (cs[b] - cs[a]) / np.maximum(n, 1), np.nan)

    last_coll = np.full(H, -10_000)
    lc = -10_000
    for h in range(H):
        if s.coll[h]:
            lc = h
        last_coll[h] = lc
    # latest fill known at the origin (within 3 h)
    fill_now = s.f.copy()
    for back in (1, 2, 3):
        fill_now = np.where(np.isnan(fill_now), _shift(s.f, back), fill_now)
    cal_cache: dict[date, tuple] = {}
    rows, ys, oo, kk = [], [], [], []
    for k in leads:
        st = origins + k
        valid = st < H - WINDOW if with_target else np.ones(len(origins), bool)
        o, st = origins[valid], st[valid]
        y = inc3[np.minimum(st, H - 1)] if with_target else np.zeros(len(o))
        if with_target:
            keep = ~np.isnan(y)
            o, st, y = o[keep], st[keep], y[keep]
        if not len(o):
            continue
        hrs = s.start_ts + st * 3600
        loc = [datetime.fromtimestamp(int(t), s.tz) for t in hrs]
        hour = np.array([d.hour for d in loc])
        dow = np.array([d.weekday() for d in loc])
        if s.calendar:
            cal = []
            for d in loc:
                dd = d.date()
                if dd not in cal_cache:
                    cal_cache[dd] = _calendar_feats(dd)
                cal.append(cal_cache[dd])
            cal = np.array(cal)
        else:
            cal = np.zeros((len(o), 4))
        X = np.column_stack([
            np.full(len(o), k), hour, dow, (dow >= 5).astype(int), cal[:, 0], cal[:, 1], cal[:, 2], cal[:, 3],
            same7[k][np.minimum(st, H - 1)], same4w[np.minimum(st, H - 1)],
            win_mean(o - 168, o - WINDOW + 1), win_mean(o - 24, o - WINDOW + 1), win_mean(o - 6, o - WINDOW + 1),
            fill_now[o], np.minimum(o - last_coll[o], 72),
            np.full(len(o), s.static.get("capacity_l", 0)), np.full(len(o), s.static.get("site_code", 0)),
            np.full(len(o), s.static.get("priority", 0)),
        ])
        rows.append(X)
        ys.append(y)
        oo.append(o)
        kk.append(np.full(len(o), k))
    if not rows:
        return np.zeros((0, len(FEATURES))), np.zeros(0), np.zeros(0, int), np.zeros(0, int)
    return np.vstack(rows), (np.concatenate(ys) if with_target else None), np.concatenate(oo), np.concatenate(kk)


# ------------------------------------------------------------------ datasets
def sanpada_series(conn: sqlite3.Connection, start_ts: int, end_ts: int, bins: list[dict] | None = None,
                   skip: set[str] | None = None) -> list[Series]:
    start_ts -= start_ts % 3600
    out = []
    for b in (bins or db.bins(conn)):
        if not b["has_sensor"] or (skip and b["id"] in skip):
            continue
        rs = conn.execute("SELECT ts, fill_pct FROM readings WHERE bin_id=? AND source IN ('sensor','api')"
                          " AND ts BETWEEN ? AND ? AND fill_pct IS NOT NULL ORDER BY ts",
                          (b["id"], start_ts, end_ts)).fetchall()
        if len(rs) < 24:
            continue
        ts = np.array([r[0] for r in rs], dtype=np.int64)
        v = np.array([r[1] for r in rs], dtype=float)
        colls = np.array([r[0] for r in conn.execute(
            "SELECT ts FROM events WHERE bin_id=? AND kind='collected' AND ts BETWEEN ? AND ?",
            (b["id"], start_ts, end_ts))], dtype=np.int64)
        fl = anomaly.rule_flags(ts, v, colls)
        mask = np.zeros(len(v), bool)
        for k in anomaly.RULES:
            mask |= fl[k]
        out.append(to_series(b["id"], ts, v, colls, start_ts, end_ts,
                             {"capacity_l": b["capacity_l"], "site_code": SITE_CODES.get(b["site_type"], 0),
                              "priority": b["priority"]}, mask=mask))
    return out


def dataset(series: list[Series], origin_step: int = 3, leads: list[int] = TRAIN_LEADS):
    Xs, ys, ts, keys = [], [], [], []
    for s in series:
        origins = np.arange(24 * 7, len(s.f) - 1, origin_step)
        X, y, o, k = feature_rows(s, origins, leads)
        Xs.append(X)
        ys.append(y)
        ts.append(s.start_ts + o * 3600)
        keys.extend([s.key] * len(y))
    return np.vstack(Xs), np.concatenate(ys), np.concatenate(ts), np.array(keys)


def chrono_split(origin_ts: np.ndarray, fractions=(0.6, 0.2), gap_h: int = 27, bounds: dict | None = None):
    """Boolean masks train / val / test by origin time, with gaps so targets never cross."""
    lo, hi = int(origin_ts.min()), int(origin_ts.max())
    if bounds is None:
        b1 = lo + int((hi - lo) * fractions[0])
        b2 = lo + int((hi - lo) * (fractions[0] + fractions[1]))
        bounds = {"val_start": b1, "test_start": b2, "test_end": hi}
    g = gap_h * 3600
    tr = origin_ts < bounds["val_start"] - g
    va = (origin_ts >= bounds["val_start"]) & (origin_ts < bounds["test_start"] - g)
    te = (origin_ts >= bounds["test_start"]) & (origin_ts <= bounds["test_end"])
    return tr, va, te, bounds


# ------------------------------------------------------------------ models
def gbt_library() -> str | None:
    for name in ("lightgbm", "xgboost"):
        try:
            __import__(name)
            return name
        except ImportError:
            continue
    return None


def _fill_nan(X: np.ndarray) -> np.ndarray:
    return np.where(np.isnan(X), -1.0, X)


class Forecaster:
    """HistGradientBoosting + a gradient-boosted tree library + their average."""

    def __init__(self):
        self.hgb = None
        self.gbt = None
        self.gbt_lib = None
        self.trained_at = None
        self.meta: dict = {}

    def fit(self, Xtr, ytr, Xva, yva, seed: int = 0):
        from sklearn.ensemble import HistGradientBoostingRegressor
        best = None
        for it in (150, 300, 500):
            m = HistGradientBoostingRegressor(max_iter=it, learning_rate=0.06, max_leaf_nodes=31,
                                              l2_regularization=1.0, early_stopping=False, random_state=seed)
            m.fit(Xtr, ytr)
            mae = float(np.abs(m.predict(Xva) - yva).mean())
            if best is None or mae < best[0]:
                best = (mae, m, it)
        self.hgb = best[1]
        self.meta["hgb_iterations"] = best[2]
        self.gbt_lib = gbt_library()
        if self.gbt_lib == "lightgbm":
            import lightgbm as lgb
            self.gbt = lgb.LGBMRegressor(n_estimators=1500, learning_rate=0.05, num_leaves=31, min_child_samples=40,
                                         subsample=0.8, subsample_freq=1, colsample_bytree=0.9, reg_lambda=1.0,
                                         random_state=seed, verbose=-1)
            stop = [lgb.early_stopping(50, verbose=False)]
            try:     # LightGBM >= 4.7 names the validation set eval_X / eval_y
                self.gbt.fit(_fill_nan(Xtr), ytr, eval_X=(_fill_nan(Xva),), eval_y=(yva,), callbacks=stop)
            except TypeError:
                self.gbt.fit(_fill_nan(Xtr), ytr, eval_set=[(_fill_nan(Xva), yva)], callbacks=stop)
            self.meta["gbt_iterations"] = int(self.gbt.best_iteration_ or self.gbt.n_estimators)
        elif self.gbt_lib == "xgboost":
            import xgboost as xgb
            self.gbt = xgb.XGBRegressor(n_estimators=1500, learning_rate=0.05, max_depth=7, subsample=0.8,
                                        colsample_bytree=0.9, reg_lambda=1.0, random_state=seed,
                                        early_stopping_rounds=50, tree_method="hist", n_jobs=os.cpu_count())
            self.gbt.fit(_fill_nan(Xtr), ytr, eval_set=[(_fill_nan(Xva), yva)], verbose=False)
            self.meta["gbt_iterations"] = int(self.gbt.best_iteration + 1)
        self.trained_at = int(time.time())
        return self

    def predict_parts(self, X: np.ndarray, gbt_predict=None) -> dict[str, np.ndarray]:
        out = {"hgb": self.hgb.predict(X)}
        if self.gbt is not None:
            out["gbt"] = gbt_predict(_fill_nan(X)) if gbt_predict else self.gbt_predict(_fill_nan(X))
            out["ensemble"] = 0.5 * (out["hgb"] + out["gbt"])
        return out

    def gbt_predict(self, X: np.ndarray) -> np.ndarray:
        if self.gbt_lib == "lightgbm":
            return self.gbt.predict(X, num_iteration=self.meta.get("gbt_iterations"))
        return self.gbt.predict(X)

    def export_gbt(self, path: Path) -> tuple[Path, str]:
        """Save the boosted trees in a format IBM Snap ML can import."""
        if self.gbt_lib == "lightgbm":
            p = path / "gbt_lightgbm.txt"
            self.gbt.booster_.save_model(str(p), num_iteration=self.meta.get("gbt_iterations"))
            return p, "lightgbm"
        p = path / "gbt_xgboost.json"
        self.gbt.get_booster().save_model(str(p))
        return p, "xgb_json"


def baselines(X: np.ndarray) -> dict[str, np.ndarray]:
    col = {n: i for i, n in enumerate(FEATURES)}

    def pick(*names):
        out = np.full(len(X), np.nan)
        for n in names:
            out = np.where(np.isnan(out), X[:, col[n]], out)
        return np.nan_to_num(out, nan=0.0)

    return {"same_hour_last_7_days": pick("same_hour_7d", "mean_7d"),
            "mean_rate_last_7_days": pick("mean_7d", "last_24h"),
            "last_24_hours": pick("last_24h", "mean_7d")}


def scores(y: np.ndarray, preds: dict[str, np.ndarray]) -> dict[str, dict]:
    out = {}
    for name, p in preds.items():
        e = (p - y) * WINDOW      # error in fill points over the 3-hour window
        out[name] = {"mae_3h_points": round(float(np.abs(e).mean()), 3),
                     "rmse_3h_points": round(float(np.sqrt((e ** 2).mean())), 3),
                     "bias_3h_points": round(float(e.mean()), 3)}
    return out


# ------------------------------------------------------------------ registry
def _models_dir() -> Path:
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return config.MODELS_DIR


def promoted(conn: sqlite3.Connection) -> dict | None:
    r = db.one(conn, "SELECT * FROM models WHERE kind='forecaster' AND promoted=1 ORDER BY id DESC LIMIT 1")
    if r:
        r["metrics"] = json.loads(r["metrics"])
    return r


def _load(path: str) -> Forecaster:
    with open(Path(path) / "forecaster.pkl", "rb") as f:
        return pickle.load(f)


def train_and_register(conn: sqlite3.Connection, user: str | None = None, end_ts: int | None = None,
                       history_days: int = 180) -> dict:
    """Train on the Sanpada history, evaluate on the locked test window, promote if better."""
    from . import audit
    end_ts = end_ts or int(conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0])
    start_ts = end_ts - history_days * 86400
    t0 = time.time()
    X, y, ots, keys = dataset(sanpada_series(conn, start_ts, end_ts))
    lock = db.get_meta(conn, "forecast_test_window")
    bounds = json.loads(lock) if lock else None
    if bounds and not (ots.min() < bounds["val_start"] < bounds["test_start"] <= ots.max()):
        bounds = None       # data was regenerated: lock a new test window
    tr, va, te, bounds = chrono_split(ots, bounds=bounds)
    if not lock or json.loads(lock) != bounds:
        db.set_meta(conn, "forecast_test_window", json.dumps(bounds))
    model = Forecaster().fit(X[tr], y[tr], X[va], y[va])
    test_preds = {**model.predict_parts(X[te]), **baselines(X[te])}
    metrics = {"label": "trained and tested on SYNTHETIC demonstration data",
               "rows": {"train": int(tr.sum()), "validation": int(va.sum()), "test": int(te.sum())},
               "test_window": {k: datetime.fromtimestamp(v, config.IST).isoformat() for k, v in bounds.items()},
               "test": scores(y[te], test_preds), "gbt_library": model.gbt_lib, **model.meta,
               "train_seconds": round(time.time() - t0, 1)}
    best_name = min((n for n in ("hgb", "gbt", "ensemble") if n in test_preds),
                    key=lambda n: metrics["test"][n]["mae_3h_points"])
    metrics["best_model"] = best_name
    new_mae = metrics["test"][best_name]["mae_3h_points"]
    base_mae = min(v["mae_3h_points"] for k, v in metrics["test"].items() if k not in ("hgb", "gbt", "ensemble"))

    cur = promoted(conn)
    cur_mae = None
    if cur:
        try:
            old = _load(cur["path"])
            p_old = old.predict_parts(X[te])
            cur_mae = scores(y[te], {"x": p_old[cur["metrics"]["best_model"]]})["x"]["mae_3h_points"]
        except Exception:
            cur_mae = None
    promote = new_mae < base_mae and (cur_mae is None or new_mae < cur_mae - 1e-6)
    metrics["decision"] = {"new_mae": new_mae, "best_baseline_mae": base_mae, "current_promoted_mae": cur_mae,
                           "promoted": promote}
    out_dir = _models_dir() / f"forecaster-{model.trained_at}"
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "forecaster.pkl", "wb") as f:
        pickle.dump(model, f)
    if model.gbt is not None:
        p, kind = model.export_gbt(out_dir)
        metrics["gbt_export"] = {"file": p.name, "format": kind}
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    with db.tx(conn):
        if promote:
            conn.execute("UPDATE models SET promoted=0 WHERE kind='forecaster'")
        conn.execute("INSERT INTO models(created_at, kind, name, path, metrics, promoted) VALUES (?,?,?,?,?,?)",
                     (model.trained_at, "forecaster", best_name, str(out_dir), json.dumps(metrics), int(promote)))
        audit.log(conn, user, "forecaster_trained", {"mae": new_mae, "baseline": base_mae, "promoted": promote})
    _CACHE.clear()
    return metrics


# ------------------------------------------------------------------ inference for the planner
_CACHE: dict = {}


def _inference_model(conn: sqlite3.Connection):
    cur = promoted(conn)
    if not cur:
        return None
    if _CACHE.get("path") != cur["path"]:
        _CACHE.clear()
        _CACHE["path"] = cur["path"]
        _CACHE["model"] = _load(cur["path"])
        _CACHE["row"] = cur
        _CACHE["engine"] = None
    mode = db.get_settings(conn).get("telum_inference", "auto")
    if _CACHE.get("engine_mode") != mode:
        from . import telum
        _CACHE["engine"] = telum.engine_for(_CACHE["model"], Path(cur["path"]), mode)
        _CACHE["engine_mode"] = mode
    return _CACHE


def predict_rates(conn: sqlite3.Connection, at_ts: int, hours: int, bins: list[dict], states: dict,
                  fallback: dict[str, np.ndarray] | None = None) -> dict | None:
    """Hourly %/h for the next `hours` hours for each bin. Sensor bins use the promoted
    model; other bins (and faulty sensors) keep the fallback profile rates."""
    c = _inference_model(conn)
    if c is None:
        return None
    model, row, engine = c["model"], c["row"], c["engine"]
    start = at_ts - 29 * 86400
    series = sanpada_series(conn, start, at_ts, bins)
    leads = list(range(0, 24))
    rates = dict(fallback or {})
    Xs, owners = [], []
    for s in series:
        o = np.array([len(s.f) - 1])
        X, _, _, _ = feature_rows(s, o, leads, with_target=False)
        Xs.append(X)
        owners.append(s.key)
    if not Xs:
        return None
    X = np.vstack(Xs)
    t = time.perf_counter()
    parts = model.predict_parts(X, gbt_predict=engine.predict if engine else None)
    infer_ms = (time.perf_counter() - t) * 1000
    pred = np.clip(parts[row["metrics"]["best_model"]], 0.0, None)
    for i, key in enumerate(owners):
        r24 = pred[i * 24:(i + 1) * 24]
        rr = np.resize(r24, hours)       # repeat the daily pattern beyond 24 h
        rates[key] = rr
    src = f"ML forecaster ({row['metrics']['best_model']}, model #{row['id']})"
    if engine:
        src += f" - tree inference on {engine.device}"
    return {"rates": rates, "source": src, "inference_ms": round(infer_ms, 2), "rows": len(X)}


# ------------------------------------------------------------------ Mildura real-world test
def mildura_series(path: Path) -> list[Series]:
    import csv
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Melbourne")
    except Exception:
        tz = timezone(timedelta(hours=10), "AEST")
    by_dev: dict[str, list[tuple[int, float]]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:   # the file starts with a BOM
        for r in csv.DictReader(fh):
            if not r["Channel Description"].startswith("Distance to Top"):
                continue
            v = float(r["Calibrated Value"])
            if v >= 9000:
                continue          # sensor error code
            t = int(datetime.strptime(r["Timestamp (UTC)"], "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc).timestamp())
            by_dev.setdefault(r["Device Tag"], []).append((t, v))
    out = []
    for dev, rows in sorted(by_dev.items()):
        rows.sort()
        ts = np.array([r[0] for r in rows], dtype=np.int64)
        dist = np.array([r[1] for r in rows])
        if len(ts) < 500 or np.median(np.diff(ts)) > 5400:
            continue              # only the hourly devices
        depth = np.quantile(dist, 0.98)
        keep = dist <= depth * 1.15
        ts, dist = ts[keep], dist[keep]
        fill = np.clip((depth - dist) / depth * 100, 0, 110)
        # a drop of 25+ points between readings = emptied
        drop = np.r_[False, np.diff(fill) < -25]
        colls = ts[drop] - 60
        fl = anomaly.rule_flags(ts, fill, colls)
        mask = fl["stuck"] | fl["spike"] | fl["jump"]
        start = int(ts[0] - ts[0] % 3600)
        out.append(to_series(dev, ts, fill, colls, start, int(ts[-1]), {}, tz=tz, calendar=False, mask=mask))
    return out


def mildura_evaluation(path: Path, sanpada_model: Forecaster | None = None) -> dict:
    series = mildura_series(path)
    X, y, ots, keys = dataset(series)
    tr, va, te, bounds = chrono_split(ots)
    m = Forecaster().fit(X[tr], y[tr], X[va], y[va])
    preds = {**m.predict_parts(X[te]), **baselines(X[te])}
    out = {"label": "REAL data: Mildura Rural City Council smart bins (data.gov.au, CC BY 3.0 AU)",
           "devices": [s.key for s in series], "rows": {"train": int(tr.sum()), "validation": int(va.sum()),
                                                       "test": int(te.sum())},
           "test_window": {k: datetime.fromtimestamp(v, timezone.utc).date().isoformat() for k, v in bounds.items()},
           "test": scores(y[te], preds), "gbt_library": m.gbt_lib}
    if sanpada_model is not None:
        p = sanpada_model.predict_parts(X[te])
        out["transfer_from_sanpada_model"] = scores(y[te], {f"sanpada_{k}": v for k, v in p.items()})
    return out


# ------------------------------------------------------------------ accuracy check of the model in use
def evaluate_promoted(conn: sqlite3.Connection, history_days: int = 180) -> dict | None:
    """Re-test the model in use on the locked test window (no training): overall, by how far
    ahead, and by site type, against the simple baselines. Errors are in fill points (% of the bin)
    over a 3-hour window."""
    cur = promoted(conn)
    lock = db.get_meta(conn, "forecast_test_window")
    if not cur or not lock:
        return None
    model = _load(cur["path"])
    end_ts = int(conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0])
    X, y, ots, keys = dataset(sanpada_series(conn, end_ts - history_days * 86400, end_ts))
    _, _, te, bounds = chrono_split(ots, bounds=json.loads(lock))
    if not te.any():
        return None
    Xt, yt, kt = X[te], y[te], keys[te]
    best = cur["metrics"]["best_model"]
    pred = model.predict_parts(Xt)[best]
    base = baselines(Xt)
    base_name = min(base, key=lambda n: np.abs(base[n] - yt).mean())
    err = np.abs(pred - yt) * WINDOW
    berr = np.abs(base[base_name] - yt) * WINDOW
    lead_col = FEATURES.index("lead_h")
    by_lead = []
    for k in sorted(set(Xt[:, lead_col].astype(int))):
        m = Xt[:, lead_col] == k
        by_lead.append({"hours_ahead": int(k), "model": round(float(err[m].mean()), 2),
                        "baseline": round(float(berr[m].mean()), 2), "n": int(m.sum())})
    site = {b["id"]: b["site_type"] for b in db.bins(conn, active_only=False)}
    by_site: dict[str, list] = {}
    for i, key in enumerate(kt):
        by_site.setdefault(site.get(str(key), "?"), []).append(i)
    by_type = [{"site_type": t, "model": round(float(err[ix].mean()), 2), "baseline": round(float(berr[ix].mean()), 2),
                "n": len(ix)} for t, ix in sorted(by_site.items())]
    within5 = float((err <= 5).mean())
    return {"label": "SYNTHETIC demonstration data", "model_id": cur["id"], "model": best,
            "test_from": datetime.fromtimestamp(bounds["test_start"], config.IST).date().isoformat(),
            "test_to": datetime.fromtimestamp(bounds["test_end"], config.IST).date().isoformat(),
            "rows": int(te.sum()), "model_mae": round(float(err.mean()), 2),
            "baseline": base_name, "baseline_mae": round(float(berr.mean()), 2),
            "improvement_pct": round((1 - err.mean() / berr.mean()) * 100, 1),
            "share_within_5_points": round(within5, 3), "by_lead": by_lead, "by_site_type": by_type}
