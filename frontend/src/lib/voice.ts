// Voice loop core (spec: docs/superpowers/specs/2026-09-15-voice-loop-design.md).
// No browser globals in this file: the recorder/transcriber/speaker are injected so
// every transition is unit-testable. Browser adapters live in audio.ts.

export interface VadConfig { threshold: number; silenceMs: number; maxS: number }
export interface VadState { speaking: boolean; silentMs: number; elapsedMs: number }
export const vadInitial: VadState = { speaking: false, silentMs: 0, elapsedMs: 0 };

export function vadStep(state: VadState, level: number, frameMs: number, cfg: VadConfig): { state: VadState; action: "continue" | "end" } {
  const elapsedMs = state.elapsedMs + frameMs;
  const loud = level >= cfg.threshold;
  const speaking = state.speaking || loud;
  const silentMs = speaking && !loud ? state.silentMs + frameMs : 0;
  const next = { speaking, silentMs, elapsedMs };
  const end = (speaking && silentMs >= cfg.silenceMs) || elapsedMs >= cfg.maxS * 1000;
  return { state: next, action: end ? "end" : "continue" };
}

export function rms(frame: Float32Array): number {
  if (frame.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
  return Math.sqrt(sum / frame.length);
}

export function encodeWav(pcm: Float32Array, sampleRate: number): Uint8Array {
  const out = new Uint8Array(44 + pcm.length * 2);
  const dv = new DataView(out.buffer);
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) out[o + i] = s.charCodeAt(i); };
  str(0, "RIFF"); dv.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, sampleRate, true); dv.setUint32(28, sampleRate * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  str(36, "data"); dv.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) {
    const v = Math.max(-1, Math.min(1, pcm[i]));
    dv.setInt16(44 + i * 2, v < 0 ? v * 32768 : v * 32767, true);
  }
  return out;
}

export function stripForSpeech(md: string): string {
  return md
    .replace(/^#{1,6}\s+(.+)$/gm, "$1.")
    .replace(/^\s*[-*]\s+(.+)$/gm, "$1.")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/`(.+?)`/g, "$1")
    .replace(/\.\.+/g, ".")
    .replace(/\s+/g, " ")
    .trim();
}

export type VoiceState = "idle" | "listening" | "transcribing" | "asking" | "speaking";
export type VoiceMode = "ptt" | "conversation";
export type Earcon = "listen-start" | "listen-end" | "error";

export interface Recorder {
  /** `onFrame` gets each PCM chunk plus its sample rate, so the loop can clock
   *  VAD/barge-in from the chunk's real duration. */
  start(onFrame: (frame: Float32Array, sampleRate?: number) => void): Promise<void>;
  stop(): Promise<{ pcm: Float32Array; sampleRate: number }>;
  /** Release the mic/stream entirely; the next start() re-acquires it. Optional. */
  dispose?(): Promise<void>;
  /** Drop audio buffered so far, keeping the last `keepMs`. Optional. */
  clear?(keepMs?: number): void;
}
export interface Transcriber { transcribe(wav: Uint8Array): Promise<{ text: string; seconds: number }> }
export interface Speaker { speak(text: string): Promise<void>; cancel(): void; earcon(kind: Earcon): void }
export interface VoiceLoopOptions {
  recorder: Recorder; transcriber: Transcriber; speaker: Speaker;
  ask(text: string, mode: VoiceMode): void;
  vad: VadConfig; conversationIdleS: number; frameMs?: number;
  /** Seconds idle before the recorder's dispose() is called (default MIC_RELEASE_S). */
  micReleaseS?: number;
  setTimeout?: (fn: () => void, ms: number) => unknown; clearTimeout?: (h: unknown) => void;
}

const MIC_ERROR = "I can't hear the mic";
// Barge-in gate (conversation mode): sustained loudness needed to interrupt playback.
export const BARGE_IN_MS = 400;
export const BARGE_IN_LEVEL_MULT = 2;
// Idle seconds before the held-open mic stream is released (OS indicator goes off).
export const MIC_RELEASE_S = 60;
const DIDNT_CATCH = "Didn't catch that";

export class VoiceLoop {
  state: VoiceState = "idle";
  mode: VoiceMode | null = null;
  error: string | null = null;
  speakingMessage: number | null = null;
  // The text of the most recently completed utterance, set right before the
  // asking transition. Lets a host (ChatPanel) learn what was asked purely from
  // `subscribe()`, independent of whatever `ask` callback it was constructed
  // with -- so an injected loop (tests) and a self-built one (the app) both
  // drive the same UI hookup instead of duplicating it in two places.
  lastQuestion: string | null = null;

  #o: VoiceLoopOptions;
  #listeners = new Set<(v: VoiceLoop) => void>();
  #vad: VadState = vadInitial;
  #recording = false;
  #starting: Promise<void> | null = null;
  #bargeMs = 0;   // consecutive ms above the barge-in level while speaking
  #releaseTimer: unknown = null;
  #idleTimer: unknown = null;
  #speakSeq = 0;
  #setTimeout: (fn: () => void, ms: number) => unknown;
  #clearTimeout: (h: unknown) => void;

  constructor(o: VoiceLoopOptions) {
    this.#o = o;
    this.#setTimeout = o.setTimeout ?? ((fn, ms) => setTimeout(fn, ms));
    this.#clearTimeout = o.clearTimeout ?? ((h) => clearTimeout(h as number));
  }

  subscribe(run: (v: VoiceLoop) => void): () => void {
    this.#listeners.add(run); run(this);
    return () => this.#listeners.delete(run);
  }
  #set(state: VoiceState) {
    this.state = state;
    // The recorder holds the mic open between presses so a press starts instantly
    // (and the OS mic indicator stays lit). Give it back after a quiet spell; the
    // next press re-acquires it (one slow start, as before the keep-open fix).
    if (this.#releaseTimer !== null) { this.#clearTimeout(this.#releaseTimer); this.#releaseTimer = null; }
    if (state === "idle" && this.#o.recorder.dispose) {
      this.#releaseTimer = this.#setTimeout(() => {
        this.#releaseTimer = null;
        if (this.state === "idle" && !this.#recording && !this.#starting) void this.#o.recorder.dispose?.();
      }, (this.#o.micReleaseS ?? MIC_RELEASE_S) * 1000);
    }
    this.#notify();
  }
  #notify() { for (const l of this.#listeners) l(this); }

  // ---- PTT ----
  press(): void {
    if (this.state === "speaking") { this.#cancelSpeech(); this.speakingMessage = null; this.#listen("ptt"); return; }
    if (this.state !== "idle") return;
    this.#listen("ptt");
  }
  release(): void {
    if (this.state !== "listening" || this.mode !== "ptt") return;
    void this.#finishUtterance();
  }

  // ---- conversation ----
  toggleConversation(): void {
    if (this.state === "idle") { this.#listen("conversation"); return; }
    // A quick tap while a PTT press is already listening (mic already open):
    // convert this SAME listen into conversation mode in place -- no stop/start
    // of the recorder, no earcon, so nothing at the start of the utterance is
    // lost. Any other in-flight state (transcribing/asking/speaking) still
    // just stops.
    if (this.state === "listening" && this.mode === "ptt") {
      this.mode = "conversation";
      this.#vad = vadInitial;
      this.#armIdle();
      this.#notify();
      return;
    }
    this.stop();
  }

  stop(): void {
    this.#clearIdle();
    this.#cancelSpeech();
    void this.#stopRecorder();
    this.mode = null;
    this.speakingMessage = null;
    this.#set("idle");
  }

  // ---- coach events ----
  onAnswer(answer: string, spoken?: string | null): void {
    if (this.state !== "asking") return;
    const text = this.mode === "ptt" ? (spoken ?? firstParagraph(answer)) : answer;
    void this.#speakThen(stripForSpeech(text), () => {
      if (this.mode === "conversation") this.#listen("conversation"); else { this.mode = null; this.#set("idle"); }
    });
  }
  onError(message: string): void {
    if (this.state !== "asking") return;
    this.#fail(message);
  }

  // ---- ▶ on a message ----
  speakMessage(index: number, text: string): void {
    if (this.speakingMessage === index) { this.#cancelSpeech(); this.speakingMessage = null; this.#set("idle"); return; }
    if (this.state !== "idle" && this.state !== "speaking") return;
    if (this.state === "speaking") this.#cancelSpeech();
    this.speakingMessage = index;
    void this.#speakThen(stripForSpeech(text), () => { this.speakingMessage = null; this.#set("idle"); });
  }

  // ---- internals ----
  #listen(mode: VoiceMode, keepMs = 0): void {
    this.mode = mode; this.error = null; this.#vad = vadInitial;
    // The recorder may already be running (it stays open through the coach's
    // answer for barge-in). Everything it heard until now is not the driver's
    // question -- rig: a quick "yes" after a long answer was sent with the whole
    // answer's audio in front of it and rejected as "longer than 30 s".
    if (this.#recording) this.#o.recorder.clear?.(keepMs);
    this.#set("listening");
    this.#o.speaker.earcon("listen-start");
    this.#armIdle();
    this.#startRecorder().catch(() => this.#fail(MIC_ERROR));
  }

  #startRecorder(): Promise<void> {
    if (this.#recording) return Promise.resolve();
    if (!this.#starting) {
      this.#starting = this.#o.recorder.start((frame, rate) => this.#onFrame(frame, rate))
        .then(() => { this.#recording = true; })
        .finally(() => { this.#starting = null; });
    }
    return this.#starting;
  }
  async #stopRecorder(): Promise<{ pcm: Float32Array; sampleRate: number } | null> {
    if (this.#starting) await this.#starting.catch(() => {});
    if (!this.#recording) return null;
    this.#recording = false;
    return this.#o.recorder.stop();
  }

  #onFrame(frame: Float32Array, sampleRate?: number): void {
    if (this.mode !== "conversation") return;
    const level = rms(frame);
    // Clock from the chunk's real duration when the recorder says its rate;
    // `frameMs` is only the fallback for recorders that don't.
    const frameMs = sampleRate ? (frame.length / sampleRate) * 1000 : (this.#o.frameMs ?? 50);
    if (this.state === "speaking") {            // barge-in
      // The coach's own playback leaks into a desk mic, so a single frame over the
      // listening threshold is not speech: require a SUSTAINED burst at twice the
      // threshold before cutting the answer off. ponytail: fixed multiplier and
      // window; an echo-cancelling reference of the TTS output would be the upgrade.
      if (level >= this.#o.vad.threshold * BARGE_IN_LEVEL_MULT) {
        this.#bargeMs += frameMs;
        // Keep the burst that triggered the barge-in (plus a little lead-in) so
        // the interrupting words are part of the utterance.
        if (this.#bargeMs >= BARGE_IN_MS) { this.#bargeMs = 0; this.#cancelSpeech(); this.#listen("conversation", BARGE_IN_MS + 200); }
      } else {
        this.#bargeMs = 0;
      }
      return;
    }
    if (this.state !== "listening") return;
    const r = vadStep(this.#vad, level, frameMs, this.#o.vad);
    this.#vad = r.state;
    if (r.state.speaking) this.#clearIdle();
    if (r.action === "end") void this.#finishUtterance();
  }

  async #finishUtterance(): Promise<void> {
    this.#clearIdle();
    this.#set("transcribing");
    this.#o.speaker.earcon("listen-end");
    const rec = await this.#stopRecorder();
    if (!rec) { this.#fail(MIC_ERROR); return; }
    let text: string;
    try {
      text = (await this.#o.transcriber.transcribe(encodeWav(rec.pcm, rec.sampleRate))).text.trim();
    } catch (e) {
      this.#fail(e instanceof Error ? e.message : String(e)); return;
    }
    if (text.split(/\s+/).filter(Boolean).length < 2) { this.#fail(DIDNT_CATCH, false); return; }
    this.lastQuestion = text;
    this.#set("asking");
    this.#o.ask(text, this.mode ?? "ptt");
  }

  async #speakThen(text: string, after: () => void): Promise<void> {
    const seq = ++this.#speakSeq;
    this.#set("speaking");
    if (this.mode === "conversation") this.#startRecorder().catch(() => {});  // keep the mic open for barge-in
    await this.#o.speaker.speak(text);
    if (seq === this.#speakSeq) after();
  }
  #cancelSpeech(): void { this.#speakSeq++; this.#o.speaker.cancel(); }

  #fail(message: string, keepError = true): void {
    this.#clearIdle();
    void this.#stopRecorder();
    this.#cancelSpeech();
    this.error = keepError ? message : null;
    this.mode = null; this.speakingMessage = null;
    // Not for the mic-permission failure: the driver already knows the mic
    // didn't open, and a harsh beep on top of that first prompt is worse than
    // just hearing the spoken message.
    if (message !== MIC_ERROR) this.#o.speaker.earcon("error");
    void this.#o.speaker.speak(message);
    this.#set("idle");
  }

  #armIdle(): void {
    this.#clearIdle();
    if (this.mode !== "conversation") return;
    this.#idleTimer = this.#setTimeout(() => this.stop(), this.#o.conversationIdleS * 1000);
  }
  #clearIdle(): void { if (this.#idleTimer !== null) { this.#clearTimeout(this.#idleTimer); this.#idleTimer = null; } }
}

export function firstParagraph(text: string): string {
  for (const block of text.split(/\n\s*\n/)) if (block.trim()) return block.trim();
  return "";
}
