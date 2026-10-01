const test = require('node:test'); const assert = require('node:assert');
const E = require('../../public/risk/engine.js');

test('weights are normalised and negatives ignored', () => {
  const w = E.normalizeWeights({ flood: 2, lowland: 2, slope: -5, rainfall: 0, landcover: 0 });
  assert.strictEqual(w.flood, 0.5); assert.strictEqual(w.slope, 0);
  assert.deepStrictEqual(E.normalizeWeights({}), { flood: 0, lowland: 0, slope: 0, rainfall: 0, landcover: 0 });
});

test('risk = hazard * exposure; no population keeps the floor', () => {
  const L = { flood: [1, 1], population: [1, 0] };
  const r = E.computeRisk(L, { flood: 1 }, 2);
  assert.ok(Math.abs(r.risk[0] - 1) < 1e-6); assert.ok(Math.abs(r.risk[1] - E.EXPOSURE_FLOOR) < 1e-6);
});

test('quantile', () => { assert.strictEqual(E.quantile([1, 2, 3, 4, 5], 0.5), 3); assert.strictEqual(E.quantile([0, 10], 0.25), 2.5); });

test('grid sampling is north-up and clamps outside bounds', () => {
  const g = [1, 2, 3, 4]; const b = [0, 0, 2, 2]; // 2x2, row0 = north
  assert.strictEqual(E.sampleGrid(g, 2, 2, b, 1.9, 0.1), 1);
  assert.strictEqual(E.sampleGrid(g, 2, 2, b, 0.1, 1.9), 4);
  assert.strictEqual(E.sampleGrid(g, 2, 2, b, 99, -99), 1);
});

// square: 0-1-2 along the top (short, risky), 0-3-4-2 around the bottom (longer, safe)
const graph = { nodes: [[0, 0], [0, 1], [0, 2], [-1, 0], [-1, 2]], edges: [[0, 1, 100], [1, 2, 100], [0, 3, 120], [3, 4, 200], [4, 2, 120]] };
test('shortest vs least-risk diverge when the short route is risky', () => {
  const nr = [0, 0.9, 0, 0.05, 0.05];
  const lowAlpha = E.compareRoutes(graph, nr, 0, [2], 0, 0.5);
  assert.deepStrictEqual(lowAlpha.shortest.path.nodes, [0, 1, 2]);
  assert.deepStrictEqual(lowAlpha.safest.path.nodes, [0, 1, 2]); // alpha 0 = plain shortest
  const r = E.compareRoutes(graph, nr, 0, [2], 5, 0.4);
  assert.deepStrictEqual(r.shortest.path.nodes, [0, 1, 2]);
  assert.deepStrictEqual(r.safest.path.nodes, [0, 3, 4, 2]);
  assert.ok(r.safest.stats.max_risk < r.shortest.stats.max_risk);
  assert.ok(r.safest.stats.length_m > r.shortest.stats.length_m);
  assert.strictEqual(r.shortest.stats.high_risk_m, 200);
});

test('picks the best facility per mode; unreachable gives null', () => {
  const g = { nodes: [[0, 0], [0, 1], [5, 5]], edges: [[0, 1, 10]] };
  const r = E.compareRoutes(g, [0, 0, 0], 0, [1], 1, 0.5);
  assert.strictEqual(r.safest.dest, 1);
  assert.strictEqual(E.compareRoutes(g, [0, 0, 0], 0, [2], 1, 0.5).shortest, null);
});

test('nearestNode', () => { assert.strictEqual(E.nearestNode(graph.nodes, -0.9, 1.9), 4); });
