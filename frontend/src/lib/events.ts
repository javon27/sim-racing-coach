// WS client for the /events multiplexed feed (simcoach/server/app.py @app.websocket("/events")).
//
// #106: the UI used to run six independent HTTP pollers (status, laps, live map,
// map capture, voice setup), which starved the browser's 6-connection pool the
// moment one of them blocked on a slow LMU probe. The sidecar now pushes all of
// that over one socket as {topic, data} frames; each topic gets its own store and
// components subscribe instead of fetching on a timer.
//
// UI-agnostic core rule: this file only streams + fans out frames, no analysis.

import type { LapRow, StatusResponse, TrackMap, VoiceStatus } from "./api";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765";
const WS_BASE = API_BASE.replace(/^http/, "ws");
const RECONNECT_MS = 1500;

/** Minimal Svelte-store-compatible readable: subscribe replays the current value. */
export class Topic<T> {
  #listeners = new Set<(v: T) => void>();

  constructor(public value: T) {}

  /** Live listener count — lets a component test prove it unsubscribed. */
  get subscriberCount(): number {
    return this.#listeners.size;
  }

  subscribe(run: (v: T) => void): () => void {
    this.#listeners.add(run);
    run(this.value);
    return () => {
      this.#listeners.delete(run);
    };
  }

  set(v: T): void {
    this.value = v;
    for (const l of this.#listeners) l(v);
  }
}

/** `laps` frame: the lap rows of the live session (`session_id: null` = no session).
    `optimal`: #176's headline optimal-lap figure, same as GET /sessions/{sid}/laps. */
export interface LapsFrame {
  session_id: string | null;
  laps: LapRow[];
  optimal: number | null;
}

/** `mapcapture` frame: same shape as GET /mapcapture/status. */
export interface MapCaptureFrame {
  active: boolean;
  live_track: string | null;
  laps: { idx: number; samples: number; length: number }[];
}

/** `map` frame: the GET /track/map payload, or `{}` when no track is known. */
export type MapFrame = TrackMap | Record<string, never>;

export class EventFeed {
  readonly status = new Topic<StatusResponse | null>(null);
  readonly laps = new Topic<LapsFrame | null>(null);
  readonly map = new Topic<MapFrame | null>(null);
  readonly mapcapture = new Topic<MapCaptureFrame | null>(null);
  readonly voiceSetup = new Topic<VoiceStatus | null>(null);
  /** True between the first frame of a connection and its close — the liveness
      signal a header can show "connecting" on. An open socket that has not sent
      anything yet does not count. */
  readonly connected = new Topic<boolean>(false);

  #ws: WebSocket | null = null;
  #stopped = false;
  #timer: ReturnType<typeof setTimeout> | null = null;

  start(): void {
    if (typeof WebSocket === "undefined") return; // SSR / non-browser
    if (this.#ws) return; // already started: don't leak a second socket
    this.#stopped = false;
    this.#open();
  }

  stop(): void {
    this.#stopped = true;
    if (this.#timer) {
      clearTimeout(this.#timer);
      this.#timer = null;
    }
    this.#close();
    this.connected.set(false);
  }

  /** Route one frame. The socket calls this; tests push frames the same way. */
  push(topic: string, data: unknown): void {
    switch (topic) {
      case "status":
        this.status.set(data as StatusResponse);
        break;
      case "laps":
        this.laps.set(data as LapsFrame);
        break;
      case "map":
        this.map.set(data as MapFrame);
        break;
      case "mapcapture":
        this.mapcapture.set(data as MapCaptureFrame);
        break;
      case "voice_setup":
        this.voiceSetup.set(data as VoiceStatus);
        break;
      default:
        return; // unknown topic: a newer sidecar, nothing to do here
    }
    if (!this.connected.value) this.connected.set(true);
  }

  /** Clear every topic. The feed is an app-lifetime singleton, so component
      tests reset it between cases (see src/test-setup.ts). */
  reset(): void {
    this.status.set(null);
    this.laps.set(null);
    this.map.set(null);
    this.mapcapture.set(null);
    this.voiceSetup.set(null);
    this.connected.set(false);
  }

  #open(): void {
    const ws = new WebSocket(`${WS_BASE}/events`);
    this.#ws = ws;
    ws.onmessage = (e: MessageEvent) => {
      let msg: { topic?: string; data?: unknown };
      try {
        msg = JSON.parse(e.data as string);
      } catch {
        return;
      }
      if (typeof msg?.topic === "string") this.push(msg.topic, msg.data);
    };
    ws.onerror = () => ws.close();
    ws.onclose = () => {
      this.connected.set(false);
      if (!this.#stopped && !this.#timer) {
        this.#timer = setTimeout(() => {
          this.#timer = null;
          this.#open();
        }, RECONNECT_MS);
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
}

// Shared, app-lifetime feed: started once at boot (App.svelte) and kept streaming
// regardless of the active view. Components subscribe; they must NOT start/stop it.
export const eventFeed = new EventFeed();
