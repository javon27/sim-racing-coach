// Typed HTTP client for the SimCoach FastAPI server (simcoach/server/app.py).
// UI-agnostic core rule: this file only fetches + shapes data, no analysis logic.

const BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765";

export interface HealthResponse {
  status: string;
}

export interface StatusResponse {
  track: string | null;
  car: string | null;
  car_model?: string | null;
  on_track: boolean;
  // Live driving state (from scoring, detected immediately): on the circuit,
  // in the pit lane, in the garage, or null in menus / not in a session.
  activity: "track" | "pits" | "garage" | null;
  has_map: boolean;
  lmu_connected: boolean;
  session_id: string | null;
  lap_id: number | null;
}

export interface SessionRow {
  session_id: string;
  track: string;
  car: string;
  car_model?: string | null;
  laps: number;
  best: number | null;
  started: number;
  last: number;
  has_chat?: boolean;
}

/** The kind of UNFINISHED lap a row is, or absent/null for a normal timed lap (#77). */
export type PartialKind = "out" | "in" | "partial";

export interface LapRow {
  id: number;
  /** null ONLY on a partial lap — the game posts no time for one. */
  lap_time: number | null;
  valid: number;
  ts: number;
  s1: number | null;
  s2: number | null;
  s3: number | null;
  phase: string | null;
  track?: string;
  car?: string;
  car_model?: string | null;
  lap_no?: number | null;
  pit?: boolean | null;
  partial?: PartialKind | null;
  /** 1-based race number this session, null for a non-race lap (#142). */
  race_index?: number | null;
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
  brake_point: number | null;      // null = never braked in this corner (may be < 0: see #7)
  throttle_point: number | null;   // null = never past the throttle gate in this corner
  trailbrake_overlap: number;
  peak_brake: number;
  peak_lat_g: number;
  peak_brake_g: number | null;
  peak_accel_g: number | null;
  peak_steer: number | null;
  abs_pct: number | null;
  tc_pct: number | null;
  track_edge_min: number | null;
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
  spoken?: string | null;
}

// Billed tokens for a whole chat. Computed by the core (simcoach/store.py +
// simcoach/pricing.py) -- the UI only formats these numbers, never prices anything.
export interface ChatUsage {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  total: number;
  messages: number;
}

export interface ChatUsageResponse {
  usage: ChatUsage;
  // null = the model could not be priced; 0 = a genuinely free model (render both
  // as a dash, never "$0.00" -- see ChatPanel.formatCost).
  cost: number | null;
  // false = at least one model in the chat was unpriceable, so `cost` is a floor.
  cost_complete: boolean;
}

export interface DriverProfile {
  braking_style: string | null;
  balance_preference: string | null;
  risk_tolerance: string | null;
}

export interface VoiceSettings {
  enabled: boolean;
  ptt_key: string;
  ptt_wheel: string | null;
  ptt_gamepad: string | null;
  ptt_wheel_name: string | null;
  ptt_gamepad_name: string | null;
  ptt_focus_gate: boolean;
  tts_voice: string | null;
  tts_rate: number;
  tts_engine: "windows" | "kokoro";
  kokoro_voice: string;
  mic_device_id: string | null;
  vad_threshold: number;
  vad_silence_ms: number;
  vad_max_s: number;
  conversation_idle_s: number;
}

export interface SettingsResponse {
  units: "metric" | "imperial";
  input_device: "wheel" | "controller";
  model: "opus" | "sonnet" | "haiku";
  corner_naming: "names" | "numbers";
  show_cost: boolean;
  log_level: string;
  driver: DriverProfile;
  voice: VoiceSettings;
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
    // Surface the server's FastAPI {detail} so callers can show WHY (e.g. a 422
    // validation reason) instead of a generic failure. Body may not be JSON.
    let detail = "";
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON / empty body */
    }
    throw new Error(
      `API request failed: ${init?.method ?? "GET"} ${path} -> ${res.status}${detail ? `: ${detail}` : ""}`,
    );
  }
  return res.json() as Promise<T>;
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

export function getStatus(): Promise<StatusResponse> {
  return request<StatusResponse>("/status");
}

export interface CarGroup {
  car_class: string;
  cars: string[];
}

export interface SessionsPage {
  rows: SessionRow[];
  total: number;
  tracks: string[];
  cars: string[];
  car_groups: CarGroup[];
}

export interface SessionsFilter {
  track?: string;
  car?: string;
  car_class?: string;
  offset?: number;
  limit?: number;
}

export interface SessionsSummary {
  count: number;
  cars: number;
  personal_best: number | null;
  consistency: number | null;
  improvement: number | null;
  series: { car: string; points: { x: number; value: number }[] }[];
}

function sessionQuery(f: SessionsFilter = {}): string {
  const q = new URLSearchParams();
  if (f.track) q.set("track", f.track);
  if (f.car) q.set("car", f.car);
  if (f.car_class) q.set("car_class", f.car_class);
  if (f.offset != null) q.set("offset", String(f.offset));
  if (f.limit != null) q.set("limit", String(f.limit));
  const s = q.toString();
  return s ? `?${s}` : "";
}

export function getSessions(f: SessionsFilter = {}): Promise<SessionsPage> {
  return request<SessionsPage>(`/sessions${sessionQuery(f)}`);
}

export function getSessionsSummary(f: Pick<SessionsFilter, "track" | "car" | "car_class"> = {}): Promise<SessionsSummary> {
  return request<SessionsSummary>(`/sessions/summary${sessionQuery(f)}`);
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

export type SectionKind = "straight" | "complex" | "section" | "drs";
export interface Section {
  name: string;
  kind: SectionKind;
  start_dist: number;
  end_dist: number;
}

export interface TrackMap {
  width: number;
  height: number;
  path: [number, number][];
  dist: number[]; // lap_distance parallel to path (same length)
  sectors: number[]; // lap_distance of S1/S2 boundaries; [] when unset
  detected_sectors?: number[]; // game's real S1/S2 boundary lap_distances (seed source)
  shipped?: boolean; // built-in authoritative geometry: annotate but don't rebuild layout
  curating?: boolean; // dev build: layout tools + track name are editable
  track_name?: string; // display name (stored override, else the live/canonical name)
  sections: Section[]; // named distance spans (straights, complexes, DRS)
  corners: {
    number: number; // internal positional order key
    label: string; // what the map shows (e.g. "8A"); defaults to str(number)
    name: string;
    direction: string;
    x: number;
    y: number;
    apex_dist: number; // primary apex
    apexes: number[]; // all apexes (lap_distance); [apex_dist] when single
    start_dist: number;
    end_dist: number;
  }[];
  rotation?: number; // baked orientation in degrees (server rotate-then-fit)
}

export function getLapMap(id: number, avg?: boolean): Promise<TrackMap | Record<string, never>> {
  return request<TrackMap | Record<string, never>>(`/laps/${id}/map${avg ? "?avg=1" : ""}`);
}

// Canonical map for the current LIVE track (Pit Wall) — no completed lap needed.
export function getCurrentMap(): Promise<TrackMap | Record<string, never>> {
  return request<TrackMap | Record<string, never>>("/track/map");
}

// Canonical map by track slug (Tracks screen) — no lap needed.
export function getTrackMap(slug: string): Promise<TrackMap | Record<string, never>> {
  return request<TrackMap | Record<string, never>>(`/tracks/${encodeURIComponent(slug)}/map`);
}

export function setMapRotation(id: number, degrees: number): Promise<TrackMap> {
  return request<TrackMap>(`/laps/${id}/rotation`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ degrees }),
  });
}

// Server derives direction from geometry — never send it.
export interface CornerEdit {
  number: number;
  label: string;
  name: string;
  apex_dist: number;
  apexes: number[];
  start_dist?: number;
  end_dist?: number;
}

export function saveLapMap(
  id: number,
  body: { corners: CornerEdit[]; sectors: number[]; sections: Section[]; name?: string },
): Promise<TrackMap> {
  return request<TrackMap>(`/laps/${id}/map`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

// Edit a track's owned map by slug (Tracks screen) — no driven lap needed.
export function saveTrackMap(
  slug: string,
  body: { corners: CornerEdit[]; sectors: number[]; sections: Section[]; name?: string },
): Promise<TrackMap> {
  return request<TrackMap>(`/tracks/${encodeURIComponent(slug)}/map`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

export function setTrackMapRotation(slug: string, degrees: number): Promise<TrackMap> {
  return request<TrackMap>(`/tracks/${encodeURIComponent(slug)}/rotation`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ degrees }),
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
    label?: string;
    name: string;
    direction: string;
    apex_dist: number;
    apexes?: number[];
    start_dist: number;
    end_dist: number;
    x: number;
    y: number;
  }[];
  sectors: number[];
  track_name?: string; // session track name, shown in the header + saved with the layout
  /** Rebuild only: how many point clouds this geometry was reconstructed from. Shown
   *  before Save, because a map off 1-3 laps is thin and the driver should know. */
  lap_count?: number;
}

// Driver-input traces for the Pit Wall graphs (#21). Already resampled onto a
// uniform lap_distance grid and reduced to 500 bins by the sidecar — this client
// only fetches, the component only draws.
//
// Each channel is a per-bin min/max ENVELOPE, not a point sample: one bin spans
// 180-400 ms, at or coarser than the game's 200 ms scoring tick, so a point
// sample would drop a stab or a lift depending on where it fell. `dist` holds
// the bin centres.
export interface InputBand {
  lo: number[];
  hi: number[];
  mean: number[];
}

export interface InputTrace {
  lap_id: number | null;
  lap_time: number;
  length: number;
  dist: number[];
  throttle: InputBand;
  brake: InputBand;
  steering: InputBand;
  speed?: InputBand;
}

export interface InputTraces {
  last: InputTrace | null;
  best: InputTrace | null; // null when the last lap IS the best (nothing to compare)
}

export function getInputTraces(): Promise<InputTraces> {
  return request<InputTraces>("/track/inputs");
}

// Lap-keyed input traces for the coaching screen's Inputs panel (#95): the
// selected lap vs its track/car best, plus corner markers to overlay.
export interface CornerMarker {
  label: string;
  name: string;
  start_dist: number;
  apex_dist: number;
  end_dist: number;
}

export interface LapInputs {
  lap: InputTrace | Record<string, never>;
  best: InputTrace | null;
  corners: CornerMarker[];
  length?: number;
}

export function getLapInputs(lapId: number): Promise<LapInputs> {
  return request<LapInputs>(`/laps/${lapId}/inputs`);
}

export function startMapCapture(): Promise<{ active: boolean }> {
  // Body required so the browser preflights (CSRF guard on the mutating route).
  return request<{ active: boolean }>("/mapcapture/start", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({}),
  });
}

export function cancelMapCapture(): Promise<{ active: boolean }> {
  return request<{ active: boolean }>("/mapcapture/cancel", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({}),
  });
}

export function mapCaptureStatus(): Promise<MapCaptureStatus> {
  return request<MapCaptureStatus>("/mapcapture/status");
}

// Reconstructs the surface from the whole captured point cloud (no left/right
// designation). JSON_HEADERS kept for the CSRF preflight.
export function buildRibbon(): Promise<RibbonPreview> {
  return request<RibbonPreview>("/mapcapture/build", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({}),
  });
}

// Re-reconstruct from the track's SAVED raw capture (no active capture / re-drive).
// Bind the target explicitly: the edited lap (Sessions) or the track slug (Tracks
// screen, which has no driven lap). With NEITHER, the sidecar falls back to the
// live sim or the newest saved capture — i.e. it would rebuild a different track's
// geometry over the one on screen (#26). Slug wins server-side, same priority.
export function rebuildFromCapture(lapId?: number | null,
                                   slug?: string | null): Promise<RibbonPreview> {
  const target = slug ? { slug } : lapId != null ? { lap_id: lapId } : {};
  return request<RibbonPreview>("/mapcapture/rebuild", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(target),
  });
}

export function saveRibbon(): Promise<{ saved: boolean; slug: string }> {
  return request<{ saved: boolean; slug: string }>("/mapcapture/save", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({}),
  });
}

// Persist ONLY the raw capture (for cross-section metrics) without rebuilding
// geometry -- for a track that already has a corner map you want to keep.
export function saveMapCloud(): Promise<{ saved: boolean; slug: string }> {
  return request<{ saved: boolean; slug: string }>("/mapcapture/save-cloud", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({}),
  });
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

export function getChatUsage(chatId: string): Promise<ChatUsageResponse> {
  return request<ChatUsageResponse>(`/chats/${chatId}/usage`);
}

export function getSettings(): Promise<SettingsResponse> {
  return request<SettingsResponse>("/settings");
}

export function getSetup(): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>("/setup");
}

const JSON_HEADERS = { "Content-Type": "application/json" };

// Explicit persistence of what the sim CURRENTLY reports (#51: GET is
// read-only, a no-cors <img>/<script>/prefetch/retry/replay of it must not
// write to the DB). Body is empty -- the server re-reads the setup itself
// rather than trusting caller-supplied content (that was a prompt-injection
// sink into the coach's prompt); `{}` only keeps the CSRF preflight (a
// non-simple request needs a body).
export function captureSetup(): Promise<{ saved: boolean }> {
  return request<{ saved: boolean }>("/setup/capture", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({}),
  });
}

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
  // Body required so the browser preflights (CSRF guard on a route that makes a
  // real, billed API call).
  return request<{ ok: boolean; error: string | null }>("/settings/test-key", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({}),
  });
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
  // Body required so the browser preflights (CSRF guard on the mutating route).
  return request<IngestResult>("/ingest", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({}),
  });
}

export interface TrackRow {
  slug: string;
  name: string;
  has_map: boolean;
  has_notes: boolean;
  user_edited: boolean;
  /** Exactly the clouds a Build would consume (the track's saved capture if it has
   *  one, else its usable live laps). This is the build gate: 0 means nothing to
   *  rebuild from, whatever live_laps says. */
  captures: number;
  /** Live-captured lap rows. Exceeds `captures` for laps recorded before position
   *  capture existed (47 channels, no pos_*) -- they can never be rebuilt from. */
  live_laps: number;
  /** A saved capture exists but yields no usable cloud (corrupt, or no lap with
   *  position data). It blocks the build (captures 0) whatever live_laps holds (#84). */
  capture_unreadable?: boolean;
}

export interface TrackNotes {
  overview: string;
  corners: Record<string, string>;
  reference_times: Record<string, string>;
  source: string;
}

export function listTracks(): Promise<TrackRow[]> {
  return request<TrackRow[]>("/tracks");
}

export function draftNotes(slug: string, track: string, guides: string): Promise<{ draft: TrackNotes }> {
  return request<{ draft: TrackNotes }>(`/tracks/${slug}/draft-notes`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ track, guides }),
  });
}

export interface SaveNotesResult {
  ok: boolean;
  rev: string;                              // send this with the next save
  dropped_notes: Record<string, string>;
}

// `rev` is the one getTrackNotes (or the previous save) returned. The server refuses a
// missing one (428) and a stale one (409): a map save can re-key the notes underneath a
// screen that is holding a copy, and writing that copy back puts every old key back.
export function saveNotes(slug: string, track: string, notes: TrackNotes, rev: string | null): Promise<SaveNotesResult> {
  return request<SaveNotesResult>(`/tracks/${slug}/notes`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify({ track, notes, rev }),
  });
}

export function getTrackNotes(slug: string): Promise<TrackNotes & { rev?: string }> {
  return request<TrackNotes & { rev?: string }>(`/tracks/${slug}/notes`);
}

// --- Driver Profile & Memories

export interface DriverMemory {
  id: number;
  text: string;
  status: "pending" | "active" | "retired";
  track: string | null;
  car: string | null;
  source: "coach_tool" | "distill" | "manual";
  session_id: string | null;
  created_ts: number;
  updated_ts: number;
}

export interface DriverSummary {
  text: string;
  status: "pending" | "active";
}

export interface DriverPrefs {
  braking_style: string | null;
  balance_preference: string | null;
  risk_tolerance: string | null;
}

export interface DriverProfileData {
  prefs: DriverPrefs;
  summary: DriverSummary;
  memories: {
    active: DriverMemory[];
    pending: DriverMemory[];
  };
}

export function getProfile(): Promise<DriverProfileData> {
  return request<DriverProfileData>("/profile");
}

export function putProfileSummary(
  text: string,
  status: "pending" | "active",
): Promise<DriverSummary> {
  return request<DriverSummary>("/profile/summary", {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify({ text, status }),
  });
}

export interface AddMemoryInput {
  text: string;
  track?: string | null;
  car?: string | null;
}

export interface AddMemoryResponse {
  id: number;
  text: string;
  status: "active" | "pending";
  track: string | null;
  car: string | null;
}

export function addMemory(m: AddMemoryInput): Promise<AddMemoryResponse> {
  return request<AddMemoryResponse>("/profile/memories", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(m),
  });
}

export interface UpdateMemoryPatch {
  text?: string;
  status?: "pending" | "active" | "retired";
}

export function updateMemory(
  id: number,
  patch: UpdateMemoryPatch,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/profile/memories/${id}`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(patch),
  });
}

export interface DistillProposals {
  memories: Array<{
    id: number;
    text: string;
    status: "pending";
    track: string | null;
    car: string | null;
  }>;
  summary: string;
}

export function distillProfile(sessionId: string): Promise<DistillProposals> {
  return request<DistillProposals>("/profile/distill", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ session_id: sessionId }),
  });
}

// --- Voice

export interface VoiceStatus {
  installed: boolean;
  installing: boolean;
  progress_bytes: number;
  total_bytes: number | null;
  error: string | null;
  // Absent on a whisper frame (its installer predates this field); present and
  // "kokoro" on a Kokoro frame -- the /events feed multiplexes both installers'
  // progress over the same voice_setup topic, so a subscriber must check this to
  // avoid reacting to the other engine's frame.
  engine?: "whisper" | "kokoro";
}

// Dedicated fetch (not `request()`): the thrown message must be the server's
// `detail` alone, with no "API request failed: ..." prefix, because it gets spoken.
export async function transcribe(wav: Uint8Array): Promise<{ text: string; seconds: number }> {
  const res = await fetch(`${BASE}/voice/transcribe`, {
    method: "POST",
    headers: { "content-type": "audio/wav" },
    body: wav as BodyInit,  // Uint8Array is a valid fetch body at runtime; TS's stricter typed-array generics disagree
  });
  if (!res.ok) {
    let detail = res.status === 409 ? "voice isn't set up yet — enable it in Settings" : "couldn't transcribe that";
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON / empty body -- keep the default */
    }
    throw new Error(detail);
  }
  return res.json();
}

export function voiceStatus(): Promise<VoiceStatus> {
  return request<VoiceStatus>("/voice/status");
}

export function voiceSetup(): Promise<VoiceStatus & { started: boolean }> {
  return request<VoiceStatus & { started: boolean }>("/voice/setup", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({}),
  });
}

export interface KokoroStatus extends VoiceStatus { engine: "kokoro"; voices: string[] }

export function kokoroStatus(): Promise<KokoroStatus> {
  return request<KokoroStatus>("/voice/kokoro/status");
}

export function kokoroSetup(): Promise<KokoroStatus & { started: boolean }> {
  return request("/voice/kokoro/setup", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({}) });
}

export interface TtsCacheStats { files: number; bytes: number }

export function ttsCacheStats(): Promise<TtsCacheStats> {
  return request<TtsCacheStats>("/voice/tts-cache");
}

export function ttsCacheClear(): Promise<{ removed: number }> {
  return request<{ removed: number }>("/voice/tts-cache", {
    method: "DELETE",
    headers: JSON_HEADERS,
    body: JSON.stringify({ confirm: true }),
  });
}

// A 422 whose `detail` is {"error": "unpronounceable", "words": [...]} (#118/#123) --
// distinct from the plain-string 422 for a bad speed, so KokoroSpeaker can fall back
// to Windows speech for just this utterance instead of the wholesale onUnavailable path.
export class UnpronounceableError extends Error {
  words: string[];
  constructor(words: string[]) {
    super("unpronounceable");
    this.name = "UnpronounceableError";
    this.words = words;
  }
}

// Dedicated fetch (not `request()`): mirrors transcribe() -- the thrown message
// must be the server's `detail` alone (Speaker surfaces it via onUnavailable).
export async function speak(
  text: string,
  opts: { voice?: string; speed?: number; trackSlug?: string },
  signal?: AbortSignal,
): Promise<ArrayBuffer> {
  const res = await fetch(`${BASE}/voice/speak`, {
    method: "POST", headers: JSON_HEADERS, signal,
    body: JSON.stringify({ text, voice: opts.voice, speed: opts.speed, track_slug: opts.trackSlug }),
  });
  if (!res.ok) {
    let detail: unknown = "couldn't synthesise speech";
    try { const b = await res.json(); if (b?.detail !== undefined) detail = b.detail; } catch { /* keep default */ }
    if (res.status === 422 && typeof detail === "object" && detail !== null
      && (detail as { error?: string }).error === "unpronounceable") {
      const words = (detail as { words?: unknown }).words;
      throw new UnpronounceableError(Array.isArray(words) ? words.map(String) : []);
    }
    throw new Error(typeof detail === "string" ? detail : "couldn't synthesise speech");
  }
  return res.arrayBuffer();
}
