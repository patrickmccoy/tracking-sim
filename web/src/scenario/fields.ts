// The sensor fields the map editor shows, by group.
//
// `scale` converts the stored SI value for display (shown = stored / scale),
// so a range stored in metres can be edited in kilometres. Fields that only
// apply to one sensor type or detection model say so with `when`.

import type { SensorSpec } from "../api";
import { getAt, type Path } from "./paths";

export interface FieldDef {
  path: Path;
  label: string;
  unit?: string;
  scale?: number;
  step?: number;
  min?: number;
  max?: number;
  digits?: number;
  kind?: "number" | "bool" | "select";
  options?: { value: string; label: string }[];
  when?: (sensor: SensorSpec) => boolean;
}

export interface FieldGroup {
  title: string;
  fields: FieldDef[];
}

const radar = (s: SensorSpec) => s.type === "rotating_radar_2d";
const passive = (s: SensorSpec) => s.type === "passive_bearing";
const model = (s: SensorSpec) => getAt(s, ["detection", "model"]);

export const sensorGroups: FieldGroup[] = [
  {
    title: "Position",
    fields: [
      { path: ["position", "lat"], label: "Latitude", unit: "°", step: 0.001, digits: 5, min: -90, max: 90 },
      { path: ["position", "lon"], label: "Longitude", unit: "°", step: 0.001, digits: 5, min: -180, max: 180 },
      { path: ["position", "alt_m"], label: "Ground height", unit: "m", step: 10, digits: 0 },
      { path: ["position", "mast_m"], label: "Mast", unit: "m", step: 1, min: 0, digits: 1 },
    ],
  },
  {
    title: "Coverage",
    fields: [
      { path: ["coverage", "range_max_m"], label: "Max range", unit: "km", scale: 1000, step: 5, min: 0, digits: 1 },
      { path: ["coverage", "range_min_m"], label: "Min range", unit: "m", step: 100, min: 0, digits: 0 },
      { path: ["coverage", "azimuth_deg", 0], label: "Sector from", unit: "°", step: 5, min: 0, max: 360, digits: 1 },
      { path: ["coverage", "azimuth_deg", 1], label: "Sector to", unit: "°", step: 5, min: 0, max: 360, digits: 1 },
      { path: ["coverage", "elevation_deg", 0], label: "Lowest elevation", unit: "°", step: 0.5, min: -90, max: 90, digits: 1 },
      { path: ["coverage", "elevation_deg", 1], label: "Highest elevation", unit: "°", step: 1, min: -90, max: 90, digits: 1 },
      { path: ["coverage", "radar_horizon"], label: "Radar horizon", kind: "bool" },
      { path: ["coverage", "terrain_masking"], label: "Terrain masking", kind: "bool" },
    ],
  },
  {
    title: "Scan",
    fields: [
      { path: ["scan", "period_s"], label: "Rotation period", unit: "s", step: 0.5, min: 0, digits: 2, when: radar },
      { path: ["scan", "beamwidth_deg"], label: "Beamwidth", unit: "°", step: 0.1, min: 0, digits: 2, when: radar },
      { path: ["update_period_s"], label: "Update period", unit: "s", step: 0.5, min: 0, digits: 2, when: passive },
      { path: ["latency_s"], label: "Latency", unit: "s", step: 0.1, min: 0, digits: 2 },
    ],
  },
  {
    title: "Accuracy",
    fields: [
      { path: ["noise", "range_sigma_m"], label: "Range sigma", unit: "m", step: 5, min: 0, digits: 1, when: radar },
      { path: ["noise", "bearing_sigma_deg"], label: "Bearing sigma", unit: "°", step: 0.05, min: 0, digits: 3 },
      { path: ["resolution", "range_m"], label: "Range resolution", unit: "m", step: 10, min: 0, digits: 0, when: radar },
      { path: ["resolution", "bearing_deg"], label: "Bearing resolution", unit: "°", step: 0.1, min: 0, digits: 2, when: radar },
    ],
  },
  {
    title: "Detection",
    fields: [
      {
        path: ["detection", "model"],
        label: "Model",
        kind: "select",
        options: [{ value: "constant", label: "Constant Pd" }],
        // Each type adds its own SNR model below.
      },
      { path: ["detection", "pd"], label: "Pd", step: 0.01, min: 0, max: 1, digits: 3, when: (s) => model(s) === "constant" },
      { path: ["detection", "reference_snr_db"], label: "Reference SNR", unit: "dB", step: 1, digits: 1, when: (s) => model(s) !== "constant" },
      { path: ["detection", "reference_range_m"], label: "at range", unit: "km", scale: 1000, step: 5, min: 0, digits: 1, when: (s) => model(s) !== "constant" },
      { path: ["detection", "pfa"], label: "Pfa", step: 1e-6, min: 0, max: 1, digits: 9, when: (s) => model(s) !== "constant" },
      { path: ["detection", "false_alarm_density"], label: "False alarms", unit: "per km² per scan", scale: 1e-6, step: 0.0001, min: 0, digits: 5, when: radar },
      { path: ["detection", "false_alarm_rate"], label: "False bearings", unit: "per update", step: 0.1, min: 0, digits: 2, when: passive },
    ],
  },
];

export function detectionModels(sensor: SensorSpec): { value: string; label: string }[] {
  return [
    { value: "constant", label: "Constant Pd" },
    radar(sensor)
      ? { value: "radar_equation", label: "Radar equation" }
      : { value: "one_way", label: "One-way link" },
  ];
}

/** Fill in what a newly chosen detection model needs. */
export function withDetectionModel(sensor: SensorSpec, next: string): SensorSpec {
  const detection: Record<string, unknown> = { ...(sensor.detection as Record<string, unknown>), model: next };
  if (next === "constant") detection.pd ??= 0.9;
  else {
    detection.reference_snr_db ??= 13;
    detection.reference_range_m ??= 100_000;
  }
  return { ...sensor, detection };
}
