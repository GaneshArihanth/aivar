/* Demo — drive a real agent against a real provider and watch enforcement act.
 *
 * The rest of the dashboard shows what the controller *has* decided. This page
 * exists to make it decide something while you watch, against a live provider,
 * so the numbers are not the mock's invention.
 *
 * Two things make that awkward, and shape everything here:
 *
 *   1. The dashboard cannot read agent keys. Only HMACs are stored, and the raw
 *      key is shown exactly once at creation. So this page mints its own agent
 *      and holds that key in memory for the session; nothing is persisted.
 *
 *   2. Real models are cheap. Gemini 2.0 Flash is $0.0001 per 1k input tokens,
 *      so a seeded $50/month agent would need on the order of half a billion
 *      tokens before a single threshold moved. A demo on those budgets proves
 *      nothing. The agent minted here gets a deliberately tiny budget, which is
 *      what lets warn → substitute → block happen inside a handful of calls and
 *      a fraction of a cent.
 */

import { api } from "../api.js";
import { copyText, toast } from "../components.js";
import { $, esc, stateClass, usdPrecise } from "../util.js";

// Sized against what a call actually costs, which is the whole difficulty.
// gemini-2.0-flash runs about $0.00015 a call at these prompt lengths, so a
// tenth of a cent is roughly six calls: enough that the first one is not also
// the last, few enough that warn (80%) → substitute (90%) → block (100%) all
// happen while someone is still watching. A $0.02 budget — never mind the
// seeded $50 — simply never reaches the interesting part.
const DEMO_MONTHLY_USD = 0.001;
const DEMO_SESSION_USD = 0.001;

// Held in memory only. A reload mints a fresh agent rather than persisting a
// credential in localStorage where a shared browser would leak it.
let state = {
  config: null,
  agent: null, // { id, name, key }
  model: null,
  live: false,
  sessionId: null,
  turns: [], // { role, text, meta }
  agentStatus: null, // precise figures from /v1/budget/status
  busy: false,
};

const SCENARIOS = [
  {
    id: "normal",
    label: "Normal call",
    hint: "Baseline: a call that should succeed and meter real cost.",
    prompt: "In one sentence, what is a token budget?",
    maxTokens: 60,
  },
  {
    id: "warn",
    label: "Push past 80%",
    hint: "Larger answers drain the budget until the warning threshold trips.",
    prompt: "Explain LLM cost control in about 120 words.",
    maxTokens: 200,
  },
  {
    id: "substitute",
    label: "Force substitution",
    hint: "Past 90% the controller should serve a cheaper model than requested.",
    prompt: "Describe three ways to reduce inference spend, briefly.",
    maxTokens: 250,
  },
  {
    id: "exhaust",
    label: "Exhaust the budget",
    hint: "Keeps calling until the hard stop returns 402 rather than spending.",
    prompt: "Write a detailed paragraph about budget enforcement in agents.",
    maxTokens: 400,
    repeat: 8,
  },
  {
    id: "runaway",
    label: "Trip the runaway detector",
    hint: "Arms the breaker, then bursts — the agent is paused for human review.",
    prompt: "Summarise why runaway agents are expensive.",
    maxTokens: 300,
    repeat: 8,
    // Re-arms the breaker this agent was created without.
    armRunaway: 0.2,
  },
];

/* ------------------------------------------------------------------ helpers */

function headerMeta(res) {
  const h = (name) => res.headers.get(name);
  return {
    requestId: h("X-Budget-Request-Id"),
    cost: h("X-Budget-Cost-USD"),
    requested: h("X-Budget-Model-Requested"),
    served: h("X-Budget-Model-Served"),
    agentRemaining: h("X-Budget-Agent-Remaining-USD"),
    teamRemaining: h("X-Budget-Team-Remaining-USD"),
    sessionRemaining: h("X-Budget-Session-Remaining-USD"),
    substitution: h("X-Budget-Substitution-Reason"),
    warning: h("X-Budget-Warning"),
  };
}

/** The proxy is not the admin API: it needs the agent key and, unlike api.js,
 *  its headers are the interesting part of the answer. Hence raw fetch. */
async function dispatch({ prompt, maxTokens }) {
  const messages = [
    ...state.turns
      .filter((t) => t.role === "user" || t.role === "assistant")
      .map((t) => ({ role: t.role, content: t.text })),
    { role: "user", content: prompt },
  ];

  const res = await fetch("/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Agent-Key": state.agent.key,
      "X-Session-Id": state.sessionId,
      ...(state.live ? { "X-Budget-Live-Dispatch": "1" } : {}),
    },
    body: JSON.stringify({
      model: state.model,
      messages,
      max_tokens: maxTokens,
    }),
  });

  let body = null;
  try {
    body = await res.json();
  } catch {
    body = { error: { message: await res.text() } };
  }
  return { res, body, meta: headerMeta(res) };
}

/* --------------------------------------------------------------- rendering */

function metaChips(meta) {
  const chips = [];
  if (meta.cost) chips.push(`<span class="pill">cost ${esc(meta.cost)}</span>`);
  if (meta.served && meta.requested && meta.served !== meta.requested) {
    chips.push(
      `<span class="pill pill--warn" title="${esc(meta.substitution || "substituted")}">
         ${esc(meta.requested)} → ${esc(meta.served)}</span>`
    );
  } else if (meta.served) {
    chips.push(`<span class="pill pill--ok">${esc(meta.served)}</span>`);
  }
  if (meta.warning) {
    chips.push(`<span class="pill pill--warn">warning: ${esc(meta.warning)}</span>`);
  }
  return chips.join(" ");
}

function turnMarkup(turn) {
  if (turn.role === "user") {
    return `<div class="demo-turn demo-turn--user"><div class="demo-bubble">${esc(
      turn.text
    )}</div></div>`;
  }
  if (turn.role === "blocked") {
    return `
      <div class="demo-turn demo-turn--blocked">
        <div class="demo-bubble demo-bubble--blocked">
          <div class="demo-blocked-title">Blocked · HTTP ${esc(String(turn.status))}</div>
          <div class="demo-blocked-type mono">${esc(turn.errorType || "")}</div>
          <div>${esc(turn.text)}</div>
        </div>
      </div>`;
  }
  return `
    <div class="demo-turn demo-turn--agent">
      <div class="demo-bubble">${esc(turn.text)}</div>
      <div class="demo-meta">${metaChips(turn.meta || {})}</div>
    </div>`;
}

/* The X-Budget-*-Remaining headers are formatted to cents, which is the right
 * call for a $500 team budget and useless here: every tile would read $0.00
 * against a tenth-of-a-cent budget and look broken. /v1/budget/status carries
 * the unrounded figures, so the panel reads from there and uses the headers
 * only for last-call cost, which is already sent at full precision. */
function budgetPanel() {
  const last = [...state.turns].reverse().find((t) => t.meta);
  const s = state.agentStatus;

  const pct = s ? Math.min(100, (s.pct || 0) * 100) : 0;
  const fine = (n) =>
    "$" + Number(n || 0).toFixed(6).replace(/0+$/, "").replace(/\.$/, ".0");

  const stat = (label, value) => `
    <div class="demo-stat">
      <div class="demo-stat-label">${label}</div>
      <div class="demo-stat-value">${value == null ? "—" : esc(String(value))}</div>
    </div>`;

  return `
    <div class="demo-stats">
      ${stat("Spent", s ? fine(s.consumed_usd) : null)}
      ${stat("Budget", s ? fine(s.limit_usd) : null)}
      ${stat("Used", s ? pct.toFixed(1) + "%" : null)}
      ${stat("Last call", last?.meta?.cost ? "$" + last.meta.cost : null)}
    </div>
    ${
      s
        ? `<div class="demo-gauge">
             <div class="bar">
               <div class="bar-fill ${stateClass(s.state)}" style="width:${pct.toFixed(1)}%"></div>
             </div>
           </div>`
        : ""
    }`;
}

function setupMarkup() {
  const cfg = state.config;
  const models = (cfg?.dispatchable_models || []).filter((m) => m.base_url !== null);

  const options = models
    .map(
      (m) =>
        `<option value="${esc(m.model_id)}" ${m.model_id === state.model ? "selected" : ""}>
           ${esc(m.model_id)} — ${esc(m.provider)}${m.ready ? "" : " (key unset)"}
         </option>`
    )
    .join("");

  const selected = models.find((m) => m.model_id === state.model);
  const liveBlocked = !cfg?.live_allowed || (selected && !selected.ready);

  let liveNote = "";
  if (!cfg?.live_allowed) {
    liveNote = "Live dispatch is disabled on this server (DEMO_ALLOW_LIVE=false).";
  } else if (selected && !selected.ready) {
    liveNote = `${selected.api_key_env} is not set, so this model can only be called through the mock.`;
  } else if (state.live) {
    liveNote = "Calls go to the real provider and spend real quota.";
  } else {
    liveNote = "Calls go to the bundled mock — free, and enforcement still applies.";
  }

  return `
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Demo</h2>
          <p class="panel-sub">
            Drive a real agent and watch the controller decide. The agent minted here
            gets a ${usdPrecise(DEMO_MONTHLY_USD)} monthly budget on purpose — real models are
            cheap enough that a normal budget would never visibly move.
          </p>
        </div>
      </div>

      <div class="demo-setup">
        <label class="field">
          <span class="field-label">Model</span>
          <select id="demo-model">${options}</select>
        </label>

        <label class="field">
          <span class="field-label">Dispatch</span>
          <label class="demo-toggle">
            <input type="checkbox" id="demo-live" ${state.live ? "checked" : ""}
                   ${liveBlocked ? "disabled" : ""} />
            <span>Use real provider</span>
          </label>
          <span class="field-hint">${esc(liveNote)}</span>
        </label>

        <div class="field">
          <span class="field-label">Agent</span>
          <div id="demo-agent-state">
            ${
              state.agent
                ? `<div class="demo-agent-ok">
                     <span class="pill pill--ok">${esc(state.agent.name)}</span>
                     <button class="btn btn--ghost btn--sm" id="demo-copy-key">Copy key</button>
                     <button class="btn btn--ghost btn--sm" id="demo-reset">Reset</button>
                   </div>`
                : `<button class="btn btn--primary" id="demo-create">Create demo agent</button>`
            }
          </div>
        </div>
      </div>
    </section>`;
}

function scenarioMarkup() {
  if (!state.agent) return "";
  return `
    <section class="panel">
      <div class="panel-head"><h2>Scenarios</h2></div>
      <div class="demo-scenarios">
        ${SCENARIOS.map(
          (s) => `
          <button class="demo-scenario" data-scenario="${s.id}" ${state.busy ? "disabled" : ""}>
            <span class="demo-scenario-label">${esc(s.label)}</span>
            <span class="demo-scenario-hint">${esc(s.hint)}</span>
          </button>`
        ).join("")}
      </div>
    </section>`;
}

function chatMarkup() {
  if (!state.agent) return "";
  return `
    <section class="panel">
      <div class="panel-head">
        <h2>Conversation</h2>
        <span class="panel-sub mono">session ${esc(state.sessionId || "")}</span>
      </div>
      ${budgetPanel()}
      ${
        state.agentStatus?.blocked
          ? `<div class="demo-blocked-bar">
               <span>This agent is paused and will refuse every call until released.</span>
               <button class="btn btn--primary btn--sm" id="demo-unblock">Release for another run</button>
             </div>`
          : ""
      }
      <div class="demo-thread" id="demo-thread">
        ${
          state.turns.length
            ? state.turns.map(turnMarkup).join("")
            : `<div class="empty"><p>No calls yet. Send a message, or run a scenario.</p></div>`
        }
      </div>
      <form class="demo-composer" id="demo-form">
        <input type="text" id="demo-input" placeholder="Ask the agent something…"
               autocomplete="off" ${state.busy ? "disabled" : ""} />
        <button class="btn btn--primary" type="submit" ${state.busy ? "disabled" : ""}>
          ${state.busy ? "Sending…" : "Send"}
        </button>
      </form>
    </section>`;
}

/* ------------------------------------------------------------------- actions */

async function createAgent() {
  const teams = await api.get("/admin/teams");
  if (!teams.length) {
    toast("No teams exist — seed the fleet first.", "error");
    return;
  }
  const name = `demo-${Math.random().toString(36).slice(2, 8)}`;
  // The raw key comes back alongside the agent, not inside it — this is the
  // only response in the API that carries one, and only this once.
  const created = await api.post("/admin/agents", {
    name,
    team_id: teams[0].id,
    monthly_budget_usd: DEMO_MONTHLY_USD,
    session_budget_usd: DEMO_SESSION_USD,
    preferred_model: state.model,
    allow_substitution: true,
    // Disabled, or it fires before anything else can be shown. The breaker
    // trips at 20% of the monthly budget burned within an hour; this agent
    // burns its whole budget in seconds, so runaway would always win the race
    // and the budget ladder below it would be unreachable. The runaway
    // scenario turns it back on deliberately.
    runaway_hourly_fraction: 0,
  });
  state.agent = {
    id: created.agent.id,
    name: created.agent.name,
    key: created.api_key,
  };
  state.sessionId = `demo-${Date.now().toString(36)}`;
  state.turns = [];
  toast(
    `Created ${created.agent.name} with a ${usdPrecise(DEMO_MONTHLY_USD)} budget`,
    "ok"
  );
}

/** Pull the unrounded spend figures for the demo agent. Best-effort: a failed
 *  refresh should leave the last known numbers on screen, not blank the panel
 *  or abort the scenario that is mid-run. */
async function refreshStatus() {
  if (!state.agent) return;
  try {
    const status = await api.get("/v1/budget/status");
    const agents = (status.teams || []).flatMap((t) => t.agents || []);
    const mine = agents.find((a) => String(a.scope_id) === String(state.agent.id));
    if (mine) state.agentStatus = mine;
  } catch {
    /* keep whatever we last had */
  }
}

async function send({ prompt, maxTokens = 120 }) {
  state.turns.push({ role: "user", text: prompt });
  state.busy = true;
  render();

  try {
    const { res, body, meta } = await dispatch({ prompt, maxTokens });

    if (res.ok) {
      const text =
        body?.choices?.[0]?.message?.content ?? "(no content in response)";
      state.turns.push({ role: "assistant", text, meta });
      return { ok: true };
    }

    // A rejection is the product working, not an error to hide. Surface which
    // rule fired so the demo reads as enforcement rather than breakage.
    const err = body?.error || {};
    state.turns.push({
      role: "blocked",
      status: res.status,
      errorType: err.type || "",
      text: err.message || "Request was refused.",
      meta,
    });
    return { ok: false, status: res.status };
  } catch (e) {
    state.turns.push({
      role: "blocked",
      status: 0,
      errorType: "network_error",
      text: String(e.message || e),
    });
    return { ok: false, status: 0 };
  } finally {
    await refreshStatus();
    state.busy = false;
    render();
  }
}

async function runScenario(scenario) {
  if (scenario.armRunaway != null) {
    await api.patch(`/admin/agents/${state.agent.id}`, {
      runaway_hourly_fraction: scenario.armRunaway,
    });
    toast(`Runaway breaker armed at ${scenario.armRunaway * 100}% per hour`, "ok");
  }
  const rounds = scenario.repeat || 1;
  for (let i = 0; i < rounds; i += 1) {
    const outcome = await send({
      prompt: scenario.prompt,
      maxTokens: scenario.maxTokens,
    });
    // Stop the moment enforcement acts — continuing past the block would just
    // stack identical 402s and bury the thing the scenario set out to show.
    if (!outcome.ok) break;
  }
}

/* --------------------------------------------------------------------- view */

let root = null;

function render() {
  if (!root) return;
  root.innerHTML = setupMarkup() + scenarioMarkup() + chatMarkup();

  const modelSel = $("#demo-model", root);
  if (modelSel) {
    modelSel.onchange = () => {
      state.model = modelSel.value;
      render();
    };
  }

  const liveBox = $("#demo-live", root);
  if (liveBox) {
    liveBox.onchange = () => {
      state.live = liveBox.checked;
      render();
    };
  }

  const createBtn = $("#demo-create", root);
  if (createBtn) {
    createBtn.onclick = async () => {
      createBtn.disabled = true;
      try {
        await createAgent();
      } catch (e) {
        toast(e.message || "Could not create the demo agent", "error");
      } finally {
        render();
      }
    };
  }

  const copyBtn = $("#demo-copy-key", root);
  if (copyBtn) copyBtn.onclick = () => copyText(state.agent.key);

  const resetBtn = $("#demo-reset", root);
  if (resetBtn) {
    resetBtn.onclick = () => {
      state.agent = null;
      state.turns = [];
      state.sessionId = null;
      state.agentStatus = null;
      render();
    };
  }

  root.querySelectorAll("[data-scenario]").forEach((btn) => {
    btn.onclick = () => {
      const scenario = SCENARIOS.find((s) => s.id === btn.dataset.scenario);
      if (scenario) runScenario(scenario);
    };
  });

  const form = $("#demo-form", root);
  if (form) {
    form.onsubmit = (ev) => {
      ev.preventDefault();
      const input = $("#demo-input", root);
      const text = input.value.trim();
      if (text) send({ prompt: text });
    };
  }

  const unblockBtn = $("#demo-unblock", root);
  if (unblockBtn) {
    unblockBtn.onclick = async () => {
      unblockBtn.disabled = true;
      try {
        await api.post(`/admin/agents/${state.agent.id}/unblock`);
        // Disarm the breaker again, otherwise the next burst re-pauses
        // immediately and "release" looks like it did nothing.
        await api.patch(`/admin/agents/${state.agent.id}`, {
          runaway_hourly_fraction: 0,
        });
        toast("Agent released", "ok");
      } catch (e) {
        toast(e.message || "Could not release the agent", "error");
      } finally {
        await refreshStatus();
        render();
      }
    };
  }

  const thread = $("#demo-thread", root);
  if (thread) thread.scrollTop = thread.scrollHeight;
}

export async function demoView(outlet) {
  root = outlet;
  outlet.innerHTML = `<section class="panel"><div class="empty"><p>Loading…</p></div></section>`;

  try {
    state.config = await api.get("/admin/demo/config");
  } catch (e) {
    outlet.innerHTML = `
      <section class="panel"><div class="empty">
        <p>Could not load demo configuration.</p>
        <p class="field-hint">${esc(String(e.message || e))}</p>
      </div></section>`;
    return { unmount() {} };
  }

  const models = state.config.dispatchable_models || [];
  if (!state.model) {
    // Prefer a hosted model whose key is set, so the live toggle works without
    // further setup. "ready" alone is not enough: a local Ollama entry needs no
    // key and so always reports ready, which would make an unreachable box on
    // localhost:11434 the default and the first call a connection error.
    const hosted = models.filter((m) => m.api_key_env);
    state.model =
      (hosted.find((m) => m.ready) || hosted[0] || models[0])?.model_id || "gpt-4o";
  }

  render();
  return {
    unmount() {
      root = null;
    },
  };
}
