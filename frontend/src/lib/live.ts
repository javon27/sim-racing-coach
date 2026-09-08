// WS client for the /live pit-wall feed (simcoach/server/app.py @app.websocket("/live")).
// UI-agnostic core rule: this file only streams + shapes the snapshot, no analysis.

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765";
const WS_BASE = API_BASE.replace(/^http/, "ws");
const RECONNECT_MS = 1500;

// Class colors matching the game's representation (mirrors the v1 prototype).
// Keyed on the LIVE scoring class strings (see store.py _CLASS_ORDER): the Hypercar
// class reports as "Hyper", and ELMS P2 as "LMP2_ELMS" — both were missing before,
// so those cars fell through to the grey fallback.
export const CLASS_COLOR: Record<string, string> = {
  Hyper: "#EF4444", Hypercar: "#EF4444",
  LMP2: "#3B82F6", LMP2_ELMS: "#3B82F6",
  LMP3: "#A855F7", GT3: "#22C55E", GTE: "#F59E0B",
};

export interface VehicleState {
  id: number; place: number; class_position: number;
  driver: string; car: string; cls: string;
  lap_dist: number; speed: number; sector: number; laps: number;
  in_pits: boolean; is_player: boolean; best_lap: number | null; last_lap: number | null;
  best_s1: number | null; best_s2: number | null;
  gap_leader: number | null; laps_behind_leader: number;
  gap_next: number | null; laps_behind_next: number;
}

export interface SessionState {
  live: boolean; phase: string;
  current_lap: number | null; max_laps: number | null;
  track_len: number;
  elapsed: number; end_time: number;
  cur_s1: number | null; cur_s2: number | null;
  best_s1: number | null; best_s2: number | null;
  last_s1: number | null; last_s2: number | null;
  last_lap: number | null; best_lap: number | null;
}

export interface FieldSnapshot { session: SessionState; cars: VehicleState[]; }

export class LiveField {
  snapshot: FieldSnapshot | null = null;
  connected = false;

  #ws: WebSocket | null = null;
  #stopped = false;
  #timer: ReturnType<typeof setTimeout> | null = null;
  #listeners = new Set<(value: this) => void>();

  subscribe(run: (value: this) => void): () => void {
    this.#listeners.add(run);
    run(this);
    return () => this.#listeners.delete(run);
  }

  start(): void {
    if (typeof WebSocket === "undefined") return; // SSR / non-browser
    this.#stopped = false;
    this.#open();
  }

  stop(): void {
    this.#stopped = true;
    if (this.#timer) { clearTimeout(this.#timer); this.#timer = null; }
    this.#close();
    this.connected = false;
    this.#notify();
  }

  #open(): void {
    const ws = new WebSocket(`${WS_BASE}/live`);
    this.#ws = ws;
    ws.onopen = () => { this.connected = true; this.#notify(); };
    ws.onmessage = (e: MessageEvent) => {
      try { this.snapshot = JSON.parse(e.data as string) as FieldSnapshot; }
      catch { return; }
      this.#notify();
    };
    ws.onerror = () => ws.close();
    ws.onclose = () => {
      this.connected = false;
      this.#notify();
      if (!this.#stopped && !this.#timer) {
        this.#timer = setTimeout(() => { this.#timer = null; this.#open(); }, RECONNECT_MS);
      }
    };
  }

  #close(): void {
    const ws = this.#ws;
    this.#ws = null;
    if (!ws) return;
    ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
    ws.close();
  }

  #notify(): void { for (const l of this.#listeners) l(this); }
}

// Shared, app-lifetime live feed: started once at boot (App.svelte) and kept
// streaming regardless of the active view, so the Pit Wall reflects live data
// even while you're on another tab (e.g. editing a track map). Components
// subscribe to it; they must NOT start/stop it.
export const liveField = new LiveField();
