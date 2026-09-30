/* Client-side date-window checks. Mirrors common/validation.py:check_windows exactly
   (tests/fixtures/window_cases.json is run against both). The server re-checks everything. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.Validate = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  var DAY = 86400000, S1_START = Date.UTC(2014, 9, 3);

  function parse(s) {
    if (typeof s !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
    var p = s.split('-').map(Number), t = Date.UTC(p[0], p[1] - 1, p[2]), d = new Date(t);
    if (d.getUTCFullYear() !== p[0] || d.getUTCMonth() !== p[1] - 1 || d.getUTCDate() !== p[2]) return null;
    return t;
  }
  function issue(level, code, message) { return { level: level, code: code, message: message }; }
  function hasErrors(list) { return list.some(function (i) { return i.level === 'error'; }); }

  /* pre/post: [start, end] ISO strings. lim: {pre_days:[lo,hi], post_days:[lo,hi], max_gap_days, data_lag_days}.
     today: ISO string (tests) or undefined (= now). */
  function checkWindows(pre, post, lim, today) {
    var pre_s = parse(pre[0]), pre_e = parse(pre[1]), post_s = parse(post[0]), post_e = parse(post[1]);
    if ([pre_s, pre_e, post_s, post_e].some(function (x) { return x === null; }))
      return [issue('error', 'bad_date', 'Dates must be valid, in YYYY-MM-DD format.')];
    var now = today ? parse(today) : Date.UTC.apply(null, (function (d) { return [d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()]; })(new Date()));
    var out = [];
    [['Pre-flood', 'pre', pre_s, pre_e, lim.pre_days], ['Post-flood', 'post', post_s, post_e, lim.post_days]].forEach(function (w) {
      var label = w[0], key = w[1], a = w[2], b = w[3], lo = w[4][0], hi = w[4][1];
      if (b < a) { out.push(issue('error', key + '_order', label + ' window ends before it starts.')); return; }
      var days = Math.round((b - a) / DAY) + 1;
      if (days < lo || days > hi) out.push(issue('error', key + '_length', label + ' window is ' + days + ' days; it must be ' + lo + '-' + hi + ' days.'));
      if (a < S1_START) out.push(issue('error', key + '_too_early', label + ' window starts before Sentinel-1 data exists (2014-10-03).'));
    });
    if (hasErrors(out)) return out;
    if (post_s <= pre_e)
      out.push(issue('error', 'windows_overlap', 'The pre-flood and post-flood windows overlap. The post-flood window must start after the pre-flood window ends.'));
    else if ((post_s - pre_e) / DAY > lim.max_gap_days)
      out.push(issue('error', 'gap_too_long', 'The windows are more than ' + lim.max_gap_days + ' days apart.'));
    if (post_e > now - lim.data_lag_days * DAY)
      out.push(issue('error', 'post_in_future', 'Sentinel-1 scenes from the last ' + lim.data_lag_days + ' days may not be available yet. Choose an earlier post-flood window.'));
    return out;
  }

  return { parse: parse, checkWindows: checkWindows, hasErrors: hasErrors };
});
