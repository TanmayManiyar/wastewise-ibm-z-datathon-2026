"""Plans made in the background.

Making a plan takes about half a minute on the LinuxONE VM. It runs in a background thread so the
page stays usable: the user can look at other tabs while the progress (route variations tried, the
best distance found so far) updates. The finished plan is saved by the thread itself, so it is not
lost if the browser is closed. One job per plan day and round at a time, shared by all users: a
second person pressing Generate sees the plan already being made.
"""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

from . import db, planner

PHASE_TEXT = {"queued": "Starting", "data": "Reading the latest data and forecasting how full each bin gets",
              "search": "Trying route variations", "check": "Checking the plan independently",
              "saving": "Saving the plan"}


@dataclass
class Job:
    key: str                    # e.g. "main:2026-10-18" or "extra:2026-10-18"
    label: str                  # "plan", "re-plan", "evening round"
    user: str
    started: float = field(default_factory=time.time)
    phase: str = "queued"
    iterations: int = 0
    best_km: float | None = None
    status: str = "running"     # running | done | failed
    plan_id: int | None = None
    error: str | None = None
    finished: float | None = None
    expected_s: float = 30.0

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started

    def fraction(self) -> float:
        """Rough share done, for a progress bar (the search stops when it stops finding better plans)."""
        if self.status != "running":
            return 1.0
        return min(0.95, self.elapsed / max(self.expected_s, 1.0))


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def get(key: str) -> Job | None:
    return _JOBS.get(key)


def running(key: str) -> Job | None:
    j = _JOBS.get(key)
    return j if j and j.status == "running" else None


def start(key: str, label: str, user: str, make: Callable[..., dict], db_path=None) -> Job:
    """make(conn, progress) builds the plan dict; it is saved as a draft when done.
    Returns the running job for this key if there already is one."""
    with _LOCK:
        cur = running(key)
        if cur:
            return cur
        job = Job(key, label, user)
        conn0 = db.connect(db_path)
        try:
            job.expected_s = float(db.get_meta(conn0, f"plan_seconds:{label}", "30") or 30)
        finally:
            conn0.close()
        _JOBS[key] = job

    def progress(phase: str, iterations: int | None, best_km: float | None) -> None:
        job.phase = phase
        if iterations is not None:
            job.iterations = iterations
        if best_km is not None:
            job.best_km = best_km

    def run() -> None:
        conn = db.connect(db_path)
        try:
            plan = make(conn, progress)
            job.phase = "saving"
            job.plan_id = planner.save(conn, plan, user)
            job.status = "done"
            db.set_meta(conn, f"plan_seconds:{label}", f"{time.time() - job.started:.1f}")
        except planner.PlanError as exc:
            job.status, job.error = "failed", str(exc)
        except Exception as exc:  # shown to the user in plain words; details go to the server log
            traceback.print_exc()
            job.status, job.error = "failed", f"The plan could not be made ({type(exc).__name__}). Please try again."
        finally:
            job.finished = time.time()
            conn.close()

    threading.Thread(target=run, daemon=True, name=f"wastewise-plan-{key}").start()
    return job


def wait(key: str, timeout: float = 120.0) -> Job | None:
    """For tests and scripts."""
    t = time.time() + timeout
    while time.time() < t:
        j = _JOBS.get(key)
        if j is None or j.status != "running":
            return j
        time.sleep(0.1)
    return _JOBS.get(key)
