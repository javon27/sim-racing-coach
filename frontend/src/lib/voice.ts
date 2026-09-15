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
