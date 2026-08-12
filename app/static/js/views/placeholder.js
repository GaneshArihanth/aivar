/* Placeholder views for pages arriving in later workstreams.
 *
 * Routed and navigable from the start so the shell can be verified end to end
 * before the pages exist — a nav link that 404s is harder to trust than one
 * that says plainly what is coming.
 */

export function placeholderView(title, description, workstream) {
  return function mount(outlet) {
    outlet.innerHTML = `
      <section class="panel">
        <div class="panel-head"><h2>${title}</h2></div>
        <div class="empty placeholder">
          <p><strong>${description}</strong></p>
          <p class="field-hint">Arriving in workstream ${workstream}.</p>
        </div>
      </section>`;
    return null;
  };
}
