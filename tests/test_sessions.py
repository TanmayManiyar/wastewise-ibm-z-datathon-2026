"""Stay signed in: the signed session cookie."""
from wastewise import auth, db, sessions

T = 1792186200


def _users(c):
    auth.create_user(c, "op1", "Operator", "operator", password="pw-123456")
    auth.create_user(c, "crewx", "Crew", "crew", vehicle_id="TRK-1", password="pw-123456")


def test_cookie_round_trip_and_lifetimes(empty_conn, tmp_path):
    _users(empty_conn)
    tok, life = sessions.issue("op1", "operator", now_ts=T, secrets_dir=tmp_path)
    assert life == 8 * 3600
    u = sessions.check(empty_conn, tok, now_ts=T + 3600, secrets_dir=tmp_path)
    assert u["username"] == "op1" and u["role"] == "operator"
    assert sessions.check(empty_conn, tok, now_ts=T + 8 * 3600, secrets_dir=tmp_path) is None   # expired
    tok, life = sessions.issue("crewx", "crew", now_ts=T, secrets_dir=tmp_path)
    assert life == 12 * 3600
    assert sessions.check(empty_conn, tok, now_ts=T + 11 * 3600, secrets_dir=tmp_path)["vehicle_id"] == "TRK-1"


def test_forged_or_tampered_cookie_is_rejected(empty_conn, tmp_path):
    _users(empty_conn)
    tok, _ = sessions.issue("op1", "operator", now_ts=T, secrets_dir=tmp_path)
    body, sig = tok.split(".")
    forged_body = sessions._b64(b'{"u":"admin","iat":1792186200000,"exp":1892186200}')
    assert sessions.check(empty_conn, f"{forged_body}.{sig}", now_ts=T, secrets_dir=tmp_path) is None
    assert sessions.check(empty_conn, body + "." + sig[:-2] + "AA", now_ts=T, secrets_dir=tmp_path) is None
    assert sessions.check(empty_conn, "garbage", now_ts=T, secrets_dir=tmp_path) is None
    other = tmp_path / "other"
    assert sessions.check(empty_conn, tok, now_ts=T, secrets_dir=other) is None    # different server key


def test_sign_out_and_deactivation_end_the_session(empty_conn, tmp_path):
    _users(empty_conn)
    tok, _ = sessions.issue("op1", "operator", now_ts=T, secrets_dir=tmp_path)
    sessions.revoke(empty_conn, "op1", now_ts=T + 60)
    assert sessions.check(empty_conn, tok, now_ts=T + 120, secrets_dir=tmp_path) is None
    new, _ = sessions.issue("op1", "operator", now_ts=T + 120, secrets_dir=tmp_path)   # signing in again works
    assert sessions.check(empty_conn, new, now_ts=T + 130, secrets_dir=tmp_path)
    with db.tx(empty_conn):
        empty_conn.execute("UPDATE users SET active=0 WHERE username='op1'")
    assert sessions.check(empty_conn, new, now_ts=T + 140, secrets_dir=tmp_path) is None
