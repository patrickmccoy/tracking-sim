import { coverageSteps } from "../theme";

interface CoverageLegendProps {
  /** The sensor shown, or null for all of them. */
  sensorId: string | null;
  heights: number[];
  visible: boolean;
  busy: boolean;
  onHeights: (heights: number[]) => void;
  onVisible: (visible: boolean) => void;
}

/** Which coverage heights are drawn; heights are above each sensor's ground. */
export function CoverageLegend({ sensorId, heights, visible, busy, onHeights, onVisible }: CoverageLegendProps) {
  return (
    <div className="map-panel legend">
      <label className="field check">
        <input type="checkbox" checked={visible} onChange={(e) => onVisible(e.target.checked)} />
        <span className="strong">Coverage</span>
        <span className="muted small">{sensorId ?? "all sensors"}</span>
        {busy && <span className="muted small">updating…</span>}
      </label>
      {coverageSteps.map((step) => (
        <label key={step.height_m} className="field check" title={`${step.height_m} m above the sensor's ground`}>
          <input
            type="checkbox"
            disabled={!visible}
            checked={heights.includes(step.height_m)}
            onChange={(e) =>
              onHeights(e.target.checked ? [...heights, step.height_m] : heights.filter((h) => h !== step.height_m))
            }
          />
          <span className="swatch" style={{ background: step.color }} />
          <span>{step.label}</span>
        </label>
      ))}
      <p className="muted small">above the sensor's ground</p>
    </div>
  );
}
