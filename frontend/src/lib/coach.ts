// WS client for the SimCoach coach endpoint (simcoach/server/app.py, @app.websocket("/coach")).
// UI-agnostic core rule: this file only streams + shapes coach events, no analysis logic.

import type { ChatUsage } from "./api";
import type { VoiceMode } from "./voice";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765";
const WS_BASE = API_BASE.replace(/^http/, "ws"); // http -> ws, https -> wss

export type CoachPhase = "idle" | "connecting" | "tool" | "streaming" | "done" | "error";

export interface ToolActivity {
  name: string;
  input: unknown;
}

export interface AskOptions {
  sessionId?: string;
  lapId?: number;
  chatId?: string; // continue an existing thread; omit to start a new one (server mints the id)
  voice?: VoiceMode;
}

// Svelte store contract (subscribe returns an unsubscribe fn, called immediately
// with the current value) so a component can bind with `$coachStream` — plain
// fields underneath, no store dependency needed.
export class CoachStream {
  phase: CoachPhase = "idle";
  toolActivity: ToolActivity | null = null;
  answer = "";
  chatId: string | null = null;
  error: string | null = null;
  // The CHAT cumulative usage + estimated $ the server reports on `done`. Null
  // until a done frame carries them (an older sidecar sends none).
  usage: ChatUsage | null = null;
  cost: number | null = null;
  costComplete = true;
  // The server's soft "this chat is getting long" signal (it owns the threshold).
  longChat = false;

  #ws: WebSocket | null = null;
  #listeners = new Set<(value: this) => void>();
  #settled = false; // true once done/error was received for the in-flight ask

  subscribe(run: (value: this) => void): () => void {
    this.#listeners.add(run);
    run(this);
    return () => this.#listeners.delete(run);
  }

  ask(question: string, opts: AskOptions = {}): void {
    this.#close(); // one in-flight ask at a time — replace, don't queue
    this.phase = "connecting";
    this.toolActivity = null;
    this.answer = "";
    this.chatId = null;
    this.error = null;
    this.usage = null;
    this.cost = null;
    this.costComplete = true;
    this.longChat = false;
    this.#settled = false;
    this.#notify();

    const ws = new WebSocket(`${WS_BASE}/coach`);
    this.#ws = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({
        question, session_id: opts.sessionId, lap_id: opts.lapId, chat_id: opts.chatId, voice: opts.voice,
      }));
    };

    ws.onmessage = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data as string);
        switch (data.type) {
          case "tool":
            this.phase = "tool";
            this.toolActivity = { name: data.name, input: data.input };
            break;
          case "text":
            this.phase = "streaming";
            this.answer += data.text;
            break;
          case "done":
            this.phase = "done";
            this.answer = data.answer ?? this.answer;
            this.chatId = data.chat_id ?? null;
            this.usage = data.usage ?? null;
            this.cost = data.cost ?? null;
            this.costComplete = data.cost_complete ?? true;
            this.longChat = data.long_chat === true;
            this.#settled = true;
            this.#close();
            break;
          case "error":
            this.phase = "error";
            this.error = data.message;
            this.#settled = true;
            this.#close();
            break;
        }
      } catch {
        this.#fail("received a malformed message from the coach server");
        return;
      }
      this.#notify();
    };

    ws.onerror = () => this.#lostConnection();
    ws.onclose = () => this.#lostConnection();
  }

  // Socket errored or closed before a done/error message arrived — surface it
  // instead of leaving phase stuck on connecting/tool/streaming forever.
  #lostConnection(): void {
    this.#fail("lost connection to the coach server");
  }

  #fail(message: string): void {
    if (this.#settled) return;
    this.#settled = true;
    this.phase = "error";
    this.error = message;
    this.#close();
    this.#notify();
  }

  #close(): void {
    const ws = this.#ws;
    this.#ws = null;
    if (!ws) return;
    ws.onopen = null;
    ws.onmessage = null;
    ws.onerror = null;
    ws.onclose = null;
    ws.close();
  }

  #notify(): void {
    for (const listener of this.#listeners) listener(this);
  }
}
