import { useMemo } from "react";
import type { DetectionData, TrackData, TruthData } from "../api";
import { GeoJsonLayer } from "../map/GeoJsonLayer";
import { colors } from "../theme";
import { detectionFrame, trackFrame, truthFrame } from "./frame";

export interface Selection {
  kind: "truth" | "track";
  id: string;
  tracker?: string;
}

export interface LayerOptions {
  truth: boolean;
  targets: boolean;
  clutter: boolean;
  tracks: boolean;
  /** Seconds of history drawn behind aircraft and tracks. */
  trailS: number;
  /** Seconds a detection stays on screen. */
  persistS: number;
}

interface ReplayLayersProps {
  truth: TruthData | null;
  detections: DetectionData | null;
  tracks: { tracker: string; data: TrackData | undefined; color: string }[];
  t: number;
  options: LayerOptions;
  selection: Selection | null;
  onSelect: (selection: Selection) => void;
}

const empty: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

export function ReplayLayers({ truth, detections, tracks, t, options, selection, onSelect }: ReplayLayersProps) {
  const truthData = useMemo(
    () => (truth && options.truth ? truthFrame(truth.segments, t, options.trailS) : empty),
    [truth, t, options.truth, options.trailS],
  );
  const detectionData = useMemo(
    () => (detections ? detectionFrame(detections, t, options.persistS) : empty),
    [detections, t, options.persistS],
  );
  const trackData = useMemo(() => {
    const features = tracks.flatMap((s) => (s.data ? trackFrame(s.data.tracks, t, options.trailS, s.tracker).features : []));
    return { type: "FeatureCollection", features } as GeoJSON.FeatureCollection;
  }, [tracks, t, options.trailS]);

  // A match needs at least one case; with none there is nothing to colour.
  const trackerColor = useMemo(
    () =>
      tracks.length
        ? ["match", ["get", "tracker"], ...tracks.flatMap((s) => [s.tracker, s.color]), colors.trackA]
        : colors.trackA,
    [tracks],
  );
  const selectedTruth = selection?.kind === "truth" ? selection.id : "";
  const selectedTrack = selection?.kind === "track" ? selection.id : "";

  const kinds = [
    ...(options.targets ? ["target"] : []),
    ...(options.clutter ? ["clutter"] : []),
  ];

  return (
    <>
      <GeoJsonLayer
        id="detections"
        data={detectionData}
        filter={["in", ["get", "kind"], ["literal", kinds]] as never}
        layers={[
          {
            id: "detections-dot",
            type: "circle",
            paint: {
              "circle-radius": ["match", ["get", "kind"], "target", 3, 2],
              "circle-color": ["match", ["get", "kind"], "target", colors.detection, colors.clutter],
              "circle-opacity": ["-", 1, ["*", 0.7, ["get", "age"]]],
            },
          },
        ]}
      />
      <GeoJsonLayer
        id="truth"
        data={truthData}
        onClick={(f) => onSelect({ kind: "truth", id: String(f.properties?.id) })}
        layers={[
          {
            id: "truth-trail",
            type: "line",
            filter: ["==", ["get", "kind"], "trail"],
            paint: { "line-color": colors.truth, "line-width": 1.5, "line-opacity": 0.55 },
          },
          {
            id: "truth-aircraft",
            type: "circle",
            filter: ["==", ["get", "kind"], "aircraft"],
            paint: {
              "circle-radius": ["case", ["==", ["get", "id"], selectedTruth], 6, 4],
              "circle-color": ["case", ["==", ["get", "id"], selectedTruth], colors.sensorSelected, colors.truth],
              "circle-stroke-color": colors.halo,
              "circle-stroke-width": 1.5,
            },
          },
          {
            id: "truth-label",
            type: "symbol",
            minzoom: 8.5,
            filter: ["==", ["get", "kind"], "aircraft"],
            layout: {
              "text-field": ["get", "label"],
              "text-size": 11,
              "text-offset": [0.8, 0],
              "text-anchor": "left",
              "text-optional": true,
            },
            paint: { "text-color": colors.truthLabel, "text-halo-color": colors.halo, "text-halo-width": 1.5 },
          },
        ]}
      />
      <GeoJsonLayer
        id="tracks"
        data={trackData}
        visible={options.tracks}
        onClick={(f) => onSelect({ kind: "track", id: String(f.properties?.id), tracker: String(f.properties?.tracker) })}
        layers={[
          {
            id: "tracks-trail",
            type: "line",
            filter: ["==", ["get", "kind"], "trail"],
            paint: { "line-color": trackerColor as never, "line-width": 2, "line-opacity": 0.85 },
          },
          {
            id: "tracks-head",
            type: "circle",
            filter: ["==", ["get", "kind"], "head"],
            paint: {
              "circle-radius": ["case", ["==", ["get", "id"], selectedTrack], 7, 5],
              "circle-color": colors.halo,
              "circle-stroke-color": trackerColor as never,
              "circle-stroke-width": 2.5,
            },
          },
        ]}
      />
    </>
  );
}
