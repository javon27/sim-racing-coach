// Typed HTTP client for the SimCoach FastAPI server (simcoach/server/app.py).
// UI-agnostic core rule: this file only fetches + shapes data, no analysis logic.

const BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765";

export interface HealthResponse {
  status: string;
}

export interface StatusResponse {
  track: string | null;
  car: string | null;
  on_track: boolean;
  has_map: boolean;
  lmu_connected: boolean;
}

export interface SessionRow {
  session_id: string;
  track: string;
  car: string;
  laps: number;
  best: number | null;
  started: number;
  last: number;
}

export interface LapRow {
  id: number;
  lap_time: number;
  valid: number;
  ts: number;
}

export interface CornerMetrics {
  corner_no: number;
  name: string;
  direction: string;
  apex_dist: number;
  entry_spd: number;
  apex_spd: number;
  min_spd: number;
  exit_spd: number;
  brake_point: number;
  throttle_point: number;
  trailbrake_overlap: number;
  peak_brake: number;
  peak_lat_g: number;
  min_grip: number | null;
  min_spd_delta: number | null;
}

export interface LapAnalysisResponse {
  lap_time: number;
  corners: CornerMetrics[];
}

export interface ChatRow {
  chat_id: string;
  started: number;
  last: number;
  message_count: number;
  tracks: string;
}

export interface ChatMessage {
  ts: number;
  session_id: string;
  track: string;
  car: string;
  lap_id: number | null;
  question: string;
  answer: string;
  tool_calls: unknown;
}

export interface DriverProfile {
  braking_style: string | null;
  balance_preference: string | null;
  risk_tolerance: string | null;
}

export interface SettingsResponse {
  units: "metric" | "imperial";
  input_device: "wheel" | "controller";
  model: "opus" | "sonnet" | "haiku";
  corner_naming: "names" | "numbers";
  driver: DriverProfile;
  api_key_set: boolean;
  data_dir: string;
  app_version: string;
  lmu_build: string | null;
}

// Retries a rejected fn up to `tries` times, waiting `delayMs` between attempts.
// Used by components to ride out the sidecar's startup race (server not bound yet).
export async function withRetry<T>(fn: () => Promise<T>, tries = 8, delayMs = 750): Promise<T> {
  let lastErr: unknown;
  for (let i = 0; i < tries; i++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (i < tries - 1) await new Promise((r) => setTimeout(r, delayMs));
    }
  }
  throw lastErr;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    throw new Error(`API request failed: ${init?.method ?? "GET"} ${path} -> ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

export function getStatus(): Promise<StatusResponse> {
  return request<StatusResponse>("/status");
}

export function getSessions(): Promise<SessionRow[]> {
  return request<SessionRow[]>("/sessions");
}

export function getSessionLaps(sid: string): Promise<LapRow[]> {
  return request<LapRow[]>(`/sessions/${sid}/laps`);
}

export function getLapAnalysis(id: number): Promise<LapAnalysisResponse> {
  return request<LapAnalysisResponse>(`/laps/${id}/analysis`);
}

export function getChats(): Promise<ChatRow[]> {
  return request<ChatRow[]>("/chats");
}

export function getChatMessages(chatId: string): Promise<ChatMessage[]> {
  return request<ChatMessage[]>(`/chats/${chatId}`);
}

export function getSettings(): Promise<SettingsResponse> {
  return request<SettingsResponse>("/settings");
}

export function getSetup(): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>("/setup");
}
