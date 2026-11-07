'use strict';
const assert = require('node:assert/strict');
const {performance} = require('node:perf_hooks');
const fit = require('../src/web/auto_curve.js');
const near = (a, b, tolerance = 1e-7) => assert.ok(Math.abs(a - b) < tolerance, `${a} != ${b}`);

function bars(count = 31, down = false, interval = 900) {
  return Array.from({length: count}, (_, i) => {
    const low = 100 + .2 * i + .01 * i * i + [4, 2, 0, 2, 4][i % 5];
    const bar = {time: 100000 + i * interval, low, high: low + 10, open: low + 4, close: low + 5};
    return down ? {...bar, low: 400 - bar.high, high: 400 - bar.low, open: 400 - bar.open, close: 400 - bar.close} : bar;
  });
}

function run(frame, first = {i: 0, price: 0}, second = {i: frame.length - 1, price: 1000}, timeframe = '15m', now = Infinity) {
  return fit.fit(frame, first, second, timeframe, now);
}

const rising = run(bars());
assert.equal(rising.direction, 'up');
assert.ok(rising.samples.length >= 3);
for (let i = 0; i <= 30; i++) near(fit.value(rising.points, i), 100 + .2 * i + .01 * i * i);
near(fit.slope(rising.points), .8);
near(fit.value(rising.points, 34), fit.value(rising.points, 30) + 4 * .8);
assert.equal(fit.value(rising.points, -1), null);
const falling = run(bars(31, true));
assert.equal(falling.direction, 'down');
near(fit.slope(falling.points), -.8);

function assertDirection(result) {
  const sign = result.direction === 'up' ? 1 : -1;
  const [a, b, c] = result.points;
  const initial = sign * (b.price - a.price) / (b.i - a.i);
  const terminal = sign * fit.slope(result.points);
  assert.ok(initial >= -1e-8 && terminal >= initial - 1e-8);
  let previous = fit.value(result.points, a.i);
  // Fractional samples catch reversals hidden between candles; include the
  // upcoming-candle tangent rather than checking only the selected endpoints.
  for (let x = a.i + .1; x <= c.i + 8; x += .1) {
    const price = fit.value(result.points, x);
    assert.ok(sign * (price - previous) >= -1e-8);
    previous = price;
  }
}
assertDirection(rising);
assertDirection(falling);
for (const down of [false, true]) {
  for (const baseline of [i => 100 - .8 * i + .05 * i * i,
    i => 100 + 4 * i - .05 * i * i, i => 100 + 3.2 * i - .1 * i * i]) {
    const frame = Array.from({length: 31}, (_, i) => {
      const base = baseline(i), low = base + [40, 20, 0, 20, 40][i % 5];
      const bar = {time: 100000 + i * 900, low, high: base + 80, open: base + 50, close: base + 50};
      return down ? {...bar, low: 600 - bar.high, high: 600 - bar.low,
        open: 600 - bar.open, close: 600 - bar.close} : bar;
    });
    const constrained = run(frame);
    assert.equal(constrained.direction, down ? 'down' : 'up');
    assert.equal(constrained.linear, true);
    assert.ok(constrained.samples.length >= 3);
    assertDirection(constrained);
  }
  // Wicks can trend against the closes. Their unconstrained line must not
  // override the detected trend, even when only two pivots are selected.
  for (const count of [11, 31]) {
    const frame = Array.from({length: count}, (_, i) => {
      const bar = {time: 100000 + i * 900, low: 100 - i + [40, 20, 0, 20, 40][i % 5],
        high: 210 + i, open: 200 + i, close: 200 + i};
      return down ? {...bar, low: 600 - bar.high, high: 600 - bar.low,
        open: 600 - bar.open, close: 600 - bar.close} : bar;
    });
    const constrained = run(frame);
    assert.equal(constrained.direction, down ? 'down' : 'up');
    assert.equal(constrained.linear, true);
    near(fit.slope(constrained.points), 0);
    assertDirection(constrained);
  }
}
const reversed = run(bars(), {i: 30, price: 1000}, {i: 0, price: 0});
assert.deepEqual(reversed, rising);

const bounded = run(bars(), {i: 0, price: 103}, {i: 30, price: 112});
assert.ok(bounded.samples.length < rising.samples.length);
assert.ok(bounded.samples.every(p => p.price >= 103 && p.price <= 112));
const two = run(bars(11));
assert.equal(two.samples.length, 2);
assert.equal(two.linear, true);
near(fit.value(two.points, 5), (two.points[0].price + two.points[2].price) / 2);

const plateau = bars();
plateau[3].low = plateau[2].low;
const flattened = run(plateau);
assert.equal(flattened.samples.filter(p => p.i === 2 || p.i === 3).length, 1);
assert.throws(() => run(bars(), {i: 0, price: 2000}, {i: 30, price: 2100}), /不足/);
assert.throws(() => run(bars(), {i: 0, price: 102}, {i: 30, price: 103}), /至少需要/);
assert.throws(() => run(bars(), {i: 0, price: 0}, {i: 1, price: 1000}), /3 根/);
assert.throws(() => run(bars().map(b => ({...b, close: 100}))), /平坦/);

for (const timeframe of ['1m', '5m', '15m', '1h']) {
  const frame = bars(31, false, fit.seconds[timeframe]);
  const last = frame.at(-1);
  last.low = -1e6; last.high = 1e6; last.close = -1e6;
  const result = run(frame, undefined, undefined, timeframe, last.time + fit.seconds[timeframe] / 2);
  assert.equal(result.direction, 'up');
  assert.equal(result.points.at(-1).i, 29);
  assertDirection(result); // Odd span: control is not at the exact midpoint.
}
const gap = bars().map((b, i) => ({...b, time: b.time + (i >= 15 ? 36000 : 0)}));
assert.deepEqual(run(gap).points.map(p => p.price), rising.points.map(p => p.price));
const shifted = fit.translate(rising.points, bars(50), 5.2, 12.5);
near(fit.slope(shifted), fit.slope(rising.points));
near(fit.value(shifted, 15), fit.value(rising.points, 10) + 12.5);
assert.equal(fit.translate(shifted, bars(50), -100, 0)[0].i, 0);
assert.equal(fit.translate(shifted, bars(50), 100, 0).at(-1).i, 49);
const reference = fit.projection(rising.points, .8);
near(reference.reference_angle_degrees, 45);
assert.deepEqual(fit.projection(shifted, .8), reference);
assert.match(fit.label(reference, '15m'), /\+45.0°.*\+0.8.*15m/);

// A month of minute candles: one fit, with no per-pointer scan of history.
const large = bars(34200, false, 60).map((b, i) => {
  const low = 100 + i * .02 + [4, 2, 0, 2, 4][i % 5];
  return {...b, low, high: low + 10, close: low + 5, open: low + 4};
});
const started = performance.now();
const result = run(large, undefined, undefined, '1m');
const elapsed = performance.now() - started;
assert.equal(result.direction, 'up');
assertDirection(result);
assert.ok(elapsed < 1000, `month fit took ${elapsed.toFixed(1)}ms`);
console.log(`Auto curve fitting/geometry checks passed; 34,200-bar fit ${elapsed.toFixed(1)}ms`);
