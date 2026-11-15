import type { ExpressionSpecification, FilterSpecification } from "maplibre-gl";
import { coverageSteps } from "../theme";
import { GeoJsonLayer } from "./GeoJsonLayer";

const color: ExpressionSpecification = [
  "match",
  ["get", "height_m"],
  ...coverageSteps.flatMap((s) => [s.height_m, s.color]),
  "#888888",
] as unknown as ExpressionSpecification;

interface CoverageLayerProps {
  data: GeoJSON.FeatureCollection;
  heights: number[];
  visible: boolean;
  /** Only this sensor's coverage, or every sensor's when null. */
  sensorId: string | null;
}

/** Coverage outlines, lowest height on top (darkest and smallest). */
export function CoverageLayer({ data, heights, visible, sensorId }: CoverageLayerProps) {
  const byHeight = ["in", ["get", "height_m"], ["literal", heights]];
  const filter = (
    sensorId === null ? byHeight : ["all", byHeight, ["==", ["get", "sensor"], sensorId]]
  ) as unknown as FilterSpecification;
  return (
    <GeoJsonLayer
      id="coverage"
      data={data}
      visible={visible}
      filter={filter}
      layers={[
        // Terrain splits coverage into many one-degree sectors: outlines would
        // stripe it, and anti-aliasing would show their seams.
        { id: "coverage-fill", type: "fill", paint: { "fill-color": color, "fill-opacity": 0.16, "fill-antialias": false } },
      ]}
    />
  );
}
