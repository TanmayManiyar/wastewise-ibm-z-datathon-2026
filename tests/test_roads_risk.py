import numpy as np
import pytest

from wastewise import db, osm, risk, roads, state
from conftest import NOW_TS


@pytest.fixture(scope="module")
def net(template_db, tmp_path_factory):
    conn = db.connect(template_db)
    n = roads.network_for(conn, cache_dir=tmp_path_factory.mktemp("net"))
    conn.close()
    return n


def tiny_graph():
    # 0 -> 1 -> 2 one-way chain plus a slow two-way shortcut 0 <-> 2
    roads_json = {"elements": [
        {"type": "node", "id": 1, "lat": 19.0, "lon": 73.0},
        {"type": "node", "id": 2, "lat": 19.0, "lon": 73.001},
        {"type": "node", "id": 3, "lat": 19.0, "lon": 73.002},
        {"type": "way", "id": 10, "nodes": [1, 2, 3], "tags": {"highway": "primary", "oneway": "yes"}},
        {"type": "way", "id": 11, "nodes": [1, 3], "tags": {"highway": "residential"}},
        {"type": "way", "id": 12, "nodes": [2, 3], "tags": {"highway": "residential", "bridge": "yes"}},
    ]}
    return roads.build_graph(roads_json, {"primary": 30, "residential": 10, "road": 10})


def test_oneway_and_bridge_rules():
    g = tiny_graph()
    succ = {u: {v for v, _, _ in g.adj[u]} for u in range(g.n)}
    assert 1 in succ[0] and 0 not in succ[1]            # one-way primary
    assert 2 in succ[0] and 0 in succ[2]                # two-way residential
    # node 1 (id 2) lies only on the one-way primary and the bridge: no stopping there
    assert not g.stoppable[1] and g.stoppable[0] and g.stoppable[2]


def test_dijkstra_matches_brute_force():
    g = tiny_graph()
    t, d, pred = roads.dijkstra(g, 0)
    # brute force over the two possible routes 0->2
    via_primary = sum(min(s for (w, s, _) in g.adj[u] if w == v) for u, v in ((0, 1), (1, 2)))
    direct = next(s for w, s, _ in g.adj[0] if w == 2)
    assert t[2] == pytest.approx(min(via_primary, direct))
    # from the middle node, going back must go round via node 2 (no edge 1 -> 0)
    t1 = roads.dijkstra(g, 1)[0]
    back = next(s for w, s, _ in g.adj[1] if w == 2) + next(s for w, s, _ in g.adj[2] if w == 0)
    assert t1[0] == pytest.approx(back)


def test_real_oneways_respected(net):
    raw = osm.load(osm.ROADS_FILE)
    g = net.graph
    pos = {int(n): i for i, n in enumerate(g.node_ids)}
    checked = 0
    for w in raw["elements"]:
        t = w.get("tags", {})
        if w["type"] == "way" and t.get("oneway") == "yes" and t.get("highway") == "residential":
            a, b = w["nodes"][0], w["nodes"][1]
            ia, ib = pos[a], pos[b]
            assert ib in {v for v, _, _ in g.adj[ia]}
            assert ia not in {v for v, _, _ in g.adj[ib]}
            checked += 1
    assert checked > 0


def test_matrix_properties(net):
    T, D = net.time_s, net.dist_m
    assert np.isfinite(T).all() and np.isfinite(D).all()
    assert np.allclose(np.diag(T), 0)
    m = len(T)
    # fastest paths satisfy the triangle inequality
    viol = T[:, None, :] > T[:, :, None] + T[None, :, :] + 1e-6
    assert not viol.any()
    assert (T[~np.eye(m, dtype=bool)] > 0).all()


def test_stops_never_on_flyovers(net):
    g = net.graph
    assert g.stoppable[net.stop_node].all()
    assert net.snap_m[2:].max() < 120   # bins sit at the kerb


def test_path_geometry_matches_matrix(net):
    i, j = net.index("DEPOT"), net.index("LANDFILL")
    pts = net.path(i, j)
    g = net.graph
    assert (pts[0][0], pts[0][1]) == (g.lat[net.stop_node[i]], g.lon[net.stop_node[i]])
    assert (pts[-1][0], pts[-1][1]) == (g.lat[net.stop_node[j]], g.lon[net.stop_node[j]])
    assert 1000 < net.dist_m[i, j] < 8000   # Sanpada to Turbhe landfill is a few km
    assert net.path_streets(i, j)


# ---------------------------------------------------------------- risk
def test_risk_curves_monotone_and_overflow_minutes():
    ids = ["A", "B", "C"]
    mu0 = np.array([20.0, 95.0, 120.0])
    sd0 = np.array([3.0, 3.0, 3.0])
    rates = np.full((3, 6), 5.0)
    rc = risk.build(ids, mu0, sd0, rates, start_ts=0, horizon_min=480)
    assert (np.diff(rc.p_full, axis=1) >= -1e-6).all()
    assert (np.diff(rc.cum_overflow_min, axis=1) >= -1e-9).all()
    assert rc.overflow_min_at(2, 3600) == pytest.approx(60, abs=1)    # already overflowing
    assert rc.overflow_min_at(0, 3600) == pytest.approx(0, abs=0.1)   # far from full
    assert rc.full_eta(1) == pytest.approx(3600, abs=600)             # 95 % + 5 %/h
    assert rc.status_at(2, 0) == "red"
    assert rc.status_at(1, 2 * 3600) == "orange"
    assert rc.status_at(0, 7 * 3600) == "amber"
    assert rc.status_at(0, 0) == "green"
    assert rc.overflowing_since[2] == pytest.approx(-4 * 3600)


def test_state_estimate_close_to_truth(conn):
    prof = state.rate_profiles(conn, NOW_TS)
    st = state.estimate(conn, NOW_TS, prof)
    truth = dict(conn.execute("SELECT bin_id, fill_pct FROM synthetic_truth WHERE ts = "
                              "(SELECT MAX(ts) FROM synthetic_truth WHERE ts <= ?)", (NOW_TS,)).fetchall())
    sensor = [b["id"] for b in db.bins(conn) if b["has_sensor"]]
    mae = np.mean([abs(st[b].fill - truth[b]) for b in sensor])
    assert mae < 10


def test_state_uses_latest_manual_reading(conn):
    prof = state.rate_profiles(conn, NOW_TS)
    db.add_reading(conn, "SNP-010", 42.0, "supervisor", ts=NOW_TS - 60, user="supervisor")
    st = state.estimate(conn, NOW_TS, prof)["SNP-010"]
    assert st.basis == "supervisor" and 42 <= st.fill < 46
