// Browser adapters for VoiceLoop (voice.ts): mic capture, HTTP transcription, and
// speech synthesis. No browser global is touched at import time -- jsdom (tests) has
// none of AudioContext/MediaRecorder/speechSynthesis, and voice is always injected
// as a VoiceLoop prop in tests -- only the real app constructs these.
import { speak as apiSpeak, transcribe as apiTranscribe, type VoiceSettings } from "./api";
import type { Earcon, Recorder, Speaker, Transcriber } from "./voice";

export class MicRecorder implements Recorder {
  #ctx: AudioContext | null = null;
  #stream: MediaStream | null = null;
  #source: MediaStreamAudioSourceNode | null = null;
  #node: ScriptProcessorNode | null = null;
  #chunks: Float32Array[] = [];
  #deviceId: string | null;

  constructor(deviceId: string | null = null) {
    this.#deviceId = deviceId;
  }

  // Acquired once, lazily, and kept for the life of the page -- getUserMedia +
  // AudioContext creation costs hundreds of ms, which used to run on every press.
  async #ensureStream(): Promise<void> {
    if (this.#stream && this.#ctx) return;
    const base = { channelCount: 1, echoCancellation: true, noiseSuppression: true };
    try {
      this.#stream = await navigator.mediaDevices.getUserMedia({
        audio: this.#deviceId ? { ...base, deviceId: { exact: this.#deviceId } } : base,
      });
    } catch (err) {
      // Configured device unplugged/gone -- fall back to the system default
      // rather than leaving voice dead.
      const name = (err as { name?: string })?.name;
      if (this.#deviceId && (name === "OverconstrainedError" || name === "NotFoundError")) {
        this.#stream = await navigator.mediaDevices.getUserMedia({ audio: base });
      } else {
        throw err;
      }
    }
    this.#ctx = new AudioContext({ sampleRate: 16000 });
    this.#source = this.#ctx.createMediaStreamSource(this.#stream);
  }

  async start(onFrame: (frame: Float32Array, sampleRate?: number) => void): Promise<void> {
    await this.#ensureStream();
    const ctx = this.#ctx!;
    // ponytail: ScriptProcessorNode is deprecated but inline and universal; AudioWorklet when it breaks.
    // 1024 samples = 64 ms at 16 kHz: fine enough for the VAD/barge-in clocks
    // (4096 was 256 ms per frame, which the loop was counting as 50 ms -- every
    // timing gate ran ~5x slow at the rig).
    this.#node = ctx.createScriptProcessor(1024, 1, 1);
    this.#chunks = [];
    this.#node.onaudioprocess = (e) => {
      const chunk = new Float32Array(e.inputBuffer.getChannelData(0));
      this.#chunks.push(chunk);
      onFrame(chunk, ctx.sampleRate);
    };
    this.#source!.connect(this.#node);
    this.#node.connect(ctx.destination);
  }

  /** Drop buffered audio, keeping only the last `keepMs` (default 0). Used when
   *  listening resumes after the coach spoke: the mic stayed open for barge-in
   *  and everything it heard meanwhile must not ride along with the next question. */
  clear(keepMs = 0): void {
    if (keepMs <= 0) { this.#chunks = []; return; }
    const rate = this.#ctx?.sampleRate ?? 16000;
    let keep = Math.round((keepMs / 1000) * rate);
    const kept: Float32Array[] = [];
    for (let i = this.#chunks.length - 1; i >= 0 && keep > 0; i--) {
      const c = this.#chunks[i];
      kept.unshift(c.length <= keep ? c : c.subarray(c.length - keep));
      keep -= c.length;
    }
    this.#chunks = kept;
  }

  async stop(): Promise<{ pcm: Float32Array; sampleRate: number }> {
    const sampleRate = this.#ctx?.sampleRate ?? 16000;
    const total = this.#chunks.reduce((n, c) => n + c.length, 0);
    const pcm = new Float32Array(total);
    let offset = 0;
    for (const c of this.#chunks) { pcm.set(c, offset); offset += c.length; }
    this.#node?.disconnect();
    this.#node = null; this.#chunks = [];
    return { pcm, sampleRate };
  }

  // Releases the mic + AudioContext. Not called anywhere yet (the stream is
  // meant to live for the page's lifetime); here for when a real teardown
  // point (e.g. voice disabled in Settings) needs it.
  async dispose(): Promise<void> {
    this.#node?.disconnect();
    this.#stream?.getTracks().forEach((t) => t.stop());
    await this.#ctx?.close();
    this.#ctx = null; this.#stream = null; this.#source = null; this.#node = null; this.#chunks = [];
  }
}

export class HttpTranscriber implements Transcriber {
  transcribe(wav: Uint8Array): Promise<{ text: string; seconds: number }> {
    return apiTranscribe(wav);
  }
}

const EARCON_HZ: Record<Earcon, number> = { "listen-start": 880, "listen-end": 660, error: 220 };

// Shared by both speakers -- SynthSpeaker keeps its own lazy AudioContext, KokoroSpeaker
// reuses the one it already opened for playback.
export function playEarcon(ctx: AudioContext, kind: Earcon): void {
  if (ctx.state === "suspended") void ctx.resume();
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.frequency.value = EARCON_HZ[kind];
  const t = ctx.currentTime;
  gain.gain.setValueAtTime(0, t);
  gain.gain.linearRampToValueAtTime(0.08, t + 0.01);
  gain.gain.linearRampToValueAtTime(0, t + 0.08);
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.start();
  osc.stop(t + 0.08);
}

export class SynthSpeaker implements Speaker {
  voiceName: string | null = null;
  rate = 1.0;
  #beepCtx: AudioContext | null = null;
  #pending = 0;
  #idleWaiters: (() => void)[] = [];

  // getVoices() is empty until the async `voiceschanged` event fires in
  // Chromium/WebView2 -- gating on its length here made loadVoice() at
  // startup see "unavailable" and silently disable voice until something
  // (e.g. opening Settings) happened to trigger a re-check. speechSynthesis
  // existing is enough; the actual voice list is read lazily by speak()/the
  // Settings screen once it's populated.
  static available(): boolean {
    return typeof speechSynthesis !== "undefined";
  }

  speak(text: string): Promise<void> {
    return new Promise((resolve) => {
      const u = new SpeechSynthesisUtterance(text);
      u.rate = this.rate;
      if (this.voiceName) {
        const v = speechSynthesis.getVoices().find((x) => x.name === this.voiceName);
        if (v) u.voice = v;
      }
      u.onend = () => resolve();
      u.onerror = () => resolve();
      speechSynthesis.speak(u);
    });
  }

  // Conversation mode: queue an utterance without awaiting it -- speechSynthesis.speak()
  // already queues natively, so back-to-back calls just play in order.
  enqueue(text: string): void {
    const u = new SpeechSynthesisUtterance(text);
    u.rate = this.rate;
    if (this.voiceName) {
      const v = speechSynthesis.getVoices().find((x) => x.name === this.voiceName);
      if (v) u.voice = v;
    }
    this.#pending++;
    const settle = () => {
      this.#pending = Math.max(0, this.#pending - 1);
      if (this.#pending === 0) {
        const waiters = this.#idleWaiters;
        this.#idleWaiters = [];
        waiters.forEach((w) => w());
      }
    };
    u.onend = settle;
    u.onerror = settle;
    speechSynthesis.speak(u);
  }

  idle(): Promise<void> {
    if (this.#pending === 0) return Promise.resolve();
    return new Promise((resolve) => this.#idleWaiters.push(resolve));
  }

  cancel(): void {
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
    this.#pending = 0;
    const waiters = this.#idleWaiters;
    this.#idleWaiters = [];
    waiters.forEach((w) => w());   // queue is empty now -- resolve, don't strand callers
  }

  earcon(kind: Earcon): void {
    if (!this.#beepCtx) this.#beepCtx = new AudioContext();
    playEarcon(this.#beepCtx, kind);
  }
}

// Sidecar-synthesised speech (Kokoro): fetches WAV bytes from the server and plays
// them via Web Audio, one synthesis in flight ahead of playback so the pipe never
// stalls waiting on the network between sentences. Falls back to SynthSpeaker (via
// onUnavailable) the first time a fetch fails -- e.g. the voice isn't installed.
export class KokoroSpeaker implements Speaker {
  voice = "af_heart";
  rate = 1.0;
  trackSlug: string | null = null;
  onUnavailable: ((reason: string) => void) | null = null;
  #fetchWav: typeof apiSpeak;
  #ctx: AudioContext | null;
  #queue: { text: string; resolve: () => void }[] = [];
  #ahead: { buffer: Promise<AudioBuffer | null>; resolve: () => void; abort: AbortController } | null = null;
  #playing: AudioBufferSourceNode | null = null;
  #idleWaiters: (() => void)[] = [];
  #failed = false;
  // Bumped by cancel() so an in-flight #play() -- even one already past its
  // synthesis, decoded and waiting inside #drainPlaying() for the previous
  // sentence to end -- notices and bails instead of playing a cancelled sentence.
  #gen = 0;

  constructor(deps: { fetchWav?: typeof apiSpeak; audio?: AudioContext } = {}) {
    this.#fetchWav = deps.fetchWav ?? apiSpeak;
    this.#ctx = deps.audio ?? null;
  }
  #audio(): AudioContext {
    this.#ctx ??= new AudioContext();
    if (this.#ctx.state === "suspended") void this.#ctx.resume();
    return this.#ctx;
  }
  speak(text: string): Promise<void> { return new Promise((resolve) => { this.#queue.push({ text, resolve }); this.#pump(); }); }
  enqueue(text: string): void { this.#queue.push({ text, resolve: () => {} }); this.#pump(); }
  idle(): Promise<void> {
    if (!this.#queue.length && !this.#ahead && !this.#playing) return Promise.resolve();
    return new Promise((r) => this.#idleWaiters.push(r));
  }
  cancel(): void {
    this.#gen++;
    this.#ahead?.abort.abort(); this.#ahead?.resolve(); this.#ahead = null;
    try { this.#playing?.stop(); } catch { /* already stopped */ }
    this.#playing = null;
    for (const q of this.#queue.splice(0)) q.resolve();
    this.#settleIdle();
  }
  earcon(kind: Earcon): void { playEarcon(this.#audio(), kind); }

  #settleIdle(): void {
    if (this.#queue.length || this.#ahead || this.#playing) return;
    const w = this.#idleWaiters; this.#idleWaiters = []; w.forEach((f) => f());
  }
  // One synthesis in flight ahead of playback: fetch the head while the previous plays.
  #pump(): void {
    if (this.#ahead || !this.#queue.length) return;
    const item = this.#queue.shift()!;
    const abort = new AbortController();
    const buffer = this.#fetchWav(item.text, { voice: this.voice, speed: this.rate, trackSlug: this.trackSlug ?? undefined }, abort.signal)
      .then((bytes) => this.#audio().decodeAudioData(bytes))
      .catch((e: unknown) => { this.#fail(e); return null; });
    this.#ahead = { buffer, resolve: item.resolve, abort };
    void this.#play();
  }
  async #play(): Promise<void> {
    const ahead = this.#ahead!;
    const gen = this.#gen;
    const buf = await ahead.buffer;
    if (gen !== this.#gen) { ahead.resolve(); this.#settleIdle(); return; }   // cancelled while synthesising
    await this.#drainPlaying();
    if (gen !== this.#gen) { ahead.resolve(); this.#settleIdle(); return; }   // cancelled while waiting to play
    this.#ahead = null;
    this.#pump();                                    // start the NEXT synthesis before this one plays
    if (!buf) { ahead.resolve(); this.#settleIdle(); return; }
    const src = this.#audio().createBufferSource();
    src.buffer = buf; src.connect(this.#audio().destination);
    this.#playing = src;
    await new Promise<void>((done) => { src.onended = () => done(); src.start(); });
    if (gen !== this.#gen) { ahead.resolve(); this.#settleIdle(); return; }   // cancelled while playing
    if (this.#playing === src) this.#playing = null;
    ahead.resolve();
    this.#settleIdle();
  }
  #drainPlaying(): Promise<void> {
    const cur = this.#playing;
    return cur ? new Promise((r) => { const prev = cur.onended; cur.onended = (e) => { prev?.call(cur, e); r(); }; }) : Promise.resolve();
  }
  #fail(e: unknown): void {
    if (this.#failed) return;
    this.#failed = true;
    this.onUnavailable?.(e instanceof Error ? e.message : String(e));
  }
}

// Picks the engine a caller (ChatPanel, Settings preview) should speak through:
// Kokoro only when both selected AND confirmed installed -- otherwise Windows TTS
// is the ceiling, same as before Kokoro existed. `onFallback` is wired as the
// Kokoro speaker's onUnavailable so a later runtime failure (network hiccup,
// uninstalled after all) can still be handled -- callers swap in a fresh
// SynthSpeaker themselves (VoiceLoop.setSpeaker) since this factory has no
// loop to reach into. trackSlug isn't part of VoiceSettings -- callers set it.
export function makeSpeaker(v: VoiceSettings, kokoroInstalled: boolean, onFallback: (reason: string) => void): Speaker {
  if (v.tts_engine === "kokoro" && kokoroInstalled) {
    const s = new KokoroSpeaker();
    s.voice = v.kokoro_voice;
    s.rate = v.tts_rate;
    s.onUnavailable = onFallback;
    return s;
  }
  const s = new SynthSpeaker();
  s.voiceName = v.tts_voice;
  s.rate = v.tts_rate;
  return s;
}
