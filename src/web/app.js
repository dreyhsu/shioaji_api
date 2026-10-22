'use strict';
// The Plotly page, kept at /plotly as a reference to diff the
// lightweight-charts chart against.  Everything that renders /api/state
// lives in status.js, shared with lwc_app.js.

const REFRESH_MS = 60000;

let chartReady = false;

async function refresh() {
  try {
    const [stateResponse, figureResponse] = await Promise.all([
      fetch('/api/state'),
      fetch('/api/figure'),
    ]);

    if (stateResponse.ok) {
      const state = await stateResponse.json();
      if (state.ready) SNRStatus.renderAll(state);
      else SNRStatus.setFreshness(state.message || '尚無資料', 'warn');
    }

    if (figureResponse.ok) {
      const figure = await figureResponse.json();
      figure.layout.autosize = true;
      figure.layout.paper_bgcolor = '#1a1c23';
      figure.layout.plot_bgcolor = '#1a1c23';
      // react() diffs against the current figure, so a once-a-minute full
      // redraw of ~800 bars stays cheap and keeps zoom/pan where the user left it.
      await Plotly.react('chart', figure.data, figure.layout, { responsive: true, displaylogo: false });
      chartReady = true;
    } else if (!chartReady) {
      document.getElementById('chart').innerHTML =
        '<div class="empty" style="padding:24px">尚無足夠資料繪圖</div>';
    }
  } catch (error) {
    SNRStatus.setFreshness('連線失敗', 'bad');
    console.error(error);
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
