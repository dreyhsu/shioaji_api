'use strict';

// A single selected order, drawn in price coordinates and a fixed pane-relative
// horizontal span. Canvas-only: zoom, pane resizing and high DPI use LWC's renderer.
(function () {
  const num = (value) => Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 });
  const signed = (value) => `${value > 0 ? '+' : ''}${num(value)}`;
  const finite = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
  const green = '#009b83';
  const red = '#ee465b';

  function PnlPrimitive() {
    this.order = null;
    this.pointer = null;
    this.dragging = false;
    this.labelBounds = [];
    this.view = {
      zOrder: () => 'top',
      renderer: () => ({ draw: (target) => target.useMediaCoordinateSpace((scope) => this.draw(scope)) }),
    };
  }
  PnlPrimitive.prototype.attached = function ({ series, chart, requestUpdate }) {
    this.series = series;
    this.chart = chart;
    this.requestUpdate = requestUpdate;
  };
  PnlPrimitive.prototype.detached = function () { this.series = null; this.requestUpdate = null; };
  PnlPrimitive.prototype.paneViews = function () { return [this.view]; };
  PnlPrimitive.prototype.updateAllViews = function () {};
  PnlPrimitive.prototype.setOrder = function (order) {
    if (this.order?.id !== order?.id) {
      this.pointer = null;
      this.labelBounds = [];
    }
    this.order = order;
    this.requestUpdate?.();
    return this.isHovered();
  };
  PnlPrimitive.prototype.isHovered = function () {
    if (!this.order || !this.series || !this.chart) return false;
    if (this.dragging) return true;
    if (!this.pointer) return false;
    const { x, y } = this.pointer;
    const width = this.chart.timeScale().width();
    const height = this.chart.panes()[0].getHeight();
    if (x < Math.round(width * 0.65) || x > width - 6 || y < 0 || y > height) return false;
    const order = this.order;
    const prices = [order.avg_fill_price || order.price, order.stop_price,
      order.curve_id ? order.curve_price : order.target_price];
    const ys = prices.filter(finite).map((price) => this.series.priceToCoordinate(Number(price))).filter(finite);
    return (ys.length > 0 && y >= Math.min(...ys) - 7 && y <= Math.max(...ys) + 7)
      || this.labelBounds.some((bounds) => y >= bounds.top && y <= bounds.bottom);
  };
  PnlPrimitive.prototype.setPointer = function (point) {
    const before = this.isHovered();
    this.pointer = point;
    const after = this.isHovered();
    if (before !== after) this.requestUpdate?.();
    return after;
  };
  PnlPrimitive.prototype.setDragging = function (dragging) {
    this.dragging = dragging;
    this.requestUpdate?.();
    return this.isHovered();
  };
  PnlPrimitive.prototype.draw = function ({ context: ctx, mediaSize: size }) {
    const order = this.order;
    if (!order || !this.series) return;
    const entry = Number(order.avg_fill_price || order.price);
    if (!(entry > 0)) return;
    const direction = order.action === 'Buy' ? 1 : -1;
    const x1 = Math.round(size.width * 0.65);
    const x2 = size.width - 6;
    const yEntry = this.series.priceToCoordinate(entry);
    if (yEntry === null) return;
    const hovered = this.isHovered();
    this.labelBounds = [];
    const editable = order.risk_editable;
    const levels = [
      { name: order.curve_id ? 'Target · 曲線' : 'Target', price: order.curve_id ? order.curve_price : order.target_price, editable: editable && !order.curve_id },
      { name: 'Stop', price: order.stop_price, editable },
    ];
    const labels = [];
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, size.width, size.height); ctx.clip();
    ctx.font = '11px -apple-system, "Helvetica Neue", sans-serif';
    for (const level of levels) {
      if (!finite(level.price)) {
        labels.push({ y: 3, color: '#637186', lines: [level.name, '暫不可用'] });
        continue;
      }
      const price = Number(level.price);
      const points = direction * (price - entry);
      const color = points >= 0 ? green : red;
      const y = this.series.priceToCoordinate(price);
      if (y === null) continue;
      ctx.fillStyle = points >= 0 ? 'rgba(0, 155, 131, .17)' : 'rgba(238, 70, 91, .17)';
      ctx.fillRect(x1, Math.min(y, yEntry), x2 - x1, Math.abs(y - yEntry));
      if (hovered) {
        ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.strokeRect(x1, Math.min(y, yEntry), x2 - x1, Math.abs(y - yEntry));
      }
      if (hovered && level.editable) {
        ctx.fillStyle = '#fff'; ctx.strokeStyle = '#377bff';
        for (const x of [x1, x2]) {
          ctx.fillRect(x - 4, y - 4, 8, 8); ctx.strokeRect(x - 4, y - 4, 8, 8);
        }
      }
      labels.push({ y: y + (y < yEntry ? -41 : 5), color,
        lines: [`${level.name} ${num(price)}`, `${signed(points)} 點 (${signed(points / entry * 100)}%)`] });
    }
    if (!hovered) { ctx.restore(); return; }
    ctx.strokeStyle = '#c9a43b'; ctx.lineWidth = 2; ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(x1, yEntry); ctx.lineTo(x2, yEntry); ctx.stroke();
    const qty = Math.max(0, Number(order.filled_quantity) - Number(order.exit_filled_quantity));
    const floating = finite(order.last_quote) ? direction * (Number(order.last_quote) - entry) : null;
    const target = order.curve_id ? order.curve_price : order.target_price;
    const reward = finite(target) ? direction * (Number(target) - entry) : null;
    const risk = finite(order.stop_price) ? direction * (entry - Number(order.stop_price)) : null;
    const ratio = reward > 0 && risk > 0 ? num(reward / risk) : risk <= 0 ? '—／已保本' : '—';
    labels.push({ y: yEntry - 24, color: floating === null || floating >= 0 ? green : red,
      lines: qty ? [`均價 ${num(entry)} · ${qty} 口`, `浮盈虧 ${floating === null ? '—' : signed(floating)} 點／口`, `R/R ${ratio}`]
        : [`限價 ${num(entry)}`, `${order.quantity} 口（未成交預覽）`, `R/R ${ratio}`] });
    // Place labels independently of the fills; prevent overlap near break-even.
    labels.sort((a, b) => a.y - b.y);
    const gap = 4;
    for (let i = 0; i < labels.length; i++) {
      labels[i].height = labels[i].lines.length * 15 + 8;
      labels[i].y = Math.max(i ? labels[i - 1].y + labels[i - 1].height + gap : 3, labels[i].y);
    }
    const overflow = Math.max(0, labels.at(-1).y + labels.at(-1).height + 3 - size.height);
    for (const label of labels) {
      const y = Math.max(2, label.y - overflow);
      const width = Math.min(x2 - x1, Math.max(...label.lines.map((line) => ctx.measureText(line).width)) + 16);
      const x = x2 - width;
      ctx.fillStyle = label.color;
      ctx.fillRect(x, y, width, label.height);
      ctx.fillStyle = '#fff'; ctx.textBaseline = 'middle';
      label.lines.forEach((line, index) => ctx.fillText(line, x + 8, y + 12 + index * 15, Math.max(1, width - 16)));
      this.labelBounds.push({ top: y, bottom: y + label.height });
    }
    ctx.restore();
  };
  window.PnlPrimitive = PnlPrimitive;
})();
