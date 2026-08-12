/* Inline SVG charts.
 *
 * Hand-rolled rather than pulled from a library: a charting dependency means
 * either a CDN (which a self-hosted control plane should not need at runtime)
 * or a bundler (which this project deliberately does without). Two chart types
 * cover everything the detail page needs.
 *
 * Colours come from the same CSS custom properties as the rest of the UI, so
 * the charts follow the theme rather than carrying their own palette.
 */

import { esc } from "./util.js";

const PAD = { top: 12, right: 12, bottom: 22, left: 46 };

function niceCeiling(value) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * magnitude;
}

const money = (n) =>
  n >= 1 ? `$${n.toFixed(2)}` : n >= 0.01 ? `$${n.toFixed(3)}` : `$${n.toFixed(5)}`;

/**
 * Bar chart of spend over time.
 * `points` = [{ bucket: ISO string, usd, calls }]
 */
export function spendChart(points, { width = 720, height = 200, granularity = "hour" } = {}) {
  if (!points.length) {
    return `<div class="chart-empty">No calls in this window.</div>`;
  }

  const plotWidth = width - PAD.left - PAD.right;
  const plotHeight = height - PAD.top - PAD.bottom;
  const max = niceCeiling(Math.max(...points.map((p) => p.usd)));
  const barWidth = Math.max(1, plotWidth / points.length - 1);

  const bars = points
    .map((point, index) => {
      const x = PAD.left + (index * plotWidth) / points.length;
      const barHeight = max ? (point.usd / max) * plotHeight : 0;
      const y = PAD.top + plotHeight - barHeight;
      const when = new Date(point.bucket).toLocaleString();
      return (
        `<rect class="chart-bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" ` +
        `width="${barWidth.toFixed(1)}" height="${Math.max(0, barHeight).toFixed(1)}" ` +
        `rx="1"><title>${esc(when)}\n${money(point.usd)} · ${point.calls} call${
          point.calls === 1 ? "" : "s"
        }</title></rect>`
      );
    })
    .join("");

  // Three gridlines is enough to read a magnitude off; more is chartjunk.
  const gridlines = [0, 0.5, 1]
    .map((fraction) => {
      const y = PAD.top + plotHeight - fraction * plotHeight;
      return (
        `<line class="chart-grid" x1="${PAD.left}" y1="${y}" x2="${width - PAD.right}" y2="${y}" />` +
        `<text class="chart-axis" x="${PAD.left - 6}" y="${y + 3}" text-anchor="end">` +
        `${money(max * fraction)}</text>`
      );
    })
    .join("");

  const first = new Date(points[0].bucket);
  const last = new Date(points[points.length - 1].bucket);
  const fmt = (d) =>
    granularity === "hour"
      ? d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric" })
      : d.toLocaleDateString([], { month: "short", day: "numeric" });

  return `
    <svg class="chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"
         role="img" aria-label="Spend over time">
      ${gridlines}
      ${bars}
      <text class="chart-axis" x="${PAD.left}" y="${height - 6}">${esc(fmt(first))}</text>
      <text class="chart-axis" x="${width - PAD.right}" y="${height - 6}"
            text-anchor="end">${esc(fmt(last))}</text>
    </svg>`;
}

/**
 * Horizontal proportion bar — input vs output tokens, model mix, and so on.
 * `segments` = [{ label, value, className }]
 */
export function proportionBar(segments) {
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  if (!total) return `<div class="chart-empty">Nothing recorded yet.</div>`;

  const bars = segments
    .filter((s) => s.value > 0)
    .map(
      (s) =>
        `<div class="prop-seg ${s.className || ""}" style="width:${(
          (s.value / total) * 100
        ).toFixed(2)}%" title="${esc(s.label)}: ${s.value.toLocaleString()}"></div>`
    )
    .join("");

  const legend = segments
    .map(
      (s) =>
        `<span class="prop-key"><i class="${s.className || ""}"></i>${esc(s.label)}
          <strong>${s.value.toLocaleString()}</strong>
          <span class="muted">${((s.value / total) * 100).toFixed(1)}%</span></span>`
    )
    .join("");

  return `<div class="prop-bar">${bars}</div><div class="prop-legend">${legend}</div>`;
}
