'use strict';
const assert = require('node:assert/strict');
const {hit, distance} = require('../src/web/curve_hover.js');
const line = (id, value, extra = {}) => ({id, label: 'Curve ' + id, timeframe: '1h',
  data: [0, 1, 2, 3].map(time => ({time, value})), ...extra});
const options = {logical: 1.5, tolerance: 8, x: value => value * 100, y: (curve, value) => value};
assert.equal(distance({x: 50, y: 55}, {x: 0, y: 50}, {x: 100, y: 50}), 5);
assert.equal(hit([line(2, 50), line(1, 50)], {x: 150, y: 55}, options).curve.id, 1);
assert.equal(hit([line(2, 52), line(1, 50)], {x: 150, y: 53}, options).curve.id, 2);
assert.equal(hit([line(1, 50)], {x: 150, y: 59}, options), null);
assert.equal(hit([line(1, 50, {data: [{time: 0}, {time: 1}, {time: 2}]})], {x: 150, y: 50}, options), null);
const moved = {...options, x: value => value * 200 - 100, y: (curve, value) => value * 2 - 40};
assert.equal(hit([line(1, 50)], {x: 200, y: 63}, moved).curve.id, 1);
assert.equal(hit([], {x: 150, y: 50}, options), null);
const zoomedOut = {...options, logical: 8, barSpacing: .5, x: value => value * .5};
assert.equal(hit([line(1, 50)], {x: 4, y: 50}, zoomedOut).curve.id, 1);
console.log('8 curve hover geometry checks passed.');
