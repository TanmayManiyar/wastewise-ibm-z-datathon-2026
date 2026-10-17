import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wastewise import area, config, db, osm, synth  # noqa: E402

# Fixed "now" so tests are reproducible: 17 Oct 2026 05:30 IST (before the shift).
NOW_TS = 1792186200


def _osm_available() -> bool:
    return (config.DATA_DIR / osm.ROADS_FILE).exists() and (config.DATA_DIR / osm.POIS_FILE).exists()


@pytest.fixture(scope="session")
def template_db(tmp_path_factory) -> Path:
    if not _osm_available():
        try:
            osm.download()
        except Exception as exc:  # pragma: no cover - offline machine
            pytest.skip(f"OSM extract not available and download failed: {exc}")
    path = tmp_path_factory.mktemp("tpl") / "template.db"
    conn = db.connect(path)
    db.init(conn)
    area.setup(conn)
    synth.generate(conn, end_ts=NOW_TS, days=60)
    conn.close()
    return path


@pytest.fixture()
def conn(template_db, tmp_path):
    path = tmp_path / "test.db"
    shutil.copy(template_db, path)
    c = db.connect(path)
    yield c
    c.close()


@pytest.fixture()
def empty_conn(tmp_path):
    c = db.connect(tmp_path / "empty.db")
    db.init(c)
    yield c
    c.close()
