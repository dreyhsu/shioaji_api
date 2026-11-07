'use strict';
// Range interaction and draft-only editing. The adapter owns persistence and
// chart refresh; all pointer work stays local to this frozen chart snapshot.
class AutoCurveTool {
  constructor(options) {
    Object.assign(this, options);
    this.active = false;
    this.busy = false;
    this.frame = 0;
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'auto-curve-overlay';
    this.canvas.setAttribute('aria-hidden', 'true');
    this.element.appendChild(this.canvas);
    this.button.addEventListener('click', () => this.active ? this.cancel() : this.start());
    this.saveButton.addEventListener('click', () => this.commit());
    this.cancelButton.addEventListener('click', () => this.cancel());
    this.element.addEventListener('pointerdown', e => this.down(e), true);
    this.element.addEventListener('pointermove', e => this.move(e), true);
    this.element.addEventListener('pointerup', e => this.up(e), true);
    this.element.addEventListener('pointercancel', e => this.abortPointer(e), true);
    this.element.addEventListener('lostpointercapture', e => this.abortPointer(e), true);
    for (const type of ['mousedown', 'touchstart', 'dblclick']) {
      this.element.addEventListener(type, e => {
        if (this.active && (!this.draft || this.pointer)) this.consume(e);
      }, true);
    }
    this.element.addEventListener('wheel', () => this.schedule(), {passive: true});
    this.element.addEventListener('pointerleave', () => {
      if (!this.active) this.showProjection(null);
    });
    document.addEventListener('keydown', e => {
      if (!this.active || /INPUT|TEXTAREA|SELECT/.test(e.target?.tagName || '')) return;
      if (e.key === 'Escape') { e.preventDefault(); this.cancel(); }
      else if (e.key === 'Enter' && this.draft) { e.preventDefault(); this.commit(); }
    });
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(() => this.schedule());
    this.chart.subscribeCrosshairMove(() => this.schedule());
    this.observer = new ResizeObserver(() => this.schedule());
    this.observer.observe(this.element);
  }

  consume(event) {
    event.stopImmediatePropagation();
    if (event.cancelable) event.preventDefault();
  }

  start() {
    if (this.busy || !this.available()) return;
    this.beforeStart();
    const snapshot = this.snapshot();
    if (snapshot.bars.length < 3) return;
    this.bars = snapshot.bars.slice();
    this.timeframe = snapshot.timeframe;
    this.now = snapshot.now;
    this.active = true;
    this.first = this.second = this.draft = this.savedProjection = null;
    this.previewKey = null;
    this.button.classList.add('active');
    this.button.setAttribute('aria-pressed', 'true');
    this.cancelButton.hidden = false;
    this.lockSelection();
    this.hint.hidden = false;
    this.hint.textContent = '自動曲線：點起點，再移動滑鼠選取時間與價格範圍 · Esc 取消';
    this.element.style.cursor = 'crosshair';
    this.schedule();
  }

  lockSelection() {
    const settings = this.chart.options();
    this.interactions = {handleScroll: structuredClone(settings.handleScroll), handleScale: structuredClone(settings.handleScale)};
    this.chart.applyOptions({handleScroll: false, handleScale: false});
    this.selectionAutoScale = this.candles.priceScale().options().autoScale;
    this.candles.priceScale().applyOptions({autoScale: false});
  }

  unlockSelection() {
    if (!this.interactions) return;
    this.chart.applyOptions(this.interactions);
    this.candles.priceScale().applyOptions({autoScale: this.selectionAutoScale});
    this.interactions = null;
  }

  point(event, clamp = false) {
    const rect = this.element.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    const width = this.chart.timeScale().width(), height = this.chart.panes()[0].getHeight();
    if (!clamp && (x < 0 || x > width || y < 0 || y > height)) return null;
    const logical = this.chart.timeScale().coordinateToLogical(Math.max(0, Math.min(width, x)));
    const price = this.candles.coordinateToPrice(Math.max(0, Math.min(height, y)));
    if (logical === null || price === null || !Number.isFinite(price)) return null;
    const i = Math.max(0, Math.min(this.bars.length - 1, Math.round(logical)));
    return {i, price, x, y, logical};
  }

  draftHit(point) {
    if (!this.draft) return false;
    const points = this.draft.points;
    // Use the same sampled-segment distance as saved-curve hover, including the
    // tangent to the right. Only a few nearby segments need inspection.
    const spacing = this.chart.timeScale().options().barSpacing;
    const radius = Math.ceil(8 / spacing) + 1;
    const center = Math.floor(point.logical);
    for (let i = Math.max(points[0].i, center - radius); i <= Math.min(this.bars.length - 2, center + radius); i++) {
      const a = this.xy(i, AutoCurve.value(points, i));
      const b = this.xy(i + 1, AutoCurve.value(points, i + 1));
      if (a && b && CurveHover.distance(point, a, b) <= 8) return true;
    }
    return false;
  }

  down(event) {
    if (!this.active || event.button !== 0 || this.busy) return;
    const point = this.point(event);
    if (!point || (this.draft && !this.draftHit(point))) return;
    this.consume(event);
    this.pointer = {id: event.pointerId, start: point, clientX: event.clientX, clientY: event.clientY};
    if (this.draft) {
      this.pointer.points = this.draft.points.map(p => ({...p}));
      this.pointer.autoScale = this.candles.priceScale().options().autoScale;
      this.candles.priceScale().applyOptions({autoScale: false});
      this.element.style.cursor = 'grabbing';
    }
    this.element.setPointerCapture(event.pointerId);
  }

  move(event) {
    if (this.draft || this.savedProjection) this.schedule();
    if (!this.active) return;
    const point = this.point(event, !!this.pointer);
    if (!point) return;
    if (!this.draft || this.pointer) this.consume(event);
    if (this.pointer?.points) {
      this.draft.points = AutoCurve.translate(this.pointer.points, this.bars,
        point.logical - this.pointer.start.logical, point.price - this.pointer.start.price);
      this.schedule();
    } else if (!this.draft && this.first) {
      this.second = point;
      this.schedule();
    } else if (this.draft) {
      this.element.style.cursor = this.draftHit(point) ? 'grab' : '';
      // Box/order listeners must not overwrite the draft cursor or claim it.
      if (this.draftHit(point)) this.consume(event);
    }
  }

  finishPointer(rollback = false) {
    const pointer = this.pointer;
    this.pointer = null;
    if (!pointer) return;
    if (pointer.points) {
      if (rollback) this.draft.points = pointer.points;
      this.candles.priceScale().applyOptions({autoScale: pointer.autoScale});
    }
    if (this.element.hasPointerCapture(pointer.id)) this.element.releasePointerCapture(pointer.id);
    this.element.style.cursor = this.draft ? 'grab' : 'crosshair';
  }

  up(event) {
    if (!this.pointer || event.pointerId !== this.pointer.id) return;
    this.consume(event);
    const point = this.point(event, true);
    const pointer = this.pointer;
    this.finishPointer();
    if (pointer.points) { this.schedule(); return; }
    if (!point || Math.hypot(event.clientX - pointer.clientX, event.clientY - pointer.clientY) > 4) return;
    if (!this.first) {
      this.first = this.second = point;
      this.hint.textContent = '自動曲線：移動紅框，再點終點；只擬合框內局部高／低點 · Esc 取消';
    } else {
      this.second = point;
      try {
        const draft = AutoCurve.fit(this.bars, this.first, this.second, this.timeframe, this.now);
        const priceA = this.candles.coordinateToPrice(0), priceB = this.candles.coordinateToPrice(100);
        const reference = Math.abs(priceA - priceB) / 100 * this.chart.timeScale().options().barSpacing;
        if (!Number.isFinite(reference) || reference <= 0) throw new Error('價格比例無法讀取，請重新選取');
        this.reference = reference;
        this.draft = draft;
        this.preview = this.chart.addSeries(window.LightweightCharts.LineSeries, {
          color: '#f5c542', lineWidth: 2, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
          autoscaleInfoProvider: () => null,
        }, 0);
        this.unlockSelection();
        this.saveButton.hidden = false;
        this.hint.textContent = `自動曲線：${draft.direction === 'up' ? '上升／局部低點' : '下降／局部高點'} · ${draft.samples.length} 個轉折點${draft.linear ? '（直線擬合）' : ''} · 拖曳調整後按「儲存曲線」· Esc 取消`;
        this.element.style.cursor = 'grab';
      } catch (error) {
        this.hint.textContent = error.message + '；移動滑鼠再點終點，或 Esc 取消';
      }
    }
    this.schedule();
  }

  abortPointer(event) {
    if (!this.pointer || event.pointerId !== this.pointer.id) return;
    this.consume(event);
    const wasDrag = !!this.pointer.points;
    this.finishPointer(true);
    if (!wasDrag) this.cancel();
    else this.schedule();
  }

  async commit() {
    if (!this.draft || this.busy || this.pointer) return;
    this.busy = true;
    this.saveButton.disabled = this.cancelButton.disabled = this.button.disabled = true;
    try {
      await this.save({kind: 'curve', timeframe: this.timeframe,
        anchors: this.draft.points.map(p => ({time: p.time, price: p.price})),
        angle_reference_points_per_bar: this.reference});
      this.busy = false;
      this.cancel();
    } catch (error) {
      this.hint.textContent = '儲存失敗，草稿已保留：' + error.message + '；請重試或取消';
    } finally {
      this.busy = false;
      this.saveButton.disabled = this.cancelButton.disabled = this.button.disabled = false;
    }
  }

  cancel() {
    if (this.busy || !this.active) return;
    this.finishPointer(true);
    this.unlockSelection();
    if (this.preview) this.chart.removeSeries(this.preview);
    this.preview = this.draft = null;
    this.active = false;
    this.button.classList.remove('active');
    this.button.setAttribute('aria-pressed', 'false');
    this.saveButton.hidden = this.cancelButton.hidden = this.hint.hidden = true;
    this.element.style.cursor = '';
    this.schedule();
    this.afterEnd();
  }

  showProjection(curve) {
    if (this.active) return;
    this.savedProjection = curve;
    this.schedule();
  }

  schedule() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.render();
    });
  }

  xy(index, price) {
    const x = this.chart.timeScale().logicalToCoordinate(index), y = this.candles.priceToCoordinate(price);
    return x === null || y === null ? null : {x, y};
  }

  render() {
    const width = this.chart.timeScale().width(), height = this.chart.panes()[0].getHeight();
    const ratio = window.devicePixelRatio || 1;
    if (this.canvas.width !== Math.round(width * ratio) || this.canvas.height !== Math.round(height * ratio)) {
      this.canvas.width = Math.round(width * ratio);
      this.canvas.height = Math.round(height * ratio);
      this.canvas.style.width = width + 'px';
      this.canvas.style.height = height + 'px';
    }
    const ctx = this.canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (this.active && !this.draft && this.first && this.second) {
      const a = this.xy(this.first.i, this.first.price), b = this.xy(this.second.i, this.second.price);
      if (a && b) {
        ctx.fillStyle = 'rgba(239, 83, 80, 0.17)';
        ctx.strokeStyle = '#ef5350';
        ctx.lineWidth = 1;
        ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(a.x - b.x), Math.abs(a.y - b.y));
        ctx.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(a.x - b.x), Math.abs(a.y - b.y));
      }
    }
    if (this.draft) {
      const points = this.draft.points;
      const range = this.chart.timeScale().getVisibleLogicalRange();
      // Keep only visible samples and its immediate neighbors. Every time is
      // already on the candle grid, and setData never adds future timestamps.
      const start = Math.max(points[0].i, Math.floor(range?.from ?? 0) - 1);
      const end = Math.min(this.bars.length - 1, Math.ceil(range?.to ?? this.bars.length) + 1);
      const data = [];
      for (let i = start; i <= end; i++) data.push({time: this.bars[i].time, value: AutoCurve.value(points, i)});
      // setData emits crosshair events synchronously. Do not schedule another
      // frame unless the sampled draft or viewport actually changed.
      const key = [start, end, ...points.flatMap(p => [p.i, p.price])].join('|');
      if (key !== this.previewKey) {
        this.previewKey = key;
        this.preview.setData(data);
      }
      const index = Math.max(points[2].i, this.bars.length - 1);
      this.drawProjection(ctx, index, AutoCurve.value(points, index), AutoCurve.slope(points),
        AutoCurve.projection(points, this.reference), this.timeframe, width, height);
    } else if (this.savedProjection?.projection) {
      const curve = this.savedProjection;
      const bars = this.snapshot().bars;
      const index = bars.length - 1;
      const sample = curve.data[index];
      if (sample && Number.isFinite(sample.value)) {
        const scale = AutoCurve.seconds[this.snapshot().timeframe] / AutoCurve.seconds[curve.timeframe];
        this.drawProjection(ctx, index, sample.value, curve.projection.slope_per_bar * scale,
          curve.projection, curve.timeframe, width, height);
      }
    }
  }

  drawProjection(ctx, index, price, slope, info, timeframe, width, height) {
    const a = this.xy(index, price), b = this.xy(index + 8, price + slope * 8);
    if (!a || !b) return;
    ctx.strokeStyle = '#f5c542';
    ctx.lineWidth = 2;
    ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    ctx.setLineDash([]);
    // Fixed in a corner so an offscreen tangent cannot hide its reference angle.
    const text = AutoCurve.label(info, timeframe);
    ctx.font = '12px sans-serif';
    const size = Math.min(width - 8, ctx.measureText(text).width + 16);
    ctx.fillStyle = 'rgba(26, 28, 35, 0.94)';
    ctx.fillRect(4, 6, size, 25);
    ctx.fillStyle = '#f5c542';
    ctx.fillText(text, 12, Math.min(height - 4, 23), Math.max(0, width - 24));
  }
}
globalThis.AutoCurveTool = AutoCurveTool;
