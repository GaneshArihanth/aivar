/* Event detail overlay.
 *
 * The feed answers "what happened"; this answers "to which call, under which
 * rule, and by how much". Where an event carries a request id it is joined to
 * its ledger row, so a refusal can be inspected down to the tokens and the
 * hold that was released.
 *
 * No prompt or completion text is shown — none is stored. This system keeps
 * token counts and costs, never payloads.
 */

import { api } from "./api.js";
import { openDrawer } from "./components.js";
import { esc, num, usd, usdPrecise } from "./util.js";

const SEVERITY_LABEL = { info: "Info", warning: "Warning", critical: "Critical" };

const EXPLAIN = {
  "budget.warning":
    "A scope crossed its warning threshold. Fired once per scope per period, on the crossing.",
  "budget.rejected_budget":
    "The monthly budget for this scope was exhausted. The request was refused before dispatch, so it cost nothing.",
  "budget.rejected_session":
    "The per-session cap was reached. That session is closed; the agent's monthly budget is untouched and it may open a new one.",
  "budget.rejected_runaway":
    "The agent is paused by the runaway detector and stays paused until a human releases it.",
  "agent.runaway_blocked":
    "Spend velocity crossed the runaway threshold — far more per hour than the monthly budget implies. Paused for review.",
  "agent.unblocked": "A human reviewed the pause and released the agent.",
  "model.substituted":
    "Budget pressure rerouted the call down the fallback chain to a cheaper model.",
  "session.terminated": "An operator closed this session from the dashboard.",
  "agent.moved":
    "The agent was reassigned. Its spend and runaway state moved with it; the previous team's total did not change.",
  "agent.key_rotated": "A new API key was issued and the previous one revoked.",
  "agent.deleted": "The agent was retired and its key revoked. Its ledger history is kept.",
};

function row(label, value) {
  if (value === undefined || value === null || value === "") return "";
  return `<div class="kv"><span class="kv-key">${esc(label)}</span>
            <span class="kv-val">${value}</span></div>`;
}

const BREACH_VERB = {
  EXHAUSTED: "EXCEEDED",
  SESSION_EXHAUSTED: "EXCEEDED",
  SESSION_CLOSED: "CLOSED",
  BLOCKED: "PAUSED",
  PRESSURE: "UNDER PRESSURE",
};

function breachMarkup(event, payload) {
  if (payload.limit_usd === undefined) return "";

  // Taken from the decision, not from comparing the two numbers. A limit is
  // breached when consumed *plus this call's reservation* would exceed it, so
  // the recorded spend is still below the limit at the moment of refusal —
  // comparing them would label every exhaustion as mere "pressure".
  const verb =
    BREACH_VERB[payload.status] ||
    (event.type === "budget.warning" ? "THRESHOLD CROSSED" : "");
  const over = ["EXCEEDED", "PAUSED", "CLOSED"].includes(verb);

  return `
    <div class="breach ${over ? "breach--over" : ""}">
      <div class="breach-rule">${esc((payload.scope || "budget").toUpperCase())} LIMIT ${esc(verb)}</div>
      <div class="breach-figures">
        <strong>${usdPrecise(payload.consumed_usd)}</strong>
        <span class="muted">of</span>
        <strong>${usdPrecise(payload.limit_usd)}</strong>
      </div>
      ${
        payload.would_have_cost_usd
          ? `<div class="breach-note">
               This call reserved ${usdPrecise(payload.would_have_cost_usd)}, which
               ${over ? "did not fit in what was left" : "took it past the threshold"}.
             </div>`
          : ""
      }
    </div>`;
}

function callMarkup(call) {
  if (!call) return "";
  return `
    <h3 class="sub-head">The call</h3>
    <div class="kv-grid">
      ${row("Request", `<span class="mono">${esc(call.request_id)}</span>`)}
      ${row("Decision", `<span class="badge badge--paused">${esc(call.decision)}</span>`)}
      ${row("Requested", `<span class="mono">${esc(call.requested_model)}</span>`)}
      ${row(
        "Served",
        call.served_model
          ? `<span class="mono">${esc(call.served_model)}</span>${
              call.substituted ? ' <span class="badge badge--sub">substituted</span>' : ""
            }`
          : '<span class="muted">never dispatched</span>'
      )}
      ${row("Input tokens", num(call.prompt_tokens))}
      ${row("Output tokens", num(call.completion_tokens))}
      ${row("Latency", `${num(call.latency_ms)} ms`)}
      ${row("Reserved", usdPrecise(call.estimated_usd))}
      ${row("Charged", `<strong>${usdPrecise(call.cost_usd)}</strong>`)}
      ${row(
        "Refunded",
        call.refunded_usd > 0
          ? `${usdPrecise(call.refunded_usd)} <span class="muted">unused hold</span>`
          : null
      )}
      ${row("Session", `<span class="mono">${esc(call.session_id || "—")}</span>`)}
    </div>`;
}

export async function openEventDetail(event) {
  const payload = event.payload || {};
  const when = new Date(event.created_at);

  const handle = openDrawer({
    title: SEVERITY_LABEL[event.severity] || "Event",
    subtitle: event.type,
    body: `<div id="event-detail-body"><p class="empty">Loading…</p></div>`,
  });

  let call = null;
  if (payload.request_id) {
    try {
      call = await api.get(`/v1/budget/calls/${encodeURIComponent(payload.request_id)}`);
    } catch {
      call = null; // rejected before a ledger row existed, or already pruned
    }
  }

  const known = new Set([
    "request_id", "agent_id", "agent_name", "team_id", "team_name", "scope",
    "scope_id", "status", "session_id", "consumed_usd", "limit_usd",
    "would_have_cost_usd", "period", "requested_model",
  ]);
  const extra = Object.entries(payload).filter(([key]) => !known.has(key));

  const bodyNode = handle.query("#event-detail-body");
  bodyNode.innerHTML = `
    <div class="event-headline sev-${esc(event.severity || "info")}">
      <p>${esc(event.message || event.type)}</p>
      <span class="cell-sub">${when.toLocaleString()}</span>
    </div>

    ${EXPLAIN[event.type] ? `<p class="dialog-detail">${EXPLAIN[event.type]}</p>` : ""}

    ${breachMarkup(event, payload)}

    <h3 class="sub-head">Context</h3>
    <div class="kv-grid">
      ${row("Type", `<span class="mono">${esc(event.type)}</span>`)}
      ${row("Scope", `${esc(event.scope)} · ${esc(event.scope_id)}`)}
      ${row(
        "Agent",
        payload.agent_id
          ? `<a class="link" href="#/agents/${payload.agent_id}">${esc(
              payload.agent_name || payload.agent_id
            )}</a>`
          : null
      )}
      ${row("Team", payload.team_name ? esc(payload.team_name) : null)}
      ${row("Period", payload.period ? esc(payload.period) : null)}
      ${row("Actor", event.actor ? esc(event.actor) : null)}
      ${row("Reason", payload.reason ? esc(payload.reason) : null)}
    </div>

    ${callMarkup(call)}

    ${
      payload.request_id && !call
        ? `<p class="field-hint">No ledger row for this request — it was refused
             before any call was recorded, or the row has since been pruned.</p>`
        : ""
    }

    ${
      extra.length
        ? `<h3 class="sub-head">Raw payload</h3>
           <pre class="raw-json">${esc(JSON.stringify(Object.fromEntries(extra), null, 2))}</pre>`
        : ""
    }`;

  // Clicking an agent link should leave the overlay behind.
  bodyNode.querySelectorAll('a[href^="#/"]').forEach((link) =>
    link.addEventListener("click", () => handle.close())
  );

  return handle;
}
