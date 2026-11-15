// Where an uploaded capture's traffic is: a sample of its positions as faint
// dots, so sensors can be placed under it. The map moves to it when it is
// picked.

import { useEffect, useMemo, useState } from "react";
import { api, type Capture, type CapturePreview } from "../api";
import { colors } from "../theme";
import { GeoJsonLayer } from "./GeoJsonLayer";
import { useMap } from "./MapView";

export function TrafficLayer({ capture }: { capture: Capture }) {
  const map = useMap();
  const [preview, setPreview] = useState<CapturePreview | null>(null);

  useEffect(() => {
    let live = true;
    setPreview(null);
    api
      .capturePreview(capture.id)
      .then((p) => live && setPreview(p))
      .catch((e: Error) => console.warn("capture preview:", e.message));
    return () => {
      live = false;
    };
  }, [capture.id]);

  useEffect(() => {
    const [west, south, east, north] = capture.bbox;
    map.fitBounds(
      [
        [west, south],
        [east, north],
      ],
      { padding: 60, maxZoom: 10, duration: 600 },
    );
  }, [map, capture.id]);

  const data = useMemo<GeoJSON.FeatureCollection>(
    () => ({
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: {},
          geometry: {
            type: "MultiPoint",
            coordinates: preview ? preview.lat.map((lat, i) => [preview.lon[i]!, lat]) : [],
          },
        },
      ],
    }),
    [preview],
  );

  return (
    <GeoJsonLayer
      id="traffic"
      data={data}
      before="coverage-fill"
      layers={[
        {
          id: "traffic-dots",
          type: "circle",
          paint: { "circle-radius": 1.5, "circle-color": colors.truth, "circle-opacity": 0.35 },
        },
      ]}
    />
  );
}
