import { useEffect, useState } from "react";

interface NumberFieldProps {
  label: string;
  value: number | undefined;
  onChange: (value: number) => void;
  unit?: string;
  /** Shown value = stored value / scale. */
  scale?: number;
  step?: number;
  min?: number;
  max?: number;
  digits?: number;
  error?: string;
}

const show = (v: number | undefined, scale: number, digits: number) =>
  v === undefined || v === null ? "" : String(Number((v / scale).toFixed(digits)));

/** A number input that lets you type freely and commits only valid numbers. */
export function NumberField({ label, value, onChange, unit, scale = 1, step, min, max, digits = 6, error }: NumberFieldProps) {
  const [text, setText] = useState(show(value, scale, digits));
  const [focused, setFocused] = useState(false);

  // Follow outside changes (a dragged marker) unless the user is typing here.
  useEffect(() => {
    if (!focused) setText(show(value, scale, digits));
  }, [value, scale, digits, focused]);

  const parsed = text.trim() === "" ? NaN : Number(text);
  const invalid =
    Number.isNaN(parsed) || (min !== undefined && parsed * scale < min) || (max !== undefined && parsed * scale > max);

  return (
    <label className={`field number ${invalid || error ? "invalid" : ""}`} title={error}>
      <span>{label}</span>
      <span className="input-unit">
        <input
          type="number"
          inputMode="decimal"
          value={text}
          step={step === undefined ? "any" : step / scale}
          onFocus={() => setFocused(true)}
          onBlur={() => {
            setFocused(false);
            setText(show(value, scale, digits));
          }}
          onChange={(e) => {
            setText(e.target.value);
            const next = Number(e.target.value);
            if (e.target.value.trim() !== "" && !Number.isNaN(next)) {
              const stored = next * scale;
              if ((min === undefined || stored >= min) && (max === undefined || stored <= max)) onChange(stored);
            }
          }}
        />
        {unit && <span className="unit">{unit}</span>}
      </span>
    </label>
  );
}
