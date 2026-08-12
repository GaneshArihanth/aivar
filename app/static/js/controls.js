/* Incident controls: freeze, boost, rate limits.
 *
 * All three are destructive or spend-granting, so each one states its blast
 * radius before it acts. A freeze in particular is the biggest button in the
 * app: it stops every agent everywhere.
 */

import { api } from "./api.js";
import { confirmDialog, openModal, promptDialog, toast } from "./components.js";
import { store } from "./store.js";
import { $, esc, usd, usdPrecise } from "./util.js";

export const freezeState = { global: false, reason: null, teams: [] };

export async function refreshFreeze() {
  try {
    const status = await api.get("/admin/freeze");
    freezeState.global = status.global.frozen;
    freezeState.reason = status.global.reason;
    freezeState.teams = status.teams;
  } catch {
    /* leave the last known state rather than implying "not frozen" */
  }
  return freezeState;
}

/** The header kill switch. */
export async function toggleGlobalFreeze() {
  if (freezeState.global) {
    const ok = await confirmDialog({
      title: "Resume all dispatch?",
      message: "Every agent starts spending again immediately.",
      detail: freezeState.reason
        ? `Frozen because: <em>${esc(freezeState.reason)}</em>`
        : "",
      confirmLabel: "Resume dispatch",
    });
    if (!ok) return;
    try {
      await api.del("/admin/freeze");
      toast("Dispatch resumed");
    } catch (err) {
      toast(err.message, "err");
    }
  } else {
    const reason = await promptDialog({
      title: "Freeze all dispatch",
      label: "Reason",
      multiline: true,
      placeholder: "e.g. Anthropic returning 500s across the fleet",
      detail:
        "<strong>Every agent, every team stops immediately.</strong> In-flight calls " +
        "already sent to a provider will finish; nothing new is admitted until you " +
        "lift this. It does not expire on its own.",
      confirmLabel: "Freeze everything",
      validate: (v) => (v.length >= 3 ? null : "Say why — this is recorded."),
    });
    if (reason === null) return;
    try {
      await api.post("/admin/freeze", { reason, actor: "dashboard" });
      toast("All dispatch frozen", "err");
    } catch (err) {
      toast(err.message, "err");
    }
  }
  await refreshFreeze();
  store.emit("freeze");
}

export async function toggleTeamFreeze(teamId, teamName) {
  const entry = freezeState.teams.find((t) => String(t.team_id) === String(teamId));
  const frozen = entry?.frozen;

  if (frozen) {
    const ok = await confirmDialog({
      title: `Resume ${teamName}?`,
      message: "Agents on this team start spending again immediately.",
      detail: entry.reason ? `Frozen because: <em>${esc(entry.reason)}</em>` : "",
      confirmLabel: "Resume team",
    });
    if (!ok) return;
    await api.del(`/admin/teams/${teamId}/freeze`);
    toast(`${teamName} resumed`);
  } else {
    const reason = await promptDialog({
      title: `Freeze ${teamName}`,
      label: "Reason",
      multiline: true,
      detail: `Every agent on <strong>${esc(teamName)}</strong> stops. Other teams
               are unaffected.`,
      confirmLabel: "Freeze team",
      validate: (v) => (v.length >= 3 ? null : "Say why — this is recorded."),
    });
    if (reason === null) return;
    await api.post(`/admin/teams/${teamId}/freeze`, { reason, actor: "dashboard" });
    toast(`${teamName} frozen`, "err");
  }
  await refreshFreeze();
  store.emit("freeze");
}

/** One-time budget grant, from the agent detail page. */
export async function boostAgent(agent) {
  const current = await api
    .get(`/admin/agents/${agent.scope_id ?? agent.id}/boost`)
    .catch(() => ({ active_boost_usd: 0, grants: [] }));

  const handle = openModal({
    title: `Budget boost — ${agent.name}`,
    size: "sm",
    body: `
      <p class="dialog-detail">
        Grants extra budget for this period <strong>without changing the monthly
        limit</strong>. Use it to let a critical job finish; the baseline is
        still there tomorrow.
      </p>
      ${
        current.active_boost_usd > 0
          ? `<div class="form-warning">Already boosted by
               ${usdPrecise(current.active_boost_usd)} this period. Boosts add up.</div>`
          : ""
      }
      <div class="field">
        <label>Amount</label>
        <div class="boost-presets">
          ${[1, 5, 10, 25]
            .map(
              (amount) =>
                `<button type="button" class="btn btn--sm" data-preset="${amount}">
                   +$${amount}</button>`
            )
            .join("")}
        </div>
        <input id="boost-amount" type="number" min="0.01" step="0.01" value="10"
               data-autofocus />
      </div>
      <div class="field">
        <label for="boost-reason">Reason</label>
        <textarea id="boost-reason" rows="2"
                  placeholder="e.g. finish the nightly reconciliation run"></textarea>
        <p class="field-error" id="boost-error"></p>
      </div>
      <div class="field">
        <label for="boost-hours">Expires after</label>
        <select id="boost-hours">
          <option value="2">2 hours</option>
          <option value="8">8 hours</option>
          <option value="24" selected>24 hours</option>
          <option value="72">3 days</option>
        </select>
        <p class="field-hint">Expires sooner if the billing period ends first.</p>
      </div>`,
    footer: `
      <button class="btn btn--ghost" data-cancel>Cancel</button>
      <button class="btn btn--primary" data-confirm>Grant boost</button>`,
  });

  handle.node.querySelectorAll("[data-preset]").forEach((button) =>
    button.addEventListener("click", () => {
      handle.query("#boost-amount").value = button.dataset.preset;
    })
  );
  handle.query("[data-cancel]").addEventListener("click", () => handle.close());
  handle.query("[data-confirm]").addEventListener("click", async (event) => {
    const amount = Number(handle.query("#boost-amount").value);
    const reason = handle.query("#boost-reason").value.trim();
    if (!(amount > 0)) {
      handle.query("#boost-error").textContent = "Enter an amount greater than zero.";
      return;
    }
    if (reason.length < 3) {
      handle.query("#boost-error").textContent = "Say why — this is recorded.";
      return;
    }
    event.currentTarget.disabled = true;
    try {
      const result = await api.post(`/admin/agents/${agent.scope_id ?? agent.id}/boost`, {
        amount_usd: amount,
        reason,
        actor: "dashboard",
        hours: Number(handle.query("#boost-hours").value),
      });
      handle.close();
      toast(`${agent.name} boosted by ${usd(result.granted_usd)}`);
      await store.refreshStatus();
    } catch (err) {
      handle.query("#boost-error").textContent = err.message;
      event.currentTarget.disabled = false;
    }
  });
}

/** RPM/TPM sliders, from the agent detail page. */
export async function openRateLimits(agent, onSaved) {
  const record = await api.get(`/admin/agents/${agent.scope_id ?? agent.id}`);
  const rpm = record.rpm_limit || 0;
  const tpm = record.tpm_limit || 0;

  const handle = openModal({
    title: `Rate limits — ${agent.name}`,
    size: "sm",
    body: `
      <p class="dialog-detail">
        Pacing, not spend. These bound how hard this agent may hit a provider
        during a traffic spike; the budget is unaffected either way, and a
        request refused here is not charged.
      </p>

      <div class="field">
        <label for="rpm-range">Requests per minute
          <output id="rpm-out">${rpm || "unlimited"}</output>
        </label>
        <input id="rpm-range" type="range" min="0" max="600" step="10" value="${rpm}" />
        <p class="field-hint">0 = no cap.</p>
      </div>

      <div class="field">
        <label for="tpm-range">Tokens per minute
          <output id="tpm-out">${tpm ? tpm.toLocaleString() : "unlimited"}</output>
        </label>
        <input id="tpm-range" type="range" min="0" max="1000000" step="10000" value="${tpm}" />
        <p class="field-hint">Counts the worst case per call: prompt + max_tokens.</p>
      </div>`,
    footer: `
      <button class="btn btn--ghost" data-cancel>Cancel</button>
      <button class="btn btn--primary" data-confirm>Apply</button>`,
  });

  const bind = (rangeId, outId, format) => {
    const range = handle.query(rangeId);
    range.addEventListener("input", () => {
      handle.query(outId).textContent = Number(range.value)
        ? format(Number(range.value))
        : "unlimited";
    });
  };
  bind("#rpm-range", "#rpm-out", (v) => String(v));
  bind("#tpm-range", "#tpm-out", (v) => v.toLocaleString());

  handle.query("[data-cancel]").addEventListener("click", () => handle.close());
  handle.query("[data-confirm]").addEventListener("click", async () => {
    try {
      await api.patch(`/admin/agents/${agent.scope_id ?? agent.id}`, {
        rpm_limit: Number(handle.query("#rpm-range").value),
        tpm_limit: Number(handle.query("#tpm-range").value),
      });
      handle.close();
      toast(`Rate limits updated for ${agent.name}`);
      if (onSaved) await onSaved();
    } catch (err) {
      toast(err.message, "err");
    }
  });
}

/** The banner shown across the app while anything is frozen. */
export function freezeBannerMarkup() {
  if (freezeState.global) {
    return `<div class="freeze-banner">
      <strong>All dispatch is frozen.</strong>
      ${freezeState.reason ? esc(freezeState.reason) : ""}
      <button class="btn btn--sm" id="banner-unfreeze">Resume</button>
    </div>`;
  }
  const frozenTeams = freezeState.teams.filter((t) => t.frozen);
  if (frozenTeams.length) {
    return `<div class="freeze-banner freeze-banner--team">
      <strong>${frozenTeams.length} team${frozenTeams.length === 1 ? "" : "s"} frozen:</strong>
      ${frozenTeams.map((t) => esc(t.name)).join(", ")}
    </div>`;
  }
  return "";
}
