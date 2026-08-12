/* Hash router.
 *
 * Hash rather than the History API because the app is served by FastAPI from a
 * single static file — a real path like /agents/7 would need a server-side
 * catch-all route, and hash routing needs nothing.
 *
 * Each route owns a mount/unmount pair so a view can tear down its store
 * subscription; without that, navigating away leaves listeners rendering into
 * detached DOM.
 */

const routes = [];
let current = null;
let outlet = null;

/** `pattern` may contain :params, e.g. "/agents/:id". */
export function route(pattern, view) {
  const names = [];
  const regex = new RegExp(
    "^" +
      pattern
        .replace(/\//g, "\\/")
        .replace(/:(\w+)/g, (_, name) => {
          names.push(name);
          return "([^\\/]+)";
        }) +
      "$"
  );
  routes.push({ pattern, regex, names, view });
}

function parse() {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = raw.split("?");
  return { path: path || "/", query: new URLSearchParams(query) };
}

function match(path) {
  for (const entry of routes) {
    const found = path.match(entry.regex);
    if (!found) continue;
    const params = {};
    entry.names.forEach((name, index) => (params[name] = decodeURIComponent(found[index + 1])));
    return { entry, params };
  }
  return null;
}

async function render() {
  const { path, query } = parse();
  const found = match(path);

  if (current?.unmount) {
    try {
      current.unmount();
    } catch (err) {
      console.error("view unmount failed", err);
    }
  }
  current = null;
  outlet.innerHTML = "";

  document.querySelectorAll("[data-nav]").forEach((link) => {
    const target = link.getAttribute("href").replace(/^#/, "");
    const active = target === "/" ? path === "/" : path.startsWith(target);
    link.classList.toggle("nav-link--active", active);
  });

  if (!found) {
    outlet.innerHTML = `
      <section class="panel">
        <div class="empty">
          <p>Nothing here.</p>
          <p><a class="link" href="#/">Back to the dashboard</a></p>
        </div>
      </section>`;
    return;
  }

  try {
    current = (await found.entry.view(outlet, found.params, query)) || null;
  } catch (err) {
    console.error("view failed to mount", err);
    outlet.innerHTML = `
      <section class="panel"><div class="empty">
        <p>This view failed to load.</p>
        <p class="field-hint">${String(err.message || err)}</p>
      </div></section>`;
  }
  window.scrollTo(0, 0);
}

export function navigate(path) {
  if (window.location.hash === `#${path}`) render();
  else window.location.hash = path;
}

export function start(outletNode) {
  outlet = outletNode;
  window.addEventListener("hashchange", render);
  return render();
}
