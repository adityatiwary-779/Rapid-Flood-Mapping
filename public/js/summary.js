/* Plain-language event summary built from a result manifest with a fixed template (no AI, no API key). */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.Summary = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var CAVEAT = 'The Random Forest is trained on labels produced by the threshold step, so it is NOT independent validation. ' +
    'Compare these results with a Copernicus EMS map or news-reported flooded areas before relying on them.';

  function num(x, digits) {
    return Number(x).toLocaleString('en-IN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  function km2(x) {                      // area / length with sensible precision
    if (x === null || x === undefined || isNaN(x)) return '–';
    x = Number(x);
    if (x >= 100) return num(Math.round(x), 0);
    if (x >= 1) return num(x, 1);
    return num(x, 2);
  }
  function people(x) {                   // estimates are rounded so they do not look exact
    if (x === null || x === undefined || isNaN(x)) return '–';
    x = Number(x);
    var step = x >= 1000 ? 100 : 10;
    return num(Math.round(x / step) * step, 0);
  }
  function pct(part, whole) {
    if (!whole) return '–';
    var p = 100 * part / whole;
    return (p >= 10 ? num(Math.round(p), 0) : num(p, 1)) + '%';
  }
  function date(iso) {
    var p = iso.split('-').map(Number);
    return p[2] + ' ' + MONTHS[p[1] - 1] + ' ' + p[0];
  }
  function range(w) { return date(w[0]) + ' to ' + date(w[1]); }
  function shortRange(w) {              // "1-31 Jul 2018", "15 Aug - 5 Sep 2018", "28 Dec 2017 - 6 Jan 2018"
    var a = w[0].split('-').map(Number), b = w[1].split('-').map(Number);
    if (a[0] === b[0] && a[1] === b[1]) return a[2] + '–' + b[2] + ' ' + MONTHS[b[1] - 1] + ' ' + b[0];
    if (a[0] === b[0]) return a[2] + ' ' + MONTHS[a[1] - 1] + ' – ' + b[2] + ' ' + MONTHS[b[1] - 1] + ' ' + b[0];
    return date(w[0]) + ' – ' + date(w[1]);
  }
  function list(items) {
    if (items.length < 2) return items.join('');
    return items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1];
  }
  function short(place) { return String(place || '').split(',')[0].replace(/ district$/i, ''); }

  function build(m) {
    var s = m.stats, c = m.config || {}, out = [], district = short(m.place);
    var flood = s.flood_km2;
    out.push((m.title || 'Flood event') + ': Sentinel-1 radar comparing ' + range(m.windows.pre) + ' (before) with ' +
      range(m.windows.post) + ' (after) shows about ' + km2(flood) + ' km² of flooding in ' + district +
      (s.region_area_km2 ? ' (' + pct(flood, s.region_area_km2) + ' of the district\'s ' + km2(s.region_area_km2) + ' km²).' : '.'));

    var parts = [];
    if (s.cropland_km2 > 0) parts.push(km2(s.cropland_km2) + ' km² of cropland (' + pct(s.cropland_km2, flood) + ' of the flooded area)');
    if (s.tree_cover_km2 > 0) parts.push(km2(s.tree_cover_km2) + ' km² of tree cover');
    if (s.builtup_km2 > 0) parts.push(km2(s.builtup_km2) + ' km² of built-up land');
    if (s.other_km2 > 0) parts.push(km2(s.other_km2) + ' km² of other land cover (such as water bodies, wetland, grass and shrub)');
    if (parts.length) out.push('The flooded area includes ' + list(parts) +
      '. Radar under-detects flooding in built-up areas, so the built-up figure is a lower bound.');

    if (s.population_exposed !== null && s.population_exposed !== undefined)
      out.push('An estimated ' + people(s.population_exposed) + ' people live inside the flooded area (WorldPop 2020 grid; an estimate, not a census count).');

    var r = m.roads;
    if (r && !r.error) {
      out.push('About ' + km2(r.road_km_flooded) + ' km of roads (out of ' + km2(r.road_km_total) + ' km mapped in OpenStreetMap' +
        (r.motorable_only ? ', motorable roads only' : '') + ') cross flood water, and ' + r.settlements_affected + ' of ' +
        r.settlements_total + ' mapped settlements lie ' + (r.settlement_buffer_m ? 'within ' + r.settlement_buffer_m + ' m of it' : 'inside it') + '.');
    } else if (r && r.error) {
      out.push('Road and settlement analysis is not available for this run.');
    }

    var method = 'Method: change in VV backscatter (post minus pre) after ' + (c.speckle || 'speckle filtering') +
      ', thresholded at ' + (c.threshold_db !== undefined ? num(c.threshold_db, 2) : '?') + ' dB' +
      (c.orbit_pass ? ', ' + String(c.orbit_pass).toLowerCase() + ' pass, ' + c.n_pre_images + ' before / ' + c.n_post_images + ' after images' : '') +
      '; permanent water and steep or high ground are masked out.';
    if (c.used_rf) method += ' A Random Forest then refined the result.';
    if (c.used_rf && s.rule_based_km2 !== undefined && s.rule_based_km2 !== null)
      method += ' The threshold-only map covers ' + km2(s.rule_based_km2) + ' km² and the refined map ' + km2(flood) + ' km², so the true extent is uncertain.';
    if (s.flood_unmasked_km2) method += ' Without the terrain and permanent-water masks the map would show ' + km2(s.flood_unmasked_km2) + ' km².';
    out.push(method);

    (m.warnings || []).forEach(function (w) { out.push('Note: ' + w.message); });
    out.push('Important: ' + (m.caveat || CAVEAT));
    return { title: m.title, paragraphs: out, text: out.join('\n\n') };
  }

  return { build: build, fmt: { shortRange: shortRange, list: list, km2: km2, people: people, pct: pct, date: date, range: range, short: short }, CAVEAT: CAVEAT };
});
