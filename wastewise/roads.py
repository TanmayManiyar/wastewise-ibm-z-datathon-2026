"""Road network from OpenStreetMap: directed graph and fastest-path matrices.

* One-way streets (oneway=yes/-1, roundabouts, motorways by OSM convention)
  are respected.
* Each road class has a typical loaded-truck speed (Settings), capped by an
  explicit maxspeed tag.
* Trucks may drive over flyovers but never stop on them: bins, the depot and
  the landfill snap only to nodes on stoppable roads, inside the largest
  strongly connected part of the network (so every stop can be reached and
  left again).
* Matrices hold the time and the distance of the *fastest* path between every
  pair of stops; predecessor trees let the map draw each route on the streets.
Pure Python + numpy so it runs unchanged on s390x.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config, geo, osm

STOP_CLASSES = {"secondary", "tertiary", "unclassified", "residential", "living_street", "service", "road"}
NO_STOP_NAME = re.compile(r"flyover|bridge|underpass|subway|\bFOB\b|ramp", re.I)
IMPLIED_ONEWAY = {"motorway", "motorway_link"}


def _speed_class(hw: str) -> str:
    return "link" if hw.endswith("_link") else hw


def _maxspeed(tag: str | None) -> float | None:
    if not tag:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)", tag)
    if not m:
        return None
    v = float(m.group(1))
    return v * 1.609 if "mph" in tag else v


@dataclass
class Graph:
    node_ids: np.ndarray            # OSM node id per index
    lat: np.ndarray
    lon: np.ndarray
    adj: list[list[tuple[int, float, float]]]   # u -> [(v, seconds, metres)]
    stoppable: np.ndarray           # bool per node
    edge_name: dict[tuple[int, int], str] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.node_ids)

    def n_edges(self) -> int:
        return sum(len(a) for a in self.adj)


def build_graph(roads: dict, speeds_kmh: dict[str, float]) -> Graph:
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in roads["elements"] if e["type"] == "node"}
    index: dict[int, int] = {}
    lat, lon, ids = [], [], []

    def idx(nid: int) -> int:
        if nid not in index:
            index[nid] = len(ids)
            ids.append(nid)
            lat.append(nodes[nid][0])
            lon.append(nodes[nid][1])
        return index[nid]

    edges: list[tuple[int, int, float, float, str]] = []
    stop_nodes: set[int] = set()
    for w in roads["elements"]:
        if w["type"] != "way":
            continue
        t = w.get("tags", {})
        hw = t.get("highway")
        if not hw:
            continue
        kmh = float(speeds_kmh.get(_speed_class(hw), speeds_kmh.get("road", 14)))
        ms = _maxspeed(t.get("maxspeed"))
        if ms:
            kmh = min(kmh, ms)
        mps = kmh / 3.6
        ow = t.get("oneway", "")
        forward_only = ow in ("yes", "true", "1") or t.get("junction") in ("roundabout", "circular") \
            or (hw in IMPLIED_ONEWAY and ow != "no")
        reverse_only = ow == "-1"
        can_stop = (hw in STOP_CLASSES and not t.get("bridge") and not t.get("tunnel")
                    and t.get("layer", "0") in ("0", "") and not NO_STOP_NAME.search(t.get("name", "")))
        seq = [n for n in w["nodes"] if n in nodes]
        name = t.get("name") or t.get("ref") or hw.replace("_", " ")
        for a, b in zip(seq, seq[1:]):
            ia, ib = idx(a), idx(b)
            d = geo.haversine_m(lat[ia], lon[ia], lat[ib], lon[ib])
            s = d / mps
            if not reverse_only:
                edges.append((ia, ib, s, d, name))
            if not forward_only:
                edges.append((ib, ia, s, d, name))
            if can_stop:
                stop_nodes.update((ia, ib))
    n = len(ids)
    adj: list[list[tuple[int, float, float]]] = [[] for _ in range(n)]
    names = {}
    for u, v, s, d, name in edges:
        adj[u].append((v, s, d))
        names[(u, v)] = name
    stoppable = np.zeros(n, dtype=bool)
    stoppable[list(stop_nodes)] = True
    return Graph(np.array(ids, dtype=np.int64), np.array(lat), np.array(lon), adj, stoppable, names)


def largest_scc(g: Graph) -> np.ndarray:
    """Boolean mask of the largest strongly connected component (iterative Kosaraju)."""
    n = g.n
    order, seen = [], np.zeros(n, dtype=bool)
    for s in range(n):
        if seen[s]:
            continue
        stack = [(s, 0)]
        seen[s] = True
        while stack:
            u, i = stack[-1]
            if i < len(g.adj[u]):
                stack[-1] = (u, i + 1)
                v = g.adj[u][i][0]
                if not seen[v]:
                    seen[v] = True
                    stack.append((v, 0))
            else:
                order.append(u)
                stack.pop()
    radj: list[list[int]] = [[] for _ in range(n)]
    for u in range(n):
        for v, _, _ in g.adj[u]:
            radj[v].append(u)
    comp = np.full(n, -1)
    c = 0
    for s in reversed(order):
        if comp[s] >= 0:
            continue
        stack = [s]
        comp[s] = c
        while stack:
            u = stack.pop()
            for v in radj[u]:
                if comp[v] < 0:
                    comp[v] = c
                    stack.append(v)
        c += 1
    sizes = np.bincount(comp)
    return comp == int(np.argmax(sizes))


def dijkstra(g: Graph, src: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fastest paths from src: (seconds, metres along that path, predecessor)."""
    n = g.n
    inf = float("inf")
    t = [inf] * n           # plain lists: much faster than numpy scalars in this loop
    d = [inf] * n
    pred = [-1] * n
    t[src] = 0.0
    d[src] = 0.0
    heap = [(0.0, src)]
    adj = g.adj
    while heap:
        tu, u = heapq.heappop(heap)
        if tu > t[u]:
            continue
        du = d[u]
        for v, s, m in adj[u]:
            tv = tu + s
            if tv < t[v]:
                t[v] = tv
                d[v] = du + m
                pred[v] = u
                heapq.heappush(heap, (tv, v))
    return np.array(t), np.array(d), np.array(pred, dtype=np.int64)


@dataclass
class Network:
    graph: Graph
    stop_ids: list[str]             # e.g. ["DEPOT", "LANDFILL", "SNP-001", ...]
    stop_node: np.ndarray           # graph node index per stop
    snap_m: np.ndarray              # distance from the stop to its road node
    time_s: np.ndarray              # [i, j] fastest travel time
    dist_m: np.ndarray              # [i, j] distance along that fastest path
    preds: np.ndarray               # [i, node] predecessor tree from stop i

    def index(self, stop_id: str) -> int:
        return self.stop_ids.index(stop_id)

    def path(self, i: int, j: int) -> list[tuple[float, float]]:
        """Street geometry (lat, lon) of the fastest path from stop i to stop j."""
        g, src, dst = self.graph, int(self.stop_node[i]), int(self.stop_node[j])
        seq = [dst]
        while seq[-1] != src:
            p = int(self.preds[i, seq[-1]])
            if p < 0:
                return []
            seq.append(p)
        seq.reverse()
        return [(float(g.lat[k]), float(g.lon[k])) for k in seq]

    def path_streets(self, i: int, j: int) -> list[str]:
        g, src, dst = self.graph, int(self.stop_node[i]), int(self.stop_node[j])
        seq = [dst]
        while seq[-1] != src:
            p = int(self.preds[i, seq[-1]])
            if p < 0:
                return []
            seq.append(p)
        seq.reverse()
        out: list[str] = []
        for a, b in zip(seq, seq[1:]):
            name = g.edge_name.get((a, b), "")
            if name and (not out or out[-1] != name):
                out.append(name)
        return out


def snap(g: Graph, mask: np.ndarray, lat: float, lon: float) -> tuple[int, float]:
    cand = np.nonzero(mask)[0]
    p1, p2 = np.radians(lat), np.radians(g.lat[cand])
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(g.lon[cand] - lon) / 2) ** 2
    d = 2 * geo.EARTH_M * np.arcsin(np.sqrt(a))
    k = int(np.argmin(d))
    return int(cand[k]), float(d[k])


def build_network(stops: list[tuple[str, float, float]], speeds_kmh: dict, roads: dict | None = None) -> Network:
    roads = roads or osm.load(osm.ROADS_FILE)
    g = build_graph(roads, speeds_kmh)
    mask = largest_scc(g) & g.stoppable
    nodes, snaps = [], []
    for _, la, lo in stops:
        k, dm = snap(g, mask, la, lo)
        nodes.append(k)
        snaps.append(dm)
    m = len(stops)
    T = np.zeros((m, m))
    D = np.zeros((m, m))
    P = np.zeros((m, g.n), dtype=np.int32)
    node_arr = np.array(nodes)
    for i, k in enumerate(nodes):
        t, d, pred = dijkstra(g, k)
        T[i] = t[node_arr]
        D[i] = d[node_arr]
        P[i] = pred
    return Network(g, [s[0] for s in stops], node_arr, np.array(snaps), T, D, P)


# ---------------------------------------------------------------- cache
def stops_from_db(conn) -> list[tuple[str, float, float]]:
    from . import db
    p = db.places(conn)
    stops = [("DEPOT", p["depot"]["lat"], p["depot"]["lon"]),
             ("LANDFILL", p["landfill"]["lat"], p["landfill"]["lon"])]
    stops += [(b["id"], b["lat"], b["lon"]) for b in db.bins(conn, active_only=False)]
    return stops


def _cache_key(stops, speeds, roads_meta) -> str:
    blob = json.dumps([stops, speeds, roads_meta], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


_MEMO: dict[str, Network] = {}


def network_for(conn, cache_dir: Path | None = None) -> Network:
    """Network for the current bins/depot/landfill/speeds; cached in memory and on disk."""
    from . import db
    stops = [(s, round(a, 6), round(b, 6)) for s, a, b in stops_from_db(conn)]
    speeds = db.get_settings(conn)["speeds_kmh"]
    roads = osm.load(osm.ROADS_FILE)
    key = _cache_key(stops, speeds, roads.get("osm3s", {}).get("timestamp_osm_base"))
    if key in _MEMO:
        return _MEMO[key]
    cache_dir = Path(cache_dir or config.DATA_DIR)
    path = cache_dir / f"network_{key}.npz"
    g = build_graph(roads, speeds)
    if path.exists():
        z = np.load(path)
        net = Network(g, [str(s) for s in z["stop_ids"]], z["stop_node"], z["snap_m"], z["time_s"], z["dist_m"], z["preds"])
    else:
        net = build_network(stops, speeds, roads)
        np.savez_compressed(path, stop_ids=np.array(net.stop_ids), stop_node=net.stop_node, snap_m=net.snap_m,
                            time_s=net.time_s, dist_m=net.dist_m, preds=net.preds)
    _MEMO[key] = net
    return net
