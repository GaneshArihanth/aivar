/* Agent detail — spend history, token economics, live sessions, controls. */

import {
  deleteAgent,
  editBudget,
  editSessionBudget,
  rotateKey,
  toggleStatus,
  toggleSubstitution,
  unblockAgent,
} from "../agent-actions.js";
import { api } from "../api.js";
import { openChainEditor } from "../chain-editor.js";
import { confirmDialog, toast } from "../components.js";
import { boostAgent, openRateLimits } from "../controls.js";
import { proportionBar, spendChart } from "../charts.js";
import { store } from "../store.js";
import { $, esc, isBlocked, num, pctText, timeAgo, usd, usdPrecise } from "../util.js";

export function agentDetailView(outlet, params) {
  const agentId = params.id;
  let days = 7;
  let history = null;
  let sessions = [];
  let config = null;

  outlet.innerHTML = `<section class="panel"><div class="empty">Loading…</div></section>`;

  /**
   * Live spend comes from the status payload; the agent's *configuration* —
   * session cap, fallback chain, cross-provider permission — does not, because
   * the dashboard has no use for it. Merge the two here rather than widening
   * the status payload that every poll carries for every agent.
   */
  function agent() {
    const live = store.agent(agentId);
    if (!live && !config) return null;
    return { ...(config || {}), ...(live || {}), scope_id: agentId };
  }

  async function loadConfig() {
    try {
      const record = await api.get(`/admin/agents/${agentId}`);
      config = {
        name: record.name,
        team_name: record.team_name,
        preferred_model: record.preferred_model,
        status: record.status,
        allow_substitution: record.allow_substitution,
        allow_cross_provider: record.allow_cross_provider,
        session_budget_usd: record.session_budget_usd,
        limit_usd: record.monthly_budget_usd,
        fallback_chain: record.fallback_chain,
        chain_is_custom: record.chain_is_custom,
        key_prefix: record.key_prefix,
        rpm_limit: record.rpm_limit,
        tpm_limit: record.tpm_limit,
        runaway_hourly_fraction: record.runaway_hourly_fraction,
        id: record.id,
      };
    } catch {
      config = null;
    }
  }

  async function loadHistory() {
    try {
      history = await api.get(`/admin/agents/${agentId}/history?days=${days}`);
    } catch (err) {
      history = null;
      console.error("history failed", err);
    }
  }

  async function loadSessions() {
    try {
      sessions = (await api.get(`/admin/agents/${agentId}/sessions`)).sessions;
    } catch {
      sessions = [];
    }
  }

  function headerMarkup(a) {
    const blocked = isBlocked(a);
    return `
      <div class="detail-head">
        <div>
          <a class="link back" href="#/agents">← All agents</a>
          <h2 class="detail-title">
            ${esc(a.name)}
            ${
              blocked
                ? '<span class="badge badge--blocked">paused</span>'
                : a.status === "paused"
                ? '<span class="badge badge--paused">paused</span>'
                : ""
            }
          </h2>
          <p class="detail-sub">
            ${esc(a.team_name)} · <span class="mono">${esc(a.preferred_model)}</span>
          </p>
        </div>
        <div class="detail-actions">
          ${
            blocked
              ? `<button class="btn btn--danger btn--sm" data-act="unblock">Review &amp; resume</button>`
              : `<button class="btn btn--ghost btn--sm" data-act="toggle">
                   ${a.status === "paused" ? "Resume" : "Pause"}
                 </button>`
          }
          <button class="btn btn--ghost btn--sm" data-act="boost">+ Boost budget</button>
          <button class="btn btn--ghost btn--sm" data-act="rates">Rate limits</button>
          <button class="btn btn--ghost btn--sm" data-act="chain">Fallback chain</button>
          <button class="btn btn--ghost btn--sm" data-act="rotate">Rotate key</button>
          <button class="btn btn--ghost btn--sm danger" data-act="delete">Delete</button>
        </div>
      </div>

      ${
        blocked
          ? `<div class="blocked-banner detail-banner">
               <p><strong>Runaway detected.</strong> Spent
                  ${usdPrecise(a.hour_spend_usd)} in the last hour and was paused
                  pending human review.</p>
               <button class="btn btn--danger btn--sm" data-act="unblock">Review &amp; resume</button>
             </div>`
          : ""
      }`;
  }

  function budgetCards(a) {
    return `
      <div class="detail-grid">
        <div class="tile">
          <span class="tile-label">Monthly budget</span>
          <strong class="tile-value">${usdPrecise(a.consumed_usd)}</strong>
          <span class="tile-sub">of ${usd(a.limit_usd)} · ${pctText(a.pct)}</span>
          <div class="bar"><div class="bar-fill state-${a.state}"
               style="width:${Math.min(100, a.pct * 100)}%"></div></div>
          <button class="btn btn--ghost btn--sm tile-action" data-act="budget">Edit</button>
        </div>
        <div class="tile">
          <span class="tile-label">Last hour</span>
          <strong class="tile-value">${usdPrecise(a.hour_spend_usd || 0)}</strong>
          <span class="tile-sub">velocity the runaway detector watches</span>
        </div>
        <div class="tile">
          <span class="tile-label">Session cap</span>
          <strong class="tile-value">${usd(a.session_budget_usd ?? 0)}</strong>
          <span class="tile-sub">applies to each session separately</span>
          <button class="btn btn--ghost btn--sm tile-action" data-act="session-budget">Edit</button>
        </div>
        <div class="tile">
          <span class="tile-label">Fallback chain</span>
          <strong class="tile-value tile-value--sm">${
            (a.fallback_chain || [a.preferred_model]).length
          } step${(a.fallback_chain || []).length === 1 ? "" : "s"}</strong>
          <span class="tile-sub mono chain-preview">${esc(
            (a.fallback_chain || [a.preferred_model]).join(" → ")
          )}</span>
          <button class="btn btn--ghost btn--sm tile-action" data-act="chain">Edit chain</button>
        </div>
        <div class="tile">
          <span class="tile-label">Rate limits</span>
          <strong class="tile-value tile-value--sm">${
            a.rpm_limit || a.tpm_limit
              ? `${a.rpm_limit ? a.rpm_limit + "/min" : "—"}`
              : "None"
          }</strong>
          <span class="tile-sub">${
            a.tpm_limit
              ? `${a.tpm_limit.toLocaleString()} tokens/min`
              : "pacing only — budget is separate"
          }</span>
          <button class="btn btn--ghost btn--sm tile-action" data-act="rates">Adjust</button>
        </div>
        <div class="tile">
          <span class="tile-label">Substitution</span>
          <strong class="tile-value">${a.allow_substitution ? "On" : "Off"}</strong>
          <span class="tile-sub">
            ${
              a.allow_substitution
                ? "reroutes to a cheaper model under pressure"
                : "hard-blocks rather than downgrading"
            }
          </span>
          <button class="btn btn--ghost btn--sm tile-action" data-act="sub">
            ${a.allow_substitution ? "Disable" : "Enable"}
          </button>
        </div>
      </div>`;
  }

  function historyMarkup() {
    if (!history) return '<div class="chart-empty">History unavailable.</div>';
    const t = history.totals;
    return `
      <div class="panel-head">
        <h2>Spend history</h2>
        <div class="panel-actions seg">
          ${[7, 30, 90]
            .map(
              (d) =>
                `<button class="seg-btn ${d === days ? "seg-btn--on" : ""}"
                         data-days="${d}">${d}d</button>`
            )
            .join("")}
        </div>
      </div>
      <div class="panel-body">
        ${spendChart(history.series, { granularity: history.granularity })}
        <div class="stat-row">
          <div><span class="stat-label">Calls</span><strong>${num(t.calls)}</strong></div>
          <div><span class="stat-label">Spend</span><strong>${usdPrecise(t.usd)}</strong></div>
          <div><span class="stat-label">Avg latency</span><strong>${t.avg_latency_ms} ms</strong></div>
          <div><span class="stat-label">Slowest</span><strong>${num(t.max_latency_ms)} ms</strong></div>
          <div><span class="stat-label">Substituted</span><strong>${num(t.substituted)}</strong></div>
        </div>

        <h3 class="sub-head">Token split</h3>
        ${proportionBar([
          { label: "Input tokens", value: t.prompt_tokens, className: "seg-in" },
          { label: "Output tokens", value: t.completion_tokens, className: "seg-out" },
        ])}

        ${
          t.by_model.length
            ? `<h3 class="sub-head">Where the money went</h3>
               <table class="table table--compact">
                 <thead><tr><th>Model served</th><th class="num">Calls</th>
                   <th class="num">Spend</th></tr></thead>
                 <tbody>
                   ${t.by_model
                     .map(
                       (m) =>
                         `<tr><td class="mono">${esc(m.model)}</td>
                          <td class="num">${num(m.calls)}</td>
                          <td class="num">${usdPrecise(m.usd)}</td></tr>`
                     )
                     .join("")}
                 </tbody>
               </table>`
            : ""
        }
      </div>`;
  }

  function sessionsMarkup() {
    const open = sessions.filter((s) => s.status === "open");
    return `
      <div class="panel-head">
        <h2>Sessions</h2>
        <span class="panel-hint">${open.length} open · ${sessions.length} recent</span>
      </div>
      <div class="table-wrap">
        <table class="table table--compact">
          <thead>
            <tr><th>Session</th><th>Status</th><th class="num">Spend</th>
              <th>Opened</th><th></th></tr>
          </thead>
          <tbody>
            ${
              sessions.length
                ? sessions
                    .map(
                      (s) => `
                <tr>
                  <td class="mono cell-url">${esc(s.session_id)}</td>
                  <td>${
                    s.status === "open"
                      ? '<span class="badge badge--ok">open</span>'
                      : `<span class="badge badge--paused">${esc(s.status)}</span>`
                  }${
                    s.close_reason
                      ? `<div class="cell-sub">${esc(s.close_reason)}</div>`
                      : ""
                  }</td>
                  <td class="num">${usdPrecise(s.spend_usd)}${
                    s.limit_usd ? ` / ${usd(s.limit_usd)}` : ""
                  }</td>
                  <td class="cell-sub">${
                    s.opened_at ? timeAgo(new Date(s.opened_at * 1000).toISOString()) : "—"
                  }</td>
                  <td class="row-actions">
                    ${
                      s.status === "open"
                        ? `<button class="btn btn--ghost btn--sm danger"
                                   data-terminate="${esc(s.session_id)}">Terminate</button>`
                        : ""
                    }
                  </td>
                </tr>`
                    )
                    .join("")
                : '<tr><td colspan="5" class="empty">No sessions recorded yet.</td></tr>'
            }
          </tbody>
        </table>
      </div>`;
  }

  function render() {
    const a = agent();
    if (!a) {
      outlet.innerHTML = `
        <section class="panel"><div class="empty">
          <p>No such agent, or it has been deleted.</p>
          <p><a class="link" href="#/agents">Back to all agents</a></p>
        </div></section>`;
      return;
    }

    outlet.innerHTML = `
      <section class="panel panel--plain">${headerMarkup(a)}</section>
      ${budgetCards(a)}
      <div class="columns columns--detail">
        <section class="panel">${historyMarkup()}</section>
        <section class="panel">${sessionsMarkup()}</section>
      </div>`;
  }

  const onClick = async (event) => {
    const a = agent();
    if (!a) return;

    const dayButton = event.target.closest("[data-days]");
    if (dayButton) {
      days = Number(dayButton.dataset.days);
      await loadHistory();
      render();
      return;
    }

    const terminate = event.target.closest("[data-terminate]")?.dataset.terminate;
    if (terminate) {
      const ok = await confirmDialog({
        title: "Terminate session?",
        message: `Session ${terminate} will be closed immediately.`,
        detail:
          "The agent's monthly budget is untouched — it can open a new session and " +
          "carry on. This stops only this conversation.",
        confirmLabel: "Terminate session",
        danger: true,
      });
      if (!ok) return;
      try {
        await api.del(`/admin/agents/${agentId}/sessions/${encodeURIComponent(terminate)}`);
        toast("Session terminated");
        await loadSessions();
        render();
      } catch (err) {
        toast(err.message, "err");
      }
      return;
    }

    const act = event.target.closest("[data-act]")?.dataset.act;
    if (!act) return;

    if (act === "toggle") await toggleStatus(a);
    if (act === "sub") await toggleSubstitution(a);
    if (act === "budget") await editBudget(a);
    if (act === "session-budget") await editSessionBudget(a);
    if (act === "rotate") await rotateKey(a);
    if (act === "boost") await boostAgent(a);
    if (act === "rates") await openRateLimits(a, loadConfig);
    if (act === "unblock") await unblockAgent(a);
    if (act === "chain") openChainEditor(a, () => store.refreshStatus());
    if (act === "delete") {
      if (await deleteAgent(a)) window.location.hash = "/agents";
    }
  };

  outlet.addEventListener("click", onClick);

  const unsubscribe = store.subscribe((_s, reason) => {
    if (reason === "event" || reason === "events") return;
    render();
  });

  (async () => {
    if (!store.status) await store.refreshStatus();
    await Promise.all([loadConfig(), loadHistory(), loadSessions()]);
    render();
  })();

  // Sessions and history are not in the status payload, so they need their own
  // refresh; slower than the dashboard poll because they change less often.
  const timer = setInterval(async () => {
    await Promise.all([loadConfig(), loadHistory(), loadSessions()]);
    render();
  }, 15000);

  return {
    unmount() {
      unsubscribe();
      clearInterval(timer);
    },
  };
}
