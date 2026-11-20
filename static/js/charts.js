/* Post Office: interactive charts, drawn with the vendored Chart.js (static/js/vendor/chart.umd.min.js).

   The server puts a JSON description next to the canvases:

     <script type="application/json" data-po-charts>
       {"group": "engagement", "charts": [
         {"canvas": "ch-opens", "type": "line" | "bar", "labels": [...], "values": [...],
          "color": "#0B7A85", "unit": ["open", "opens"], "notes": ["Winter coat drive", ...],
          "peak": 12, "ticks": 5}
       ]}
     </script>

   Charts in one group share their time axis: pointing at a day in one highlights that day in all.
   No build step; plain script. */
(function () {
  "use strict";
  if (!window.Chart) return;

  var INK = "#1B1F2A", MUTED = "#5B6170", GRID = "#ECE8E1", BASE = "#D9D4CB";
  var FONT = 'Inter, system-ui, -apple-system, "Segoe UI", sans-serif';
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  Chart.defaults.font.family = FONT;
  Chart.defaults.font.size = 12;
  Chart.defaults.color = MUTED;

  function fmt(n) { return Math.round(n).toLocaleString("en-US"); }
  function withUnit(n, unit) { return fmt(n) + " " + (n === 1 ? unit[0] : unit[1]); }
  function rgba(hex, alpha) {
    var v = parseInt(hex.slice(1), 16);
    return "rgba(" + (v >> 16) + "," + ((v >> 8) & 255) + "," + (v & 255) + "," + alpha + ")";
  }
  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // A vertical guide line at the day being pointed at.
  var guide = {
    id: "poGuide",
    afterDatasetsDraw: function (chart) {
      var active = chart.getActiveElements();
      if (!active.length) return;
      var x = active[0].element.x, area = chart.chartArea, ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = rgba(INK, 0.28);
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x, area.top);
      ctx.lineTo(x, area.bottom);
      ctx.stroke();
      ctx.restore();
    }
  };

  // The busiest point keeps a label naming what caused it, until the pointer is on the chart.
  var peakLabel = {
    id: "poPeak",
    afterDatasetsDraw: function (chart) {
      var spec = chart.$po;
      if (!spec || spec.peak == null || chart.getActiveElements().length) return;
      var point = chart.getDatasetMeta(0).data[spec.peak];
      if (!point) return;
      var name = (spec.notes && spec.notes[spec.peak]) || spec.labels[spec.peak];
      var value = withUnit(spec.values[spec.peak], spec.unit);
      var ctx = chart.ctx, area = chart.chartArea;
      ctx.save();
      ctx.font = "500 12px " + FONT;
      var maxText = Math.max(60, (area.right - area.left) * 0.5);
      while (name.length > 4 && ctx.measureText(name).width > maxText) name = name.slice(0, -2).trimEnd() + "…";
      var w1 = ctx.measureText(name).width;
      ctx.font = "700 14px " + FONT;
      var w = Math.max(w1, ctx.measureText(value).width) + 24, h = 46;
      var x = point.x + 14;
      if (x + w > area.right) x = point.x - 14 - w;
      var y = Math.min(Math.max(point.y - h / 2, area.top), area.bottom - h);
      ctx.shadowColor = "rgba(27,31,42,.25)";
      ctx.shadowBlur = 16;
      ctx.shadowOffsetY = 6;
      ctx.fillStyle = INK;
      roundRect(ctx, x, y, w, h, 12);
      ctx.fill();
      ctx.shadowColor = "transparent";
      ctx.textBaseline = "top";
      ctx.fillStyle = "#C4E3E6";
      ctx.font = "500 12px " + FONT;
      ctx.fillText(name, x + 12, y + 8);
      ctx.fillStyle = "#FFFFFF";
      ctx.font = "700 14px " + FONT;
      ctx.fillText(value, x + 12, y + 24);
      ctx.restore();
    }
  };

  function build(canvas, spec, group) {
    var existing = Chart.getChart(canvas);
    if (existing) existing.destroy();
    var isLine = spec.type === "line";
    var n = spec.values.length;
    var every = Math.max(1, Math.ceil(n / (spec.ticks || 6)));
    var dataset = isLine ? {
      data: spec.values,
      borderColor: spec.color,
      borderWidth: 3,
      backgroundColor: rgba(spec.color, 0.12),
      fill: true,
      tension: 0.25,
      pointRadius: spec.values.map(function (_v, i) { return i === spec.peak ? 6 : 0; }),
      pointBackgroundColor: "#FFFFFF",
      pointBorderColor: spec.color,
      pointBorderWidth: 3,
      pointHoverRadius: 6,
      pointHoverBackgroundColor: "#FFFFFF",
      pointHoverBorderWidth: 3,
      pointHitRadius: 12
    } : {
      data: spec.values,
      backgroundColor: rgba(spec.color, 0.8),
      hoverBackgroundColor: spec.color,
      borderRadius: 4,
      borderSkipped: false,
      maxBarThickness: 28,
      minBarLength: 2
    };
    var chart = new Chart(canvas, {
      type: isLine ? "line" : "bar",
      data: { labels: spec.labels, datasets: [dataset] },
      plugins: [guide, peakLabel],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: reduceMotion ? false : { duration: 350 },
        interaction: { mode: "index", intersect: false },
        layout: { padding: { top: 8, right: 4 } },
        scales: {
          x: {
            grid: { display: false },
            border: { color: BASE },
            ticks: {
              autoSkip: false, maxRotation: 0,
              callback: function (_value, index) {
                var last = n - 1;
                if (index === last) return spec.labels[index];
                return (index % every === 0 && last - index >= every / 2) ? spec.labels[index] : "";
              }
            }
          },
          y: {
            beginAtZero: true,
            suggestedMax: spec.suggestedMax || 4,
            grid: { color: GRID },
            border: { display: false },
            ticks: { maxTicksLimit: spec.yTicks || 4, precision: 0, callback: function (v) { return fmt(v); } }
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: INK, titleColor: "#C4E3E6", bodyColor: "#FFFFFF", footerColor: "#C4E3E6",
            titleFont: { size: 12, weight: "500" }, bodyFont: { size: 14, weight: "700" }, footerFont: { size: 12, weight: "500" },
            padding: 10, cornerRadius: 12, displayColors: false, caretSize: 5,
            callbacks: {
              label: function (item) { return withUnit(item.parsed.y, spec.unit); },
              footer: function (items) {
                var note = spec.notes && spec.notes[items[0].dataIndex];
                return note || "";
              }
            }
          }
        },
        onHover: function (_event, elements) {
          if (!group) return;
          var index = elements.length ? elements[0].index : null;
          group.forEach(function (other) {
            if (other === chart) return;
            var active = index == null || index >= other.data.labels.length ? [] : [{ datasetIndex: 0, index: index }];
            other.setActiveElements(active);
            other.tooltip.setActiveElements(active, { x: 0, y: 0 });
            other.update("none");
          });
        }
      }
    });
    chart.$po = spec;
    canvas.addEventListener("mouseleave", function () {
      if (!group) return;
      group.forEach(function (other) {
        other.setActiveElements([]);
        other.tooltip.setActiveElements([], { x: 0, y: 0 });
        other.update("none");
      });
    });
    return chart;
  }

  function init(root) {
    (root || document).querySelectorAll("script[data-po-charts]").forEach(function (node) {
      var config;
      try { config = JSON.parse(node.textContent); } catch (e) { return; }
      var group = config.group ? [] : null;
      (config.charts || []).forEach(function (spec) {
        var canvas = document.getElementById(spec.canvas);
        if (!canvas) return;
        var chart = build(canvas, spec, group);
        if (group) group.push(chart);
      });
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { init(document); });
  else init(document);
  // HTMX swaps (the 7 / 30 / 90 day switch) bring a fresh description with them.
  document.addEventListener("htmx:afterSwap", function (e) {
    var target = e.target;
    init(target && target.parentNode ? target.parentNode : document);
  });
})();
