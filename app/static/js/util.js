/* Formatting and DOM helpers shared by every view. */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const usd = (n) =>
  "$" +
  Number(n || 0).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

/** Single calls routinely cost a fraction of a cent; cent-rounding them all to
 *  "$0.00" tells the reader nothing. */
export const usdPrecise = (n) => {
  const v = Number(n || 0);
  if (v > 0 && v < 0.01) return "$" + v.toFixed(4);
  return usd(v);
};

export const pctText = (p) => (Number(p || 0) * 100).toFixed(1) + "%";

export const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

export const num = (n) => Number(n || 0).toLocaleString("en-US");

export function timeAgo(iso) {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export const clockTime = (iso) => new Date(iso || Date.now()).toLocaleTimeString();

/** Build an element from an HTML string. */
export function el(html) {
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  return template.content.firstElementChild;
}

/** Severity/state → CSS modifier, kept in one place so the palette stays
 *  consistent across the dashboard, the agents table and the event feed. */
export const stateClass = (state) => `state-${state || "ok"}`;

export function isBlocked(agent) {
  return Boolean(agent.blocked || agent.status === "blocked");
}
