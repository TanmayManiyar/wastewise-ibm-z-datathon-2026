"""Detect the machine WasteWise is running on (architecture, IBM Z model, Telum).

Telum (the processor with the on-chip Integrated Accelerator for AI) ships in
IBM z16 / LinuxONE 4 (machine types 3931, 3932) and IBM z17 / LinuxONE 5
(9175, 9176, Telum II). Linux reports the accelerator as the ``nnpa``
(Neural Network Processing Assist) CPU feature, which is the authoritative
check; the machine type is reported alongside it.
"""
from __future__ import annotations

import platform
import re
import subprocess
import sys
from functools import lru_cache

MACHINE_TYPES = {
    "2964": "IBM z13", "2965": "IBM z13s",
    "3906": "IBM z14", "3907": "IBM z14 ZR1 / LinuxONE Rockhopper II",
    "8561": "IBM z15 T01 / LinuxONE III LT1", "8562": "IBM z15 T02 / LinuxONE III LT2",
    "3931": "IBM z16 A01 / LinuxONE Emperor 4", "3932": "IBM z16 A02 / LinuxONE Rockhopper 4",
    "9175": "IBM z17 ME1 / LinuxONE Emperor 5", "9176": "IBM z17 / LinuxONE 5",
}
TELUM_TYPES = {"3931", "3932", "9175", "9176"}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _lscpu() -> str:
    try:
        return subprocess.run(["lscpu"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


@lru_cache(maxsize=1)
def detect() -> dict:
    arch = platform.machine()
    cpuinfo = _read("/proc/cpuinfo")
    machine_type = None
    m = re.search(r"machine\s*=\s*([0-9A-Fa-f]{4})", cpuinfo)
    if m:
        machine_type = m.group(1)
    features: list[str] = []
    m = re.search(r"^features\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
    if m:
        features = m.group(1).split()
    nnpa = "nnpa" in features
    telum = nnpa or (machine_type in TELUM_TYPES)
    lscpu = _lscpu()
    model_name = None
    m = re.search(r"^Model name:\s*(.+)$", lscpu, re.MULTILINE)
    if m:
        model_name = m.group(1).strip()
    return {
        "arch": arch,
        "byteorder": sys.byteorder,
        "is_s390x": arch == "s390x",
        "machine_type": machine_type,
        "machine_name": MACHINE_TYPES.get(machine_type or "", None),
        "cpu_model_name": model_name,
        "nnpa_feature": nnpa,
        "telum": telum,
        "python": platform.python_version(),
        "system": f"{platform.system()} {platform.release()}",
    }


def badge_text(llm_name: str | None = None) -> str:
    """Short header badge, e.g. 'IBM LinuxONE s390x · Telum yes · Granite'."""
    p = detect()
    if p["is_s390x"]:
        base = "IBM LinuxONE s390x"
        if p["machine_name"]:
            base += f" ({p['machine_name']})"
        tel = "Telum yes" if p["telum"] else "Telum no"
        parts = [base, tel]
    else:
        parts = [f"{p['system']} {p['arch']} (not LinuxONE)"]
    if llm_name:
        parts.append(llm_name)
    return " · ".join(parts)


if __name__ == "__main__":
    import json
    print(json.dumps(detect(), indent=2))
