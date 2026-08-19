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
    title: stored ? `Replace ${cred.env_name}` : `Set ${cred.env_name}`,
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
          ${
            !stored && cred.has_env_default
              ? "This provider already has a key from the deployment; saving here overrides it until you remove it again."
              : ""
          }
        </p>
      </div>`,
    footer: `
      ${
        stored
          ? `<button class="btn btn--danger" data-act="clear">${
              cred.has_env_default ? "Revert to deployed key" : "Remove"
            }</button>`
          : ""
      }
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
      cred.has_env_default
        ? `${cred.env_name} reverted to the deployed key`
        : `${cred.env_name} removed`
    )
  );
}

/** Markup for the credentials panel on the Models page. */
export function credentialsPanel(creds) {
  if (!creds.length) return "";

  const row = (c) => {
    // Says which value is actually in use, not merely that one exists. When a
    // stored key sits on top of a deployed one, both are worth naming: the
    // difference decides whether "Remove" unconfigures the provider or simply
    // reverts to what the deployment supplies.
    let state;
    if (c.source === "stored") {
      state = `<span class="pill pill--ok">stored ····${esc(c.last4 || "")}</span>`;
      if (c.overrides_environment) {
        state += `<span class="cell-sub">overriding the deployed key</span>`;
      }
    } else if (c.source === "environment") {
      state = `<span class="pill pill--ok">from deployment</span>
               <span class="cell-sub">set in .env or SSM — override it here</span>`;
    } else {
      state = `<span class="pill pill--warn">not set</span>`;
    }

    let label = "Set key";
    if (c.source === "stored") label = "Replace";
    else if (c.source === "environment") label = "Override";

    return `
      <tr>
        <td class="mono">${esc(c.env_name)}</td>
        <td>${state}</td>
        <td class="row-actions">
          <button class="btn btn--ghost btn--sm" data-cred="${esc(c.env_name)}">${label}</button>
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
            A key set here overrides one supplied by the deployment; remove it
            and the deployed value takes over again.
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
