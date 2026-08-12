/* Fallback chain editor.
 *
 * The ladder the proxy walks when the preferred model no longer fits the
 * budget. Two rules are enforced visibly rather than only on save, because
 * both are easy to get wrong and expensive to discover later:
 *
 *   · each step must cost the same or less than the one above it — the chain
 *     is walked *because* a reservation was refused, so a pricier fallback
 *     cannot succeed either;
 *   · providers only change when the agent is allowed to cross them.
 */

import { api } from "./api.js";
import { openModal, toast } from "./components.js";
import { store } from "./store.js";
import { $, $$, esc } from "./util.js";

const blended = (m) => (m.input_usd_per_1k + m.output_usd_per_1k) / 2;
const price = (m) => `$${m.input_usd_per_1k}/$${m.output_usd_per_1k} per 1k`;

function stepMarkup(model, index, total) {
  return `
    <li class="chain-step" draggable="true" data-model="${esc(model.model_id)}"
        data-index="${index}">
      <span class="chain-grip" aria-hidden="true">⠿</span>
      <span class="chain-pos">${index === 0 ? "preferred" : index}</span>
      <span class="chain-main">
        <span class="chain-name">${esc(model.display_name)}</span>
        <span class="chain-sub">${esc(model.provider)} · ${price(model)}</span>
      </span>
      <span class="chain-move">
        <button class="icon-btn" data-up="${index}" ${index === 0 ? "disabled" : ""}
                aria-label="Move up">↑</button>
        <button class="icon-btn" data-down="${index}" ${
          index === total - 1 ? "disabled" : ""
        } aria-label="Move down">↓</button>
        <button class="icon-btn danger" data-remove="${index}" ${
          total === 1 ? "disabled" : ""
        } aria-label="Remove">×</button>
      </span>
    </li>`;
}

export function openChainEditor(agent, onSaved) {
  const catalog = new Map(store.models.map((m) => [m.model_id, m]));
  let chain = [];
  let allowCross = Boolean(agent.allow_cross_provider);

  const handle = openModal({
    title: `Fallback chain — ${agent.name}`,
    size: "lg",
    body: '<p class="empty">Loading…</p>',
    footer: `
      <button class="btn btn--ghost" data-auto>Rebuild from catalog</button>
      <span class="foot-spacer"></span>
      <button class="btn btn--ghost" data-cancel>Cancel</button>
      <button class="btn btn--primary" data-save>Save chain</button>`,
  });

  const q = (sel) => handle.query(sel);

  function analyse() {
    const problems = [];
    const warnings = [];
    let previous = null;
    let headProvider = null;

    chain.forEach((id, index) => {
      const model = catalog.get(id);
      if (!model) {
        problems.push({ index, text: `'${id}' is no longer in the catalog.` });
        return;
      }
      if (headProvider === null) headProvider = model.provider;
      else if (model.provider !== headProvider && !allowCross) {
        problems.push({
          index,
          text: `${model.display_name} is a ${model.provider} model — enable cross-provider below.`,
        });
      }
      const cost = blended(model);
      if (previous !== null && cost > previous) {
        // Allowed, but worth saying: budget pressure never falls through to a
        // pricier step. It only serves if the one above it leaves the catalog.
        warnings.push(
          `${model.display_name} costs more than the step above it — budget pressure ` +
            `will not fall through to it.`
        );
      } else if (previous !== null && cost === previous) {
        warnings.push(`${model.display_name} costs the same as the step above it.`);
      }
      previous = cost;
    });

    if (chain.length === 1) {
      warnings.push("With one entry there is nothing to fall back to.");
    }
    const providers = new Set(
      chain.map((id) => catalog.get(id)?.provider).filter(Boolean)
    );
    return { problems, warnings, crosses: providers.size > 1 };
  }

  function render() {
    const { problems, warnings, crosses } = analyse();
    const available = [...catalog.values()]
      .filter((m) => !chain.includes(m.model_id))
      .sort((a, b) => blended(b) - blended(a));

    handle.setBody(`
      <p class="dialog-detail">
        Walked top to bottom when a reservation is refused. The first entry is the
        model the agent asks for.
      </p>

      <ol class="chain-list" id="chain-list">
        ${chain
          .map((id, index) => {
            const model = catalog.get(id) || {
              model_id: id,
              display_name: id,
              provider: "unknown",
              input_usd_per_1k: 0,
              output_usd_per_1k: 0,
            };
            return stepMarkup(model, index, chain.length);
          })
          .join("")}
      </ol>

      <div class="field chain-add">
        <label for="chain-add-select">Add a step</label>
        <div class="chain-add-row">
          <select id="chain-add-select">
            <option value="">Choose a model…</option>
            ${available
              .map(
                (m) =>
                  `<option value="${esc(m.model_id)}">${esc(m.display_name)} — ${esc(
                    m.provider
                  )} · ${price(m)}</option>`
              )
              .join("")}
          </select>
          <button class="btn btn--sm" id="chain-add-btn">Add</button>
        </div>
      </div>

      <div class="field field--toggle">
        <label class="toggle">
          <input type="checkbox" id="chain-cross" ${allowCross ? "checked" : ""} />
          <span class="toggle-track" aria-hidden="true"><span class="toggle-thumb"></span></span>
          <span class="toggle-label">
            Allow cross-provider substitution
            <small>Responses are translated back into the OpenAI schema, but
            tokenization and model behaviour differ between vendors.</small>
          </span>
        </label>
      </div>

      ${
        problems.length
          ? `<div class="form-error" id="chain-problems">
               ${problems.map((p) => `<div>• ${esc(p.text)}</div>`).join("")}
             </div>`
          : ""
      }
      ${
        warnings.length
          ? `<div class="form-warning">${warnings
              .map((w) => `<div>• ${esc(w)}</div>`)
              .join("")}</div>`
          : ""
      }
      ${
        crosses && allowCross
          ? `<p class="field-hint">This chain crosses providers.</p>`
          : ""
      }`);

    q("[data-save]").disabled = problems.length > 0;
    wire();
  }

  function move(from, to) {
    if (to < 0 || to >= chain.length) return;
    const [item] = chain.splice(from, 1);
    chain.splice(to, 0, item);
    render();
  }

  function wire() {
    $$("[data-up]", handle.node).forEach((b) =>
      b.addEventListener("click", () => move(Number(b.dataset.up), Number(b.dataset.up) - 1))
    );
    $$("[data-down]", handle.node).forEach((b) =>
      b.addEventListener("click", () =>
        move(Number(b.dataset.down), Number(b.dataset.down) + 1)
      )
    );
    $$("[data-remove]", handle.node).forEach((b) =>
      b.addEventListener("click", () => {
        chain.splice(Number(b.dataset.remove), 1);
        render();
      })
    );

    q("#chain-add-btn").addEventListener("click", () => {
      const value = q("#chain-add-select").value;
      if (!value) return;
      chain.push(value);
      render();
    });

    q("#chain-cross").addEventListener("change", (event) => {
      allowCross = event.target.checked;
      render();
    });

    // Drag to reorder. The arrow buttons do the same job for anyone not using
    // a pointer — a drag-only control would make the ladder unorderable by
    // keyboard.
    let dragFrom = null;
    $$(".chain-step", handle.node).forEach((node) => {
      node.addEventListener("dragstart", (event) => {
        dragFrom = Number(node.dataset.index);
        node.classList.add("chain-step--dragging");
        event.dataTransfer.effectAllowed = "move";
      });
      node.addEventListener("dragend", () => node.classList.remove("chain-step--dragging"));
      node.addEventListener("dragover", (event) => {
        event.preventDefault();
        node.classList.add("chain-step--over");
      });
      node.addEventListener("dragleave", () => node.classList.remove("chain-step--over"));
      node.addEventListener("drop", (event) => {
        event.preventDefault();
        node.classList.remove("chain-step--over");
        const to = Number(node.dataset.index);
        if (dragFrom !== null && dragFrom !== to) move(dragFrom, to);
        dragFrom = null;
      });
    });
  }

  q("[data-cancel]").addEventListener("click", () => handle.close());

  q("[data-auto]").addEventListener("click", async () => {
    try {
      const result = await api.post(`/admin/agents/${agent.id ?? agent.scope_id}/chain/auto`);
      chain = result.chain;
      allowCross = result.allow_cross_provider;
      render();
      toast("Chain rebuilt from the catalog");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  q("[data-save]").addEventListener("click", async () => {
    const id = agent.id ?? agent.scope_id;
    const button = q("[data-save]");
    button.disabled = true;
    try {
      // The flag has to land before the chain, or a cross-provider chain would
      // be validated against the old permission and refused.
      if (allowCross !== Boolean(agent.allow_cross_provider)) {
        await api.patch(`/admin/agents/${id}`, { allow_cross_provider: allowCross });
      }
      await api.put(`/admin/agents/${id}/chain`, { chain });
      handle.close();
      toast(`Chain saved: ${chain.join(" → ")}`);
      if (onSaved) await onSaved();
    } catch (err) {
      const problems = q("#chain-problems");
      if (problems) problems.innerHTML = `<div>• ${esc(err.message)}</div>`;
      else toast(err.message, "err");
      button.disabled = false;
    }
  });

  (async () => {
    try {
      const current = await api.get(`/admin/agents/${agent.id ?? agent.scope_id}/chain`);
      chain = current.chain.slice();
      allowCross = current.allow_cross_provider;
      render();
    } catch (err) {
      handle.setBody(`<p class="empty">Could not load the chain: ${esc(err.message)}</p>`);
    }
  })();

  return handle;
}
