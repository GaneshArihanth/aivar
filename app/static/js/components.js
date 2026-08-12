/* Shared UI primitives: toast, modal, confirm, prompt, drawer.
 *
 * The confirm/prompt dialogs exist because the browser's own are unstyled,
 * unformattable and truncate long text — and several of this app's
 * confirmations genuinely need emphasis ("$32.10 is already spent; a $20 limit
 * blocks this agent immediately"). They return promises, so call sites read
 * the same as the native ones they replace.
 */

import { $, $$, el } from "./util.js";

/* ------------------------------------------------------------------ toast */

let toastTimer = null;

export function toast(message, kind = "ok") {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast toast--${kind}`;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), 3600);
}

/* ------------------------------------------------------------------ modal */

const stack = [];

function focusables(root) {
  return $$(
    'button:not([disabled]), input:not([disabled]), select, textarea, [href], summary, [tabindex]:not([tabindex="-1"])',
    root
  ).filter((node) => node.offsetParent !== null);
}

function onKeydown(event) {
  const top = stack[stack.length - 1];
  if (!top) return;

  if (event.key === "Escape" && top.dismissible) {
    event.preventDefault();
    top.close();
    return;
  }
  if (event.key !== "Tab") return;

  // A modal that lets Tab wander behind it is a modal in name only.
  const items = focusables(top.node);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

document.addEventListener("keydown", onKeydown);

/**
 * Open a modal. Returns a handle with { node, close(), setBody(html) }.
 * `size` is "sm" | "md" | "lg"; `onClose` fires however it is dismissed.
 */
export function openModal({
  title,
  body = "",
  footer = "",
  size = "md",
  dismissible = true,
  onClose = null,
  className = "",
}) {
  const restoreFocus = document.activeElement;

  const backdrop = el(`
    <div class="backdrop">
      <div class="modal modal--${size} ${className}" role="dialog" aria-modal="true"
           aria-label="${title ? String(title).replace(/"/g, "&quot;") : "Dialog"}">
        ${title
          ? `<header class="modal-head">
               <h2>${title}</h2>
               ${dismissible ? '<button class="icon-btn" data-modal-close aria-label="Close">×</button>' : ""}
             </header>`
          : ""}
        <div class="modal-body"></div>
        ${footer ? `<footer class="modal-foot"></footer>` : ""}
      </div>
    </div>`);

  const bodyNode = $(".modal-body", backdrop);
  if (typeof body === "string") bodyNode.innerHTML = body;
  else bodyNode.appendChild(body);

  const footNode = $(".modal-foot", backdrop);
  if (footNode) {
    if (typeof footer === "string") footNode.innerHTML = footer;
    else footNode.appendChild(footer);
  }

  const handle = {
    node: backdrop,
    dismissible,
    close(result) {
      const index = stack.indexOf(handle);
      if (index === -1) return;
      stack.splice(index, 1);
      backdrop.remove();
      if (!stack.length) document.body.classList.remove("modal-open");
      if (restoreFocus && restoreFocus.focus) restoreFocus.focus();
      if (onClose) onClose(result);
    },
    setBody(html) {
      bodyNode.innerHTML = html;
    },
    query(sel) {
      return $(sel, backdrop);
    },
  };

  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop && dismissible) handle.close();
  });
  $$("[data-modal-close]", backdrop).forEach((node) =>
    node.addEventListener("click", () => handle.close())
  );

  document.body.appendChild(backdrop);
  document.body.classList.add("modal-open");
  stack.push(handle);

  setTimeout(() => {
    const target = $("[data-autofocus]", backdrop) || focusables(backdrop)[0];
    if (target) target.focus();
  }, 20);

  return handle;
}

/* ---------------------------------------------------------------- confirm */

export function confirmDialog({
  title = "Are you sure?",
  message = "",
  detail = "",
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };

    const handle = openModal({
      title,
      size: "sm",
      body: `
        <p class="dialog-message">${message}</p>
        ${detail ? `<p class="dialog-detail">${detail}</p>` : ""}`,
      footer: `
        <button class="btn btn--ghost" data-cancel>${cancelLabel}</button>
        <button class="btn ${danger ? "btn--danger" : "btn--primary"}" data-confirm
                data-autofocus>${confirmLabel}</button>`,
      onClose: () => finish(false),
    });

    handle.query("[data-cancel]").addEventListener("click", () => {
      finish(false);
      handle.close();
    });
    handle.query("[data-confirm]").addEventListener("click", () => {
      finish(true);
      handle.close();
    });
  });
}

/* ----------------------------------------------------------------- prompt */

/**
 * Ask for a single value. `validate` returns an error string or null.
 * Resolves with the value, or null if cancelled.
 */
export function promptDialog({
  title,
  label,
  value = "",
  help = "",
  detail = "",
  type = "text",
  confirmLabel = "Save",
  placeholder = "",
  validate = null,
  multiline = false,
}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };

    const field = multiline
      ? `<textarea id="dlg-input" rows="3" placeholder="${placeholder}" data-autofocus>${value}</textarea>`
      : `<input id="dlg-input" type="${type}" value="${value}" placeholder="${placeholder}"
                step="any" data-autofocus />`;

    const handle = openModal({
      title,
      size: "sm",
      body: `
        ${detail ? `<p class="dialog-detail">${detail}</p>` : ""}
        <div class="field">
          <label for="dlg-input">${label}</label>
          ${field}
          ${help ? `<p class="field-hint">${help}</p>` : ""}
          <p class="field-error" id="dlg-error"></p>
        </div>`,
      footer: `
        <button class="btn btn--ghost" data-cancel>Cancel</button>
        <button class="btn btn--primary" data-confirm>${confirmLabel}</button>`,
      onClose: () => finish(null),
    });

    const input = handle.query("#dlg-input");
    const error = handle.query("#dlg-error");

    const submit = () => {
      const raw = input.value.trim();
      const problem = validate ? validate(raw) : null;
      if (problem) {
        error.textContent = problem;
        input.classList.add("invalid");
        return;
      }
      finish(raw);
      handle.close();
    };

    input.addEventListener("input", () => {
      error.textContent = "";
      input.classList.remove("invalid");
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !multiline) {
        event.preventDefault();
        submit();
      }
    });
    handle.query("[data-confirm]").addEventListener("click", submit);
    handle.query("[data-cancel]").addEventListener("click", () => {
      finish(null);
      handle.close();
    });
  });
}

/* ----------------------------------------------------------------- drawer */

/** Side panel for detail views that should not lose the page behind them. */
export function openDrawer({ title, body = "", subtitle = "", onClose = null }) {
  const restoreFocus = document.activeElement;

  const wrap = el(`
    <div class="drawer-backdrop">
      <aside class="drawer" role="dialog" aria-modal="true">
        <header class="drawer-head">
          <div>
            <h2>${title}</h2>
            ${subtitle ? `<p class="drawer-sub">${subtitle}</p>` : ""}
          </div>
          <button class="icon-btn" data-modal-close aria-label="Close">×</button>
        </header>
        <div class="drawer-body"></div>
      </aside>
    </div>`);

  const bodyNode = $(".drawer-body", wrap);
  if (typeof body === "string") bodyNode.innerHTML = body;
  else bodyNode.appendChild(body);

  const handle = {
    node: wrap,
    dismissible: true,
    close() {
      const index = stack.indexOf(handle);
      if (index === -1) return;
      stack.splice(index, 1);
      wrap.remove();
      if (!stack.length) document.body.classList.remove("modal-open");
      if (restoreFocus && restoreFocus.focus) restoreFocus.focus();
      if (onClose) onClose();
    },
    query: (sel) => $(sel, wrap),
    setBody(html) {
      bodyNode.innerHTML = html;
    },
  };

  wrap.addEventListener("click", (event) => {
    if (event.target === wrap) handle.close();
  });
  $$("[data-modal-close]", wrap).forEach((node) =>
    node.addEventListener("click", () => handle.close())
  );

  document.body.appendChild(wrap);
  document.body.classList.add("modal-open");
  stack.push(handle);
  setTimeout(() => wrap.classList.add("drawer-backdrop--in"), 10);

  return handle;
}

/* -------------------------------------------------------------- clipboard */

export async function copyText(text) {
  // The async Clipboard API needs a secure context and, in most browsers, a
  // live user gesture. Rather than predicting whether both hold, try it and
  // fall back on failure.
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* fall through */
    }
  }
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "");
  helper.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(helper);
  helper.select();
  helper.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  helper.remove();
  return ok;
}
