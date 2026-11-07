'use strict';
// Pure fitting geometry. No DOM, network, broker, or wall-clock dependencies.
(function (root) {
  const seconds = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600};

  function parameter(x0, x1, x2, x) {
    const a = x0 - 2 * x1 + x2, b = 2 * (x1 - x0), c = x0 - x;
    // Rationalized root avoids cancellation for long, nearly symmetric ranges.
    return Math.abs(a) < 1e-12 ? -c / b : -2 * c / (b + Math.sqrt(Math.max(0, b * b - 4 * a * c)));
  }

  function value(points, index) {
    const [p0, p1, p2] = points;
    if (index < p0.i) return null;
    if (index > p2.i) return p2.price + slope(points) * (index - p2.i);
    const t = Math.max(0, Math.min(1, parameter(p0.i, p1.i, p2.i, index)));
    return (1 - t) ** 2 * p0.price + 2 * (1 - t) * t * p1.price + t * t * p2.price;
  }

  function slope(points) {
    return (points[2].price - points[1].price) / (points[2].i - points[1].i);
  }

  function projection(points, reference) {
    const m = slope(points);
    return {slope_per_bar: m, reference_angle_degrees: Math.atan(m / reference) * 180 / Math.PI};
  }

  function label(result, timeframe) {
    const signed = n => (n >= 0 ? '+' : '') + n.toFixed(1);
    return `參考 Reference ${signed(result.reference_angle_degrees)}° · ${signed(result.slope_per_bar)} 點/${timeframe} candle`;
  }

  function regression(points) {
    const x = points.reduce((sum, p) => sum + p.i, 0) / points.length;
    const y = points.reduce((sum, p) => sum + p.price, 0) / points.length;
    let numerator = 0, denominator = 0;
    for (const p of points) {
      numerator += (p.i - x) * (p.price - y);
      denominator += (p.i - x) ** 2;
    }
    return {x, y, m: denominator ? numerator / denominator : 0};
  }

  // Reduced QR on three normalized Bezier basis columns; center prices to keep
  // a large absolute contract price from overwhelming a shallow curve's bend.
  function leastSquares(samples, indices) {
    const origin = samples[0].price;
    const columns = [[], [], []];
    for (const sample of samples) {
      const t = parameter(...indices, sample.i);
      columns[0].push((1 - t) ** 2);
      columns[1].push(2 * t * (1 - t));
      columns[2].push(t * t);
    }
    const q = [], r = Array.from({length: 3}, () => [0, 0, 0]);
    for (let j = 0; j < 3; j++) {
      const v = columns[j].slice();
      for (let k = 0; k < j; k++) {
        r[k][j] = q[k].reduce((sum, n, i) => sum + n * v[i], 0);
        for (let i = 0; i < v.length; i++) v[i] -= r[k][j] * q[k][i];
      }
      r[j][j] = Math.sqrt(v.reduce((sum, n) => sum + n * n, 0));
      if (r[j][j] < 1e-10) return null;
      q.push(v.map(n => n / r[j][j]));
    }
    const y = q.map(column => column.reduce((sum, n, i) => sum + n * (samples[i].price - origin), 0));
    const solution = [0, 0, 0];
    for (let j = 2; j >= 0; j--) {
      solution[j] = (y[j] - r[j].reduce((sum, n, i) => sum + (i > j ? n * solution[i] : 0), 0)) / r[j][j];
    }
    return solution.map(n => n + origin);
  }

  function turningPoints(bars, start, end, field, bottom, top) {
    const result = [], lower = field === 'low';
    for (let first = start; first <= end;) {
      let last = first;
      const price = bars[first][field];
      while (last < end && bars[last + 1][field] === price) last++;
      let extreme = true, strict = false;
      for (let j = Math.max(start, first - 2); j <= Math.min(end, last + 2); j++) {
        const other = bars[j][field];
        if (lower ? other < price : other > price) extreme = false;
        if (lower ? other > price : other < price) strict = true;
      }
      if (extreme && strict && price >= bottom && price <= top) result.push({i: Math.floor((first + last) / 2), price});
      first = last + 1;
    }
    return result;
  }

  function fit(bars, first, second, timeframe, now) {
    const start = Math.min(first.i, second.i), requestedEnd = Math.max(first.i, second.i);
    const bottom = Math.min(first.price, second.price), top = Math.max(first.price, second.price);
    let end = requestedEnd;
    while (end >= start && bars[end].time + seconds[timeframe] > now) end--;
    if (end - start < 2) throw new Error('至少選取 3 根已收盤 K 棒，請擴大範圍');
    const closes = [];
    for (let i = start; i <= end; i++) {
      const bar = bars[i];
      if (bar.high >= bottom && bar.low <= top) closes.push({i, price: bar.close});
    }
    if (closes.length < 2) throw new Error('價格範圍內的 K 棒不足，請擴大紅框');
    const trend = regression(closes);
    if (Math.abs(trend.m) < 1e-9) throw new Error('範圍內趨勢平坦，請選取更明確的上升或下降區間');
    const direction = trend.m > 0 ? 'up' : 'down';
    const samples = turningPoints(bars, start, end, direction === 'up' ? 'low' : 'high', bottom, top);
    if (samples.length < 2) throw new Error('紅框內至少需要 2 個局部' + (direction === 'up' ? '低點' : '高點') + '，請調整範圍');
    const indices = [start, Math.floor((start + end) / 2), end];
    let prices = samples.length >= 3 ? leastSquares(samples, indices) : null;
    const sign = direction === 'up' ? 1 : -1;
    // x(t) is strictly increasing. Ordered endpoint tangents guarantee both
    // monotonic prices and the requested bend everywhere, not just at sampled
    // candles: signed start slope >= 0 and signed end slope >= start slope.
    const initial = prices && sign * (prices[1] - prices[0]) / (indices[1] - indices[0]);
    const terminal = prices && sign * (prices[2] - prices[1]) / (indices[2] - indices[1]);
    const linear = !prices || !(initial >= 0 && terminal >= initial);
    if (linear) {
      const line = regression(samples);
      // Direction-constrained least-squares line through the same pivots.
      // Conflicting pivot/close trends have their best valid fit at slope zero.
      const m = sign * Math.max(0, sign * line.m);
      prices = indices.map(i => line.y + m * (i - line.x));
    }
    if (!prices.every(Number.isFinite)) throw new Error('無法計算穩定曲線，請調整範圍');
    const points = indices.map((i, j) => ({i, time: bars[i].time, price: prices[j]}));
    return {points, direction, samples, linear};
  }

  function translate(points, bars, deltaIndex, deltaPrice) {
    const shift = Math.max(-points[0].i, Math.min(bars.length - 1 - points[2].i, Math.round(deltaIndex)));
    return points.map(p => ({i: p.i + shift, time: bars[p.i + shift].time, price: p.price + deltaPrice}));
  }

  const api = {seconds, value, slope, projection, label, fit, translate};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.AutoCurve = api;
})(globalThis);
