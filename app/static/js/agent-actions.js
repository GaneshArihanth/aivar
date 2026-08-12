/* Agent actions shared by the dashboard, the agents table and the detail page.
 *
 * Every one of these ends in a store refresh, so whichever view invoked it
 * re-renders from the same authoritative status payload rather than patching
 * its own DOM and drifting from the server.
 */

import { api } from "./api.js";
import { confirmDialog, openModal, promptDialog, toast } from "./components.js";
import { showKeyModal } from "./agent-modal.js";
import { store } from "./store.js";
import { esc, usd, usdPrecise } from "./util.js";

const agentId = (agent) => agent.scope_id ?? agent.id;

async function done(message) {
  await store.refreshStatus();
  if (message) toast(message);
}

function fail(err) {
  toast(err.message || "Something went wrong", "err");
}

/* ------------------------------------------------------------ edit budget */

export async function editBudget(agent) {
  const value = await promptDialog({
    title: `Monthly budget — ${agent.name}`,
    label: "Monthly budget (USD)",
    type: "number",
    value: agent.limit_usd,
    detail: `Spent so far this period: <strong>${usdPrecise(agent.consumed_usd)}</strong>`,
    help: "Setting a limit below current spend blocks the agent on its next call.",
    validate: (raw) =>
      Number(raw) > 0 ? null : "Budget must be greater than zero.",
  });
  if (value === null) return;

  const amount = Number(value);
  if (amount < agent.consumed_usd) {
    const proceed = await confirmDialog({
      title: "Budget below current spend",
      message: `${usdPrecise(agent.consumed_usd)} is already spent this period.`,
      detail: `A ${usd(amount)} limit blocks <strong>${esc(agent.name)}</strong> on its next call.`,
      confirmLabel: "Apply anyway",
      danger: true,
    });
    if (!proceed) return;
  }

  try {
    await api.patch(`/admin/agents/${agentId(agent)}`, { monthly_budget_usd: amount });
    await done(`Budget for ${agent.name} set to ${usd(amount)}`);
  } catch (err) {
    fail(err);
  }
}

export async function editSessionBudget(agent) {
  const value = await promptDialog({
    title: `Session budget — ${agent.name}`,
    label: "Per-session budget (USD)",
    type: "number",
    value: agent.session_budget_usd ?? "",
    help: "Applies to each session independently. Cannot exceed the monthly budget.",
    validate: (raw) => (Number(raw) > 0 ? null : "Must be greater than zero."),
  });
  if (value === null) return;
  try {
    await api.patch(`/admin/agents/${agentId(agent)}`, {
      session_budget_usd: Number(value),
    });
    await done(`Session budget for ${agent.name} set to ${usd(Number(value))}`);
  } catch (err) {
    fail(err);
  }
}

/* -------------------------------------------------------------- lifecycle */

export async function toggleStatus(agent) {
  const next = agent.status === "paused" ? "active" : "paused";
  try {
    await api.patch(`/admin/agents/${agentId(agent)}`, { status: next });
    await done(`${agent.name} ${next === "paused" ? "paused" : "resumed"}`);
  } catch (err) {
    fail(err);
  }
}

/** Item 3: flip substitution without opening a form. */
export async function toggleSubstitution(agent) {
  const next = !agent.allow_substitution;
  try {
    await api.patch(`/admin/agents/${agentId(agent)}`, { allow_substitution: next });
    await done(
      next
        ? `${agent.name} will reroute to a cheaper model under pressure`
        : `${agent.name} will hard-block instead of substituting`
    );
  } catch (err) {
    fail(err);
  }
}

export async function rotateKey(agent) {
  const proceed = await confirmDialog({
    title: "Rotate API key",
    message: `Issue a new key for “${agent.name}”?`,
    detail: "The current key stops working immediately. Anything still using it will get 401s.",
    confirmLabel: "Rotate key",
    danger: true,
  });
  if (!proceed) return;

  try {
    const result = await api.post(`/admin/agents/${agentId(agent)}/rotate-key`);
    showKeyModal({
      agentName: agent.name,
      apiKey: result.api_key,
      heading: "New API key issued",
    });
    await store.refreshStatus();
  } catch (err) {
    fail(err);
  }
}

export async function deleteAgent(agent) {
  const proceed = await confirmDialog({
    title: `Delete ${agent.name}?`,
    message: "Its API key is revoked immediately.",
    detail: "Spend history is kept for audit — the ledger is not rewritten.",
    confirmLabel: "Delete agent",
    danger: true,
  });
  if (!proceed) return false;

  try {
    await api.del(`/admin/agents/${agentId(agent)}`);
    await done(`${agent.name} deleted`);
    return true;
  } catch (err) {
    fail(err);
    return false;
  }
}

/* ---------------------------------------------------------------- unblock */

/** Release one agent paused by the runaway detector. Resolves true if released. */
export function unblockAgent(agent) {
  return new Promise((resolve) => {
    const handle = openModal({
      title: "Release paused agent",
      size: "sm",
      body: `
        <p class="key-lead"><strong>${esc(agent.name)}</strong> was paused by the runaway detector.</p>
        <p class="dialog-detail">
          It spent <strong>${usdPrecise(agent.hour_spend_usd || 0)}</strong> in the hour before
          it tripped. Releasing it resumes spending immediately; the reason is recorded
          against the agent for audit.
        </p>
        <div class="field">
          <label for="unblock-reason">Reason for release</label>
          <textarea id="unblock-reason" rows="3" data-autofocus
            placeholder="e.g. Retry loop in the caller was patched and deployed"></textarea>
          <p class="field-error" id="unblock-error"></p>
        </div>`,
      footer: `
        <button class="btn btn--ghost" data-cancel>Cancel</button>
        <button class="btn btn--danger" data-confirm>Release agent</button>`,
      onClose: () => resolve(false),
    });

    handle.query("[data-cancel]").addEventListener("click", () => handle.close());
    handle.query("[data-confirm]").addEventListener("click", async (event) => {
      const reason = handle.query("#unblock-reason").value.trim();
      if (reason.length < 3) {
        handle.query("#unblock-error").textContent =
          "Describe what you checked — this is recorded for audit.";
        return;
      }
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await api.post(`/admin/agents/${agentId(agent)}/unblock`, {
          reason,
          actor: "dashboard",
        });
        resolve(true);
        handle.close();
        await done(`${agent.name} released`);
      } catch (err) {
        handle.query("#unblock-error").textContent = err.message;
        button.disabled = false;
      }
    });
  });
}

/* ------------------------------------------------- blocked review queue (H) */

/**
 * The review queue behind the "Paused / blocked" tile: every blocked agent,
 * why it tripped, and a release action per row — so a human reviewing an
 * incident does not have to hunt for the agents in the team list.
 */
export function openBlockedQueue() {
  const render = (handle) => {
    const blocked = store.blockedAgents();

    if (!blocked.length) {
      handle.setBody(`
        <div class="empty queue-empty">
          <p><strong>Nothing is paused.</strong></p>
          <p class="field-hint">
            Agents appear here when the runaway detector pauses them for spending
            too fast, and stay until someone releases them.
          </p>
        </div>`);
      return;
    }

    handle.setBody(`
      <p class="dialog-detail queue-lead">
        ${blocked.length} agent${blocked.length === 1 ? "" : "s"} paused by the runaway
        detector, awaiting review. Releasing resumes spending immediately.
      </p>
      <ul class="queue">
        ${blocked
          .map(
            (agent) => `
          <li class="queue-item" data-queue-row="${agent.scope_id}">
            <div class="queue-main">
              <div class="queue-name">
                <a class="link" href="#/agents/${agent.scope_id}">${esc(agent.name)}</a>
                <span class="badge badge--blocked">paused</span>
              </div>
              <div class="queue-meta">
                ${esc(agent.team_name)} · spent
                <strong>${usdPrecise(agent.hour_spend_usd || 0)}</strong> in the last hour ·
                ${usdPrecise(agent.consumed_usd)} of ${usd(agent.limit_usd)} this month
              </div>
            </div>
            <button class="btn btn--danger btn--sm" data-release="${agent.scope_id}">
              Review &amp; resume
            </button>
          </li>`
          )
          .join("")}
      </ul>`);

    handle.node.querySelectorAll("[data-release]").forEach((button) =>
      button.addEventListener("click", async () => {
        const agent = store.agent(button.dataset.release);
        if (!agent) return;
        const released = await unblockAgent(agent);
        if (released) render(handle);
      })
    );
  };

  const handle = openModal({
    title: "Paused agents — awaiting review",
    size: "lg",
    body: "",
    footer: `<button class="btn btn--ghost" data-modal-close>Close</button>`,
  });

  render(handle);
  const unsubscribe = store.subscribe(() => render(handle));
  const originalClose = handle.close.bind(handle);
  handle.close = (...args) => {
    unsubscribe();
    originalClose(...args);
  };
  return handle;
}
