/* The "+ New Agent" flow, and the one-time API key reveal.
 *
 * Shared rather than owned by a view: it is reachable from the header on every
 * page, and the key reveal is reused verbatim by key rotation.
 */

import { api } from "./api.js";
import { copyText, openModal, toast } from "./components.js";
import { store } from "./store.js";
import { $, $$, esc } from "./util.js";

const cost = (model) => model.input_usd_per_1k + model.output_usd_per_1k;

function modelOptions(models) {
  // Grouped by provider, cheapest first, cheapest overall preselected. The
  // catalog's natural order puts the flagship first, which would make the
  // default for a new agent the most expensive model available — an odd
  // default for a tool whose purpose is controlling spend.
  const byProvider = new Map();
  for (const model of [...models].sort((a, b) => cost(a) - cost(b))) {
    if (!byProvider.has(model.provider)) byProvider.set(model.provider, []);
    byProvider.get(model.provider).push(model);
  }
  return [...byProvider.entries()]
    .map(
      ([provider, group]) =>
        `<optgroup label="${esc(provider)}">` +
        group
          .map(
            (m) =>
              `<option value="${esc(m.model_id)}" data-in="${m.input_usd_per_1k}" ` +
              `data-out="${m.output_usd_per_1k}">${esc(m.display_name)} — ` +
              `$${m.input_usd_per_1k}/$${m.output_usd_per_1k} per 1k</option>`
          )
          .join("") +
        "</optgroup>"
    )
    .join("");
}

/* ------------------------------------------------------- one-time key view */

/** Show a freshly issued key. This is the only moment it is retrievable. */
export function showKeyModal({ agentName, apiKey, heading = "Agent created" }) {
  const handle = openModal({
    title: heading,
    size: "md",
    body: `
      <p class="key-lead"><strong>${esc(agentName)}</strong> is live and already enforced.</p>
      <div class="key-warn">
        <strong>Copy this key now.</strong> It is shown once and cannot be retrieved —
        the server stores only a hash. If you lose it, rotate the key to issue a new one.
      </div>
      <label class="key-label" for="api-key">API key</label>
      <div class="key-row">
        <code id="api-key" class="key-value">${esc(apiKey)}</code>
        <button id="copy-key" class="btn btn--primary btn--sm" data-autofocus>Copy</button>
      </div>
      <details class="key-usage">
        <summary>Use it</summary>
<pre><code>curl ${location.origin}/v1/chat/completions \\
  -H "X-Agent-Key: ${esc(apiKey)}" \\
  -H "X-Session-Id: session-1" \\
  -H "Content-Type: application/json" \\
  -d '{"messages":[{"role":"user","content":"hello"}],"max_tokens":200}'</code></pre>
      </details>`,
    footer: `<button class="btn btn--primary" data-done>Done — I've saved it</button>`,
  });

  handle.query("#copy-key").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (await copyText(apiKey)) {
      button.textContent = "Copied ✓";
      setTimeout(() => (button.textContent = "Copy"), 2000);
    } else {
      const range = document.createRange();
      range.selectNodeContents(handle.query("#api-key"));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      toast("Couldn't copy automatically — the key is selected, press ⌘/Ctrl+C.", "err");
    }
  });
  handle.query("[data-done]").addEventListener("click", () => handle.close());
  return handle;
}

/* ------------------------------------------------------------ create form */

export function openNewAgentModal({ teamId = null } = {}) {
  const teams = store.teams;
  const models = store.models;

  if (!teams.length) {
    toast("Create a team first — an agent needs one to draw its budget from.", "err");
    return null;
  }

  const handle = openModal({
    title: "New agent",
    size: "md",
    body: `
      <form id="agent-form" novalidate>
        <div class="field">
          <label for="f-name">Agent name</label>
          <input id="f-name" name="name" type="text" placeholder="fraud-screener"
                 autocomplete="off" maxlength="120" data-autofocus />
          <p class="field-error" data-error-for="name"></p>
        </div>

        <div class="field">
          <label for="f-team">Team</label>
          <select id="f-team" name="team_id">
            ${teams
              .map(
                (t) =>
                  `<option value="${t.id}" ${
                    String(t.id) === String(teamId) ? "selected" : ""
                  }>${esc(t.name)}</option>`
              )
              .join("")}
          </select>
          <p class="field-error" data-error-for="team_id"></p>
        </div>

        <div class="field-row">
          <div class="field">
            <label for="f-monthly">Monthly budget (USD)</label>
            <input id="f-monthly" name="monthly_budget_usd" type="number" min="0.01"
                   step="0.01" value="50.00" />
            <p class="field-error" data-error-for="monthly_budget_usd"></p>
          </div>
          <div class="field">
            <label for="f-session">Session budget (USD)</label>
            <input id="f-session" name="session_budget_usd" type="number" min="0.01"
                   step="0.01" value="2.00" />
            <p class="field-error" data-error-for="session_budget_usd"></p>
          </div>
        </div>

        <div class="field">
          <label for="f-model">Preferred model</label>
          <select id="f-model" name="preferred_model">${modelOptions(models)}</select>
          <p class="field-hint" id="model-hint"></p>
          <p class="field-error" data-error-for="preferred_model"></p>
        </div>

        <div class="field field--toggle">
          <label class="toggle">
            <input id="f-sub" name="allow_substitution" type="checkbox" checked />
            <span class="toggle-track" aria-hidden="true"><span class="toggle-thumb"></span></span>
            <span class="toggle-label">
              Allow model substitution
              <small>Reroute to a cheaper model in the same provider under budget
              pressure, instead of hard-blocking.</small>
            </span>
          </label>
        </div>

        <p id="form-error" class="form-error" hidden></p>
      </form>`,
    footer: `
      <button class="btn btn--ghost" data-cancel>Cancel</button>
      <button class="btn btn--primary" id="submit-agent">Create agent</button>`,
  });

  const q = (sel) => handle.query(sel);

  const cheapest = [...models].sort((a, b) => cost(a) - cost(b))[0];
  if (cheapest) q("#f-model").value = cheapest.model_id;

  const updateHint = () => {
    const option = q("#f-model").selectedOptions[0];
    if (option) {
      q("#model-hint").textContent =
        `$${option.dataset.in}/1k input · $${option.dataset.out}/1k output`;
    }
  };
  updateHint();
  q("#f-model").addEventListener("change", updateHint);

  const clearErrors = () => {
    $$(".field-error", handle.node).forEach((node) => (node.textContent = ""));
    $$(".invalid", handle.node).forEach((node) => node.classList.remove("invalid"));
    q("#form-error").hidden = true;
  };
  const setFieldError = (field, message) => {
    const target = $(`[data-error-for="${field}"]`, handle.node);
    if (target) target.textContent = message;
    const input = $(`[name="${field}"]`, handle.node);
    if (input) input.classList.add("invalid");
  };

  async function submit() {
    clearErrors();
    const values = {
      name: q("#f-name").value.trim(),
      team_id: Number(q("#f-team").value),
      monthly_budget_usd: Number(q("#f-monthly").value),
      session_budget_usd: Number(q("#f-session").value),
      preferred_model: q("#f-model").value,
      allow_substitution: q("#f-sub").checked,
    };

    let ok = true;
    if (!values.name) {
      setFieldError("name", "Give the agent a name.");
      ok = false;
    }
    if (!(values.monthly_budget_usd > 0)) {
      setFieldError("monthly_budget_usd", "Must be greater than zero.");
      ok = false;
    }
    if (!(values.session_budget_usd > 0)) {
      setFieldError("session_budget_usd", "Must be greater than zero.");
      ok = false;
    }
    if (values.session_budget_usd > values.monthly_budget_usd) {
      setFieldError(
        "session_budget_usd",
        "Cannot exceed the monthly budget — the monthly limit would bind first."
      );
      ok = false;
    }
    if (!ok) return;

    const button = q("#submit-agent");
    button.disabled = true;
    button.textContent = "Creating…";
    try {
      const result = await api.post("/admin/agents", values);
      store.lastCreatedAgentId = result.agent.id;
      handle.close();
      showKeyModal({ agentName: result.agent.name, apiKey: result.api_key });
      await Promise.all([store.refreshStatus(), store.loadReference()]);
    } catch (err) {
      if (err.field) {
        setFieldError(String(err.field), err.message);
      } else if (Array.isArray(err.detail)) {
        for (const item of err.detail) {
          const field = item.loc?.[item.loc.length - 1];
          if (field) setFieldError(String(field), item.msg);
        }
        q("#form-error").textContent = "Please correct the highlighted fields.";
        q("#form-error").hidden = false;
      } else {
        q("#form-error").textContent = err.message;
        q("#form-error").hidden = false;
      }
    } finally {
      button.disabled = false;
      button.textContent = "Create agent";
    }
  }

  q("#submit-agent").addEventListener("click", submit);
  q("#agent-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });
  handle.query("[data-cancel]").addEventListener("click", () => handle.close());

  return handle;
}
