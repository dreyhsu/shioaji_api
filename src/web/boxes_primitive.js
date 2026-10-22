'use strict';
// The S/R boxes, as a lightweight-charts series primitive.
//
// A box is bounded in *time* as well as price -- its right edge freezes the
// moment the next box of the same side is born -- which rules out the cheap
// substitutes: createPriceLine() spans the whole pane width with no time extent,
// and a Baseline/Area series can be time-bounded with whitespace but renders at
// the same z-level as the candles (so `hoveredSeriesOnTop` pops it in front of
// them) and has no vertical edges.  A primitive is the only thing that draws the
// shape the Pine indicator actually describes.
//
// Styling follows snr_core._box_style, including Pine's repaint rule: a broken
// support is drawn red and a broken resistance green, both dashed.

(function () {
  const BOX_FRESH = {
    support: { border: '#20ca26', fill: 'rgba(32, 202, 38, 0.18)' },
    resistance: { border: '#e92929', fill: 'rgba(233, 41, 41, 0.18)' },
  };
  const BOX_BROKEN = {
    support: { border: '#e92929', fill: 'rgba(233, 41, 41, 0.10)' },
    resistance: { border: '#20ca26', fill: 'rgba(32, 202, 38, 0.10)' },
  };

  function boxStyle(box) {
    const table = box.state === 'broken' ? BOX_BROKEN : BOX_FRESH;
    return table[box.box_type] || BOX_FRESH.support;
  }

  // ------------------------------------------------------------------ renderer
  function BoxesRenderer(items, opts) {
    this._items = items; // the pane view's live array, mutated in place
    this._opts = opts;
  }

  BoxesRenderer.prototype.draw = function (target) {
    const items = this._items;
    if (!items.length) return;
    const showLabels = this._opts.showLabels;

    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context;
      const hr = scope.horizontalPixelRatio;
      const vr = scope.verticalPixelRatio;
      const W = scope.bitmapSize.width;
      const H = scope.bitmapSize.height;

      // 1 CSS px, rounded to a whole device pixel so the border can be crisp at
      // all; centring the stroke on edge + lw/2 makes it paint [edge, edge+lw].
      const lw = Math.max(1, Math.round(hr));
      const off = lw / 2;
      const fontPx = Math.max(9, Math.round(10 * vr));
      const padX = Math.round(4 * hr);
      const padY = Math.round(3 * vr);

      ctx.save();
      ctx.lineCap = 'butt';

      for (let i = 0; i < items.length; i++) {
        const it = items[i];

        let x1 = Math.round(it.x1 * hr);
        let x2 = it.openRight ? W : Math.round(it.x2 * hr);
        let y1 = Math.round(it.y1 * vr);
        let y2 = Math.round(it.y2 * vr);
        if (x2 < x1) { const t = x1; x1 = x2; x2 = t; }
        if (y2 < y1) { const t = y1; y1 = y2; y2 = t; }

        if (x2 < 0 || x1 > W || y2 < 0 || y1 > H) continue;

        // A clamped edge must not be stroked, or a box scrolled half off the
        // left edge grows a fake vertical line along the pane border.
        const hasLeft = x1 >= 0;
        const hasRight = !it.openRight && x2 <= W;
        const hasTop = y1 >= 0;
        const hasBottom = y2 <= H;

        const cx1 = x1 < 0 ? 0 : x1;
        const cx2 = x2 > W ? W : x2;
        const cy1 = y1 < 0 ? 0 : y1;
        const cy2 = y2 > H ? H : y2;
        const w = cx2 - cx1;
        let h = cy2 - cy1;
        if (w <= 0) continue;
        if (h < lw) h = lw;

        ctx.fillStyle = it.fill;
        ctx.fillRect(cx1, cy1, w, h);

        ctx.strokeStyle = it.border;
        ctx.lineWidth = lw;
        ctx.setLineDash(it.dashed ? [4 * lw, 3 * lw] : []);

        ctx.beginPath();
        if (hasTop) { ctx.moveTo(cx1, cy1 + off); ctx.lineTo(cx2, cy1 + off); }
        if (hasBottom) { ctx.moveTo(cx1, cy1 + h - off); ctx.lineTo(cx2, cy1 + h - off); }
        if (hasLeft) { ctx.moveTo(cx1 + off, cy1); ctx.lineTo(cx1 + off, cy1 + h); }
        if (hasRight) { ctx.moveTo(cx2 - off, cy1); ctx.lineTo(cx2 - off, cy1 + h); }
        ctx.stroke();

        if (showLabels && w > 42 * hr && h > 15 * vr) {
          ctx.setLineDash([]);
          ctx.font = fontPx + 'px -apple-system, "Helvetica Neue", sans-serif';
          ctx.textBaseline = 'top';
          ctx.textAlign = 'left';
          ctx.fillStyle = it.border;
          ctx.fillText(it.label, cx1 + padX, cy1 + padY);
        }

        // Grips on the edges of a box that can be dragged, centred on the part of
        // the box that is on screen.
        if (it.editable) {
          ctx.setLineDash([]);
          ctx.fillStyle = it.border;
          const gw = Math.round(18 * hr);
          const gh = Math.max(2, Math.round((it.active ? 4 : 3) * vr));
          const gx = Math.round((cx1 + cx2) / 2 - gw / 2);
          if (hasTop) ctx.fillRect(gx, y1 - Math.floor(gh / 2), gw, gh);
          if (hasBottom) ctx.fillRect(gx, y2 - Math.floor(gh / 2), gw, gh);
        }

        // While dragging, print both edge prices at the right end of the box.
        if (it.active) {
          ctx.setLineDash([]);
          ctx.font = 'bold ' + fontPx + 'px -apple-system, "Helvetica Neue", sans-serif';
          ctx.textAlign = 'right';
          ctx.fillStyle = it.border;
          const tx = cx2 - padX - Math.round(60 * hr);
          if (hasTop) {
            ctx.textBaseline = 'bottom';
            ctx.fillText(it.topText, tx, y1 - padY);
          }
          if (hasBottom) {
            ctx.textBaseline = 'top';
            ctx.fillText(it.bottomText, tx, y2 + padY);
          }
        }
      }
      ctx.restore();
    });
  };

  // ----------------------------------------------------------------- pane view
  function BoxesPaneView(source) {
    this._source = source;
    this._items = [];
    // A stable renderer instance lets the library reuse its wrapper instead of
    // rebuilding it every frame.
    this._renderer = new BoxesRenderer(this._items, source._opts);
  }

  // 'bottom' is the only bucket painted before the series renderer, so it is
  // what puts the boxes behind the candles.  It also paints before the grid,
  // which is why lwc_app.js turns vertical grid lines off.
  BoxesPaneView.prototype.zOrder = function () { return 'bottom'; };
  BoxesPaneView.prototype.renderer = function () { return this._renderer; };

  BoxesPaneView.prototype.update = function () {
    const src = this._source;
    const items = this._items;
    items.length = 0;

    const series = src._series;
    const chart = src._chart;
    if (!series || !chart) return;

    const timeScale = chart.timeScale();
    const boxes = src._boxes;
    // logicalToCoordinate gives the CENTRE of a bar slot and rejects fractional
    // logicals outright -- its guard is `typeof t === 'number' && t % 1 === 0`,
    // and it returns 0 rather than null for anything else, so asking for
    // `index - 0.5` silently collapses every box to zero width.  Widen by half a
    // slot in pixels instead.  options().barSpacing reports the live spacing,
    // not the configured one, so this tracks zoom.
    const halfBar = timeScale.options().barSpacing / 2;

    for (let i = 0; i < boxes.length; i++) {
      const box = boxes[i];
      if (box._left === null || box._right === null) continue;

      // priceToCoordinate returns null only when the series has no data; prices
      // off the top or bottom of the pane come back as out-of-range numbers,
      // which is exactly what the renderer wants to clamp.
      const y1 = series.priceToCoordinate(box.top);
      const y2 = series.priceToCoordinate(box.bottom);
      if (y1 === null || y2 === null) continue;

      // Affine in the logical index for any integer, including negative and
      // past-the-end ones, so a box scrolled off the left edge still resolves to
      // a (negative) coordinate instead of disappearing.  null only when the
      // time scale is empty.
      const left = timeScale.logicalToCoordinate(box._left);
      const right = timeScale.logicalToCoordinate(box._right);
      if (left === null || right === null) continue;

      const style = boxStyle(box);
      items.push({
        x1: left - halfBar,
        x2: right + halfBar,
        y1: y1,
        y2: y2,
        openRight: !!box.open,
        fill: style.fill,
        border: style.border,
        dashed: box.state === 'broken',
        label: '#' + box.box_id + (box.adjusted ? ' ✎' : ''),
        editable: src.isEditable(box),
        active: box === src._active,
        topText: String(Math.round(box.top)),
        bottomText: String(Math.round(box.bottom)),
        box: box,
      });
    }
  };

  // ----------------------------------------------------------------- primitive
  function BoxesPrimitive(options) {
    this._opts = Object.assign({ showLabels: true }, options || {});
    this._boxes = [];
    this._chart = null;
    this._series = null;
    this._requestUpdate = null;
    this._paneView = new BoxesPaneView(this);
    this._paneViews = [this._paneView];
    this._signature = null;
    this._active = null; // the box being dragged
  }

  // Only the live boxes -- the ones whose right edge is still advancing -- can be
  // reshaped; the rest are history.
  BoxesPrimitive.prototype.isEditable = function (box) { return !!box.open; };

  // Hit test in pane CSS pixels.  Returns { box, part } with part 'top', 'bottom'
  // or 'body', or null.  Edges win over the body, and the grab band is a few px
  // wide so a thin box can still be moved by its body.
  BoxesPrimitive.prototype.hitTest = function (x, y, width) {
    const items = this._paneView._items;
    const tol = 5;
    let body = null;
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (!it.editable) continue;
      const x1 = Math.min(it.x1, it.x2);
      const x2 = it.openRight ? width : Math.max(it.x1, it.x2);
      if (x < x1 || x > x2) continue;
      const top = Math.min(it.y1, it.y2);
      const bottom = Math.max(it.y1, it.y2);
      if (Math.abs(y - top) <= tol) return { box: it.box, part: 'top' };
      if (Math.abs(y - bottom) <= tol) return { box: it.box, part: 'bottom' };
      if (!body && y > top && y < bottom) body = { box: it.box, part: 'body' };
    }
    return body;
  };

  // Live preview while dragging: move the edges in place and repaint.
  BoxesPrimitive.prototype.previewEdges = function (box, top, bottom) {
    box.top = top;
    box.bottom = bottom;
    this._active = box;
    if (this._requestUpdate) this._requestUpdate();
  };

  BoxesPrimitive.prototype.endPreview = function () {
    this._active = null;
    // Forces the next setBoxes through even if the server echoes the same payload
    // the preview started from (a cancelled or failed drag).
    this._signature = null;
    if (this._requestUpdate) this._requestUpdate();
  };

  BoxesPrimitive.prototype.attached = function (params) {
    this._chart = params.chart;
    this._series = params.series;
    this._requestUpdate = params.requestUpdate;
    this._resolveIndices();
    this._requestUpdate();
  };

  BoxesPrimitive.prototype.detached = function () {
    this._chart = null;
    this._series = null;
    this._requestUpdate = null;
  };

  BoxesPrimitive.prototype.paneViews = function () { return this._paneViews; };

  // The library calls this on every pan, zoom, resize, setData and applyOptions,
  // so it recomputes coordinates and nothing else.  A crosshair move does not
  // reach it -- that only repaints the top canvas, and we are on the bottom one.
  BoxesPrimitive.prototype.updateAllViews = function () { this._paneView.update(); };

  // Keep the two live boxes on screen -- and only those two.
  //
  // Letting every box vote looks right until a box from three weeks ago, 3,500
  // points below spot, is still inside the visible *logical* range: it drags the
  // scale down and squashes every candle into the top third of the pane.  Only
  // the boxes whose right edge is still advancing (`open`) matter, and there is
  // at most one per side -- the same two the side cards report.  They sit within
  // an ATR of recent price, so this widens the scale by a few points at most.
  BoxesPrimitive.prototype.autoscaleInfo = function (startLogical, endLogical) {
    let low = Infinity;
    let high = -Infinity;
    for (let i = 0; i < this._boxes.length; i++) {
      const box = this._boxes[i];
      if (!box.open || box._left === null || box._right === null) continue;
      if (box._right < startLogical || box._left > endLogical) continue;
      if (box.bottom < low) low = box.bottom;
      if (box.top > high) high = box.top;
    }
    if (low === Infinity) return null;
    return { priceRange: { minValue: low, maxValue: high } };
  };

  // 18 records at most, so the payload is replaced wholesale.  The signature
  // check exists only to skip a pointless full repaint once a minute when
  // nothing changed.
  BoxesPrimitive.prototype.setBoxes = function (boxes) {
    const incoming = boxes || [];
    const signature = JSON.stringify(incoming);
    if (signature === this._signature) return false;
    this._signature = signature;

    const copy = new Array(incoming.length);
    for (let i = 0; i < incoming.length; i++) {
      const box = Object.assign({}, incoming[i]);
      box._left = null;
      box._right = null;
      copy[i] = box;
    }
    this._boxes = copy;

    if (!this._chart) return true; // resolved in attached()
    this._resolveIndices();
    this._requestUpdate();
    return true;
  };

  // Call after the candles' setData() when the box payload itself was unchanged:
  // appending bars shifts every logical index, so they have to be re-resolved.
  BoxesPrimitive.prototype.refreshIndices = function () {
    if (!this._chart) return;
    this._resolveIndices();
    this._requestUpdate();
  };

  // Times -> logical indices, once per data refresh rather than once per frame.
  // timeToIndex(t, /*findNearest=*/true) is what does the clamping: it snaps a
  // too-early time to bar 0, a too-late one to the last bar, and an inexact one
  // to the first bar at or after it -- and never returns null on a non-empty
  // scale.  timeToCoordinate() cannot be used instead; it hard-codes
  // findNearest=false, so any box edge that is not exactly a bar timestamp
  // (box 18 ends at 05:00, past the 04:00 last bar) would silently vanish.
  // Asking the time scale rather than indexing our own bars array also keeps
  // this correct if an indicator series ever widens the logical grid.
  BoxesPrimitive.prototype._resolveIndices = function () {
    if (!this._chart) return;
    const timeScale = this._chart.timeScale();
    for (let i = 0; i < this._boxes.length; i++) {
      const box = this._boxes[i];
      box._left = timeScale.timeToIndex(box.left_time, true);
      box._right = timeScale.timeToIndex(box.right_time, true);
    }
  };

  window.BoxesPrimitive = BoxesPrimitive;
})();
