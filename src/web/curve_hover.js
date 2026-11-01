'use strict';
(function (root) {
  function distance(point, start, end) {
    const dx = end.x - start.x, dy = end.y - start.y;
    const size = dx * dx + dy * dy;
    const t = size ? Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / size)) : 0;
    return Math.hypot(point.x - start.x - t * dx, point.y - start.y - t * dy);
  }

  function hit(curves, point, options) {
    let closest = null;
    for (const curve of curves) {
      // Every drawing is sampled on the candle grid. Only adjacent segments
      // can be closest to the pointer, including when zoomed or panned.
      const radius = Math.max(2, Math.ceil(options.tolerance / (options.barSpacing || 1)) + 1);
      const center = Math.max(0, Math.min(curve.data.length - 2, Math.floor(options.logical)));
      const first = Math.max(0, center - radius);
      const last = Math.min(curve.data.length - 2, center + radius);
      let gap = Infinity;
      for (let index = first; index <= last; index++) {
        const a = curve.data[index], b = curve.data[index + 1];
        if (!Number.isFinite(a?.value) || !Number.isFinite(b?.value)) continue;
        const ax = options.x(a.time), bx = options.x(b.time);
        const ay = options.y(curve, a.value), by = options.y(curve, b.value);
        if ([ax, bx, ay, by].some(value => value === null || !Number.isFinite(value))) continue;
        gap = Math.min(gap, distance(point, {x: ax, y: ay}, {x: bx, y: by}));
      }
      if (gap <= options.tolerance && (!closest || gap < closest.distance - 1e-9 ||
          (Math.abs(gap - closest.distance) <= 1e-9 && Number(curve.id) < Number(closest.curve.id)))) {
        closest = {curve, distance: gap};
      }
    }
    return closest;
  }
  const api = {distance, hit};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CurveHover = api;
})(globalThis);
