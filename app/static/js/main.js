/* Bootstrap: wire the shell, register routes, start the live store. */

import { openNewAgentModal } from "./agent-modal.js";
import { freezeBannerMarkup, freezeState, refreshFreeze, toggleGlobalFreeze } from "./controls.js";
import { route, start } from "./router.js";
import { store } from "./store.js";
import { $ } from "./util.js";
import { agentDetailView } from "./views/agent-detail.js";
import { agentsView } from "./views/agents.js";
import { dashboardView } from "./views/dashboard.js";
import { demoView } from "./views/demo.js";
import { modelsView } from "./views/models.js";
import { teamsView } from "./views/teams.js";

route("/", dashboardView);
route("/agents", agentsView);
route("/agents/:id", agentDetailView);
route("/teams", teamsView);
route("/models", modelsView);
route("/demo", demoView);

function renderHeader() {
  const period = $("#period");
  const resets = $("#resets");
  if (store.status) {
    period.textContent = store.status.period;
    resets.textContent = new Date(store.status.resets_at).toLocaleDateString();
  }
  const banner = $("#freeze-banner");
  banner.innerHTML = freezeBannerMarkup();
  const unfreeze = $("#banner-unfreeze");
  if (unfreeze) unfreeze.addEventListener("click", toggleGlobalFreeze);

  const freezeButton = $("#freeze-btn");
  freezeButton.textContent = freezeState.global ? "Resume all" : "Freeze all";
  freezeButton.className = freezeState.global ? "btn btn--danger" : "btn btn--ghost";

  const indicator = $("#live");
  indicator.className = `live live--${store.live ? "on" : "off"}`;
  $("#live-text").textContent = store.live ? "live" : "reconnecting";
}

async function main() {
  $("#new-agent-btn").addEventListener("click", () => openNewAgentModal());
  $("#freeze-btn").addEventListener("click", toggleGlobalFreeze);
  store.subscribe(renderHeader);

  await refreshFreeze();
  await start($("#view"));
  await store.start();
  renderHeader();
  // A freeze can be set from another browser or by curl; poll so this one
  // does not keep offering a button that no longer matches reality.
  setInterval(async () => {
    await refreshFreeze();
    renderHeader();
  }, 8000);
}

main();
