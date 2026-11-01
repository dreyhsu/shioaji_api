'use strict';
const assert = require('node:assert/strict');
global.window = {};
require('../src/web/boxes_primitive.js');
const gap = new window.BoxesPrimitive({showLabels: false});
const scale = {options: () => ({barSpacing: 10}), timeToIndex: t => t,
  logicalToCoordinate: t => t * 10};
gap.attached({chart: {timeScale: () => scale},
  series: {priceToCoordinate: p => 300 - p}, requestUpdate() {}});
gap.setBoxes([{box_id: 'gap1', box_type: 'gap', top: 120, bottom: 110,
  left_time: 2, right_time: 5, open: true}]);
gap.updateAllViews();
assert.equal(gap.autoscaleInfo(0, 10), null);
assert.equal(gap.hitTest(30, 185, 400), null);
const fills = [];
const context = new Proxy({}, {get: (obj, key) => key === 'fillRect'
  ? (...args) => fills.push({args, style: obj.fillStyle}) : () => {}});
gap.paneViews()[0].renderer().draw({useBitmapCoordinateSpace: callback => callback({
  context, horizontalPixelRatio: 1, verticalPixelRatio: 1,
  bitmapSize: {width: 400, height: 300}})});
assert.deepEqual(fills[0].args, [15, 180, 385, 10]);
assert.equal(fills[0].style, 'rgba(128, 128, 128, 0.40)');
// A filled gap freezes on its historical bar instead of extending to pane edge.
gap.setBoxes([{box_id: 'gap1', box_type: 'gap', top: 120, bottom: 110,
  left_time: 2, right_time: 5, open: false}]);
gap.updateAllViews();
fills.length = 0;
gap.paneViews()[0].renderer().draw({useBitmapCoordinateSpace: callback => callback({
  context, horizontalPixelRatio: 2, verticalPixelRatio: 2,
  bitmapSize: {width: 800, height: 600}})});
assert.deepEqual(fills[0].args, [30, 360, 80, 20]);
console.log('Gap box rendering, fill extent, hit testing and scale checks passed');
