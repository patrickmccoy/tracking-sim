import { describe, expect, it } from "vitest";
import type { DetectionData, TruthSegment } from "../api";
import { detectionFrame, lowerBound, positionAt, trackFrame, trailTo, truthFrame } from "./frame";

const seg: TruthSegment = {
  id: "abc-0",
  icao24: "abc",
  callsign: "TEST1  ",
  t: [100, 110, 120],
  lat: [40, 40.1, 40.2],
  lon: [-105, -105, -104.9],
  alt_m: [3000, 3000, 3100],
  speed_mps: [100, 100, 100],
  course_deg: [0, 0, 45],
};

describe("replay frames", () => {
  it("finds times and interpolates positions", () => {
    expect(lowerBound([1, 2, 2, 3], 2)).toBe(1);
    expect(lowerBound([1, 2, 3], 9)).toBe(3);
    expect(positionAt(seg, 105)).toEqual([-105, 40.05]);
    expect(positionAt(seg, 120)).toEqual([-104.9, 40.2]);
    expect(positionAt(seg, 99)).toBeNull();
    expect(positionAt(seg, 121)).toBeNull();
  });

  it("draws a trail ending where the aircraft is", () => {
    const trail = trailTo(seg, 115, 10);
    expect(trail).toHaveLength(2);
    expect(trail[0]).toEqual([-105, 40.1]);
    expect(trail[1]![0]).toBeCloseTo(-104.95, 9);
    expect(trail[1]![1]).toBeCloseTo(40.15, 9);
    const frame = truthFrame([seg], 115, 60);
    const plane = frame.features.find((f) => f.properties?.kind === "aircraft")!;
    expect(plane.properties).toMatchObject({ label: "TEST1", id: "abc-0" });
    expect(truthFrame([seg], 130, 60).features).toHaveLength(0);
  });

  it("shows detections from the last few seconds only", () => {
    const det: DetectionData = {
      t: [100, 104, 108, 112],
      sensor_id: ["r", "r", "r", "r"],
      scan: [0, 0, 1, 1],
      kind: ["target", "clutter", "target", "target"],
      segment_id: ["abc-0", null, "abc-0", "abc-0"],
      lat: [40, 40, 40, 40],
      lon: [-105, -105, -105, -105],
    };
    const frame = detectionFrame(det, 108, 5);
    expect(frame.features.map((f) => f.properties?.kind)).toEqual(["clutter", "target"]);
    expect(frame.features[1]!.properties?.age).toBe(0);
  });

  it("tags tracks with their tracker", () => {
    const track = { ...seg, id: "T1", segment_id: "abc-0", sigma_h_m: [1, 1, 1], confirmed: [true, true, true], updated: [true, true, true], alt_m: [1, 1, 1] };
    const frame = trackFrame([track], 110, 30, "ekf");
    expect(frame.features.map((f) => f.properties?.kind)).toEqual(["trail", "head"]);
    expect(frame.features[1]!.properties).toMatchObject({ tracker: "ekf", segment: "abc-0" });
  });
});
