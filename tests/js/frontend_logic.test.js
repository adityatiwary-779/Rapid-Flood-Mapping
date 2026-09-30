// Run: node --test tests/js/
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const V = require('../../public/js/validate.js');
const S = require('../../public/js/summary.js');
const cases = JSON.parse(fs.readFileSync(path.join(__dirname, '../fixtures/window_cases.json')));

for (const c of cases.cases) {
  test('window rules: ' + c.name, () => {
    const got = V.checkWindows(c.pre, c.post, cases.limits, cases.today).map(i => i.code);
    assert.deepStrictEqual(got, c.codes);
  });
}

const manifest = () => ({
  title: 'Kerala floods 2018 · Alappuzha', place: 'Alappuzha district, Kerala, India',
  windows: { pre: ['2018-07-01', '2018-07-31'], post: ['2018-08-15', '2018-08-25'] },
  stats: { flood_km2: 151.1338, cropland_km2: 101.8527, builtup_km2: 0.03, tree_cover_km2: 3.2779, other_km2: 45.97,
           population_exposed: 73575.02, region_area_km2: 1316.69, rule_based_km2: 83.1698, flood_unmasked_km2: 198.0 },
  config: { threshold_db: -3.9002, orbit_pass: 'DESCENDING', n_pre_images: 6, n_post_images: 2, used_rf: true, speckle: 'median composite + 50 m focal median' },
  roads: { road_km_total: 1520.3, road_km_flooded: 51.4, settlements_total: 212, settlements_affected: 4, motorable_only: true, settlement_buffer_m: 250 },
  warnings: [{ message: 'Only 2 post-flood images.' }], caveat: S.CAVEAT,
});

test('summary uses real numbers with sensible rounding', () => {
  const t = S.build(manifest()).text;
  assert.match(t, /about 151 km² of flooding in Alappuzha \(11% of the district's 1,317 km²\)/);
  assert.match(t, /102 km² of cropland \(67% of the flooded area\)/);
  assert.match(t, /0\.03 km² of built-up land, and 46\.0 km² of other land cover|built-up land and 46\.0 km² of other/);
  assert.match(t, /73,600 people/);                       // rounded to the nearest 100
  assert.match(t, /51\.4 km of roads \(out of 1,520 km/);
  assert.match(t, /4 of 212 mapped settlements lie within 250 m/);
  assert.match(t, /-3\.90 dB/);
  assert.match(t, /threshold-only map covers 83\.2 km² and the refined map 151 km², so the true extent is uncertain/);
  assert.match(t, /masks removed 115 km² \(58%\) from the threshold result \(198 to 83\.2 km²\)/);
  assert.doesNotMatch(t, /would show/);
  assert.match(t, /Note: Only 2 post-flood images\./);
  assert.match(t, /NOT independent validation/);          // caveat always included
  assert.match(t, /Copernicus EMS/);
});

test('summary copes with missing roads / failed roads / no rf', () => {
  let m = manifest(); m.roads = null;
  assert.doesNotMatch(S.build(m).text, /roads/);
  m = manifest(); m.roads = { error: 'x' };
  assert.match(S.build(m).text, /Road and settlement analysis is not available/);
  m = manifest(); m.config.used_rf = false;
  assert.doesNotMatch(S.build(m).text, /Random Forest then refined/);
  m = manifest(); delete m.caveat;
  assert.match(S.build(m).text, /NOT independent validation/);   // fallback caveat text
});

test('number formatting', () => {
  assert.strictEqual(S.fmt.km2(0.03), '0.03');
  assert.strictEqual(S.fmt.km2(5.55), '5.6');
  assert.strictEqual(S.fmt.km2(1234.6), '1,235');
  assert.strictEqual(S.fmt.km2(null), '–');
  assert.strictEqual(S.fmt.people(74), '70');
  assert.strictEqual(S.fmt.pct(1, 3), '33%');
  assert.strictEqual(S.fmt.pct(1, 30), '3.3%');
  assert.strictEqual(S.fmt.date('2018-08-05'), '5 Aug 2018');
  assert.strictEqual(S.fmt.shortRange(['2018-07-01', '2018-07-31']), '1–31 Jul 2018');
  assert.strictEqual(S.fmt.shortRange(['2018-08-25', '2018-09-05']), '25 Aug – 5 Sep 2018');
  assert.strictEqual(S.fmt.shortRange(['2017-12-28', '2018-01-06']), '28 Dec 2017 – 6 Jan 2018');
  assert.strictEqual(S.fmt.list(['a']), 'a');
  assert.strictEqual(S.fmt.list(['a', 'b']), 'a and b');
  assert.strictEqual(S.fmt.list(['a', 'b', 'c']), 'a, b and c');
});
