(function () {
  'use strict';
  var E = window.RiskEngine;
  var LABELS = { flood: 'Flood susceptibility', lowland: 'Low-lying land', slope: 'Slope (landslide)', rainfall: 'Rainfall', landcover: 'Land-cover vulnerability' };
  var CAL_LABEL = { flood: 'Flood', landslide: 'Landslide', combined: 'Combined (flood + landslide)' };
  var $ = function (id) { return document.getElementById(id); };
  var S = { pkg: null, weights: null, risk: null, nRisk: null, threshold: 1, start: null, map: null, layers: {}, markers: [] };

  function toast(msg) { var t = $('toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(function () { t.hidden = true; }, 6000); }
  function el(tag, text, cls) { var e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
  function fmtKm(m) { return (m / 1000).toFixed(m < 10000 ? 2 : 1) + ' km'; }

  function ramp(v) { // 0..1 -> blue, yellow, red
    var a = v < 0.5 ? [44, 127, 184] : [254, 217, 118], b = v < 0.5 ? [254, 217, 118] : [227, 26, 28], t = v < 0.5 ? v * 2 : (v - 0.5) * 2;
    return [0, 1, 2].map(function (i) { return Math.round(a[i] + (b[i] - a[i]) * t); });
  }

  function gridCanvas(pkg, fill) {
    var c = document.createElement('canvas'); c.width = pkg.cols; c.height = pkg.rows;
    var ctx = c.getContext('2d'), img = ctx.createImageData(pkg.cols, pkg.rows), i, px;
    for (i = 0; i < pkg.rows * pkg.cols; i++) { px = fill(i); img.data[4 * i] = px[0]; img.data[4 * i + 1] = px[1]; img.data[4 * i + 2] = px[2]; img.data[4 * i + 3] = px[3]; }
    ctx.putImageData(img, 0, 0);
    return c.toDataURL();
  }

  function bounds() { var b = S.pkg.bounds; return [[b[0], b[1]], [b[2], b[3]]]; }

  function recompute() {
    var pkg = S.pkg, n = pkg.rows * pkg.cols;
    var res = E.computeRisk(pkg.layers, S.weights, n), inside = pkg.layers.inside, vals = [], j;
    S.risk = res.risk;
    for (j = 0; j < n; j++) if (!inside || inside[j]) vals.push(res.risk[j]);   // cells outside the district boundary are ignored
    S.threshold = E.quantile(vals, 1 - Number($('topPct').value) / 100);
    S.nRisk = E.nodeRisks(pkg.graph.nodes, res.risk, pkg.rows, pkg.cols, pkg.bounds);
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), span = hi - lo || 1;
    S.lo = lo; S.span = span;
    var op = Number($('opacity').value) / 100;
    if (S.layers.risk) S.map.removeLayer(S.layers.risk);
    if (S.layers.zones) S.map.removeLayer(S.layers.zones);
    S.layers.risk = L.imageOverlay(gridCanvas(pkg, function (i) { if (inside && !inside[i]) return [0, 0, 0, 0]; var c = ramp(Math.min(1, Math.max(0, (res.risk[i] - lo) / span))); return [c[0], c[1], c[2], 255]; }), bounds(), { opacity: op, pane: 'riskPane', interactive: false }).addTo(S.map);
    S.layers.zones = L.imageOverlay(gridCanvas(pkg, function (i) { return (!inside || inside[i]) && res.risk[i] >= S.threshold ? [255, 255, 255, 70] : [0, 0, 0, 0]; }), bounds(), { opacity: 1, pane: 'riskPane', interactive: false }).addTo(S.map);
    kpis(); route();
  }

  function kpis() {
    var pkg = S.pkg, n = pkg.rows * pkg.cols, b = pkg.bounds, kmLat = (b[2] - b[0]) * 111.32, kmLon = (b[3] - b[1]) * 111.32 * Math.cos((b[0] + b[2]) / 2 * Math.PI / 180);
    var cell = kmLat * kmLon / n, hi = 0, popAll = 0, popHi = 0, cells = 0, i, p = pkg.layers.population || [], inside = pkg.layers.inside;
    for (i = 0; i < n; i++) { if (inside && !inside[i]) continue; cells++; popAll += p[i] || 0; if (S.risk[i] >= S.threshold) { hi++; popHi += p[i] || 0; } }
    var items = [['High-risk area', (hi * cell).toFixed(0) + ' km²'], ['Share of district', (100 * hi / (cells || 1)).toFixed(0) + '%'],
      ['Exposure in high-risk zones', popAll ? (100 * popHi / popAll).toFixed(0) + '% of population index' : 'n/a'],
      ['Road network', hasRoads() ? pkg.graph.nodes.length + ' nodes, ' + pkg.graph.edges.length + ' segments' : 'not loaded yet'], ['Facilities', pkg.pois.length + ' mapped']];
    var dl = $('kpis'); dl.textContent = '';
    items.forEach(function (it) { var d = el('div'); d.appendChild(el('dt', it[0])); d.appendChild(el('dd', it[1])); dl.appendChild(d); });
  }

  function hasRoads() { return S.pkg.graph.nodes.length > 0 && S.pkg.pois.length > 0; }

  function clearRoute() {
    ['shortest', 'safest', 'startPin'].forEach(function (k) { if (S.layers[k]) { S.map.removeLayer(S.layers[k]); delete S.layers[k]; } });
    $('routeTable').hidden = true; $('routeMsg').hidden = false; $('routeMsg').textContent = 'No route yet. Click the map or press “Try an example”.';
  }

  function line(path, color, dash) {
    var nodes = S.pkg.graph.nodes;
    return L.polyline(path.nodes.map(function (n) { return nodes[n]; }), { color: color, weight: dash ? 4 : 5, opacity: .95, dashArray: dash || null, pane: 'routePane' });
  }

  function route() {
    if (!S.start) return;
    if (!hasRoads()) { $('routeMsg').hidden = false; $('routeMsg').textContent = 'This district has risk layers but no road network or facilities yet, so routes are unavailable.'; $('routeTable').hidden = true; return; }
    var pkg = S.pkg, f = $('facility').value, dests = [];
    pkg.pois.forEach(function (p) { if (f === 'any' || p.type === f) dests.push(E.nearestNode(pkg.graph.nodes, p.lat, p.lon)); });
    var src = E.nearestNode(pkg.graph.nodes, S.start[0], S.start[1]);
    var r = E.compareRoutes(pkg.graph, S.nRisk, src, dests, Number($('alpha').value), S.threshold);
    ['shortest', 'safest'].forEach(function (k) { if (S.layers[k]) { S.map.removeLayer(S.layers[k]); delete S.layers[k]; } });
    if (!r.shortest) { $('routeTable').hidden = true; $('routeMsg').hidden = false; $('routeMsg').textContent = 'No facility can be reached from that point on the mapped road network. Try another start.'; return; }
    S.layers.shortest = line(r.shortest.path, '#f5a524', '8 6').addTo(S.map);
    S.layers.safest = line(r.safest.path, '#22c55e').addTo(S.map);
    var a = r.shortest.stats, b = r.safest.stats;
    var rows = [
      ['Distance', fmtKm(a.length_m), fmtKm(b.length_m), a.length_m <= b.length_m ? 0 : 1],
      ['Average risk (0–1 scale)', a.mean_risk.toFixed(2), b.mean_risk.toFixed(2), a.mean_risk <= b.mean_risk ? 0 : 1],
      ['Highest risk on route', a.max_risk.toFixed(2), b.max_risk.toFixed(2), a.max_risk <= b.max_risk ? 0 : 1],
      ['Distance through high-risk zones', fmtKm(a.high_risk_m), fmtKm(b.high_risk_m), a.high_risk_m <= b.high_risk_m ? 0 : 1],
      ['Destination', nameAt(r.shortest.dest), nameAt(r.safest.dest), -1]
    ];
    var tb = $('routeTable').tBodies[0]; tb.textContent = '';
    rows.forEach(function (row) {
      var tr = el('tr'); tr.appendChild(el('th', row[0])); tr.lastChild.scope = 'row';
      tr.appendChild(el('td', row[1], row[3] === 0 && row[1] !== row[2] ? 'better' : ''));
      tr.appendChild(el('td', row[2], row[3] === 1 && row[1] !== row[2] ? 'better' : '')); tb.appendChild(tr);
    });
    $('routeTable').hidden = false;
    var same = r.shortest.path.nodes.join() === r.safest.path.nodes.join();
    $('routeMsg').hidden = false;
    $('routeMsg').textContent = same ? 'Both routes are the same here: the shortest route is also the least risky.'
      : 'Least-risk route: ' + fmtKm(b.length_m - a.length_m) + ' longer, ' + Math.round(100 * (1 - (b.high_risk_m / (a.high_risk_m || 1)))) + '% less distance through high-risk zones' + (a.high_risk_m ? '.' : ' (shortest route had none).');
  }

  function nameAt(node) {
    var g = S.pkg.graph.nodes[node], best = null, bd = Infinity;
    S.pkg.pois.forEach(function (p) { var d = Math.abs(p.lat - g[0]) + Math.abs(p.lon - g[1]); if (d < bd) { bd = d; best = p; } });
    return best ? best.name : '–';
  }

  function setStart(lat, lon) {
    S.start = [lat, lon];
    if (S.layers.startPin) S.map.removeLayer(S.layers.startPin);
    S.layers.startPin = L.circleMarker([lat, lon], { radius: 8, color: '#fff', weight: 2, fillColor: '#3987e5', fillOpacity: 1, pane: 'routePane' }).bindTooltip('Start').addTo(S.map);
    route();
  }

  function buildSliders() {
    var box = $('weights'); box.textContent = '';
    E.LAYERS.forEach(function (k) {
      var wrap = el('div', null, 'wrow'), lab = el('label'), out = el('output', '0%'), inp = document.createElement('input');
      lab.htmlFor = 'w_' + k; lab.appendChild(el('span', LABELS[k])); lab.appendChild(out);
      inp.type = 'range'; inp.id = 'w_' + k; inp.min = 0; inp.max = 100; inp.value = 0;
      inp.addEventListener('input', function () { S.weights[k] = Number(inp.value) / 100; syncOutputs(); recompute(); });
      wrap.appendChild(lab); wrap.appendChild(inp); box.appendChild(wrap);
    });
  }
  function syncOutputs() {
    var n = E.normalizeWeights(S.weights);
    E.LAYERS.forEach(function (k) { $('w_' + k).previousSibling.lastChild.textContent = Math.round(n[k] * 100) + '%'; });
  }
  function applyPreset() {
    var p = E.PRESETS[$('calamity').value];
    S.weights = {}; E.LAYERS.forEach(function (k) { S.weights[k] = p[k]; $('w_' + k).value = Math.round(p[k] * 100); });
    syncOutputs();
  }

  function loadArea(id) {
    return fetch('../data/risk/' + encodeURIComponent(id) + '.json').then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).then(function (pkg) {
      S.pkg = pkg; S.start = null; clearRoute();
      $('areaEvents').textContent = 'Reported: ' + pkg.events;
      var cal = $('calamity'); cal.textContent = '';
      var opts = pkg.calamities.slice(); if (opts.length > 1) opts.push('combined');
      opts.forEach(function (c) { var o = el('option', CAL_LABEL[c]); o.value = c; cal.appendChild(o); });
      var syn = pkg.provenance.kind === 'synthetic';
      $('provBanner').hidden = !syn;
      $('provBanner').textContent = syn ? 'Synthetic data. ' + pkg.provenance.note : '';
      $('provNote').textContent = (syn ? 'Data: synthetic placeholder. ' : 'Data: ') + (pkg.provenance.sources || pkg.provenance.note);
      S.map.fitBounds(bounds());
      S.layers.pois && S.layers.pois.forEach(function (m) { S.map.removeLayer(m); });
      S.layers.pois = pkg.pois.map(function (p) {
        return L.circleMarker([p.lat, p.lon], { radius: 6, color: '#fff', weight: 1.5, fillColor: p.type === 'hospital' ? '#ef4444' : '#a855f7', fillOpacity: 1, pane: 'routePane' }).bindTooltip(p.name + ' (' + p.type + ')').addTo(S.map);
      });
      ['btnExample', 'facility', 'alpha'].forEach(function (id) { $(id).disabled = !hasRoads(); });
      applyPreset(); recompute();
      if (!hasRoads()) { $('routeMsg').textContent = 'Routes are unavailable for this district: no road network or facilities in its data package yet.'; }
    });
  }

  function example() {
    // Try a spread of start points and keep the one where the least-risk route avoids the most high-risk distance.
    var pkg = S.pkg, f = $('facility').value, dests = [], cand = [], i, best = null, bs = -1, step;
    pkg.pois.forEach(function (p) { if (f === 'any' || p.type === f) dests.push(E.nearestNode(pkg.graph.nodes, p.lat, p.lon)); });
    for (i = 0; i < pkg.graph.nodes.length; i++) if (S.nRisk[i] >= S.threshold * 0.8) cand.push(i);
    if (!cand.length) for (i = 0; i < pkg.graph.nodes.length; i++) cand.push(i);
    step = Math.max(1, Math.floor(cand.length / 60));
    for (i = 0; i < cand.length; i += step) {
      var r = E.compareRoutes(pkg.graph, S.nRisk, cand[i], dests, Number($('alpha').value), S.threshold);
      if (!r.shortest) continue;
      var score = (r.shortest.stats.high_risk_m - r.safest.stats.high_risk_m) + r.shortest.stats.length_m * 0.01;
      if (score > bs) { bs = score; best = cand[i]; }
    }
    var n = pkg.graph.nodes[best == null ? 0 : best];
    setStart(n[0], n[1]);
  }

  function init() {
    var map = S.map = L.map('map', { scrollWheelZoom: false });
    map.createPane('riskPane').style.zIndex = 350; map.createPane('routePane').style.zIndex = 650;
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap contributors' }).addTo(map);
    map.on('click', function (e) { if (!map.scrollWheelZoom.enabled()) map.scrollWheelZoom.enable(); setStart(e.latlng.lat, e.latlng.lng); });
    buildSliders();
    $('calamity').addEventListener('change', function () { applyPreset(); recompute(); });
    $('btnReset').addEventListener('click', function () { applyPreset(); recompute(); });
    $('topPct').addEventListener('input', function () { $('topPctOut').textContent = $('topPct').value; recompute(); });
    $('opacity').addEventListener('input', function () { if (S.layers.risk) S.layers.risk.setOpacity(Number($('opacity').value) / 100); });
    $('alpha').addEventListener('input', function () { $('alphaOut').textContent = $('alpha').value; route(); });
    $('facility').addEventListener('change', route);
    $('btnClear').addEventListener('click', function () { S.start = null; clearRoute(); });
    $('btnExample').addEventListener('click', example);
    $('area').addEventListener('change', function () { loadArea($('area').value).catch(function (e) { toast('Could not load that district: ' + e.message); }); });
    fetch('../data/risk/index.json').then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).then(function (idx) {
      idx.areas.forEach(function (a) { var o = el('option', a.name + ' (' + a.calamities.join(' + ') + ')'); o.value = a.id; $('area').appendChild(o); });
      return loadArea(idx.areas[0].id);
    }).catch(function (e) { toast('Could not load the data package: ' + e.message); });
  }
  init();
})();
