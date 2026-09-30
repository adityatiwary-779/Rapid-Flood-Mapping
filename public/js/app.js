/* Flood mapping front end. Plain JS + Leaflet. All server text is inserted with textContent (never innerHTML). */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var S = window.Summary, V = window.Validate;
  var state = { config: null, events: [], regions: {}, manifest: null, map: null, layers: {}, ratio: 0.5, timer: null, tick: null, jobId: null, busy: false };

  var DOWNLOADS = [
    ['Stats CSV', 'stats.csv'], ['Flood extent GeoTIFF', 'flood.tif'], ['Severity GeoTIFF (dB change)', 'dvv_db.tif'],
    ['Flood polygons GeoJSON', 'flood_vec.geojson'], ['Flooded roads GeoJSON', 'flooded_roads.geojson'],
    ['Affected settlements GeoJSON', 'affected_settlements.geojson'], ['Roads by type CSV', 'roads_flooded_by_type.csv'],
    ['Method and masks JSON', 'config.json']
  ];

  /* ---------------------------------------------------------------- helpers */
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') n.textContent = attrs[k]; else if (k === 'class') n.className = attrs[k];
      else if (attrs[k] !== false && attrs[k] !== null) n.setAttribute(k, attrs[k] === true ? '' : attrs[k]);
    });
    (kids || []).forEach(function (c) { n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
  function ApiError(code, message, status, data) { this.code = code; this.message = message; this.status = status; this.data = data; }
  ApiError.prototype = Object.create(Error.prototype);

  function api(path, opts) {
    return fetch(path, Object.assign({ headers: { Accept: 'application/json' } }, opts || {})).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (res.ok) return data;
        var e = (data && data.error) || {};
        var msg = e.message || ('The server returned an error (HTTP ' + res.status + ').');
        if (res.status === 429 && e.retry_after) msg += ' Try again in about ' + Math.max(1, Math.ceil(e.retry_after / 60)) + ' min.';
        throw new ApiError(e.code || 'http_' + res.status, msg, res.status, data);
      });
    }, function () {
      throw new ApiError('network', 'Cannot reach the server. Check your connection and try again.');
    });
  }
  function post(path, body) {
    return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) });
  }
  var toastTimer;
  function toast(msg) {
    var t = $('toast'); t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.hidden = true; }, 8000);
  }
  function fail(e) { toast(e && e.message ? e.message : 'Something went wrong.'); }
  function fmt(x) { return S.fmt.km2(x); }

  /* ---------------------------------------------------------------- init */
  function pollMs(factor) { return ((state.config && state.config.poll_ms) || 3000) * (factor || 1); }   // config may not have arrived yet

  function init() {
    bindUI();
    // config first and on its own: a slow events/regions response must never block validation or job polling
    api('/api/config').then(function (cfg) {
      state.config = cfg;
      $('modeChip').hidden = false; $('modeChip').textContent = cfg.mode === 'mock' ? 'Demo mode' : (cfg.mode === 'static' ? 'Static demo' : 'Live');
      if (cfg.mode === 'mock') { $('modeChip').className = 'chip demo'; $('mockBanner').hidden = false; }
      if (cfg.static_demo) { $('tabCustom').disabled = true; $('tabCustom').title = 'Not available in the static demo'; $('staticNote').hidden = false; }
      fillLimits(); validateCustom();
      return Promise.all([api('/api/events'), api('/api/regions')]);
    }).then(function (r) {
      state.events = r[0].events || []; state.regions = r[1] || {};
      fillPresets(); fillRegions(); routeFromHash();
    }).catch(function (e) { fail(e); $('empty').querySelector('p').textContent = 'The server could not be reached, so nothing can be loaded right now.'; });
  }

  function fillPresets() {
    var names = [];
    state.events.forEach(function (e) { if (names.indexOf(e.event) < 0) names.push(e.event); });
    var sel = clear($('presetEvent'));
    if (!names.length) { sel.appendChild(el('option', { text: 'No preset events available' })); $('btnLoadPreset').disabled = true; return; }
    names.forEach(function (n) { sel.appendChild(el('option', { value: n, text: n })); });
    fillDistricts();
  }
  function fillDistricts() {
    var ev = $('presetEvent').value, sel = clear($('presetDistrict'));
    state.events.filter(function (e) { return e.event === ev; }).forEach(function (e) {
      sel.appendChild(el('option', { value: e.id, text: e.district }));
    });
    showPresetWindows();
  }
  function showPresetWindows() {
    var e = state.events.filter(function (x) { return x.id === $('presetDistrict').value; })[0];
    $('presetWindows').textContent = e ? 'Before: ' + S.fmt.range(e.windows.pre) + '. After: ' + S.fmt.range(e.windows.post) + '.' : '';
  }
  function fillRegions() {
    var rows = ((state.regions.India || {})['2']) || [], parents = [];
    rows.forEach(function (r) { if (parents.indexOf(r.parent) < 0) parents.push(r.parent); });
    var sl = clear($('stateList')); parents.forEach(function (p) { sl.appendChild(el('option', { value: p })); });
    updateDistrictList();
  }
  function updateDistrictList() {
    var rows = ((state.regions.India || {})['2']) || [], st = $('cState').value.trim();
    var dl = clear($('districtList'));
    rows.filter(function (r) { return r.parent === st; }).forEach(function (r) { dl.appendChild(el('option', { value: r.name })); });
  }
  function fillLimits() {
    if (!state.config) return;
    var l = state.config.limits;
    $('limitHint').textContent = 'Pre-flood window ' + l.pre_days[0] + '–' + l.pre_days[1] + ' days, post-flood window ' + l.post_days[0] + '–' +
      l.post_days[1] + ' days, districts up to ' + l.max_area_km2.toLocaleString('en-IN') + ' km². Analyses run on Google Earth Engine and take a few minutes.';
  }

  /* ---------------------------------------------------------------- UI bindings */
  function bindUI() {
    $('presetEvent').addEventListener('change', fillDistricts);
    $('presetDistrict').addEventListener('change', showPresetWindows);
    $('btnLoadPreset').addEventListener('click', function () {
      var id = $('presetDistrict').value; if (!id) return;
      history.replaceState(null, '', '#event=' + encodeURIComponent(id)); loadPreset(id);
    });
    $('tabPreset').addEventListener('click', function () { selectTab('preset'); });
    $('tabCustom').addEventListener('click', function () { selectTab('custom'); });
    ['tabPreset', 'tabCustom'].forEach(function (id) {
      $(id).addEventListener('keydown', function (e) {
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { selectTab(id === 'tabPreset' ? 'custom' : 'preset'); $(id === 'tabPreset' ? 'tabCustom' : 'tabPreset').focus(); }
      });
    });
    ['cState', 'cDistrict', 'preStart', 'preEnd', 'postStart', 'postEnd'].forEach(function (id) {
      $(id).addEventListener('input', function () { if (id === 'cState') updateDistrictList(); validateCustom(); $('preflightOut').textContent = ''; });
    });
    $('btnCheck').addEventListener('click', runPreflight);
    $('paneCustom').addEventListener('input', function () { clear($('sIssues')); });
    $('paneCustom').addEventListener('submit', function (e) { e.preventDefault(); startJob(); });
    $('btnRetry').addEventListener('click', function () { selectTab('custom'); $('jobBox').hidden = true; $('cState').focus(); });
    document.querySelectorAll('input[name=floodMode]').forEach(function (r) { r.addEventListener('change', applyLayers); });
    ['lyrSeverity', 'lyrRoads', 'lyrSettle'].forEach(function (id) { $(id).addEventListener('change', applyLayers); });
    $('radarOpacity').addEventListener('input', applyLayers);
    bindSwipe();
    $('btnSummary').addEventListener('click', showSummary);
    $('btnCopy').addEventListener('click', copySummary);
    $('btnSaveTxt').addEventListener('click', saveSummary);
    window.addEventListener('hashchange', routeFromHash);
  }
  function selectTab(which) {
    var p = which === 'preset';
    $('tabPreset').setAttribute('aria-selected', p); $('tabCustom').setAttribute('aria-selected', !p);
    $('tabPreset').tabIndex = p ? 0 : -1; $('tabCustom').tabIndex = p ? -1 : 0;
    $('panePreset').hidden = !p; $('paneCustom').hidden = p;
  }
  function routeFromHash() {
    var h = location.hash.replace(/^#/, ''), m;
    if ((m = h.match(/^event=([a-z0-9_]+)$/))) {
      var e = state.events.filter(function (x) { return x.id === m[1]; })[0];
      if (e) { $('presetEvent').value = e.event; fillDistricts(); $('presetDistrict').value = e.id; showPresetWindows(); }
      loadPreset(m[1]);
    } else if ((m = h.match(/^job=([0-9a-f]{12})$/))) {
      if (state.jobId !== m[1]) { selectTab('custom'); pollJob(m[1]); }
    }
  }

  /* ---------------------------------------------------------------- custom form */
  function readCustom() {
    return {
      region: { country: 'India', level: 2, parent: $('cState').value.trim(), name: $('cDistrict').value.trim() },
      pre: [$('preStart').value, $('preEnd').value], post: [$('postStart').value, $('postEnd').value]
    };
  }
  function renderIssues(list, into) {
    clear(into);
    list.forEach(function (i) { into.appendChild(el('div', { class: 'issue ' + i.level, text: i.message })); });
  }
  function validateCustom() {
    if (!state.config) return [];
    var b = readCustom(), issues = [];
    if (!b.region.parent || !b.region.name) issues.push({ level: 'error', code: 'region', message: 'Enter a state and a district.' });
    else issues = V.checkWindows(b.pre, b.post, state.config.limits);
    renderIssues(issues, $('cIssues'));
    var bad = V.hasErrors(issues);
    $('btnRun').disabled = bad || state.busy; $('btnCheck').disabled = bad || state.busy;
    return issues;
  }
  function setBusy(b) { state.busy = b; validateCustom(); }

  function runPreflight() {
    if (V.hasErrors(validateCustom())) return;
    setBusy(true); $('btnCheck').textContent = 'Checking…'; clear($('sIssues'));
    return post('/api/preflight', readCustom()).then(function (r) {
      var out = clear($('preflightOut'));
      var list = el('div', { class: 'issues' }); renderIssues(r.issues || [], list); out.appendChild(list);
      if (r.counts) {
        var t = el('table', {}, [el('tr', {}, ['Pass', 'Pre images', 'Post images'].map(function (h) { return el('th', { text: h }); }))]);
        Object.keys(r.counts).forEach(function (p) {
          t.appendChild(el('tr', {}, [el('td', { text: p.charAt(0) + p.slice(1).toLowerCase() + (p === r.chosen_pass ? ' (used)' : '') }),
            el('td', { text: String(r.counts[p].pre) }), el('td', { text: String(r.counts[p].post) })]));
        });
        out.appendChild(t);
        out.appendChild(el('p', { class: 'hint', text: 'District area ' + Math.round(r.area_km2).toLocaleString('en-IN') + ' km².' + (r.ok ? ' Ready to run.' : '') }));
      }
    }).catch(fail).then(function () { $('btnCheck').textContent = 'Check images'; setBusy(false); });
  }

  function startJob() {
    if (V.hasErrors(validateCustom())) return;
    setBusy(true); $('btnRun').textContent = 'Starting…'; clear($('sIssues'));
    post('/api/jobs', readCustom()).then(function (r) {
      pollJob(r.job_id);
    }).catch(function (e) {
      var box = clear($('sIssues'));       // server messages live apart from the live client-side checks
      var issues = e.data && e.data.error && e.data.error.issues;
      if (issues && issues.length && issues.every(function (i) { return i.level && i.message; })) renderIssues(issues, box);
      else box.appendChild(el('div', { class: 'issue error', text: e.message }));
    }).then(function () { $('btnRun').textContent = 'Run analysis'; setBusy(false); });
  }

  /* ---------------------------------------------------------------- job polling */
  function stopPolling() { clearTimeout(state.timer); clearInterval(state.tick); state.timer = state.tick = null; }
  function pollJob(id) {
    stopPolling(); state.jobId = id;
    history.replaceState(null, '', '#job=' + id);
    var box = $('jobBox'); box.hidden = false; $('jobError').hidden = true; $('btnRetry').hidden = true;
    var t0 = Date.now(), fails = 0;
    state.tick = setInterval(function () {
      var s = Math.floor((Date.now() - t0) / 1000); $('jobTime').textContent = 'Elapsed ' + Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    }, 1000);
    function step() {
      api('/api/jobs/' + id).then(function (j) {
        fails = 0; renderJob(j);
        if (j.status === 'done') {
          stopPolling();
          return api('/api/jobs/' + id + '/result').then(function (m) { renderManifest(m); $('jobMsg').textContent = 'Finished.'; });
        }
        if (j.status === 'failed') { stopPolling(); jobFailed((j.error && j.error.message) || j.message || 'The analysis failed.'); return; }
        state.timer = setTimeout(step, pollMs());
      }).catch(function (e) {
        if (e.code === 'not_found' || ++fails >= 5) { stopPolling(); jobFailed(e.message); return; }
        state.timer = setTimeout(step, pollMs(2));
      });
    }
    step();
  }
  function renderJob(j) {
    $('jobFill').style.width = (j.progress || 0) + '%'; $('jobBar').setAttribute('aria-valuenow', j.progress || 0);
    $('jobMsg').textContent = j.message || j.stage || '';
  }
  function jobFailed(msg) {
    var e = $('jobError'); e.hidden = false; e.textContent = msg; e.className = 'issue error'; $('btnRetry').hidden = false;
    $('jobMsg').textContent = 'The analysis did not finish.';
  }

  /* ---------------------------------------------------------------- results */
  function loadPreset(id) {
    stopPolling(); $('jobBox').hidden = true; state.jobId = null; $('mapLoading').hidden = false;
    api('/api/events/' + encodeURIComponent(id)).then(renderManifest).catch(function (e) { fail(e); $('mapLoading').hidden = true; });
  }

  function renderManifest(m) {
    state.manifest = m;
    $('empty').hidden = true; $('content').hidden = false;
    $('evTitle').textContent = m.title; $('evPlace').textContent = m.place;
    var chips = clear($('evChips'));
    chips.appendChild(el('span', { class: 'chip', text: 'Before: ' + S.fmt.range(m.windows.pre) }));
    chips.appendChild(el('span', { class: 'chip', text: 'After: ' + S.fmt.range(m.windows.post) }));
    chips.appendChild(el('span', { class: 'chip', text: m.kind === 'preset' ? 'Preset event' : 'Custom run' }));
    if (m.mock) chips.appendChild(el('span', { class: 'chip demo', text: 'Demo data' }));
    var w = clear($('warnings')); (m.warnings || []).forEach(function (x) { w.appendChild(el('div', { class: 'issue ' + x.level, text: x.message })); });
    var caveat = m.caveat || S.CAVEAT;
    $('caveatBottom').textContent = '';
    $('caveatBottom').appendChild(el('strong', { text: 'Limitation: ' })); $('caveatBottom').appendChild(document.createTextNode(caveat));
    renderKpis(m); renderComposition(m); renderRoads(m); renderMethod(m); renderDownloads(m);
    $('summaryBox').hidden = true;
    renderMap(m);
    if (window.matchMedia('(max-width: 900px)').matches) $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function kpi(label, value, unit, sub, color) {
    var k = el('div', { class: 'kpi' }, [
      el('div', { class: 'k' }, color ? [el('span', { class: 'sw', style: 'background:' + color }), label] : [label]),
      el('div', { class: 'v' }, [String(value), unit ? el('span', { class: 'u', text: unit }) : '']),
      el('div', { class: 's', text: sub || '' })]);
    return k;
  }
  function renderKpis(m) {
    var s = m.stats, r = m.roads, box = clear($('kpis'));
    box.appendChild(kpi('Flooded area', fmt(s.flood_km2), 'km²', S.fmt.pct(s.flood_km2, s.region_area_km2) + ' of the district', 'var(--flood)'));
    box.appendChild(kpi('Cropland', fmt(s.cropland_km2), 'km²', S.fmt.pct(s.cropland_km2, s.flood_km2) + ' of flooded area', 'var(--c-crop)'));
    box.appendChild(kpi('Built-up', fmt(s.builtup_km2), 'km²', 'lower bound: radar misses urban flooding', 'var(--c-built)'));
    box.appendChild(kpi('Tree cover', fmt(s.tree_cover_km2), 'km²', S.fmt.pct(s.tree_cover_km2, s.flood_km2) + ' of flooded area', 'var(--c-tree)'));
    box.appendChild(kpi('Population exposed', S.fmt.people(s.population_exposed), 'people', 'WorldPop 2020 estimate'));
    if (r && !r.error) {
      box.appendChild(kpi('Flooded roads', fmt(r.road_km_flooded), 'km', 'of ' + fmt(r.road_km_total) + ' km mapped' + (r.motorable_only ? ' (motorable)' : '')));
      box.appendChild(kpi('Affected settlements', r.settlements_affected, '', 'of ' + r.settlements_total + ' mapped places'));
    } else {
      box.appendChild(kpi('Flooded roads', 'n/a', '', r && r.error ? 'analysis failed for this run' : 'not available'));
    }
  }

  function renderComposition(m) {
    var s = m.stats, parts = [
      ['Cropland', s.cropland_km2, 'var(--c-crop)'], ['Built-up', s.builtup_km2, 'var(--c-built)'],
      ['Tree cover', s.tree_cover_km2, 'var(--c-tree)'], ['Other (water, wetland, grass, shrub)', s.other_km2, 'var(--c-other)']];
    var bar = clear($('stack')), list = clear($('compList'));
    parts.forEach(function (p) {
      if (p[1] > 0) bar.appendChild(el('div', { class: 'seg', style: 'flex:' + p[1] + ';background:' + p[2], title: p[0] + ': ' + fmt(p[1]) + ' km²' }));
      list.appendChild(el('li', {}, [el('span', { class: 'sw', style: 'background:' + p[2] }), el('span', { class: 'nm', text: p[0] }),
        el('span', { class: 'val', text: fmt(p[1]) + ' km²' }), el('span', { class: 'pc', text: S.fmt.pct(p[1], s.flood_km2) })]));
    });
    var note = 'Land cover from ESA WorldCover. “Other” can include water that was already there.';
    if (s.flood_unmasked_km2) note += ' Without the terrain and permanent-water masks the map would show ' + fmt(s.flood_unmasked_km2) + ' km².';
    if (m.config && m.config.used_rf && s.rule_based_km2 != null) note += ' Threshold-only estimate: ' + fmt(s.rule_based_km2) + ' km²; with Random Forest: ' + fmt(s.flood_km2) + ' km².';
    $('compNote').textContent = note;
  }

  function renderRoads(m) {
    var r = m.roads, box = clear($('roadBox'));
    if (!r) { box.appendChild(el('p', { class: 'hint', text: 'Road analysis is not available for this event.' })); return; }
    if (r.error) { box.appendChild(el('div', { class: 'issue warning', text: r.error })); return; }
    box.appendChild(el('dl', { class: 'dl' }, [
      el('dt', { text: 'Flooded road length' }), el('dd', { text: fmt(r.road_km_flooded) + ' km (' + S.fmt.pct(r.road_km_flooded, r.road_km_total) + ' of ' + fmt(r.road_km_total) + ' km)' }),
      el('dt', { text: 'Settlements affected' }), el('dd', { text: r.settlements_affected + ' of ' + r.settlements_total })]));
    if (r.by_type && r.by_type.length) {
      var t = el('table', { class: 'plain' }, [el('tr', {}, [el('th', { text: 'Road type' }), el('th', { class: 'n', text: 'Flooded km' })])]);
      r.by_type.slice(0, 8).forEach(function (x) { t.appendChild(el('tr', {}, [el('td', { text: x.highway }), el('td', { class: 'n', text: fmt(x.flooded_km) })])); });
      box.appendChild(el('h4', { text: 'Flooded roads by type' })); box.appendChild(t);
      if (r.by_type.length > 8) box.appendChild(el('p', { class: 'hint', text: (r.by_type.length - 8) + ' more road types are in the CSV download.' }));
    }
    box.appendChild(el('h4', { text: 'Affected settlements' }));
    if (r.settlements && r.settlements.length) {
      var ul = el('ul'); r.settlements.slice(0, 200).forEach(function (x) { ul.appendChild(el('li', {}, [x.name || '(unnamed)', el('small', { text: x.place || '' })])); });
      box.appendChild(el('div', { class: 'scroll', tabindex: '0', 'aria-label': 'Affected settlements list' }, [ul]));
      if (r.settlements.length > 200) box.appendChild(el('p', { class: 'hint', text: 'Showing 200 of ' + r.settlements.length + '. The full list is in the GeoJSON download.' }));
    } else box.appendChild(el('p', { class: 'hint', text: 'No mapped settlements were found in or near the flood.' }));
    box.appendChild(el('p', { class: 'hint', text: 'Roads and places from OpenStreetMap' + (r.motorable_only ? '; footpaths and tracks excluded' : '') +
      '. A settlement counts as affected if its centre is ' + (r.settlement_buffer_m ? 'within ' + r.settlement_buffer_m + ' m of' : 'inside') + ' flood water. OSM completeness varies.' }));
  }

  function renderMethod(m) {
    var c = m.config || {}, mk = c.masks || {};
    function rows(dl, items) { clear(dl); items.forEach(function (i) { if (i[1] !== undefined && i[1] !== null) { dl.appendChild(el('dt', { text: i[0] })); dl.appendChild(el('dd', { text: String(i[1]) })); } }); }
    rows($('method'), [
      ['Approach', 'Change in Sentinel-1 VV backscatter (after − before)'],
      ['Speckle filter', c.speckle],
      ['Threshold', c.threshold_db !== undefined ? c.threshold_db.toFixed(2) + ' dB' + (c.otsu_ok === false ? ' (Otsu failed; fallback used)' : ' (Otsu, clamped to ' + (c.diff_clamp_db || []).join(' to ') + ' dB)') : null],
      ['Raw Otsu value', c.otsu_raw_db !== undefined ? c.otsu_raw_db.toFixed(2) + ' dB' : null],
      ['Water darker than', c.post_vv_db !== undefined ? c.post_vv_db + ' dB (post-flood VV)' : null],
      ['Orbit pass', c.orbit_pass ? c.orbit_pass.charAt(0) + c.orbit_pass.slice(1).toLowerCase() : null],
      ['Images used', c.n_pre_images !== undefined ? c.n_pre_images + ' before, ' + c.n_post_images + ' after' : null],
      ['Random Forest', c.used_rf === undefined ? null : (c.used_rf ? 'Yes, trained on threshold-step labels (not independent validation)' : 'No, threshold result only')],
      ['Output resolution', c.export_scale_m ? c.export_scale_m + ' m' : null]]);
    rows($('masks'), [
      ['Permanent water (JRC)', mk.max_jrc_occurrence_pct !== undefined ? 'removed where water ≥ ' + mk.max_jrc_occurrence_pct + '% of the time' : null],
      ['Steep ground (SRTM slope)', mk.max_slope_deg !== undefined ? 'removed above ' + mk.max_slope_deg + '°' : null],
      ['High ground (MERIT HAND)', mk.max_hand_m !== undefined ? 'removed above ' + mk.max_hand_m + ' m' : null],
      ['Small patches', mk.min_patch_pixels !== undefined ? 'removed below ' + mk.min_patch_pixels + ' pixels' : null]]);
  }

  function renderDownloads(m) {
    var box = clear($('downloads'));
    DOWNLOADS.forEach(function (d) {
      var url = m.files && m.files[d[1]];
      if (url) box.appendChild(el('a', { class: 'btn', href: url, download: d[1], text: '⬇ ' + d[0] }));
      else box.appendChild(el('span', { class: 'btn', 'aria-disabled': 'true', title: 'Not available for this event', text: '⬇ ' + d[0] }));
    });
  }

  function showSummary() {
    var s = S.build(state.manifest), box = clear($('summaryText'));
    s.paragraphs.forEach(function (p) { box.appendChild(el('p', { text: p })); });
    $('summaryBox').hidden = false; $('summaryBox').scrollIntoView({ block: 'nearest' });
  }
  function copySummary() {
    var text = S.build(state.manifest).text;
    var done = function () { $('btnCopy').textContent = 'Copied ✓'; setTimeout(function () { $('btnCopy').textContent = 'Copy text'; }, 2000); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () { toast('Copy was blocked by the browser. Select the text and copy it manually.'); });
    else { var r = document.createRange(); r.selectNodeContents($('summaryText')); var sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); done(); }
  }
  function saveSummary() {
    var s = S.build(state.manifest), a = el('a', { href: URL.createObjectURL(new Blob([s.text + '\n'], { type: 'text/plain' })), download: (state.manifest.id || 'flood') + '_summary.txt' });
    document.body.appendChild(a); a.click(); a.remove();
  }

  /* ---------------------------------------------------------------- map */
  function ensureMap() {
    if (state.map) return state.map;
    var map = L.map('map', { zoomSnap: 0.25, worldCopyJump: false, zoomControl: false });
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    var osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap contributors' }).addTo(map);
    var sat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 18, attribution: 'Imagery © Esri' });
    L.control.layers({ 'Street map': osm, 'Satellite': sat }, null, { position: 'bottomright', collapsed: true }).addTo(map);
    map.createPane('swipeL').style.zIndex = 300; map.createPane('swipeR').style.zIndex = 310;
    map.on('move zoom resize viewreset', updateClip);
    // do not hijack page scrolling: wheel-zoom only after the map has been clicked, off again when the pointer leaves
    map.scrollWheelZoom.disable();
    map.on('click focus', function () { map.scrollWheelZoom.enable(); });
    map.on('mouseout', function () { map.scrollWheelZoom.disable(); });
    map.setView([10.5, 76.5], 7);
    state.map = map; return map;
  }
  function clearLayers() {
    Object.keys(state.layers).forEach(function (k) { if (state.layers[k]) state.map.removeLayer(state.layers[k]); });
    state.layers = {};
  }
  function renderMap(m) {
    var map = ensureMap(); clearLayers();
    map.invalidateSize();
    var b = m.bounds, P = m.previews, pending = 0;
    function img(name, pane, z) {
      var o = L.imageOverlay(P[name], b, { pane: pane, zIndex: z, alt: name, interactive: false, opacity: 1 }), counted = false;
      var done = function () { if (counted) { counted = false; if (--pending <= 0) $('mapLoading').hidden = true; } };
      o.on('add', function () { if (!o._seen) { o._seen = counted = true; pending++; $('mapLoading').hidden = false; } });
      o.on('load', done); o.on('error', function () { done(); toast('Some map images could not be loaded.'); });
      state.layers[name] = o; return o;
    }
    img('pre_vv', 'swipeL', 1).addTo(map); img('post_vv', 'swipeR', 1).addTo(map);
    if (P.flood_unmasked) img('flood_unmasked', 'swipeR', 2);
    img('flood', 'swipeR', 3);
    if (P.severity) img('severity', 'swipeR', 5).setOpacity(0.9);   // severity on top so ticking it is visible
    availability(m);
    map.fitBounds(b, { padding: [20, 20] });
    $('lblBefore').textContent = 'Before · ' + S.fmt.shortRange(m.windows.pre);
    $('lblAfter').textContent = 'After · ' + S.fmt.shortRange(m.windows.post);
    $('mapNote').textContent = 'Left: Sentinel-1 VV radar, pre-flood window. Right: post-flood radar with the detected flood on top. Dark radar areas are smooth surfaces such as open water.' +
      (m.mock ? ' In demo mode the radar images are synthetic.' : '');
    setRatio(0.5); applyLayers();
    setTimeout(function () { map.invalidateSize(); map.fitBounds(b, { padding: [20, 20] }); updateClip(); }, 50);
  }

  // controls for data this event does not have are disabled (never silently do nothing)
  function availability(m) {
    var P = m.previews || {}, F = m.files || {};
    var un = document.querySelector('input[name=floodMode][value=unmasked]');
    [[un, !!P.flood_unmasked, 'Unmasked layer not included for this event'], [$('lyrSeverity'), !!P.severity, 'Severity layer not included for this event'],
     [$('lyrRoads'), !!F['flooded_roads.geojson'], 'No road data for this event'], [$('lyrSettle'), !!F['affected_settlements.geojson'], 'No settlement data for this event']
    ].forEach(function (x) {
      x[0].disabled = !x[1]; x[0].parentNode.title = x[1] ? '' : x[2]; x[0].parentNode.classList.toggle('off', !x[1]);
      if (!x[1] && x[0].checked) { x[0].checked = false; if (x[0] === un) document.querySelector('input[name=floodMode][value=masked]').checked = true; }
    });
  }

  var vec = { roads: null, settle: null };
  function loadVector(key, fileName, styleFn) {
    if (vec[key] && vec[key].mid === state.manifest.id) return Promise.resolve(vec[key].layer);
    var url = state.manifest.files && state.manifest.files[fileName];
    if (!url) { toast('That layer is not available for this event.'); return Promise.resolve(null); }
    return api(url).then(function (gj) {
      var layer = L.geoJSON(gj, styleFn()); vec[key] = { layer: layer, mid: state.manifest.id }; return layer;
    }).catch(function (e) { fail(e); return null; });
  }
  function applyLayers() {
    if (!state.map || !state.manifest) return;
    var L_ = state.layers, map = state.map, mode = document.querySelector('input[name=floodMode]:checked').value;
    function toggle(layer, on) { if (!layer) return; if (on && !map.hasLayer(layer)) layer.addTo(map); if (!on && map.hasLayer(layer)) map.removeLayer(layer); }
    toggle(L_.flood, mode === 'masked'); toggle(L_.flood_unmasked, mode === 'unmasked'); toggle(L_.severity, $('lyrSeverity').checked);
    var op = $('radarOpacity').value / 100; ['pre_vv', 'post_vv'].forEach(function (k) { if (L_[k]) L_[k].setOpacity(op); });
    var wantRoads = $('lyrRoads').checked, wantSettle = $('lyrSettle').checked;
    var mid = state.manifest.id;
    [['roads', 'flooded_roads.geojson', wantRoads, function () { return { pane: 'swipeR', style: { color: '#ff7a59', weight: 2.5, opacity: .95 } }; }],
     ['settle', 'affected_settlements.geojson', wantSettle, function () { return { pane: 'swipeR', pointToLayer: function (f, ll) { return L.circleMarker(ll, { pane: 'swipeR', radius: 6, color: '#fff', weight: 2, fillColor: '#c084fc', fillOpacity: 1 }); } }; }]
    ].forEach(function (v) {
      if (v[2]) loadVector(v[0], v[1], v[3]).then(function (layer) { if (layer && state.manifest.id === mid && $(v[0] === 'roads' ? 'lyrRoads' : 'lyrSettle').checked) toggle(layer, true); });
      else if (vec[v[0]]) toggle(vec[v[0]].layer, false);
    });
    renderLegend(mode);
  }
  function renderLegend(mode) {
    var lg = clear($('legend'));
    if (mode === 'masked') lg.appendChild(el('div', {}, [el('i', { style: 'background:var(--flood)' }), 'Flood (masked)']));
    if (mode === 'unmasked') lg.appendChild(el('div', {}, [el('i', { style: 'background:var(--unmasked)' }), 'Flood (unmasked)']));
    if ($('lyrSeverity').checked) lg.appendChild(el('div', {}, ['Backscatter drop', el('span', { class: 'grad' }), '1.5 dB → 10 dB']));
    if ($('lyrRoads').checked) lg.appendChild(el('div', {}, [el('i', { style: 'background:#ff7a59' }), 'Flooded roads']));
    if ($('lyrSettle').checked) lg.appendChild(el('div', {}, [el('i', { style: 'background:#c084fc' }), 'Affected settlements']));
    lg.hidden = !lg.children.length;
  }

  /* ---------------------------------------------------------------- swipe */
  function updateClip() {
    var map = state.map; if (!map || !map.getPane('swipeL')) return;
    var nw = map.containerPointToLayerPoint([0, 0]), se = map.containerPointToLayerPoint(map.getSize());
    var x = nw.x + (se.x - nw.x) * state.ratio;
    map.getPane('swipeL').style.clip = 'rect(' + nw.y + 'px,' + x + 'px,' + se.y + 'px,' + nw.x + 'px)';
    map.getPane('swipeR').style.clip = 'rect(' + nw.y + 'px,' + se.x + 'px,' + se.y + 'px,' + x + 'px)';
  }
  function setRatio(r) {
    state.ratio = Math.min(1, Math.max(0, r));
    var h = $('swipe'); h.style.left = (state.ratio * 100) + '%'; h.setAttribute('aria-valuenow', Math.round(state.ratio * 100));
    updateClip();
  }
  function bindSwipe() {
    var h = $('swipe'), box = h.parentNode, dragging = false;
    function move(e) { var r = box.getBoundingClientRect(); setRatio((e.clientX - r.left) / r.width); }
    h.addEventListener('pointerdown', function (e) { dragging = true; h.setPointerCapture(e.pointerId); move(e); e.preventDefault(); });
    h.addEventListener('pointermove', function (e) { if (dragging) move(e); });
    ['pointerup', 'pointercancel'].forEach(function (t) { h.addEventListener(t, function () { dragging = false; }); });
    h.addEventListener('keydown', function (e) {
      var step = e.shiftKey ? 0.1 : 0.02;
      if (e.key === 'ArrowLeft') setRatio(state.ratio - step); else if (e.key === 'ArrowRight') setRatio(state.ratio + step);
      else if (e.key === 'Home') setRatio(0); else if (e.key === 'End') setRatio(1); else return;
      e.preventDefault();
    });
    window.addEventListener('resize', updateClip);
  }

  window.floodApp = { state: state, setRatio: setRatio, applyLayers: applyLayers };   // debugging / tests
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
