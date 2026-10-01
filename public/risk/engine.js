/* Multi-criteria risk index + least-risk routing. Pure functions, no DOM: runs in the browser and in node tests. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.RiskEngine = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var LAYERS = ['flood', 'lowland', 'slope', 'rainfall', 'landcover'];
  // Starting weights per calamity. These are judgement-based defaults, not fitted values; the sliders exist so users can change them.
  var PRESETS = {
    flood:     { flood: 0.40, lowland: 0.25, slope: 0.00, rainfall: 0.25, landcover: 0.10 },
    landslide: { flood: 0.00, lowland: 0.00, slope: 0.45, rainfall: 0.30, landcover: 0.25 },
    combined:  { flood: 0.25, lowland: 0.15, slope: 0.25, rainfall: 0.20, landcover: 0.15 }
  };
  var EXPOSURE_FLOOR = 0.25; // a cell with no people still keeps 25% of its hazard (roads and land are exposed too)

  function normalizeWeights(w) {
    var sum = 0, i, out = {};
    for (i = 0; i < LAYERS.length; i++) sum += Math.max(0, Number(w[LAYERS[i]]) || 0);
    for (i = 0; i < LAYERS.length; i++) {
      out[LAYERS[i]] = sum > 0 ? Math.max(0, Number(w[LAYERS[i]]) || 0) / sum : 0;
    }
    return out;
  }

  /** layers: {name: Array(rows*cols) in 0..1}; returns {hazard, risk} Float32Array. exposure layer is "population". */
  function computeRisk(layers, weights, n) {
    var w = normalizeWeights(weights), hazard = new Float32Array(n), risk = new Float32Array(n), i, k, h;
    var pop = layers.population;
    for (i = 0; i < n; i++) {
      h = 0;
      for (k = 0; k < LAYERS.length; k++) {
        if (w[LAYERS[k]] > 0 && layers[LAYERS[k]]) h += w[LAYERS[k]] * layers[LAYERS[k]][i];
      }
      hazard[i] = h;
      risk[i] = h * (EXPOSURE_FLOOR + (1 - EXPOSURE_FLOOR) * (pop ? pop[i] : 1));
    }
    return { hazard: hazard, risk: risk };
  }

  /** Value at quantile q (0..1) of an array, linear interpolation. */
  function quantile(arr, q) {
    var a = Array.prototype.slice.call(arr).sort(function (x, y) { return x - y; });
    if (!a.length) return NaN;
    var pos = Math.min(1, Math.max(0, q)) * (a.length - 1), lo = Math.floor(pos), hi = Math.ceil(pos);
    return a[lo] + (a[hi] - a[lo]) * (pos - lo);
  }

  /** bounds = [south, west, north, east]; grid is row-major, row 0 = north. Clamped to the edge cells. */
  function sampleGrid(grid, rows, cols, bounds, lat, lon) {
    var fy = (bounds[2] - lat) / (bounds[2] - bounds[0]), fx = (lon - bounds[1]) / (bounds[3] - bounds[1]);
    var r = Math.min(rows - 1, Math.max(0, Math.floor(fy * rows))), c = Math.min(cols - 1, Math.max(0, Math.floor(fx * cols)));
    return grid[r * cols + c];
  }

  function nodeRisks(nodes, riskGrid, rows, cols, bounds) {
    var out = new Float32Array(nodes.length), i;
    for (i = 0; i < nodes.length; i++) out[i] = sampleGrid(riskGrid, rows, cols, bounds, nodes[i][0], nodes[i][1]);
    return out;
  }

  /** edges: [[a, b, length_m], ...] treated as two-way. Returns adjacency arrays. */
  function buildAdjacency(nNodes, edges) {
    var adj = new Array(nNodes), i;
    for (i = 0; i < nNodes; i++) adj[i] = [];
    for (i = 0; i < edges.length; i++) {
      adj[edges[i][0]].push([edges[i][1], i]);
      adj[edges[i][1]].push([edges[i][0], i]);
    }
    return adj;
  }

  function Heap() { this.a = []; }
  Heap.prototype.push = function (d, v) {
    var a = this.a, i = a.length, p; a.push([d, v]);
    while (i > 0) { p = (i - 1) >> 1; if (a[p][0] <= a[i][0]) break; var t = a[p]; a[p] = a[i]; a[i] = t; i = p; }
  };
  Heap.prototype.pop = function () {
    var a = this.a, top = a[0], last = a.pop(), n = a.length, i = 0, l, r, m;
    if (n) {
      a[0] = last;
      for (;;) {
        l = 2 * i + 1; r = l + 1; m = i;
        if (l < n && a[l][0] < a[m][0]) m = l;
        if (r < n && a[r][0] < a[m][0]) m = r;
        if (m === i) break;
        var t = a[m]; a[m] = a[i]; a[i] = t; i = m;
      }
    }
    return top;
  };
  Heap.prototype.size = function () { return this.a.length; };

  /** Single-source Dijkstra. costFn(edgeIndex, fromNode, toNode) must be >= 0. Returns {dist, prev, prevEdge}. */
  function dijkstra(adj, src, costFn) {
    var n = adj.length, dist = new Float64Array(n).fill(Infinity), prev = new Int32Array(n).fill(-1), prevEdge = new Int32Array(n).fill(-1);
    var h = new Heap(), top, u, d, k, v, e, nd;
    dist[src] = 0; h.push(0, src);
    while (h.size()) {
      top = h.pop(); d = top[0]; u = top[1];
      if (d > dist[u]) continue;
      for (k = 0; k < adj[u].length; k++) {
        v = adj[u][k][0]; e = adj[u][k][1];
        nd = d + costFn(e, u, v);
        if (nd < dist[v]) { dist[v] = nd; prev[v] = u; prevEdge[v] = e; h.push(nd, v); }
      }
    }
    return { dist: dist, prev: prev, prevEdge: prevEdge };
  }

  function pathTo(res, dst) {
    if (!isFinite(res.dist[dst])) return null;
    var nodes = [dst], edges = [], c = dst;
    while (res.prev[c] !== -1) { edges.push(res.prevEdge[c]); c = res.prev[c]; nodes.push(c); }
    nodes.reverse(); edges.reverse();
    return { nodes: nodes, edges: edges };
  }

  function nearestNode(nodes, lat, lon) {
    var best = -1, bd = Infinity, i, dy, dx, d, k = Math.cos(lat * Math.PI / 180);
    for (i = 0; i < nodes.length; i++) {
      dy = nodes[i][0] - lat; dx = (nodes[i][1] - lon) * k; d = dy * dy + dx * dx;
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  /** Length-weighted route statistics. highThreshold is the node-risk value above which a stretch counts as high risk. */
  function routeStats(path, edges, nRisk, highThreshold) {
    var len = 0, rl = 0, mx = 0, hi = 0, i, e, a, b, r;
    for (i = 0; i < path.edges.length; i++) {
      e = edges[path.edges[i]]; a = nRisk[path.nodes[i]]; b = nRisk[path.nodes[i + 1]];
      r = (a + b) / 2; len += e[2]; rl += r * e[2];
      mx = Math.max(mx, a, b);
      if (r >= highThreshold) hi += e[2];
    }
    return { length_m: len, mean_risk: len ? rl / len : 0, max_risk: mx, high_risk_m: hi };
  }

  /**
   * Compare shortest vs least-risk route from `src` to the best facility.
   * alpha: how strongly risk inflates distance (cost = length * (1 + alpha * risk)).
   * destNodes: candidate facility node ids; the best one is chosen separately per mode.
   */
  function compareRoutes(graph, nRisk, src, destNodes, alpha, highThreshold) {
    var adj = graph._adj || (graph._adj = buildAdjacency(graph.nodes.length, graph.edges));
    var edges = graph.edges;
    function run(costFn) {
      var res = dijkstra(adj, src, costFn), best = -1, bd = Infinity, i;
      for (i = 0; i < destNodes.length; i++) if (res.dist[destNodes[i]] < bd) { bd = res.dist[destNodes[i]]; best = destNodes[i]; }
      if (best < 0) return null;
      var p = pathTo(res, best);
      return { dest: best, path: p, stats: routeStats(p, edges, nRisk, highThreshold) };
    }
    var shortest = run(function (e) { return edges[e][2]; });
    var safest = run(function (e, u, v) { return edges[e][2] * (1 + alpha * (nRisk[u] + nRisk[v]) / 2); });
    return { shortest: shortest, safest: safest };
  }

  return {
    LAYERS: LAYERS, PRESETS: PRESETS, EXPOSURE_FLOOR: EXPOSURE_FLOOR,
    normalizeWeights: normalizeWeights, computeRisk: computeRisk, quantile: quantile,
    sampleGrid: sampleGrid, nodeRisks: nodeRisks, buildAdjacency: buildAdjacency, dijkstra: dijkstra,
    pathTo: pathTo, nearestNode: nearestNode, routeStats: routeStats, compareRoutes: compareRoutes
  };
});
