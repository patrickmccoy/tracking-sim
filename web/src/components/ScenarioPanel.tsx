import type { ScenarioDoc, TrackerSpec } from "../api";
import { NumberField } from "./NumberField";

interface ScenarioPanelProps {
  draft: ScenarioDoc;
  allTrackers: TrackerSpec[];
  dirty: boolean;
  onChange: (draft: ScenarioDoc) => void;
  onReset: () => void;
  onSave: () => void;
}

const when = (w: [string, string] | null) =>
  w ? w.map((t) => t.slice(0, 16).replace("T", " ")).join(" to ") + " UTC" : "all of it";

export function ScenarioPanel({ draft, allTrackers, dirty, onChange, onReset, onSave }: ScenarioPanelProps) {
  const enabled = new Set(draft.trackers.map((t) => t.id));
  const toggle = (tracker: TrackerSpec, on: boolean) =>
    onChange({
      ...draft,
      // Keep the scenario's own order.
      trackers: allTrackers.filter((t) => (t.id === tracker.id ? on : enabled.has(t.id))),
    });

  return (
    <section>
      <div className="row">
        <label className="field grow">
          <span>Name</span>
          <input value={draft.name} onChange={(e) => onChange({ ...draft, name: e.target.value })} />
        </label>
        <div className="seed">
          <NumberField label="Seed" value={draft.seed} min={0} step={1} digits={0} onChange={(v) => onChange({ ...draft, seed: Math.round(v) })} />
        </div>
      </div>
      <dl className="facts">
        <dt>Truth</dt>
        <dd title={draft.truth.path}>{draft.truth.path.split("/").pop()}</dd>
        <dt>Window</dt>
        <dd>{when(draft.truth.window)}</dd>
        <dt>Terrain</dt>
        <dd>{draft.terrain ? String(draft.terrain.source) : "none (smooth earth)"}</dd>
      </dl>
      <h3>Trackers</h3>
      <ul className="plain checks">
        {allTrackers.map((t) => (
          <li key={t.id}>
            <label className="field check">
              <input type="checkbox" checked={enabled.has(t.id)} onChange={(e) => toggle(t, e.target.checked)} />
              <span>{t.id}</span>
            </label>
          </li>
        ))}
      </ul>
      <div className="row actions">
        <button onClick={onReset} disabled={!dirty} title="Discard edits and reload the scenario file">
          Reset
        </button>
        <button onClick={onSave} title="Download the edited scenario as a YAML file">
          Save as YAML
        </button>
      </div>
    </section>
  );
}
