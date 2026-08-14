/* Setting provider API keys from the dashboard.
 *
 * On a deployed instance this is the only practical route: there is no shell,
 * and the alternative is editing an SSM parameter and running a redeploy.
 *
 * The key is write-only. The server never returns it, so this module never has
 * one to display and never keeps one after the request — the field is cleared
 * on submit. What comes back is the last four characters, which is enough to
 * confirm the right key landed and not enough to reconstruct it.
 */

import { api } from "./api.js";
import { openModal, toast } from "./components.js";
import { esc } from "./util.js";

/** Open the "set key" dialog for one environment variable name. */
export function editCredential(cred, onDone) {
  const stored = cred.source === "stored";

  const handle = openModal({
    title: `Set ${cred.env_name}`,
    size: "sm",
    body: `
      <p class="dialog-message">
        Sent to the server, encrypted, and stored. It is never shown again and
        never returned by the API — only its last four characters.
      </p>
      ${
        location.protocol !== "https:"
          ? `<p class="dialog-detail dialog-detail--warn">
               This page is served over plain HTTP, so the key travels
               unencrypted and anyone on the network path can read it. Put the
               site behind HTTPS before sending a key you care about.
             </p>`
          : ""
      }
      <div class="field">
        <label for="cred-value">Key</label>
        <input id="cred-value" type="password" autocomplete="off"
               spellcheck="false" placeholder="Paste the provider key" />
        <p class="field-hint">
          The key itself, not the variable name.
          ${stored ? `Replaces the stored key ending <code>${esc(cred.last4 || "")}</code>.` : ""}
        </p>
      </div>`,
    footer: `
      ${stored ? '<button class="btn btn--danger" data-act="clear">Remove</button>' : ""}
      <button class="btn" data-modal-close>Cancel</button>
      <button class="btn btn--primary" data-act="save">Save</button>`,
  });

  // handle.node is the backdrop; scoping queries to it avoids matching a
  // same-id field in a modal underneath this one on the stack.
  const root = handle.node;
  const input = root.querySelector("#cred-value");
  input?.focus();

  const finish = async (fn, okMessage) => {
    try {
      await fn();
      toast(okMessage, "ok");
      handle.close();
      onDone?.();
    } catch (e) {
      toast(e.message || "Request failed", "error");
    }
  };

  root.querySelector('[data-act="save"]')?.addEventListener("click", () => {
    const value = input.value.trim();
    if (!value) {
      toast("Enter a key first", "error");
      return;
    }
    // Cleared immediately: the DOM should not hold key material any longer
    // than the request needs it.
    input.value = "";
    finish(
      () => api.put(`/admin/credentials/${encodeURIComponent(cred.env_name)}`, { value }),
      `${cred.env_name} saved`
    );
  });

  root.querySelector('[data-act="clear"]')?.addEventListener("click", () =>
    finish(
      () => api.del(`/admin/credentials/${encodeURIComponent(cred.env_name)}`),
      `${cred.env_name} removed`
    )
  );
}

/** Markup for the credentials panel on the Models page. */
export function credentialsPanel(creds) {
  if (!creds.length) return "";

  const row = (c) => {
    let state;
    if (c.source === "environment") {
      // Set in the environment, so a stored value would never be consulted.
      // Saying so prevents the "I set it and nothing changed" confusion.
      state = `<span class="pill pill--ok">set in environment</span>
               <span class="cell-sub">deployed config wins; edit .env or SSM to change</span>`;
    } else if (c.source === "stored") {
      state = `<span class="pill pill--ok">stored ····${esc(c.last4 || "")}</span>`;
    } else {
      state = `<span class="pill pill--warn">not set</span>`;
    }

    return `
      <tr>
        <td class="mono">${esc(c.env_name)}</td>
        <td>${state}</td>
        <td class="row-actions">
          ${
            c.editable
              ? `<button class="btn btn--ghost btn--sm" data-cred="${esc(c.env_name)}">
                   ${c.source === "stored" ? "Replace" : "Set key"}
                 </button>`
              : ""
          }
        </td>
      </tr>`;
  };

  return `
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Provider credentials</h2>
          <p class="panel-sub">
            Keys for the endpoints in the catalog above. Stored encrypted, shown
            only by their last four characters, and never returned by the API.
            A value present in the environment always takes precedence.
          </p>
        </div>
      </div>
      <table class="table">
        <thead>
          <tr><th>Variable</th><th>Status</th><th></th></tr>
        </thead>
        <tbody>${creds.map(row).join("")}</tbody>
      </table>
    </section>`;
}
