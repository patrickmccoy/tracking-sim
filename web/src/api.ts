// A typed client for `tracking-sim serve` (src/tracking_sim/api).
//
// Scenario documents are kept loosely typed past the fields the UI edits:
// the server validates everything with its own models, and passing the
// rest through untouched means the UI never drops a field it doesn't know.

export interface Config {
  map_style: string;
  version: string;
}

export interface ScenarioEntry {
  key: string;
  name: string;
  file: string;
}

export interface Position {
  lat: number;
  lon: number;
  alt_m: number;
  mast_m: number;
}

export interface Coverage {
  range_min_m: number;
  range_max_m: number;
  azimuth_deg: [number, number];
  elevation_deg: [number, number];
  radar_horizon: boolean;
  [key: string]: unknown;
}

export interface SensorSpec {
  id: string;
  type: "rotating_radar_2d" | "passive_bearing";
  position: Position;
  coverage: Coverage;
  [key: string]: unknown;
}

export interface TrackerSpec {
  id: string;
  filter: string;
  [key: string]: unknown;
}

export interface ScenarioDoc {
  name: string;
  origin: { lat: number; lon: number; alt_m: number } | null;
  truth: { source: string; path: string; window: [string, string] | null; [key: string]: unknown };
  terrain: Record<string, unknown> | null;
  sensors: SensorSpec[];
  trackers: TrackerSpec[];
  seed: number;
  [key: string]: unknown;
}

export interface ScenarioResponse {
  key: string;
  scenario: ScenarioDoc;
  frame: { lat: number; lon: number; alt_m: number };
}

export interface TrackerScoreRow {
  tracker: string;
  ospa_m: number;
  [key: string]: unknown;
}

export interface RunRow {
  id: string;
  created: string;
  scenario: string;
  seed: number;
  sensors: string[];
  trackers: TrackerScoreRow[];
}

export interface OverTime {
  time: string;
  ospa_m: number;
  [key: string]: unknown;
}

export interface RunDetail extends RunRow {
  manifest: { scenario: ScenarioDoc & { frame: { lat: number; lon: number; alt_m: number } }; [key: string]: unknown };
  metrics: { trackers: (TrackerScoreRow & { over_time: OverTime[] })[]; timings_s: Record<string, number> };
}

export type JobState = "queued" | "running" | "done" | "failed";

export interface Job {
  id: string;
  run_id: string;
  scenario: string;
  state: JobState;
  stage: string | null;
  reused: boolean;
  error: string | null;
  created: string;
  started: string | null;
  finished: string | null;
}

export interface TruthSegment {
  id: string;
  icao24: string;
  callsign: string | null;
  t: number[];
  lat: number[];
  lon: number[];
  alt_m: (number | null)[];
  speed_mps: number[];
  course_deg: number[];
}

export interface TruthData {
  window: [number, number];
  step_s: number;
  segments: TruthSegment[];
}

export interface DetectionData {
  t: number[];
  sensor_id: string[];
  scan: number[];
  kind: ("target" | "clutter")[];
  segment_id: (string | null)[];
  lat: number[];
  lon: number[];
}

export interface TrackPath {
  id: string;
  segment_id: string | null;
  t: number[];
  lat: number[];
  lon: number[];
  alt_m: number[];
  speed_mps: number[];
  course_deg: number[];
  sigma_h_m: number[];
  confirmed: boolean[];
  updated: boolean[];
}

export interface TrackData {
  tracker: string;
  tracks: TrackPath[];
}

export interface CoverageFeature {
  type: "Feature";
  properties: { height_m: number; area_km2: number };
  geometry: { type: "MultiPolygon"; coordinates: number[][][][] };
}

export interface CoverageData {
  type: "FeatureCollection";
  features: CoverageFeature[];
}

/** An error the server explained: `detail` is its message or validation errors. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(describeDetail(detail) || `HTTP ${status}`);
  }
}

function describeDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((e: { loc?: unknown[]; msg?: string }) => `${(e.loc ?? []).join(".")}: ${e.msg ?? ""}`)
      .join("; ");
  }
  return "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let detail: unknown = response.statusText;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail;
    } catch {
      // not JSON: keep the status text
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

const post = <T>(path: string, body: unknown, signal?: AbortSignal) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body), signal });

export const api = {
  config: () => request<Config>("/config"),
  scenarios: () => request<ScenarioEntry[]>("/scenarios"),
  scenario: (key: string) => request<ScenarioResponse>(`/scenarios/${encodeURIComponent(key)}`),
  runs: () => request<RunRow[]>("/runs"),
  run: (id: string) => request<RunDetail>(`/runs/${encodeURIComponent(id)}`),
  truth: (id: string, stepS = 5) => request<TruthData>(`/runs/${encodeURIComponent(id)}/truth?step_s=${stepS}`),
  detections: (id: string) => request<DetectionData>(`/runs/${encodeURIComponent(id)}/detections`),
  tracks: (id: string, tracker: string) =>
    request<TrackData>(`/runs/${encodeURIComponent(id)}/tracks/${encodeURIComponent(tracker)}`),
  startRun: (scenario: ScenarioDoc) => post<Job>("/runs", { scenario }),
  job: (id: string) => request<Job>(`/jobs/${encodeURIComponent(id)}`),
  elevation: (terrain: Record<string, unknown> | null, points: [number, number][]) =>
    post<{ heights_m: (number | null)[]; source: string | null }>("/elevation", { terrain, points }),
  exportScenario: async (scenario: ScenarioDoc): Promise<Blob> => {
    const response = await fetch("/api/scenarios/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario }),
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
      throw new ApiError(response.status, body.detail ?? response.statusText);
    }
    return response.blob();
  },
  coverage: (
    sensor: SensorSpec,
    terrain: Record<string, unknown> | null,
    heightsM: number[],
    signal?: AbortSignal,
  ) => post<CoverageData>("/coverage", { sensor, terrain, heights_m: heightsM }, signal),
};
