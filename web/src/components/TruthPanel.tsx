// The scenario's traffic: its own truth file or an uploaded capture, and
// the part of it to run. Uploads go to the server, which stores each as a
// recording; picking one points the scenario's truth at it.

import { useEffect, useRef, useState } from "react";
import { CAPTURE_SUFFIXES, isSbs, uploadCapture, type Capture, type ScenarioDoc } from "../api";

type Truth = ScenarioDoc["truth"];

interface TruthPanelProps {
  truth: Truth;
  /** The scenario file's own truth, to go back to. */
  original: Truth;
  captures: Capture[];
  /** The capture `truth` points at, if any. */
  active: Capture | null;
  /** New truth, and the capture picked (null: back to the scenario's own). */
  onChange: (truth: Truth, capture?: Capture | null) => void;
  /** A capture was uploaded (refresh the list). */
  onUploaded: (capture: Capture) => void;
}

const OWN = "";

/** ISO time in UTC as a datetime-local value (seconds, no zone). */
const toInput = (iso: string) => new Date(iso).toISOString().slice(0, 19);
const fromInput = (value: string) => `${value}Z`;

function span(c: Capture): string {
  const minutes = Math.round((Date.parse(c.last) - Date.parse(c.first)) / 60000);
  const length = minutes >= 90 ? `${(minutes / 60).toFixed(1)} h` : `${minutes} min`;
  return `${c.aircraft} aircraft, ${length}`;
}

const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const zones: string[] = (() => {
  try {
    return (Intl as unknown as { supportedValuesOf(k: string): string[] }).supportedValuesOf("timeZone");
  } catch {
    return [browserZone];
  }
})();

export function TruthPanel({ truth, original, captures, active, onChange, onUploaded }: TruthPanelProps) {
  const [file, setFile] = useState<File | null>(null);
  const [tz, setTz] = useState("UTC");
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const cancel = useRef<(() => void) | null>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => () => cancel.current?.(), []);

  const chooseCapture = (c: Capture | null) =>
    onChange(
      c === null
        ? original
        : // A capture is a recording in UTC; its own time zone was applied on upload.
          { ...truth, source: "recording", path: c.truth_path, window: null, tz: "UTC" },
      c,
    );

  const pick = (f: File | null) => {
    setError(null);
    setFile(f);
    if (f && !CAPTURE_SUFFIXES.some((s) => f.name.toLowerCase().endsWith(s))) {
      setError(`Pick a recording or SBS capture (${CAPTURE_SUFFIXES.join(", ")}).`);
    }
  };

  const upload = async () => {
    if (!file) return;
    setError(null);
    setProgress(0);
    const job = uploadCapture(file, tz, setProgress);
    cancel.current = job.cancel;
    try {
      const capture = await job.done;
      onUploaded(capture);
      chooseCapture(capture);
      setFile(null);
      if (input.current) input.current.value = "";
    } catch (e) {
      setError(`Upload: ${(e as Error).message}`);
    } finally {
      cancel.current = null;
      setProgress(null);
    }
  };

  const [start, end] = truth.window ?? [null, null];
  const bounds = active ? { min: toInput(active.first), max: toInput(active.last) } : {};
  const setWindow = (which: 0 | 1, value: string) => {
    const other = truth.window?.[which === 0 ? 1 : 0] ?? (active ? (which === 0 ? active.last : active.first) : null);
    if (!value || !other) return onChange({ ...truth, window: null });
    const pair: [string, string] = which === 0 ? [fromInput(value), other] : [other, fromInput(value)];
    onChange({ ...truth, window: pair });
  };
  const uploading = progress !== null;

  return (
    <div className="truth">
      <label className="field">
        <span>Traffic</span>
        <select
          value={active?.id ?? OWN}
          onChange={(e) => chooseCapture(captures.find((c) => c.id === e.target.value) ?? null)}
        >
          <option value={OWN}>
            {active ? `back to the scenario's own: ${original.path.split("/").pop()}` : truth.path.split("/").pop()}
          </option>
          {captures.length > 0 && (
            <optgroup label="Uploaded captures">
              {captures.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} ({span(c)})
                </option>
              ))}
            </optgroup>
          )}
        </select>
      </label>
      {active && (
        <p className="muted small">
          {span(active)}, {active.first.slice(0, 16).replace("T", " ")} UTC; {active.positions.toLocaleString()} positions
          {active.tz ? `, SBS times read as ${active.tz}` : ""}.
        </p>
      )}

      <div className="window">
        <label className="field">
          <span>From (UTC)</span>
          <input type="datetime-local" step={1} value={start ? toInput(start) : ""} {...bounds} onChange={(e) => setWindow(0, e.target.value)} />
        </label>
        <label className="field">
          <span>To (UTC)</span>
          <input type="datetime-local" step={1} value={end ? toInput(end) : ""} {...bounds} onChange={(e) => setWindow(1, e.target.value)} />
        </label>
      </div>
      <p className="muted small">
        {truth.window ? (
          <>
            Part of the traffic.{" "}
            <button className="link" onClick={() => onChange({ ...truth, window: null })}>
              Use all of it
            </button>
          </>
        ) : (
          "All of the traffic. Set a window to run part of it; long captures take a while."
        )}
      </p>

      <div className="upload">
        <label className="field">
          <span>Upload a capture</span>
          <input
            ref={input}
            type="file"
            accept={CAPTURE_SUFFIXES.join(",")}
            disabled={uploading}
            onChange={(e) => pick(e.target.files?.[0] ?? null)}
          />
        </label>
        <p className="muted small">
          A Dump1090 SBS capture (.sbs, .sbs.gz) or a tracking-sim recording (.parquet, .jsonl).
        </p>
        {file && isSbs(file.name) && (
          <label className="field">
            <span>Time zone of its timestamps</span>
            <input list="time-zones" value={tz} disabled={uploading} onChange={(e) => setTz(e.target.value)} />
            <datalist id="time-zones">
              {zones.map((z) => (
                <option key={z} value={z} />
              ))}
            </datalist>
            <span className="muted small">
              Dump1090 writes the receiver host's clock: UTC if the host runs on UTC, else its zone
              {browserZone !== "UTC" ? ` (this browser's is ${browserZone})` : ""}.
            </span>
          </label>
        )}
        {file && !error && (
          <div className="row actions">
            {uploading ? (
              <>
                <progress max={1} value={progress} aria-label="Upload progress" />
                <span className="muted small">{progress < 1 ? `${Math.round(progress * 100)}%` : "reading…"}</span>
                <button onClick={() => cancel.current?.()}>Cancel</button>
              </>
            ) : (
              <button className="primary" onClick={upload}>
                Upload {(file.size / 2 ** 20).toFixed(file.size < 2 ** 20 ? 2 : 0)} MB
              </button>
            )}
          </div>
        )}
        {error && (
          <p className="error small" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
