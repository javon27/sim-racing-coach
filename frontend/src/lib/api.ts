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
  session_id: string | null;
  lap_id: number | null;
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
  s1: number | null;
  s2: number | null;
  s3: number | null;
  phase: string | null;
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

export interface RaceResult {
  position: number | null;
  class_position: number | null;
  finish_status: string | null;
  incidents: string[];
  penalties: unknown[];
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
  log_level: string;
  driver: DriverProfile;
  api_key_set: boolean;
  data_dir: string;
  app_version: string;
  lmu_build: string | null;
  lmu_dir: string | null;
  lmu_player_names: string[];
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

export function getSessionRace(sid: string): Promise<RaceResult | Record<string, never>> {
  return request<RaceResult | Record<string, never>>(`/sessions/${sid}/race`);
}

export function getLapAnalysis(id: number): Promise<LapAnalysisResponse> {
  return request<LapAnalysisResponse>(`/laps/${id}/analysis`);
}

export interface TrackMap {
  width: number;
  height: number;
  path: [number, number][];
  dist: number[]; // lap_distance parallel to path (same length)
  sectors: number[]; // lap_distance of S1/S2 boundaries; [] when unset
  corners: {
    number: number;
    name: string;
    direction: string;
    x: number;
    y: number;
    apex_dist: number;
    start_dist: number;
    end_dist: number;
  }[];
}

export function getLapMap(id: number, avg?: boolean): Promise<TrackMap | Record<string, never>> {
  return request<TrackMap | Record<string, never>>(`/laps/${id}/map${avg ? "?avg=1" : ""}`);
}

// Server derives direction from geometry — never send it.
export interface CornerEdit {
  number: number;
  name: string;
  apex_dist: number;
  start_dist?: number;
  end_dist?: number;
}

export function saveLapMap(
  id: number,
  body: { corners: CornerEdit[]; sectors: number[] },
): Promise<TrackMap> {
  return request<TrackMap>(`/laps/${id}/map`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

// --- Dedicated mapping capture (drive two boundary laps -> reconstruct a track ribbon).
// These laps are NOT persisted to the store; they route to a transient buffer.
export interface MapCaptureStatus {
  active: boolean;
  live_track: string | null;
  laps: { idx: number; samples: number; length: number }[];
}

export interface RibbonPreview {
  width: number;
  height: number;
  left: [number, number][];
  right: [number, number][];
  center: [number, number][];
  corners: {
    number: number;
    name: string;
    direction: string;
    apex_dist: number;
    start_dist: number;
    end_dist: number;
    x: number;
    y: number;
  }[];
  sectors: number[];
}

export function startMapCapture(): Promise<{ active: boolean }> {
  return request<{ active: boolean }>("/mapcapture/start", { method: "POST" });
}

export function cancelMapCapture(): Promise<{ active: boolean }> {
  return request<{ active: boolean }>("/mapcapture/cancel", { method: "POST" });
}

export function mapCaptureStatus(): Promise<MapCaptureStatus> {
  return request<MapCaptureStatus>("/mapcapture/status");
}

export function buildRibbon(left: number, right: number): Promise<RibbonPreview> {
  return request<RibbonPreview>("/mapcapture/build", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ left, right }),
  });
}

export function saveRibbon(): Promise<{ saved: boolean; slug: string }> {
  return request<{ saved: boolean; slug: string }>("/mapcapture/save", { method: "POST" });
}

export function getChats(): Promise<ChatRow[]> {
  return request<ChatRow[]>("/chats");
}

export function getSessionChats(sid: string): Promise<ChatRow[]> {
  return request<ChatRow[]>(`/sessions/${sid}/chats`);
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

const JSON_HEADERS = { "Content-Type": "application/json" };

export function putSettings(cfg: Record<string, unknown>): Promise<SettingsResponse> {
  return request<SettingsResponse>("/settings", {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(cfg),
  });
}

export function setApiKey(key: string): Promise<{ api_key_set: boolean }> {
  return request<{ api_key_set: boolean }>("/settings/api-key", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ key }),
  });
}

export function testApiKey(): Promise<{ ok: boolean; error: string | null }> {
  return request<{ ok: boolean; error: string | null }>("/settings/test-key", { method: "POST" });
}

export function clearData(): Promise<{ cleared: boolean }> {
  return request<{ cleared: boolean }>("/data", {
    method: "DELETE",
    headers: JSON_HEADERS,
    body: JSON.stringify({ confirm: true }),
  });
}

export interface IngestResult {
  scanned: number;
  imported: number;
  skipped: number;
  unmatched: number;
  errors: number;
}

export function ingestHistory(): Promise<IngestResult> {
  return request<IngestResult>("/ingest", { method: "POST" });
}
