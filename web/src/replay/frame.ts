// What the map shows at one moment of a replay: pure functions from the
// run's columnar data and a time (POSIX seconds) to GeoJSON.

import type { DetectionData, TrackPath, TruthSegment } from "../api";

/** First index with `times[i] >= t` (times ascending). */
export function lowerBound(times: readonly number[], t: number): number {
  let lo = 0;
  let hi = times.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (times[mid]! < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

interface Path {
  t: readonly number[];
  lat: readonly number[];
  lon: readonly number[];
}

/** Position on a path at `t` (linear between samples), or null outside it. */
export function positionAt(path: Path, t: number): [number, number] | null {
  const n = path.t.length;
  if (!n || t < path.t[0]! || t > path.t[n - 1]!) return null;
  const j = Math.min(Math.max(lowerBound(path.t, t), 1), n - 1);
  const t0 = path.t[j - 1]!;
  const t1 = path.t[j]!;
  const f = t1 > t0 ? (t - t0) / (t1 - t0) : 1;
  return [
    path.lon[j - 1]! + f * (path.lon[j]! - path.lon[j - 1]!),
    path.lat[j - 1]! + f * (path.lat[j]! - path.lat[j - 1]!),
  ];
}

/** The path's points from `t - trail` to `t`, ending at the position at `t`. */
export function trailTo(path: Path, t: number, trail: number): [number, number][] {
  const here = positionAt(path, t);
  if (!here) return [];
  const from = lowerBound(path.t, t - trail);
  const to = lowerBound(path.t, t);
  const points: [number, number][] = [];
  for (let i = from; i < to; i++) points.push([path.lon[i]!, path.lat[i]!]);
  points.push(here);
  return points;
}

type Collection = GeoJSON.FeatureCollection;

function collection(features: GeoJSON.Feature[]): Collection {
  return { type: "FeatureCollection", features };
}

/** Aircraft (points, with properties for labels) and their trails (lines). */
export function truthFrame(segments: readonly TruthSegment[], t: number, trail: number): Collection {
  const features: GeoJSON.Feature[] = [];
  for (const seg of segments) {
    const points = trailTo(seg, t, trail);
    const here = points[points.length - 1];
    if (!here) continue;
    const i = Math.min(lowerBound(seg.t, t), seg.t.length - 1);
    if (points.length > 1) {
      features.push({ type: "Feature", properties: { kind: "trail", id: seg.id }, geometry: { type: "LineString", coordinates: points } });
    }
    features.push({
      type: "Feature",
      properties: {
        kind: "aircraft",
        id: seg.id,
        label: seg.callsign?.trim() || seg.icao24,
        course: seg.course_deg[i] ?? 0,
      },
      geometry: { type: "Point", coordinates: here },
    });
  }
  return collection(features);
}

/** Detections reported in `(t - persist, t]`; `age` is 0 for the newest, 1 for the oldest. */
export function detectionFrame(data: DetectionData, t: number, persist: number): Collection {
  const from = lowerBound(data.t, t - persist);
  const to = lowerBound(data.t, t + 1e-9);
  const features: GeoJSON.Feature[] = [];
  for (let i = from; i < to; i++) {
    features.push({
      type: "Feature",
      properties: {
        kind: data.kind[i],
        sensor: data.sensor_id[i],
        segment: data.segment_id[i],
        age: persist > 0 ? (t - data.t[i]!) / persist : 0,
      },
      geometry: { type: "Point", coordinates: [data.lon[i]!, data.lat[i]!] },
    });
  }
  return collection(features);
}

/** Tracks alive at `t`: head points and trails, tagged with `tracker`. */
export function trackFrame(tracks: readonly TrackPath[], t: number, trail: number, tracker: string): Collection {
  const features: GeoJSON.Feature[] = [];
  for (const track of tracks) {
    const points = trailTo(track, t, trail);
    const here = points[points.length - 1];
    if (!here) continue;
    if (points.length > 1) {
      features.push({
        type: "Feature",
        properties: { kind: "trail", id: track.id, tracker },
        geometry: { type: "LineString", coordinates: points },
      });
    }
    features.push({
      type: "Feature",
      properties: { kind: "head", id: track.id, tracker, segment: track.segment_id },
      geometry: { type: "Point", coordinates: here },
    });
  }
  return collection(features);
}
