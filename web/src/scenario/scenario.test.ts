import { describe, expect, it } from "vitest";
import type { ScenarioDoc, SensorSpec } from "../api";
import { withDetectionModel } from "./fields";
import { getAt, setAt } from "./paths";
import { DEFAULT_RADAR, newSensor, uniqueId } from "./templates";

const scenario = (): ScenarioDoc => ({
  name: "s",
  origin: { lat: 39.9, lon: -105.1, alt_m: 1729 },
  truth: { source: "recording", path: "x.parquet", window: null },
  terrain: null,
  sensors: [
    { ...structuredClone(DEFAULT_RADAR), id: "radar-1", position: { lat: 39.9, lon: -105.1, alt_m: 1729, mast_m: 15 } } as SensorSpec,
  ],
  trackers: [{ id: "ekf", filter: "ekf" }],
  seed: 1,
});

describe("paths", () => {
  it("reads and writes nested values without touching the original", () => {
    const a = scenario();
    const b = setAt(a, ["sensors", 0, "coverage", "azimuth_deg", 1], 90);
    expect(getAt(b, ["sensors", 0, "coverage", "azimuth_deg"])).toEqual([0, 90]);
    expect(getAt(a, ["sensors", 0, "coverage", "azimuth_deg"])).toEqual([0, 360]);
    expect(Array.isArray(getAt(b, ["sensors"]))).toBe(true);
    expect(b.sensors[0]!.position).toBe(a.sensors[0]!.position); // untouched branches are shared
    expect(getAt(a, ["nope", "deeper"])).toBeUndefined();
  });
});

describe("new sensors", () => {
  it("copy a sensor of the same type and get the next free id", () => {
    const s = scenario();
    const radar = newSensor(s, "rotating_radar_2d", 40.123456789, -105.2);
    expect(radar.id).toBe("radar-2");
    expect(radar.position).toEqual({ lat: 40.12346, lon: -105.2, alt_m: 1729, mast_m: 15 });
    expect(radar.coverage).not.toBe(s.sensors[0]!.coverage);
    const passive = newSensor(s, "passive_bearing", 40, -105);
    expect(passive.id).toBe("passive-1");
    expect(passive.update_period_s).toBe(1);
    expect(uniqueId("radar", ["radar-1", "radar-3"])).toBe("radar-2");
  });

  it("switching the detection model fills in what it needs", () => {
    const radar = scenario().sensors[0]!;
    const snr = withDetectionModel(radar, "radar_equation");
    expect(snr.detection).toMatchObject({ model: "radar_equation", reference_snr_db: 13, pd: 0.9 });
  });
});
