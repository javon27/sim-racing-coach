// Browser adapters for VoiceLoop (voice.ts): mic capture, HTTP transcription, and
// speech synthesis. No browser global is touched at import time -- jsdom (tests) has
// none of AudioContext/MediaRecorder/speechSynthesis, and voice is always injected
// as a VoiceLoop prop in tests -- only the real app constructs these.
import { speak as apiSpeak, transcribe as apiTranscribe, UnpronounceableError, type VoiceSettings } from "./api";
import type { Earcon, Recorder, Speaker, Transcriber } from "./voice";
import { crewChiefHold, isTauri } from "./voice-shell";

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

// fetch() rejects an aborted request with a DOMException named "AbortError" --
// jsdom/vitest mocks may reject with a plain object of the same shape instead.
function isAbortError(e: unknown): boolean {
  return typeof e === "object" && e !== null && (e as { name?: string }).name === "AbortError";
}

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

// #163: awaited before each enqueue()d sentence starts; resolves early once
// `cancelled()` is true. `reset()` starts a new answer's wait budget (on cancel and on
// going idle). A rejecting hold speaks anyway. null = speak at once, as before
// (voice-shell crewChiefHold).
export type Hold = ((cancelled: () => boolean) => Promise<void>) & { reset?(): void };

export class SynthSpeaker implements Speaker {
  voiceName: string | null = null;
  rate = 1.0;
  hold: Hold | null = null;
  #chain: Promise<void> = Promise.resolve();
  #gen = 0;
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
        this.hold?.reset?.();
        const waiters = this.#idleWaiters;
        this.#idleWaiters = [];
        waiters.forEach((w) => w());
      }
    };
    if (!this.hold) {
      u.onend = settle;
      u.onerror = settle;
      speechSynthesis.speak(u);
      return;
    }
    // #163: with a hold, sentences are chained here instead of queued natively, so
    // each one can wait for CrewChief to go quiet before it starts -- between
    // sentences, never mid-word.
    const hold = this.hold;
    const gen = this.#gen;
    this.#chain = this.#chain.then(async () => {
      try { await hold(() => gen !== this.#gen); } catch { /* a broken hold must not kill speech */ }
      if (gen !== this.#gen) return;              // cancelled while held: dropped
      await new Promise<void>((done) => {
        // A cancelled utterance's late onerror must not settle the NEXT answer's count.
        const end = () => { if (gen === this.#gen) settle(); done(); };
        u.onend = end;
        u.onerror = end;
        speechSynthesis.speak(u);
      });
    });
  }

  idle(): Promise<void> {
    if (this.#pending === 0) return Promise.resolve();
    return new Promise((resolve) => this.#idleWaiters.push(resolve));
  }

  cancel(): void {
    this.#gen++;
    this.#chain = Promise.resolve();   // never let a new answer wait on a cancelled one
    this.hold?.reset?.();
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
const UNPRONOUNCEABLE = Symbol("unpronounceable");

export class KokoroSpeaker implements Speaker {
  voice = "af_heart";
  rate = 1.0;
  trackSlug: string | null = null;
  onUnavailable: ((reason: string) => void) | null = null;
  // The Windows voice/rate to fall back to for a single unpronounceable sentence
  // (#118/#123) -- makeSpeaker wires this from the same VoiceSettings.tts_voice used
  // when the engine itself is "windows".
  windowsVoiceName: string | null = null;
  hold: Hold | null = null;
  #fetchWav: typeof apiSpeak;
  #ctx: AudioContext | null;
  #windowsFallback: SynthSpeaker | null = null;
  #queue: { text: string; resolve: () => void }[] = [];
  // `buffer` resolves to the decoded audio, null (skip), or the UNPRONOUNCEABLE marker.
  // The marker is NOT spoken here -- #pump() runs a sentence AHEAD of playback, so
  // speaking it at fetch time talked over the sentence still playing (#148). `text` is
  // carried so #play() can speak it in turn.
  #ahead: { buffer: Promise<AudioBuffer | null | typeof UNPRONOUNCEABLE>; text: string;
            resolve: () => void; abort: AbortController } | null = null;
  #playing: AudioBufferSourceNode | null = null;
  // Set while a fallback sentence is being spoken by Windows, so the next sentence
  // waits for it (#drainPlaying) instead of starting on top of it.
  #windowsSpeaking: Promise<void> | null = null;
  #idleWaiters: (() => void)[] = [];
  #failed = false;
  // Decoded-audio cache (#121): replaying the same text -- voice/rate/track
  // held fixed -- skips both the fetch and the decode. An AudioBuffer can be
  // played by any number of AudioBufferSourceNodes, so the cached value is
  // reused as-is. Insertion order doubles as LRU order: a hit is deleted and
  // re-inserted so the oldest key is always the true least-recently-used one.
  #cache = new Map<string, AudioBuffer>();
  static readonly CACHE_MAX = 64;
  // Bumped by cancel() so an in-flight #play() -- even one already past its
  // synthesis, decoded and waiting inside #drainPlaying() for the previous
  // sentence to end -- notices and bails instead of playing a cancelled sentence.
  #gen = 0;

  constructor(deps: { fetchWav?: typeof apiSpeak; audio?: AudioContext; windowsFallback?: SynthSpeaker } = {}) {
    this.#fetchWav = deps.fetchWav ?? apiSpeak;
    this.#ctx = deps.audio ?? null;
    this.#windowsFallback = deps.windowsFallback ?? null;
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
    // Stop the Windows fallback too: it is a separate engine, so stopping the Web
    // Audio node left it talking on after the driver pressed Stop (#148).
    this.#windowsFallback?.cancel();
    this.#windowsSpeaking = null;
    for (const q of this.#queue.splice(0)) q.resolve();
    this.#settleIdle();
  }
  earcon(kind: Earcon): void { playEarcon(this.#audio(), kind); }

  #settleIdle(): void {
    if (this.#queue.length || this.#ahead || this.#playing) return;
    this.hold?.reset?.();
    const w = this.#idleWaiters; this.#idleWaiters = []; w.forEach((f) => f());
  }
  #cacheKey(text: string): string {
    return `${this.voice}|${this.rate}|${this.trackSlug ?? ""}|${text}`;
  }
  #cacheGet(key: string): AudioBuffer | null {
    const hit = this.#cache.get(key);
    if (!hit) return null;
    this.#cache.delete(key); this.#cache.set(key, hit);   // refresh LRU order
    return hit;
  }
  #cacheSet(key: string, buf: AudioBuffer): void {
    this.#cache.delete(key); this.#cache.set(key, buf);
    if (this.#cache.size > KokoroSpeaker.CACHE_MAX) {
      const oldest = this.#cache.keys().next().value;
      if (oldest !== undefined) this.#cache.delete(oldest);
    }
  }
  // One synthesis in flight ahead of playback: fetch the head while the previous plays.
  #pump(): void {
    if (this.#ahead || !this.#queue.length) return;
    const item = this.#queue.shift()!;
    const key = this.#cacheKey(item.text);
    const cached = this.#cacheGet(key);
    if (cached) {
      this.#ahead = { buffer: Promise.resolve(cached), resolve: item.resolve, abort: new AbortController() };
      void this.#play();
      return;
    }
    const abort = new AbortController();
    const bytes = this.#fetchWav(item.text, { voice: this.voice, speed: this.rate, trackSlug: this.trackSlug ?? undefined }, abort.signal)
      .catch((e: unknown) => {
        // cancel() aborting this fetch is not an engine failure -- only a non-OK
        // response (api.speak throws) or a network error should fall back to Windows.
        if (isAbortError(e)) return null;
        if (e instanceof UnpronounceableError) return UNPRONOUNCEABLE;
        this.#fail(e);
        return null;
      });
    // An unpronounceable sentence is MARKED here and spoken by Windows in #play(),
    // in turn (#118/#123, ordering fixed in #148). onUnavailable is never called for
    // it, and the next utterance still tries Kokoro.
    const buffer = bytes.then((b) => (
      b === UNPRONOUNCEABLE ? UNPRONOUNCEABLE : b ? this.#decode(b, key) : null
    ));
    this.#ahead = { buffer, text: item.text, resolve: item.resolve, abort };
    void this.#play();
  }
  #speakViaWindows(text: string): Promise<void> {
    this.#windowsFallback ??= new SynthSpeaker();
    this.#windowsFallback.voiceName = this.windowsVoiceName;
    this.#windowsFallback.rate = this.rate;
    return this.#windowsFallback.speak(text);
  }
  // A decode failure for one sentence isn't an engine failure -- skip just that
  // sentence. Getting/creating the AudioContext itself failing IS an engine failure
  // (no Web Audio support), same as before. A null/failed decode is never cached --
  // only a successful decode is worth replaying, and caching a miss would make a
  // transient failure sticky.
  #decode(bytes: ArrayBuffer, key: string): Promise<AudioBuffer | null> {
    let ctx: AudioContext;
    try { ctx = this.#audio(); } catch (e) { this.#fail(e); return Promise.resolve(null); }
    return ctx.decodeAudioData(bytes).then((buf) => {
      this.#cacheSet(key, buf);
      return buf;
    }).catch((e: unknown) => {
      console.warn("Kokoro: couldn't decode synthesised audio, skipping sentence", e);
      return null;
    });
  }
  async #play(): Promise<void> {
    const ahead = this.#ahead!;
    const gen = this.#gen;
    const buf = await ahead.buffer;
    if (gen !== this.#gen) { ahead.resolve(); this.#settleIdle(); return; }   // cancelled while synthesising
    await this.#drainPlaying();
    if (gen !== this.#gen) { ahead.resolve(); this.#settleIdle(); return; }   // cancelled while waiting to play
    // #163: hold for CrewChief between sentences. Still `#ahead` while held, so
    // idle() stays pending and cancel() resolves + drops it at once.
    if (this.hold) {
      try { await this.hold(() => gen !== this.#gen); } catch { /* a broken hold must not kill speech */ }
      if (gen !== this.#gen) { ahead.resolve(); this.#settleIdle(); return; } // cancelled while held
    }
    this.#ahead = null;
    this.#pump();                                    // start the NEXT synthesis before this one plays
    if (buf === UNPRONOUNCEABLE) {
      // Spoken HERE, in playback order, not when its fetch came back (#148).
      // #windowsSpeaking makes the next sentence wait for it in #drainPlaying().
      const speaking = this.#speakViaWindows(ahead.text);
      this.#windowsSpeaking = speaking;
      await speaking;
      if (this.#windowsSpeaking === speaking) this.#windowsSpeaking = null;
      ahead.resolve();
      this.#settleIdle();
      return;
    }
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
  // Waits for whatever is currently making sound -- the Web Audio node AND a Windows
  // fallback sentence (#148). Missing the latter is what let the two engines talk
  // over each other, leapfrogging paragraphs.
  #drainPlaying(): Promise<void> {
    const cur = this.#playing;
    const node: Promise<void> = cur
      ? new Promise((r) => { const prev = cur.onended; cur.onended = (e) => { prev?.call(cur, e); r(); }; })
      : Promise.resolve();
    const windows = this.#windowsSpeaking ?? Promise.resolve();
    return Promise.all([node, windows]).then(() => undefined);
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
    s.hold = crewChiefHoldFor(v);
    s.voice = v.kokoro_voice;
    s.rate = v.tts_rate;
    s.windowsVoiceName = v.tts_voice;
    s.onUnavailable = onFallback;
    return s;
  }
  const s = new SynthSpeaker();
  s.voiceName = v.tts_voice;
  s.rate = v.tts_rate;
  s.hold = crewChiefHoldFor(v);
  return s;
}

/** #163: the CrewChief hold when the setting is on and a Tauri shell can answer. */
export function crewChiefHoldFor(v: VoiceSettings): Hold | null {
  return v.crewchief_wait && isTauri() ? crewChiefHold() : null;
}
