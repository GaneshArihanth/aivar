/* Model & pricing catalog.
 *
 * Prices entered here are what reservations are sized against, so this page is
 * editing the numbers that decide whether a call is affordable — not just
 * display metadata. That is why the form states the per-1k cost in the same
 * units the enforcement layer uses.
 */

import { api } from "../api.js";
import { confirmDialog, openModal, toast } from "../components.js";
import { store } from "../store.js";
import { $, $$, esc, num, usdPrecise } from "../util.js";

let kinds = [];
let mode = "mock";
let mockBaseUrl = "";

const per1k = (n) => "$" + Number(n || 0).toFixed(6).replace(/0+$/, "").replace(/\.$/, ".0");

function kindMeta(kind) {
  return kinds.find((k) => k.kind === kind) || { label: kind, dispatchable: true, hint: "" };
}

function credentialCell(model) {
  if (!model.api_key_env) {
    return '<span class="muted">none needed</span>';
  }
  return model.credential_present
    ? `<span class="pill pill--ok" title="${esc(model.api_key_env)} is set">
         ${esc(model.api_key_env)} ✓</span>`
    : `<span class="pill pill--warn" title="${esc(model.api_key_env)} is not set in the environment">
         ${esc(model.api_key_env)} — unset</span>`;
}

function rowMarkup(model) {
  const meta = kindMeta(model.provider_kind);
  return `
    <tr data-model="${esc(model.model_id)}" class="${model.is_active ? "" : "row--muted"}">
      <td>
        <div class="cell-title">
          ${esc(model.display_name)}
          ${model.is_custom ? '<span class="badge badge--sub">custom</span>' : ""}
          ${model.is_active ? "" : '<span class="badge badge--paused">inactive</span>'}
        </div>
        <div class="cell-sub mono">${esc(model.model_id)}</div>
      </td>
      <td>
        <div>${esc(model.provider)}</div>
        <div class="cell-sub">${esc(meta.label)}${
          meta.dispatchable ? "" : " · via gateway"
        }</div>
      </td>
      <td class="num">${per1k(model.input_usd_per_1k)}</td>
      <td class="num">${per1k(model.output_usd_per_1k)}</td>
      <td class="num">${model.tier_rank}</td>
      <td class="mono cell-url" title="${esc(model.base_url || "")}">
        ${model.base_url ? esc(model.base_url) : '<span class="muted">default</span>'}
      </td>
      <td>${credentialCell(model)}</td>
      <td class="row-actions">
        <button class="btn btn--ghost btn--sm" data-test="${esc(model.model_id)}">Test</button>
        <button class="btn btn--ghost btn--sm" data-edit="${esc(model.model_id)}">Edit</button>
        <button class="btn btn--ghost btn--sm danger" data-del="${esc(model.model_id)}">Delete</button>
      </td>
    </tr>`;
}

function formMarkup(model) {
  const value = (key, fallback = "") => esc(model?.[key] ?? fallback);
  return `
    <form id="model-form" novalidate>
      <div class="field-row">
        <div class="field">
          <label for="m-id">Model ID</label>
          <input id="m-id" name="model_id" type="text" value="${value("model_id")}"
                 placeholder="llama3.1:70b" ${model ? "disabled" : ""} data-autofocus />
          <p class="field-hint">Exactly what the provider expects in the request body.</p>
          <p class="field-error" data-error-for="model_id"></p>
        </div>
        <div class="field">
          <label for="m-provider">Provider</label>
          <input id="m-provider" name="provider" type="text" value="${value("provider")}"
                 placeholder="ollama" />
          <p class="field-hint">Grouping label, e.g. openai, anthropic, ollama.</p>
          <p class="field-error" data-error-for="provider"></p>
        </div>
      </div>

      <div class="field">
        <label for="m-name">Display name</label>
        <input id="m-name" name="display_name" type="text" value="${value("display_name")}"
               placeholder="Llama 3.1 70B (local)" />
      </div>

      <div class="field-row">
        <div class="field">
          <label for="m-in">Input cost — USD per 1k tokens</label>
          <input id="m-in" name="input_usd_per_1k" type="number" min="0" step="0.000001"
                 value="${model ? model.input_usd_per_1k : "0"}" />
          <p class="field-error" data-error-for="input_usd_per_1k"></p>
        </div>
        <div class="field">
          <label for="m-out">Output cost — USD per 1k tokens</label>
          <input id="m-out" name="output_usd_per_1k" type="number" min="0" step="0.000001"
                 value="${model ? model.output_usd_per_1k : "0"}" />
          <p class="field-error" data-error-for="output_usd_per_1k"></p>
        </div>
      </div>
      <p class="field-hint cost-preview" id="cost-preview"></p>

      <div class="field-row">
        <div class="field">
          <label for="m-kind">Wire format</label>
          <select id="m-kind" name="provider_kind">
            ${kinds
              .map(
                (k) =>
                  `<option value="${esc(k.kind)}" ${
                    (model?.provider_kind || "openai") === k.kind ? "selected" : ""
                  }>${esc(k.label)}</option>`
              )
              .join("")}
          </select>
          <p class="field-hint" id="kind-hint"></p>
        </div>
        <div class="field">
          <label for="m-rank">Tier rank</label>
          <input id="m-rank" name="tier_rank" type="number" min="0" max="100"
                 value="${model ? model.tier_rank : 20}" />
          <p class="field-hint">Higher = more capable. Chains step downward.</p>
        </div>
      </div>

      <div class="field">
        <label for="m-url">Base URL</label>
        <input id="m-url" name="base_url" type="text" value="${value("base_url")}"
               placeholder="http://localhost:11434/v1" />
        <p class="field-hint">
          Include the version prefix, as the provider documents it. Leave blank to use
          the default upstream.
        </p>
      </div>

      <div class="field">
        <label for="m-key">API key environment variable</label>
        <input id="m-key" name="api_key_env" type="text" value="${value("api_key_env")}"
               placeholder="OPENAI_API_KEY" autocomplete="off" />
        <p class="field-hint">
          The variable <em>name</em>, not the key. Secrets stay in the environment —
          this app never stores or displays them.
        </p>
      </div>

      <p id="form-error" class="form-error" hidden></p>
    </form>`;
}

function openModelForm(model, onSaved) {
  const handle = openModal({
    title: model ? `Edit ${model.display_name}` : "Add model",
    size: "lg",
    body: formMarkup(model),
    footer: `
      <button class="btn btn--ghost" data-cancel>Cancel</button>
      <button class="btn btn--primary" id="save-model">${model ? "Save changes" : "Add model"}</button>`,
  });

  const q = (sel) => handle.query(sel);

  const refreshHints = () => {
    const meta = kindMeta(q("#m-kind").value);
    q("#kind-hint").innerHTML = esc(meta.hint || "");
    if (!meta.dispatchable) {
      q("#kind-hint").innerHTML += ' <strong class="warn-text">Not dispatched directly.</strong>';
    }
    const input = Number(q("#m-in").value || 0);
    const output = Number(q("#m-out").value || 0);
    // Grounding the abstract per-1k rate in a call people can picture.
    const example = input * 2 + output * 0.5;
    q("#cost-preview").textContent =
      example > 0
        ? `A 2,000-token prompt with a 500-token reply costs about ${usdPrecise(example)}.`
        : "Free at the point of use — priced at zero, so it only consumes rate limits.";
  };
  refreshHints();
  ["#m-kind", "#m-in", "#m-out"].forEach((sel) =>
    q(sel).addEventListener("input", refreshHints)
  );
  q("#m-kind").addEventListener("change", refreshHints);

  q("[data-cancel]").addEventListener("click", () => handle.close());
  q("#save-model").addEventListener("click", async () => {
    $$(".field-error", handle.node).forEach((n) => (n.textContent = ""));
    q("#form-error").hidden = true;

    const payload = {
      provider: q("#m-provider").value.trim(),
      display_name: q("#m-name").value.trim() || undefined,
      input_usd_per_1k: Number(q("#m-in").value || 0),
      output_usd_per_1k: Number(q("#m-out").value || 0),
      tier_rank: Number(q("#m-rank").value || 20),
      provider_kind: q("#m-kind").value,
      base_url: q("#m-url").value.trim(),
      api_key_env: q("#m-key").value.trim(),
    };

    const button = q("#save-model");
    button.disabled = true;
    try {
      if (model) {
        await api.patch(`/admin/models/${encodeURIComponent(model.model_id)}`, payload);
        toast(`${model.model_id} updated`);
      } else {
        payload.model_id = q("#m-id").value.trim();
        if (!payload.model_id) {
          $(`[data-error-for="model_id"]`, handle.node).textContent = "Required.";
          button.disabled = false;
          return;
        }
        await api.post("/admin/models", payload);
        toast(`${payload.model_id} added to the catalog`);
      }
      handle.close();
      await onSaved();
    } catch (err) {
      if (err.field) {
        const target = $(`[data-error-for="${err.field}"]`, handle.node);
        if (target) target.textContent = err.message;
        else {
          q("#form-error").textContent = err.message;
          q("#form-error").hidden = false;
        }
      } else {
        q("#form-error").textContent = err.message;
        q("#form-error").hidden = false;
      }
      button.disabled = false;
    }
  });
}

export function modelsView(outlet) {
  outlet.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>Model &amp; pricing catalog</h2>
        <div class="panel-actions">
          <span class="panel-hint" id="mode-hint"></span>
          <button class="btn btn--primary btn--sm" id="add-model">+ Add model</button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="table" id="models-table">
          <thead>
            <tr>
              <th>Model</th><th>Provider</th>
              <th class="num">In /1k</th><th class="num">Out /1k</th><th class="num">Rank</th>
              <th>Endpoint</th><th>Credential</th><th></th>
            </tr>
          </thead>
          <tbody><tr><td colspan="8" class="empty">Loading…</td></tr></tbody>
        </table>
      </div>
    </section>`;

  const tbody = $("#models-table tbody", outlet);

  async function load() {
    const [models, kindInfo] = await Promise.all([
      api.get("/admin/models"),
      api.get("/admin/models/provider-kinds"),
    ]);
    kinds = kindInfo.kinds;
    mode = kindInfo.mode;
    mockBaseUrl = kindInfo.mock_base_url;

    $("#mode-hint", outlet).innerHTML =
      mode === "mock"
        ? `<strong>mock mode</strong> — all traffic goes to ${esc(mockBaseUrl)}, whatever
           endpoint a model records. Set <span class="mono">UPSTREAM_MODE=live</span> to use them.`
        : `<strong class="warn-text">live mode</strong> — models dispatch to their own endpoints.`;

    tbody.innerHTML = models.length
      ? models.map(rowMarkup).join("")
      : '<tr><td colspan="8" class="empty">No models. Add one to get started.</td></tr>';

    // Keep the shared store's copy fresh so the new-agent modal lists the same
    // catalog this page is editing.
    store.models = models.filter((m) => m.is_active);
    return models;
  }

  let cache = [];
  const refresh = async () => {
    cache = await load();
  };

  const onClick = async (event) => {
    const testId = event.target.closest("[data-test]")?.dataset.test;
    const editId = event.target.closest("[data-edit]")?.dataset.edit;
    const delId = event.target.closest("[data-del]")?.dataset.del;

    if (testId) {
      const button = event.target.closest("[data-test]");
      button.disabled = true;
      button.textContent = "Testing…";
      try {
        const result = await api.post(`/admin/models/${encodeURIComponent(testId)}/test`);
        // The probe always contacts the model's own endpoint, even in mock
        // mode — so say when that is not the path live traffic takes, rather
        // than letting a green result imply more than it means.
        const caveat =
          result.tests_own_endpoint && mode === "mock"
            ? " — note: traffic currently goes to the mock"
            : "";
        if (result.ok) {
          toast(
            `${testId} reachable at ${result.base_url}` +
              `${result.reported_usage ? "" : " (no usage reported)"}${caveat}`
          );
        } else {
          toast(`${testId} — ${result.detail}`, "err");
        }
      } catch (err) {
        toast(err.message, "err");
      } finally {
        button.disabled = false;
        button.textContent = "Test";
      }
      return;
    }

    if (editId) {
      const model = cache.find((m) => m.model_id === editId);
      if (model) openModelForm(model, refresh);
      return;
    }

    if (delId) {
      const ok = await confirmDialog({
        title: `Delete ${delId}?`,
        message: "It is removed from the catalog and from any fallback chain using it.",
        detail:
          "Past ledger rows keep the model id, so historical spend keeps its provenance.",
        confirmLabel: "Delete model",
        danger: true,
      });
      if (!ok) return;
      try {
        await api.del(`/admin/models/${encodeURIComponent(delId)}`);
        toast(`${delId} removed`);
        await refresh();
      } catch (err) {
        toast(err.message, "err");
      }
    }
  };

  outlet.addEventListener("click", onClick);
  $("#add-model", outlet).addEventListener("click", () => openModelForm(null, refresh));
  refresh();

  return { unmount() {} };
}
