/* Teams hub — budgets per department, and moving agents between them.
 *
 * Drag-and-drop is the quick path; every move is also available from a menu on
 * each agent chip, because a drag-only control cannot be operated by keyboard
 * and this page decides where money is allowed to go.
 */

import { api } from "../api.js";
import { confirmDialog, openModal, promptDialog, toast } from "../components.js";
import { freezeState, toggleTeamFreeze } from "../controls.js";
import { store } from "../store.js";
import { $, esc, isBlocked, pctText, usd, usdPrecise } from "../util.js";

let dragging = null;

function agentChip(agent) {
  const blocked = isBlocked(agent);
  return `
    <li class="chip ${blocked ? "chip--blocked" : ""} ${
      agent.status === "paused" ? "chip--paused" : ""
    }" draggable="true" data-chip="${agent.scope_id}" data-team="${agent.team_id}">
      <span class="chip-grip" aria-hidden="true">⠿</span>
      <a class="chip-name link" href="#/agents/${agent.scope_id}">${esc(agent.name)}</a>
      <span class="chip-spend">${usdPrecise(agent.consumed_usd)}</span>
      <button class="icon-btn chip-move" data-move="${agent.scope_id}"
              aria-label="Move ${esc(agent.name)} to another team">⇄</button>
    </li>`;
}

function teamCard(team) {
  const agents = team.agents;
  return `
    <section class="team-card ${
      freezeState.teams.find((t) => String(t.team_id) === String(team.scope_id))?.frozen
        ? "team-card--frozen"
        : ""
    }" data-drop="${team.scope_id}">
      <header class="team-card-head">
        <div>
          <h3>${esc(team.name)}</h3>
          <p class="cell-sub">${agents.length} agent${agents.length === 1 ? "" : "s"}</p>
        </div>
        <div class="team-card-actions">
          <button class="icon-btn ${
            freezeState.teams.find((t) => String(t.team_id) === String(team.scope_id))?.frozen
              ? "frozen"
              : ""
          }" data-freeze-team="${team.scope_id}"
                  aria-label="Freeze or resume ${esc(team.name)}"
                  title="Freeze or resume this team">❄</button>
          <button class="icon-btn" data-edit-team="${team.scope_id}"
                  aria-label="Edit ${esc(team.name)}">✎</button>
          <button class="icon-btn danger" data-del-team="${team.scope_id}"
                  aria-label="Delete ${esc(team.name)}">×</button>
        </div>
      </header>

      <div class="team-card-budget">
        <div class="bar">
          <div class="bar-fill state-${team.state}"
               style="width:${Math.min(100, team.pct * 100)}%"></div>
        </div>
        <div class="cell-sub">
          <strong>${usdPrecise(team.consumed_usd)}</strong> of ${usd(team.limit_usd)}
          · ${pctText(team.pct)}
        </div>
      </div>

      <ul class="chips">
        ${
          agents.length
            ? agents.map(agentChip).join("")
            : '<li class="chips-empty">Drop an agent here</li>'
        }
      </ul>
    </section>`;
}

async function moveAgent(agentId, teamId, teamName) {
  const agent = store.agent(agentId);
  if (!agent || String(agent.team_id) === String(teamId)) return;

  const ok = await confirmDialog({
    title: `Move ${agent.name} to ${teamName}?`,
    message: `${usdPrecise(agent.consumed_usd)} of this period's spend moves with the agent.`,
    detail:
      `Its runaway state and rate history move too, so a paused agent stays paused. ` +
      `<strong>${esc(agent.team_name)}</strong>'s total is unchanged — that team did ` +
      `incur the spend.`,
    confirmLabel: "Move agent",
  });
  if (!ok) return;

  try {
    await api.post(`/admin/agents/${agentId}/move`, { team_id: Number(teamId) });
    toast(`${agent.name} moved to ${teamName}`);
    await store.refreshStatus();
  } catch (err) {
    toast(err.message, "err");
  }
}

export function teamsView(outlet) {
  outlet.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>Teams</h2>
        <div class="panel-actions">
          <span class="panel-hint">drag an agent onto another team to move it</span>
          <button class="btn btn--primary btn--sm" id="new-team">+ New team</button>
        </div>
      </div>
      <div class="team-grid" id="team-grid"><p class="empty">Loading…</p></div>
    </section>`;

  const grid = $("#team-grid", outlet);

  function render() {
    if (!store.status) return;
    grid.innerHTML = store.status.teams.length
      ? store.status.teams.map(teamCard).join("")
      : '<p class="empty">No teams yet. Create one to get started.</p>';
    wireDragAndDrop();
  }

  function wireDragAndDrop() {
    grid.querySelectorAll("[data-chip]").forEach((chip) => {
      chip.addEventListener("dragstart", (event) => {
        dragging = { agentId: chip.dataset.chip, fromTeam: chip.dataset.team };
        chip.classList.add("chip--dragging");
        event.dataTransfer.effectAllowed = "move";
        // Firefox will not start a drag without data on the transfer.
        event.dataTransfer.setData("text/plain", chip.dataset.chip);
      });
      chip.addEventListener("dragend", () => {
        chip.classList.remove("chip--dragging");
        dragging = null;
        grid.querySelectorAll(".team-card--over").forEach((n) =>
          n.classList.remove("team-card--over")
        );
      });
    });

    grid.querySelectorAll("[data-drop]").forEach((zone) => {
      zone.addEventListener("dragover", (event) => {
        if (!dragging || dragging.fromTeam === zone.dataset.drop) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        zone.classList.add("team-card--over");
      });
      zone.addEventListener("dragleave", () => zone.classList.remove("team-card--over"));
      zone.addEventListener("drop", async (event) => {
        event.preventDefault();
        zone.classList.remove("team-card--over");
        if (!dragging) return;
        const { agentId } = dragging;
        dragging = null;
        const team = store.status.teams.find(
          (t) => String(t.scope_id) === zone.dataset.drop
        );
        if (team) await moveAgent(agentId, team.scope_id, team.name);
      });
    });
  }

  async function createTeam() {
    const name = await promptDialog({
      title: "New team",
      label: "Team name",
      placeholder: "Customer Support",
      validate: (v) => (v ? null : "Give the team a name."),
    });
    if (name === null) return;

    const budget = await promptDialog({
      title: `Monthly budget for ${name}`,
      label: "Monthly budget (USD)",
      type: "number",
      value: "500",
      help: "The ceiling for every agent on this team combined.",
      validate: (v) => (Number(v) > 0 ? null : "Must be greater than zero."),
    });
    if (budget === null) return;

    try {
      await api.post("/admin/teams", { name, monthly_budget_usd: Number(budget) });
      toast(`Team ${name} created`);
      await Promise.all([store.refreshStatus(), store.loadReference()]);
    } catch (err) {
      toast(err.message, "err");
    }
  }

  async function editTeam(teamId) {
    const team = store.status.teams.find((t) => String(t.scope_id) === String(teamId));
    if (!team) return;

    const value = await promptDialog({
      title: `Monthly budget — ${team.name}`,
      label: "Monthly budget (USD)",
      type: "number",
      value: team.limit_usd,
      detail: `Spent so far this period: <strong>${usdPrecise(team.consumed_usd)}</strong>`,
      help: "A limit below current spend refuses every agent on the team immediately.",
      validate: (v) => (Number(v) > 0 ? null : "Must be greater than zero."),
    });
    if (value === null) return;

    if (Number(value) < team.consumed_usd) {
      const proceed = await confirmDialog({
        title: "Budget below current spend",
        message: `${team.name} has already spent ${usdPrecise(team.consumed_usd)}.`,
        detail: `A ${usd(Number(value))} cap blocks all ${team.agents.length} agent(s) on their next call.`,
        confirmLabel: "Apply anyway",
        danger: true,
      });
      if (!proceed) return;
    }

    try {
      await api.patch(`/admin/teams/${teamId}`, { monthly_budget_usd: Number(value) });
      toast(`${team.name} budget set to ${usd(Number(value))}`);
      await Promise.all([store.refreshStatus(), store.loadReference()]);
    } catch (err) {
      toast(err.message, "err");
    }
  }

  async function deleteTeam(teamId) {
    const team = store.status.teams.find((t) => String(t.scope_id) === String(teamId));
    if (!team) return;
    if (team.agents.length) {
      toast(
        `${team.name} still has ${team.agents.length} agent(s). Move them first.`,
        "err"
      );
      return;
    }
    const ok = await confirmDialog({
      title: `Delete ${team.name}?`,
      message: "The team and its budget are removed.",
      detail: "Ledger rows keep their original team, so past spend stays attributable.",
      confirmLabel: "Delete team",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/admin/teams/${teamId}`);
      toast(`${team.name} deleted`);
      await Promise.all([store.refreshStatus(), store.loadReference()]);
    } catch (err) {
      toast(err.message, "err");
    }
  }

  async function openMoveMenu(agentId) {
    const agent = store.agent(agentId);
    if (!agent) return;
    const others = store.status.teams.filter(
      (t) => String(t.scope_id) !== String(agent.team_id)
    );
    if (!others.length) return toast("There is nowhere else to move it to.", "err");

    const handle = openModal({
      title: `Move ${agent.name}`,
      size: "sm",
      body: `<p class="dialog-detail">Currently on <strong>${esc(agent.team_name)}</strong>.</p>
        <div class="stack">
          ${others
            .map(
              (t) =>
                `<button class="btn" data-target="${t.scope_id}">${esc(t.name)}
                   <span class="muted">${usdPrecise(t.consumed_usd)} of ${usd(t.limit_usd)}</span>
                 </button>`
            )
            .join("")}
        </div>`,
    });
    handle.node.addEventListener("click", async (event) => {
      const target = event.target.closest("[data-target]")?.dataset.target;
      if (!target) return;
      handle.close();
      const team = others.find((t) => String(t.scope_id) === target);
      await moveAgent(agentId, team.scope_id, team.name);
    });
  }

  const onClick = async (event) => {
    if (event.target.id === "new-team") return createTeam();
    const edit = event.target.closest("[data-edit-team]")?.dataset.editTeam;
    if (edit) return editTeam(edit);
    const del = event.target.closest("[data-del-team]")?.dataset.delTeam;
    if (del) return deleteTeam(del);
    const freeze = event.target.closest("[data-freeze-team]")?.dataset.freezeTeam;
    if (freeze) {
      const team = store.status.teams.find((t) => String(t.scope_id) === freeze);
      return toggleTeamFreeze(freeze, team?.name || "team");
    }
    const move = event.target.closest("[data-move]")?.dataset.move;
    if (move) return openMoveMenu(move);
  };

  outlet.addEventListener("click", onClick);

  const unsubscribe = store.subscribe((_s, reason) => {
    // A repaint mid-drag drops the element out from under the pointer.
    if (reason === "event" || reason === "events" || dragging) return;
    render();
  });
  render();

  return { unmount: unsubscribe };
}
