// New sensors dropped on the map: a copy of one already in the scenario if
// there is one of the same type, otherwise these defaults (roughly the
// example terminal-area radar, and a direction-finding receiver).

import type { ScenarioDoc, SensorSpec } from "../api";

export const DEFAULT_RADAR: Omit<SensorSpec, "id" | "position"> = {
  type: "rotating_radar_2d",
  coverage: {
    range_min_m: 500,
    range_max_m: 110_000,
    azimuth_deg: [0, 360],
    elevation_deg: [-1, 30],
    radar_horizon: true,
  },
  scan: { period_s: 5, beamwidth_deg: 1.5, start_azimuth_deg: 0 },
  noise: { range_sigma_m: 60, bearing_sigma_deg: 0.2 },
  detection: { model: "constant", pd: 0.9, false_alarm_density: 1e-9 },
  resolution: { range_m: 150, bearing_deg: 1.5 },
  latency_s: 0.5,
};

export const DEFAULT_PASSIVE: Omit<SensorSpec, "id" | "position"> = {
  type: "passive_bearing",
  coverage: {
    range_min_m: 0,
    range_max_m: 150_000,
    azimuth_deg: [0, 360],
    elevation_deg: [-1, 60],
    radar_horizon: true,
  },
  update_period_s: 1,
  noise: { bearing_sigma_deg: 1 },
  detection: { model: "constant", pd: 0.8, false_alarm_rate: 0.5 },
  latency_s: 0,
};

export function uniqueId(base: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  for (let n = 1; ; n++) {
    const id = `${base}-${n}`;
    if (!used.has(id)) return id;
  }
}

/** A new sensor of `type` at a map point; ground height from the template until known. */
export function newSensor(
  scenario: ScenarioDoc,
  type: SensorSpec["type"],
  lat: number,
  lon: number,
): SensorSpec {
  const same = scenario.sensors.find((s) => s.type === type);
  const template = same ?? { ...(type === "rotating_radar_2d" ? DEFAULT_RADAR : DEFAULT_PASSIVE) };
  const ground = same?.position.alt_m ?? scenario.origin?.alt_m ?? scenario.sensors[0]?.position.alt_m ?? 0;
  const mast = same?.position.mast_m ?? 15;
  return structuredClone({
    ...template,
    id: uniqueId(type === "rotating_radar_2d" ? "radar" : "passive", scenario.sensors.map((s) => s.id)),
    type,
    position: { lat: round(lat, 5), lon: round(lon, 5), alt_m: ground, mast_m: mast },
  }) as SensorSpec;
}

const round = (x: number, digits: number) => Math.round(x * 10 ** digits) / 10 ** digits;
