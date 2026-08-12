/* Shared application state.
 *
 * One SSE connection and one poll for the whole app, not one per view. Two
 * data paths, as before: events push the moment something happens, and a slow
 * poll is the safety net — the stream alone would drift if an event were
 * missed, and polling alone would feel dead between ticks.
 *
 * Views subscribe; they never fetch status themselves. That way navigating
 * between pages does not tear down and rebuild the live connection.
 */

import { api } from "./api.js";

const POLL_MS = 3000;
const MAX_EVENTS = 80;

const EVENT_TYPES = [
  "budget.warning",
  "budget.rejected_budget",
  "budget.rejected_session",
  "budget.rejected_runaway",
  "agent.runaway_blocked",
  "agent.unblocked",
  "agent.created",
  "agent.updated",
  "agent.deleted",
  "agent.key_rotated",
  "model.substituted",
  "team.frozen",
  "team.unfrozen",
  "agent.boosted",
  "message",
];

export const store = {
  status: null,
  events: [],
  teams: [],
  models: [],
  live: false,
  lastCreatedAgentId: null,

  _listeners: new Set(),
  _source: null,
  _timer: null,
  _key: 0,

  subscribe(fn) {
    this._listeners.add(fn);
    return () => this._listeners.delete(fn);
  },

  emit(reason = "update") {
    for (const fn of this._listeners) {
      try {
        fn(this, reason);
      } catch (err) {
        console.error("store listener failed", err);
      }
    }
  },

  /** Every agent across every team, flattened. */
  agents() {
    if (!this.status) return [];
    return this.status.teams.flatMap((team) =>
      team.agents.map((agent) => ({ ...agent, team_name: team.name, team_id: team.scope_id }))
    );
  },

  agent(id) {
    return this.agents().find((a) => String(a.scope_id) === String(id)) || null;
  },

  blockedAgents() {
    return this.agents().filter((a) => a.blocked || a.status === "blocked");
  },

  async refreshStatus() {
    try {
      this.status = await api.get("/v1/budget/status");
      this.emit("status");
    } catch (err) {
      console.error("status refresh failed", err);
    }
  },

  async loadReference() {
    const [teams, models] = await Promise.all([
      api.get("/admin/teams"),
      api.get("/admin/models"),
    ]);
    this.teams = teams;
    this.models = models;
    this.emit("reference");
  },

  /** Stable per-session identity, so views can render the feed incrementally
   *  instead of rebuilding it. Events carry no id of their own: the SSE
   *  message is published before its database row is committed, so there is no
   *  server-side id to use at that moment. */
  _tag(event) {
    event._key = ++this._key;
    return event;
  },

  eventByKey(key) {
    return this.events.find((e) => String(e._key) === String(key)) || null;
  },

  async loadEvents() {
    try {
      const events = await api.get("/v1/budget/events");
      this.events = events.slice(0, MAX_EVENTS).map((e) => this._tag(e));
      this.emit("events");
    } catch (err) {
      console.error("event backfill failed", err);
    }
  },

  pushEvent(event) {
    this.events.unshift(this._tag(event));
    if (this.events.length > MAX_EVENTS) this.events.length = MAX_EVENTS;
    this.emit("event");
  },

  clearEvents() {
    this.events = [];
    this.emit("events");
  },

  connect() {
    if (this._source) return;
    const source = new EventSource("/events/stream");
    this._source = source;

    source.addEventListener("connected", () => {
      this.live = true;
      this.emit("live");
    });
    source.onerror = () => {
      this.live = false;
      this.emit("live");
    };

    const handle = (message) => {
      let data;
      try {
        data = JSON.parse(message.data);
      } catch {
        return;
      }
      // Cache-invalidation is machinery, not news.
      if (data.type === "config.invalidate") return;
      this.pushEvent(data);
      this.refreshStatus();
    };
    for (const type of EVENT_TYPES) source.addEventListener(type, handle);

    this._timer = setInterval(() => this.refreshStatus(), POLL_MS);
  },

  async start() {
    await this.loadReference();
    await this.loadEvents();
    await this.refreshStatus();
    this.connect();
  },
};
