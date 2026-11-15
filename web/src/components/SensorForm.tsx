import type { SensorSpec } from "../api";
import { detectionModels, sensorGroups, withDetectionModel, type FieldDef } from "../scenario/fields";
import { getAt, setAt } from "../scenario/paths";
import { NumberField } from "./NumberField";

interface SensorFormProps {
  sensor: SensorSpec;
  takenIds: string[];
  onChange: (sensor: SensorSpec) => void;
  onDelete: () => void;
  /** Server validation messages by dotted field path, e.g. "scan.period_s". */
  errors: Record<string, string>;
}

export function SensorForm({ sensor, takenIds, onChange, onDelete, errors }: SensorFormProps) {
  const idTaken = takenIds.includes(sensor.id);
  const field = (f: FieldDef) => {
    if (f.when && !f.when(sensor)) return null;
    const key = f.path.join(".");
    const value = getAt(sensor, f.path);
    if (f.kind === "bool") {
      return (
        <label key={key} className="field check">
          <input
            type="checkbox"
            checked={value === undefined ? true : Boolean(value)}
            onChange={(e) => onChange(setAt(sensor, f.path, e.target.checked))}
          />
          <span>{f.label}</span>
        </label>
      );
    }
    if (f.kind === "select") {
      return (
        <label key={key} className="field">
          <span>{f.label}</span>
          <select value={String(value ?? "constant")} onChange={(e) => onChange(withDetectionModel(sensor, e.target.value))}>
            {detectionModels(sensor).map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
      );
    }
    return (
      <NumberField
        key={key}
        label={f.label}
        unit={f.unit}
        scale={f.scale}
        step={f.step}
        min={f.min}
        max={f.max}
        digits={f.digits}
        value={typeof value === "number" ? value : undefined}
        error={errors[key]}
        onChange={(v) => onChange(setAt(sensor, f.path, v))}
      />
    );
  };

  return (
    <div className="sensor-form">
      <div className="row">
        <label className={`field grow ${idTaken || !sensor.id ? "invalid" : ""}`}>
          <span>Id {idTaken && "(already used)"}</span>
          <input value={sensor.id} onChange={(e) => onChange({ ...sensor, id: e.target.value.trim() })} />
        </label>
        <button className="danger" onClick={onDelete} title="Remove this sensor from the scenario">
          Remove
        </button>
      </div>
      <p className="muted small">{sensor.type === "rotating_radar_2d" ? "Rotating 2D radar" : "Passive bearing-only sensor"}</p>
      {sensorGroups.map((group) => {
        const shown = group.fields.map(field).filter(Boolean);
        return shown.length ? (
          <fieldset key={group.title}>
            <legend>{group.title}</legend>
            <div className="grid2">{shown}</div>
          </fieldset>
        ) : null;
      })}
      {Object.entries(errors).filter(([k]) => !sensorGroups.some((g) => g.fields.some((f) => f.path.join(".") === k))).map(([k, msg]) => (
        <p key={k} className="error small">
          {k ? `${k}: ` : ""}
          {msg}
        </p>
      ))}
    </div>
  );
}
