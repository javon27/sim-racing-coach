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
export interface Speaker {
  speak(text: string): Promise<void>;
  /** Queue an utterance without awaiting it (conversation mode streams sentences
   *  in as the answer arrives); resolves nothing itself -- see `idle()`. */
  enqueue(text: string): void;
  /** Resolves once every enqueue()d utterance has finished playing (immediately
   *  if nothing is pending). */
  idle(): Promise<void>;
  cancel(): void;
  earcon(kind: Earcon): void;
}

// Pops the first complete sentence off a growing text buffer: a `.`/`!`/`?`/`\n`
// counts as the end only when followed by whitespace or the end of what has
// streamed so far -- so "1.52.3" (a lap time) isn't split mid-number. Returns
// null while the buffer holds no complete sentence yet.
function popSentence(buf: string): { sentence: string; rest: string } | null {
  for (let i = 0; i < buf.length; i++) {
    const c = buf[i];
    if (c === "\n") return { sentence: buf.slice(0, i + 1), rest: buf.slice(i + 1) };
    if (c === "." || c === "!" || c === "?") {
      const next = buf[i + 1];
      if (next === undefined || /\s/.test(next)) {
        return { sentence: buf.slice(0, i + 1), rest: buf.slice(i + 1) };
      }
    }
  }
  return null;
}
export interface VoiceLoopOptions {
  recorder: Recorder; transcriber: Transcriber; speaker: Speaker;
  ask(text: string, mode: VoiceMode): void;
  vad: VadConfig; conversationIdleS: number; frameMs?: number;
  /** Seconds idle before the recorder's dispose() is called (default MIC_RELEASE_S). */
  micReleaseS?: number;
  setTimeout?: (fn: () => void, ms: number) => unknown; clearTimeout?: (h: unknown) => void;
}

const MIC_ERROR = "I can't hear the mic";
// Barge-in gate (conversation mode): accumulated loudness needed to interrupt playback.
// 120 ms so ONE short word ("stop", ~150 ms of voiced sound) is enough -- at 300 ms with
// a double-speed drain, repeated "stop"s could never trigger it (rig 2026-09-20, #129).
// ponytail: fixed constants; promote to a Settings sensitivity knob if rigs differ.
export const BARGE_IN_MS = 120;
export const BARGE_IN_LEVEL_MULT = 2;
// The level gate adapts to the measured playback bleed: gate = max(listening threshold,
// BARGE_IN_BLEED_MULT x bleed floor). Seeded so it STARTS at the old fixed 2x threshold
// (safe for a desk mic next to speakers) and settles within a couple of seconds:
// rig 2026-09-20, headset: bleed 0.000-0.003, a normal "stop" peaked at 0.037 -- just
// under the fixed 0.040 gate, so it took several tries.
export const BARGE_IN_BLEED_MULT = 3;
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
  // Conversation mode only: text streamed via onText() not yet popped into a
  // complete sentence and enqueue()d. Dropped on any cancelSpeech (barge-in/stop).
  #textBuf = "";
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
  /** Swap the active speaker in place (e.g. a failed Kokoro fetch falling back to
   *  Windows TTS mid-session) without tearing down the rest of the loop. Cancels
   *  the old speaker so it can't keep playing/queuing as an orphan, unreachable
   *  from stop/barge-in/▶ from now on. */
  setSpeaker(s: Speaker): void {
    const old = this.#o.speaker;
    this.#o = { ...this.#o, speaker: s };
    if (old !== s) old.cancel();
  }
  /** The active speaker (e.g. so a host can keep a KokoroSpeaker's trackSlug live
   *  as the driver's track changes, without holding its own separate reference). */
  get speaker(): Speaker { return this.#o.speaker; }
  #set(state: VoiceState) {
    // A partial barge-in accumulation must not carry into the next answer.
    if (this.state === "speaking" && state !== "speaking") this.#bargeMs = 0;
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
  // PTT only: the [spoken] cue streamed ahead of the rest of the answer (coach.ts
  // `spoken` event) -- start speaking it right away instead of waiting for `done`.
  // This moves state off "asking", so onAnswer's own `state !== "asking"` guard
  // below is what stops it from re-speaking once `done` arrives.
  onSpoken(text: string): void {
    if (this.state !== "asking" || this.mode !== "ptt") return;
    void this.#speakThen(stripForSpeech(text), () => { this.mode = null; this.#set("idle"); });
  }
  onAnswer(answer: string, spoken?: string | null): void {
    if (this.mode === "conversation") {
      // Sentences already streamed in via onText() as enqueue()d utterances; this
      // just flushes whatever's left in the buffer and waits for the queue (not
      // one utterance) to finish before listening again.
      if (this.state !== "asking" && this.state !== "speaking") return;
      const remainder = stripForSpeech(this.#textBuf);
      this.#textBuf = "";
      if (remainder) {
        if (this.state !== "speaking") { this.#set("speaking"); this.#startRecorder().catch(() => {}); }
        this.#o.speaker.enqueue(remainder);
      }
      const seq = this.#speakSeq;
      void this.#o.speaker.idle().then(() => {
        if (seq === this.#speakSeq && this.state === "speaking") this.#listen("conversation");
      });
      return;
    }
    if (this.state !== "asking") return;
    void this.#speakThen(stripForSpeech(spoken ?? firstParagraph(answer)), () => {
      this.mode = null; this.#set("idle");
    });
  }

  // Conversation mode only: pop and enqueue every complete sentence the buffer
  // now holds as text streams in; the first enqueue moves state -> speaking
  // (mic stays open for barge-in, same as #speakThen).
  onText(delta: string): void {
    if (this.mode !== "conversation" || (this.state !== "asking" && this.state !== "speaking")) return;
    this.#textBuf += delta;
    let popped: { sentence: string; rest: string } | null;
    while ((popped = popSentence(this.#textBuf))) {
      this.#textBuf = popped.rest;
      const spoken = stripForSpeech(popped.sentence);
      if (this.state !== "speaking") { this.#set("speaking"); this.#startRecorder().catch(() => {}); }
      if (spoken) this.#o.speaker.enqueue(spoken);
    }
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

  // Dev-only rig readout (#129): `localStorage.voiceDebug = "1"` in devtools, then the
  // console shows the mic's peak level against the barge-in gate twice a second while
  // the coach is speaking -- the numbers a threshold complaint needs.
  // Playback level leaking into the mic while the coach speaks; < 0 = not seeded yet.
  // Kept across answers (the room doesn't change between them).
  #bleed = -1;
  #bargeGate(): number {
    const base = this.#o.vad.threshold;
    if (this.#bleed < 0) this.#bleed = (base * BARGE_IN_LEVEL_MULT) / BARGE_IN_BLEED_MULT;
    return Math.max(base, BARGE_IN_BLEED_MULT * this.#bleed);
  }
  #dbgPeak = 0; #dbgMs = 0;
  #debugBarge(level: number, frameMs: number): void {
    this.#dbgPeak = Math.max(this.#dbgPeak, level); this.#dbgMs += frameMs;
    if (this.#dbgMs < 500) return;
    try {
      if (localStorage.getItem("voiceDebug")) {
        console.debug(`[barge-in] peak ${this.#dbgPeak.toFixed(3)} gate ${this.#bargeGate().toFixed(3)} bleed ${this.#bleed.toFixed(4)} acc ${this.#bargeMs.toFixed(0)}/${BARGE_IN_MS} ms`);
      }
    } catch { /* storage blocked: no readout */ }
    this.#dbgPeak = 0; this.#dbgMs = 0;
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
      if (level >= this.#bargeGate()) {
        this.#bargeMs += frameMs;
        // Keep the burst that triggered the barge-in (plus a little lead-in) so
        // the interrupting words are part of the utterance.
        if (this.#bargeMs >= BARGE_IN_MS) { this.#bargeMs = 0; this.#cancelSpeech(); this.#listen("conversation", BARGE_IN_MS + 200); }
      } else {
        // Leaky, not reset: a dip between syllables costs its own length, so a word
        // or phrase still accumulates to the target, while steady low-level playback
        // bleed (always below 2x threshold) never creeps up.
        this.#bargeMs = Math.max(0, this.#bargeMs - frameMs);
        // Only sub-gate frames teach the floor (~1 s time constant), so the driver's
        // own interrupting words can never raise the gate against themselves.
        this.#bleed += (level - this.#bleed) * Math.min(1, frameMs / 1000);
      }
      this.#debugBarge(level, frameMs);
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
    // Only an EMPTY transcript is "didn't catch that": whisper's own VAD already
    // returns nothing on silence, and one-word replies ("yes", "no") are real
    // answers to the coach's questions (rig: a 2-word minimum ate them).
    if (!/[\p{L}\p{N}]/u.test(text)) { this.#fail(DIDNT_CATCH, false); return; }
    this.lastQuestion = text;
    this.#textBuf = "";
    this.#set("asking");
    this.#o.ask(text, this.mode ?? "ptt");
  }

  // Enqueues `text` sentence by sentence (#121: the whole cue/answer as ONE
  // item made the server synthesise every sentence before any audio played).
  // The existing one-ahead pipeline (KokoroSpeaker#pump) then starts playing
  // after just the first sentence while later ones keep synthesising.
  async #speakThen(text: string, after: () => void): Promise<void> {
    const seq = ++this.#speakSeq;
    this.#set("speaking");
    if (this.mode === "conversation") this.#startRecorder().catch(() => {});  // keep the mic open for barge-in
    let rest = text;
    let popped: { sentence: string; rest: string } | null;
    while ((popped = popSentence(rest))) {
      rest = popped.rest;
      const sentence = popped.sentence.trim();
      if (sentence) this.#o.speaker.enqueue(sentence);
    }
    if (rest.trim()) this.#o.speaker.enqueue(rest.trim());
    await this.#o.speaker.idle();
    if (seq === this.#speakSeq) after();
  }
  #cancelSpeech(): void { this.#speakSeq++; this.#o.speaker.cancel(); this.#textBuf = ""; }

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
