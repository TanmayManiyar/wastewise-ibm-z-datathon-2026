"""One-click sign-in: the sign-in page lists user types; one click enters, no password."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

from wastewise import auth, db

APP = str(Path(__file__).resolve().parent.parent / "app" / "main.py")


def test_one_click_enters_without_password(conn, monkeypatch):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("WASTEWISE_DB", path)
    from wastewise import config
    monkeypatch.setattr(config, "DB_PATH", Path(path))
    if not auth.users(conn):
        auth.seed_users(conn, Path(path).parent)
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    labels = [b.label for b in at.button]
    assert any(l.startswith("Administrator") for l in labels) and any(l.startswith("Crew - Truck 1") for l in labels)
    assert not at.text_input                                   # no password field
    next(b for b in at.button if b.label.startswith("Crew - Truck 1")).click().run()
    assert at.session_state["user"]["role"] == "crew"
    assert db.one(conn, "SELECT detail FROM audit WHERE action='login' ORDER BY id DESC LIMIT 1")["detail"].count("one-click")


def test_passwords_can_be_switched_back_on(conn, monkeypatch):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    from wastewise import config
    monkeypatch.setattr(config, "DB_PATH", Path(path))
    with db.tx(conn):
        db.set_setting(conn, "passwordless_signin", False)
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    assert [t.label for t in at.text_input] == ["User name", "Password"]
