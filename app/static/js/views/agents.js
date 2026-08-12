/* Agents page — the full management table.
 *
 * The dashboard shows spend grouped by team; this shows every agent in one
 * sortable, filterable list with its controls inline, which is what you want
 * when the job is administration rather than watching.
 */

import {
  deleteAgent,
  editBudget,
  editSessionBudget,
  rotateKey,
  toggleStatus,
  toggleSubstitution,
  unblockAgent,
} from "../agent-actions.js";
import { openNewAgentModal } from "../agent-modal.js";
import { openChainEditor } from "../chain-editor.js";
import { store } from "../store.js";
import { $, esc, isBlocked, pctText, usd, usdPrecise } from "../util.js";

const state = { query: "", team: "all", sort: "spend", onlyProblems: false };

function statusBadge(agent) {
  if (isBlocked(agent)) return '<span class="badge badge--blocked">paused</span>';
  if (agent.status === "paused") return '<span class="badge badge--paused">paused</span>';
  return `<span class="badge badge--ok">active</span>`;
}

function rowMarkup(agent) {
  const blocked = isBlocked(agent);
  return `
    <tr data-agent="${agent.scope_id}" class="${blocked ? "row--blocked" : ""}">
      <td>
        <div class="cell-title">
          <a class="link" href="#/agents/${agent.scope_id}">${esc(agent.name)}</a>
          ${statusBadge(agent)}
        </div>
        <div class="cell-sub">${esc(agent.team_name)}</div>
      </td>
      <td class="mono cell-sub">${esc(agent.preferred_model)}</td>
      <td class="bar-cell">
        <div class="bar">
          <div class="bar-fill state-${agent.state}"
               style="width:${Math.min(100, agent.pct * 100)}%"></div>
        </div>
        <div class="cell-sub">
          ${usdPrecise(agent.consumed_usd)} / ${usd(agent.limit_usd)} · ${pctText(agent.pct)}
        </div>
      </td>
      <td class="num">${usdPrecise(agent.hour_spend_usd || 0)}</td>
      <td class="num">${agent.calls_today ?? 0}</td>
      <td>
        <button class="switch ${agent.allow_substitution ? "switch--on" : ""}"
                data-sub="${agent.scope_id}" role="switch"
                aria-checked="${agent.allow_substitution}"
                title="${
                  agent.allow_substitution
                    ? "Reroutes to a cheaper model under pressure"
                    : "Hard-blocks instead of substituting"
                }">
          <span class="switch-thumb"></span>
        </button>
      </td>
      <td class="row-actions">
        ${
          blocked
            ? `<button class="btn btn--danger btn--sm" data-unblock="${agent.scope_id}">Unblock</button>`
            : `<button class="btn btn--ghost btn--sm" data-toggle="${agent.scope_id}">
                 ${agent.status === "paused" ? "Resume" : "Pause"}
               </button>`
        }
        <button class="btn btn--ghost btn--sm" data-chain="${agent.scope_id}">Chain</button>
        <button class="btn btn--ghost btn--sm" data-budget="${agent.scope_id}">Budget</button>
        <button class="btn btn--ghost btn--sm" data-more="${agent.scope_id}">⋯</button>
      </td>
    </tr>`;
}

function filtered() {
  let agents = store.agents();
  if (state.team !== "all") {
    agents = agents.filter((a) => String(a.team_id) === String(state.team));
  }
  if (state.query) {
    const needle = state.query.toLowerCase();
    agents = agents.filter(
      (a) =>
        a.name.toLowerCase().includes(needle) ||
        a.preferred_model.toLowerCase().includes(needle) ||
        a.team_name.toLowerCase().includes(needle)
    );
  }
  if (state.onlyProblems) {
    agents = agents.filter((a) => isBlocked(a) || a.pct >= 0.8 || a.status === "paused");
  }

  const sorters = {
    spend: (a, b) => b.consumed_usd - a.consumed_usd,
    pct: (a, b) => b.pct - a.pct,
    name: (a, b) => a.name.localeCompare(b.name),
    team: (a, b) => a.team_name.localeCompare(b.team_name) || a.name.localeCompare(b.name),
    hour: (a, b) => (b.hour_spend_usd || 0) - (a.hour_spend_usd || 0),
  };
  return agents.sort(sorters[state.sort] || sorters.spend);
}

export function agentsView(outlet) {
  outlet.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>Agents</h2>
        <div class="panel-actions">
          <input id="agent-search" class="input-sm" type="search"
                 placeholder="Filter by name, team or model…" />
          <select id="agent-team" class="input-sm"></select>
          <select id="agent-sort" class="input-sm">
            <option value="spend">Sort: spend</option>
            <option value="pct">Sort: % consumed</option>
            <option value="hour">Sort: last hour</option>
            <option value="name">Sort: name</option>
            <option value="team">Sort: team</option>
          </select>
          <label class="check-inline">
            <input type="checkbox" id="agent-problems" /> Needs attention
          </label>
          <button class="btn btn--primary btn--sm" id="agents-new">+ New Agent</button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>Agent</th><th>Model</th><th>Monthly budget</th>
              <th class="num">Last hour</th><th class="num">Calls 24h</th>
              <th>Substitution</th><th></th>
            </tr>
          </thead>
          <tbody id="agents-body"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody>
        </table>
      </div>
      <div class="panel-foot"><span id="agents-count" class="cell-sub"></span></div>
    </section>`;

  function renderTeams() {
    const teams = store.status?.teams || [];
    const select = $("#agent-team", outlet);
    const current = select.value || "all";
    select.innerHTML =
      '<option value="all">All teams</option>' +
      teams
        .map((t) => `<option value="${t.scope_id}">${esc(t.name)}</option>`)
        .join("");
    select.value = current;
  }

  function render() {
    if (!store.status) return;
    renderTeams();
    const agents = filtered();
    $("#agents-body", outlet).innerHTML = agents.length
      ? agents.map(rowMarkup).join("")
      : '<tr><td colspan="7" class="empty">No agents match this filter.</td></tr>';
    $("#agents-count", outlet).textContent =
      `${agents.length} of ${store.agents().length} agents`;
  }

  const onInput = (event) => {
    if (event.target.id === "agent-search") state.query = event.target.value.trim();
    else if (event.target.id === "agent-team") state.team = event.target.value;
    else if (event.target.id === "agent-sort") state.sort = event.target.value;
    else if (event.target.id === "agent-problems") state.onlyProblems = event.target.checked;
    else return;
    render();
  };

  const onClick = async (event) => {
    const target = (attr) => event.target.closest(`[data-${attr}]`)?.dataset[attr];
    const agentFor = (id) => store.agent(id);

    if (event.target.id === "agents-new") return openNewAgentModal();

    const sub = target("sub");
    if (sub) return toggleSubstitution(agentFor(sub));

    const toggle = target("toggle");
    if (toggle) return toggleStatus(agentFor(toggle));

    const unblock = target("unblock");
    if (unblock) return unblockAgent(agentFor(unblock));

    const chain = target("chain");
    if (chain) return openChainEditor(agentFor(chain), () => store.refreshStatus());

    const budgetId = target("budget");
    if (budgetId) return editBudget(agentFor(budgetId));

    const more = target("more");
    if (more) {
      const agent = agentFor(more);
      const { openModal } = await import("../components.js");
      const handle = openModal({
        title: agent.name,
        size: "sm",
        body: `
          <div class="stack">
            <button class="btn" data-act="detail">Open detail page</button>
            <button class="btn" data-act="session">Edit session budget…</button>
            <button class="btn" data-act="rotate">Rotate API key…</button>
            <button class="btn btn--danger" data-act="delete">Delete agent…</button>
          </div>`,
      });
      handle.node.addEventListener("click", async (inner) => {
        const act = inner.target.dataset.act;
        if (!act) return;
        handle.close();
        if (act === "detail") window.location.hash = `/agents/${agent.scope_id}`;
        if (act === "session") await editSessionBudget(agent);
        if (act === "rotate") await rotateKey(agent);
        if (act === "delete") await deleteAgent(agent);
      });
    }
  };

  outlet.addEventListener("input", onInput);
  outlet.addEventListener("change", onInput);
  outlet.addEventListener("click", onClick);

  const unsubscribe = store.subscribe((_s, reason) => {
    // The feed churns far more often than the fleet does; repainting this
    // table on every event would fight with typing in the filter box.
    if (reason === "event" || reason === "events") return;
    render();
  });
  render();

  return { unmount: unsubscribe };
}
