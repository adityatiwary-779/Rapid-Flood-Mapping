"""Write SYNTHETIC risk packages (same format as tools/build_risk_package.py) so the site can be built and tested
without Earth Engine or OpenStreetMap. Everything here is invented and flagged "synthetic" in the file.

    python tools/make_synthetic_risk_package.py [--out public/data/risk]
"""
import argparse
import json
import math
import pathlib

import numpy as np

AREAS = [
    dict(id="pathanamthitta", name="Pathanamthitta", state="Kerala", bounds=[9.0, 76.6, 9.5, 77.2],
         calamities=["flood", "landslide"], events="Floods 2018; landslides in the eastern hills", terrain="hilly_east", seed=1),
    dict(id="alappuzha", name="Alappuzha", state="Kerala", bounds=[9.2, 76.25, 9.7, 76.65],
         calamities=["flood"], events="Floods 2018", terrain="coastal_low", seed=2),
    dict(id="wayanad", name="Wayanad", state="Kerala", bounds=[11.45, 75.8, 11.95, 76.4],
         calamities=["landslide"], events="Landslides 2019 and 2024", terrain="highland", seed=3),
]
GRID = 60
LAYER_ORDER = ["flood", "lowland", "slope", "rainfall", "landcover", "population"]


def smooth(rng, n, k=6):
    """Low-frequency random field in 0..1."""
    y, x = np.mgrid[0:n, 0:n] / n
    f = np.zeros((n, n))
    for _ in range(k):
        fx, fy, px, py = rng.uniform(1, 4, 2).tolist() + rng.uniform(0, 6.28, 2).tolist()
        f += rng.uniform(.5, 1) * np.sin(2 * math.pi * fx * x + px) * np.cos(2 * math.pi * fy * y + py)
    return (f - f.min()) / (f.max() - f.min() + 1e-9)


def norm(a):
    return (a - a.min()) / (a.max() - a.min() + 1e-9)


def layers_for(area, rng):
    n = GRID
    y, x = np.mgrid[0:n, 0:n] / (n - 1)  # y: 0 = north
    base = smooth(rng, n)
    if area["terrain"] == "hilly_east":
        elev = 0.65 * x + 0.35 * base
    elif area["terrain"] == "coastal_low":
        elev = 0.15 * (x * 0.5 + 0.5 * base) + 0.05 * (1 - x)
    else:
        elev = 0.5 + 0.5 * base
    elev = norm(elev)
    gy, gx = np.gradient(elev)
    slope = norm(np.hypot(gx, gy))
    lowland = 1 - elev
    river = np.exp(-(((y - (0.4 + 0.2 * np.sin(5 * x))) / 0.07) ** 2))  # a meandering river corridor
    flood = norm(0.6 * lowland * (0.4 + river) + 0.4 * river)
    rain = norm(0.5 * x + 0.5 * smooth(rng, n))
    cover = smooth(rng, n)
    pop = np.zeros((n, n))
    for _ in range(7):
        cx, cy = rng.uniform(0.1, 0.9, 2)
        pop += rng.uniform(.4, 1) * np.exp(-(((x - cx) ** 2 + (y - cy) ** 2) / 0.012))
    pop = norm(pop) * (1 - 0.5 * slope)
    out = dict(flood=flood, lowland=lowland, slope=slope, rainfall=rain, landcover=cover, population=norm(pop))
    return {k: np.round(v, 2).ravel().tolist() for k, v in out.items()}


def haversine(a, b):
    r = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def road_graph(area, rng, m=24):
    s, w, nn, e = area["bounds"]
    nodes, idx = [], {}
    for i in range(m):
        for j in range(m):
            lat = nn - (i + rng.uniform(-.3, .3)) * (nn - s) / (m - 1)
            lon = w + (j + rng.uniform(-.3, .3)) * (e - w) / (m - 1)
            idx[i, j] = len(nodes)
            nodes.append([round(min(nn, max(s, lat)), 5), round(min(e, max(w, lon)), 5)])
    edges = []
    for i in range(m):
        for j in range(m):
            for di, dj in ((0, 1), (1, 0), (1, 1)):
                a, b = (i, j), (i + di, j + dj)
                if b not in idx or (rng.random() < .22 and (di, dj) != (0, 1) and j % 3):
                    continue
                edges.append([idx[a], idx[b], round(haversine(nodes[idx[a]], nodes[idx[b]]) * rng.uniform(1.0, 1.25), 1)])
    return nodes, edges


def build(area):
    rng = np.random.default_rng(area["seed"])
    nodes, edges = road_graph(area, rng)
    picks = rng.choice(len(nodes), 8, replace=False)
    pois = [dict(name=("Hospital %d" if k < 4 else "Shelter %d") % (k % 4 + 1), type="hospital" if k < 4 else "shelter",
                 lat=nodes[p][0], lon=nodes[p][1]) for k, p in enumerate(picks)]
    return dict(
        id=area["id"], name=area["name"], state=area["state"], bounds=area["bounds"], rows=GRID, cols=GRID,
        calamities=area["calamities"], events=area["events"],
        layers=layers_for(area, rng), graph=dict(nodes=nodes, edges=edges), pois=pois,
        provenance=dict(kind="synthetic",
                        note="SYNTHETIC placeholder data for developing the interface. Not real hazard, road or facility data."),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="public/data/risk")
    out = pathlib.Path(ap.parse_args().out)
    out.mkdir(parents=True, exist_ok=True)
    index = []
    for a in AREAS:
        pkg = build(a)
        (out / f"{a['id']}.json").write_text(json.dumps(pkg, separators=(",", ":")))
        index.append(dict(id=a["id"], name=a["name"], state=a["state"], calamities=a["calamities"], events=a["events"],
                          kind="synthetic"))
        print(f"{a['id']}: {(out / (a['id'] + '.json')).stat().st_size // 1024} KB, {len(pkg['graph']['nodes'])} nodes")
    (out / "index.json").write_text(json.dumps(dict(areas=index), indent=1))


if __name__ == "__main__":
    main()
