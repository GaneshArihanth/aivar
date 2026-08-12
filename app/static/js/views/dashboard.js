/* Dashboard: fleet summary tiles, live team/agent spend, and the event feed. */

import {
  deleteAgent,
  editBudget,
  openBlockedQueue,
  rotateKey,
  toggleStatus,
  toggleSubstitution,
  unblockAgent,
} from "../agent-actions.js";
import { openChainEditor } from "../chain-editor.js";
import { openEventDetail } from "../event-detail.js";
import { store } from "../store.js";
import {
  $,
  clockTime,
  esc,
  isBlocked,
  pctText,
  usd,
  usdPrecise,
} from "../util.js";

let openMenu = null;

function closeMenu() {
  if (openMenu) {
    openMenu.remove();
    openMenu = null;
  }
}

function tiles(status) {
  let spend = 0;
  let limit = 0;
  let agents = 0;
  let risk = 0;
  let blocked = 0;

  for (const team of status.teams) {
    spend += team.consumed_usd;
    limit += team.limit_usd;
    for (const agent of team.agents) {
      agents += 1;
      if (agent.pct >= 0.8) risk += 1;
      if (isBlocked(agent)) blocked += 1;
    }
  }

  return `
    <div class="tile">
      <span class="tile-label">Total spend</span>
      <strong class="tile-value">${usd(spend)}</strong>
      <span class="tile-sub">of ${usd(limit)} committed</span>
    </div>
    <div class="tile">
      <span class="tile-label">Agents</span>
      <strong class="tile-value">${agents}</strong>
      <span class="tile-sub">across ${status.teams.length} team${
        status.teams.length === 1 ? "" : "s"
      }</span>
    </div>
    <div class="tile">
      <span class="tile-label">At risk</span>
      <strong class="tile-value">${risk}</strong>
      <span class="tile-sub">over 80% consumed</span>
    </div>
    <button class="tile tile--alert ${blocked ? "tile--actionable" : ""}" id="tile-blocked-btn"
            ${blocked ? "" : "disabled"}
            title="${blocked ? "Review and resume paused agents" : "Nothing is paused"}">
      <span class="tile-label">Paused / blocked</span>
      <strong class="tile-value">${blocked}</strong>
      <span class="tile-sub">
        ${blocked ? "awaiting human review — click to review" : "awaiting human review"}
      </span>
    </button>`;
}

function agentRow(agent) {
  const blocked = isBlocked(agent);
  const classes = ["agent"];
  if (blocked) classes.push("agent--blocked");
  if (agent.status === "paused") classes.push("agent--paused");
  if (String(agent.scope_id) === String(store.lastCreatedAgentId)) {
    classes.push("agent--flash");
  }

  const badges = [];
  if (blocked) badges.push('<span class="badge badge--blocked">paused</span>');
  else if (agent.status === "paused") badges.push('<span class="badge badge--paused">paused</span>');

  const banner = blocked
    ? `<div class="blocked-banner">
         <p><strong>Runaway detected.</strong> Spent ${usdPrecise(agent.hour_spend_usd)} in the
            last hour. Paused pending human review.</p>
         <button class="btn btn--danger btn--sm" data-unblock="${agent.scope_id}">Unblock</button>
       </div>`
    : "";

  return `
    <div class="${classes.join(" ")}" data-agent-row="${agent.scope_id}">
      <div class="agent-name">
        <a class="link" href="#/agents/${agent.scope_id}">${esc(agent.name)}</a>
        ${badges.join("")}
      </div>
      <div class="agent-model">${esc(agent.preferred_model)}</div>
      <div class="bar">
        <div class="bar-fill state-${agent.state}"
             style="width:${Math.min(100, agent.pct * 100)}%"></div>
      </div>
      <div class="agent-figures">
        <strong>${usdPrecise(agent.consumed_usd)}</strong> / ${usd(agent.limit_usd)}
      </div>
      <div class="agent-pct">${pctText(agent.pct)}</div>
      <div class="menu-wrap">
        <button class="icon-btn" data-menu="${agent.scope_id}" aria-label="Actions">⋯</button>
      </div>
      ${banner}
    </div>`;
}

function teamsMarkup(status) {
  if (!status.teams.length) {
    return '<p class="empty">No teams yet. Create one on the Teams page to get started.</p>';
  }
  return status.teams
    .map(
      (team) => `
      <div class="team">
        <div class="team-head">
          <a class="team-name link" href="#/teams">${esc(team.name)}</a>
          <span class="team-figures">
            <strong>${usdPrecise(team.consumed_usd)}</strong> / ${usd(team.limit_usd)}
            · ${pctText(team.pct)}
          </span>
        </div>
        <div class="bar">
          <div class="bar-fill state-${team.state}"
               style="width:${Math.min(100, team.pct * 100)}%"></div>
        </div>
        <div class="agents">
          ${
            team.agents.length
              ? team.agents.map(agentRow).join("")
              : '<p class="empty">No agents on this team.</p>'
          }
        </div>
      </div>`
    )
    .join("");
}

function feedItemMarkup(event) {
  // No leading newline: insertAdjacentHTML would turn it into a stray text
  // node between every row.
  return (
    `<li class="sev-${event.severity || "info"}" data-event-key="${event._key}" ` +
    `tabindex="0" role="button" title="Open event details">` +
    `<div class="feed-msg">${esc(event.message || event.type)}</div>` +
    `<div class="feed-meta">` +
    `<span class="feed-type">${esc(event.type)}</span>` +
    `<span>${clockTime(event.created_at)}</span>` +
    `</div></li>`
  );
}

export function dashboardView(outlet) {
  outlet.innerHTML = `
    <section class="tiles" id="tiles" aria-label="Fleet summary"></section>
    <div class="columns">
      <section class="panel" aria-label="Teams and agents">
        <div class="panel-head">
          <h2>Live spend</h2>
          <span class="panel-hint">updates in real time</span>
        </div>
        <div id="teams" class="teams"><p class="empty">Loading…</p></div>
      </section>
      <aside class="panel panel--feed" aria-label="Budget events">
        <div class="panel-head">
          <h2>Events</h2>
          <button id="clear-feed" class="btn btn--ghost btn--sm">Clear</button>
        </div>
        <ul id="feed" class="feed"></ul>
      </aside>
    </div>`;

  const feedNode = $("#feed", outlet);
  const rendered = new Set();

  function renderTiles() {
    if (!store.status) return;
    $("#tiles", outlet).innerHTML = tiles(store.status);
    const tileButton = $("#tile-blocked-btn", outlet);
    if (tileButton && !tileButton.disabled) {
      tileButton.addEventListener("click", openBlockedQueue);
    }
  }

  function renderTeams() {
    if (!store.status) return;
    // Rebuilding this subtree destroys an open row menu, which lives inside
    // it — so a menu opened just before a poll tick would vanish under the
    // pointer. Defer until the menu closes; the next tick repaints it.
    if (openMenu) return;
    $("#teams", outlet).innerHTML = teamsMarkup(store.status);
    store.lastCreatedAgentId = null;
  }

  /**
   * Incremental, keyed. Replacing the feed's innerHTML on every store update
   * meant ~160 rows destroyed and recreated every poll tick, which re-ran the
   * slide-in animation on all of them — the feed appeared to refresh itself
   * constantly, and any selection, scroll position or hover was lost. Only
   * genuinely new events touch the DOM now.
   */
  function renderFeed() {
    if (!store.events.length) {
      if (feedNode.children.length !== 1 || !feedNode.querySelector(".feed-empty")) {
        feedNode.innerHTML = '<li class="feed-empty">No events yet.</li>';
      }
      rendered.clear();
      return;
    }

    const empty = feedNode.querySelector(".feed-empty");
    if (empty) empty.remove();

    // store.events is newest-first; insert oldest-of-the-new first so the
    // final order matches.
    const fresh = store.events.filter((event) => !rendered.has(event._key));
    for (let i = fresh.length - 1; i >= 0; i--) {
      feedNode.insertAdjacentHTML("afterbegin", feedItemMarkup(fresh[i]));
      rendered.add(fresh[i]._key);
    }

    while (feedNode.children.length > 60) {
      const last = feedNode.lastElementChild;
      rendered.delete(Number(last.dataset.eventKey));
      last.remove();
    }
  }

  const render = (_store, reason) => {
    // Each region repaints only when its own data moves. Before this, a status
    // poll every three seconds repainted the event feed too.
    if (reason === "event" || reason === "events") {
      renderFeed();
      return;
    }
    if (reason === "live" || reason === "reference") return;
    renderTiles();
    renderTeams();
  };

  function openRowMenu(id, anchor) {
    closeMenu();
    const agent = store.agent(id);
    if (!agent) return;

    const menu = document.createElement("div");
    menu.className = "menu";
    menu.innerHTML = `
      <button data-act="detail">Open details…</button>
      <button data-act="edit">Edit budget…</button>
      <button data-act="chain">Fallback chain…</button>
      <button data-act="sub">${
        agent.allow_substitution ? "Disable substitution" : "Enable substitution"
      }</button>
      <button data-act="rotate">Rotate API key…</button>
      <button data-act="toggle">${agent.status === "paused" ? "Resume" : "Pause"}</button>
      ${isBlocked(agent) ? '<button data-act="unblock">Unblock…</button>' : ""}
      <hr />
      <button data-act="delete" class="danger">Delete agent…</button>`;
    anchor.parentElement.appendChild(menu);
    openMenu = menu;

    menu.addEventListener("click", async (event) => {
      const action = event.target.dataset.act;
      if (!action) return;
      closeMenu();
      if (action === "detail") window.location.hash = `/agents/${agent.scope_id}`;
      if (action === "edit") await editBudget(agent);
      if (action === "chain") openChainEditor(agent, () => store.refreshStatus());
      if (action === "sub") await toggleSubstitution(agent);
      if (action === "rotate") await rotateKey(agent);
      if (action === "toggle") await toggleStatus(agent);
      if (action === "unblock") await unblockAgent(agent);
      if (action === "delete") await deleteAgent(agent);
    });
  }

  const onClick = async (event) => {
    const unblockButton = event.target.closest("[data-unblock]");
    if (unblockButton) {
      const agent = store.agent(unblockButton.dataset.unblock);
      if (agent) await unblockAgent(agent);
      return;
    }

    const menuButton = event.target.closest("[data-menu]");
    if (menuButton) {
      if (openMenu) closeMenu();
      else openRowMenu(menuButton.dataset.menu, menuButton);
      return;
    }

    const feedItem = event.target.closest("[data-event-key]");
    if (feedItem) {
      const detail = store.eventByKey(feedItem.dataset.eventKey);
      if (detail) openEventDetail(detail);
      return;
    }

    if (!event.target.closest(".menu")) {
      const hadMenu = Boolean(openMenu);
      closeMenu();
      // Teams repaints were suppressed while the menu was open; catch up now.
      if (hadMenu) renderTeams();
    }
  };

  const onClear = (event) => {
    if (event.target.id === "clear-feed") store.clearEvents();
  };

  const onKey = (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const feedItem = event.target.closest("[data-event-key]");
    if (!feedItem) return;
    event.preventDefault();
    const detail = store.eventByKey(feedItem.dataset.eventKey);
    if (detail) openEventDetail(detail);
  };

  outlet.addEventListener("click", onClear);
  outlet.addEventListener("keydown", onKey);
  document.addEventListener("click", onClick);
  const unsubscribe = store.subscribe(render);
  renderTiles();
  renderTeams();
  renderFeed();

  return {
    unmount() {
      unsubscribe();
      document.removeEventListener("click", onClick);
      closeMenu();
    },
  };
}
