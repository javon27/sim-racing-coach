// Browser adapters for VoiceLoop (voice.ts): mic capture, HTTP transcription, and
// speech synthesis. No browser global is touched at import time -- jsdom (tests) has
// none of AudioContext/MediaRecorder/speechSynthesis, and voice is always injected
// as a VoiceLoop prop in tests -- only the real app constructs these.
import { transcribe as apiTranscribe } from "./api";
import type { Earcon, Recorder, Speaker, Transcriber } from "./voice";

export class MicRecorder implements Recorder {
  #ctx: AudioContext | null = null;
  #stream: MediaStream | null = null;
  #source: MediaStreamAudioSourceNode | null = null;
  #node: ScriptProcessorNode | null = null;
  #chunks: Float32Array[] = [];

  // Acquired once, lazily, and kept for the life of the page -- getUserMedia +
  // AudioContext creation costs hundreds of ms, which used to run on every press.
  async #ensureStream(): Promise<void> {
    if (this.#stream && this.#ctx) return;
    this.#stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    this.#ctx = new AudioContext({ sampleRate: 16000 });
    this.#source = this.#ctx.createMediaStreamSource(this.#stream);
  }

  async start(onFrame: (frame: Float32Array) => void): Promise<void> {
    await this.#ensureStream();
    const ctx = this.#ctx!;
    // ponytail: ScriptProcessorNode is deprecated but inline and universal; AudioWorklet when it breaks.
    this.#node = ctx.createScriptProcessor(4096, 1, 1);
    this.#chunks = [];
    this.#node.onaudioprocess = (e) => {
      const chunk = new Float32Array(e.inputBuffer.getChannelData(0));
      this.#chunks.push(chunk);
      onFrame(chunk);
    };
    this.#source!.connect(this.#node);
    this.#node.connect(ctx.destination);
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

export class SynthSpeaker implements Speaker {
  voiceName: string | null = null;
  rate = 1.0;
  #beepCtx: AudioContext | null = null;

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

  cancel(): void {
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
  }

  earcon(kind: Earcon): void {
    if (!this.#beepCtx) this.#beepCtx = new AudioContext();
    const ctx = this.#beepCtx;
    const osc = ctx.createOscillator();
    osc.frequency.value = EARCON_HZ[kind];
    osc.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.08);
  }
}
