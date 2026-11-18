'use strict';

// Price-to-body fill and broken trend lines, painted behind the candles.
// Points use exactly the candle grid, so this overlay cannot add phantom bars.
(function () {
  const UP = '#4caf50';
  const DOWN = '#ff4054';

  function SupertrendPrimitive(options = {}) {
    this.options = { upColor: UP, downColor: DOWN, fill: true, ...options };
    this.visible = true;
    this.data = [];
    this.indices = [];
    this.byTime = new Map();
    this.view = {
      zOrder: () => 'bottom',
      renderer: () => ({ draw: target => target.useMediaCoordinateSpace(scope => this.draw(scope)) }),
    };
  }
  SupertrendPrimitive.prototype.attached = function ({ series, chart, requestUpdate }) {
    this.series = series;
    this.chart = chart;
    this.requestUpdate = requestUpdate;
  };
  SupertrendPrimitive.prototype.detached = function () {
    this.series = null;
    this.chart = null;
    this.requestUpdate = null;
  };
  SupertrendPrimitive.prototype.paneViews = function () { return [this.view]; };
  SupertrendPrimitive.prototype.updateAllViews = function () {};
  SupertrendPrimitive.prototype.setData = function (payload) {
    this.data = payload?.data || [];
    this.byTime = new Map(this.data.map(point => [point.time, point]));
    this.indices = this.data.map(point => this.chart.timeScale().timeToIndex(point.time, false));
    this.requestUpdate?.();
  };
  SupertrendPrimitive.prototype.pointAt = function (time) { return this.byTime.get(time); };
  SupertrendPrimitive.prototype.latest = function () { return this.data[this.data.length - 1]; };
  SupertrendPrimitive.prototype.setVisible = function (visible) {
    this.visible = visible;
    this.requestUpdate?.();
  };
  SupertrendPrimitive.prototype.autoscaleInfo = function (start, end) {
    if (!this.visible) return null;
    let low = Infinity;
    let high = -Infinity;
    for (let i = Math.max(0, Math.floor(start)); i <= Math.min(this.data.length - 1, Math.ceil(end)); i++) {
      const point = this.data[i];
      if (Number.isFinite(point.value)) {
        low = Math.min(low, point.value);
        high = Math.max(high, point.value);
      }
    }
    return low === Infinity ? null : { priceRange: { minValue: low, maxValue: high } };
  };
  SupertrendPrimitive.prototype.draw = function ({ context: ctx, mediaSize: size }) {
    if (!this.visible || !this.chart || !this.series) return;
    const scale = this.chart.timeScale();
    const range = scale.getVisibleLogicalRange();
    if (!range) return;
    const first = Math.max(0, Math.floor(range.from) - 1);
    const last = Math.min(this.data.length - 1, Math.ceil(range.to) + 1);
    const halfBar = scale.options().barSpacing * 0.35;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, size.width, size.height);
    ctx.clip();
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    let run = [];
    let direction = null;
    const flush = () => {
      if (!run.length) return;
      const points = run.length === 1
        ? [{ ...run[0], x: run[0].x - halfBar }, { ...run[0], x: run[0].x + halfBar }]
        : run;
      if (this.options.fill) {
        ctx.fillStyle = direction === 1 ? 'rgba(76, 175, 80, 0.09)' : 'rgba(255, 64, 84, 0.09)';
        ctx.beginPath();
        ctx.moveTo(points[0].x, points[0].y);
        for (let i = 1; i < points.length; i++) ctx.lineTo(points[i].x, points[i].y);
        for (let i = points.length - 1; i >= 0; i--) ctx.lineTo(points[i].x, points[i].body);
        ctx.closePath();
        ctx.fill();
      }
      ctx.strokeStyle = direction === 1 ? this.options.upColor : this.options.downColor;
      ctx.beginPath();
      ctx.moveTo(points[0].x, points[0].y);
      for (let i = 1; i < points.length; i++) ctx.lineTo(points[i].x, points[i].y);
      ctx.stroke();
      run = [];
    };
    for (let i = first; i <= last; i++) {
      const point = this.data[i];
      const index = this.indices[i];
      if (!Number.isFinite(point.value) || index === null) { flush(); direction = null; continue; }
      const x = scale.logicalToCoordinate(index);
      const y = this.series.priceToCoordinate(point.value);
      const body = this.options.fill ? this.series.priceToCoordinate(point.midpoint) : y;
      if (x === null || y === null || body === null) { flush(); direction = null; continue; }
      if (direction !== point.direction) flush();
      direction = point.direction;
      run.push({ x, y, body });
    }
    flush();
    ctx.restore();
  };
  window.SupertrendPrimitive = SupertrendPrimitive;
})();
