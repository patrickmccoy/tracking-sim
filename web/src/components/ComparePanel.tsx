import type { RunDetail, RunRow, TrackerScoreRow } from "../api";

export interface CompareChoice {
  runId: string;
  tracker: string;
}

interface ComparePanelProps {
  runs: RunRow[];
  a: { detail: RunDetail; tracker: string };
  b: { runId: string; detail: RunDetail | null; tracker: string };
  onA: (tracker: string) => void;
  onB: (choice: CompareChoice) => void;
  onSwap: () => void;
  colors: [string, string];
}

// What each row is, and which way is better.
const ROWS: { key: string; label: string; digits: number; better: "lower" | "higher" | null; title: string }[] = [
  { key: "ospa_m", label: "OSPA (m)", digits: 0, better: "lower", title: "Mean OSPA: overall error, metres" },
  { key: "gospa_localisation", label: "GOSPA: position", digits: 0, better: "lower", title: "GOSPA from position error" },
  { key: "gospa_missed", label: "GOSPA: missed", digits: 0, better: "lower", title: "GOSPA from aircraft with no track" },
  { key: "gospa_false", label: "GOSPA: false", digits: 0, better: "lower", title: "GOSPA from tracks with no aircraft" },
  { key: "completeness", label: "Completeness", digits: 2, better: "higher", title: "Share of visible truth that is tracked" },
  { key: "ambiguity", label: "Ambiguity", digits: 2, better: "lower", title: "Tracks per tracked aircraft (1 is ideal)" },
  { key: "spuriousness", label: "Spuriousness", digits: 2, better: "lower", title: "Share of tracks following nothing" },
  { key: "position_accuracy_m", label: "Position accuracy (m)", digits: 0, better: "lower", title: "Mean position error of assigned tracks" },
  { key: "tracks", label: "Tracks", digits: 0, better: null, title: "Tracks made (fewer, longer tracks is usually better)" },
  { key: "median_latency_s", label: "Latency (s)", digits: 0, better: "lower", title: "Median time from first chance to a confirmed track" },
  { key: "run_s", label: "Run time (s)", digits: 1, better: "lower", title: "Tracker wall-clock time" },
];

const value = (row: TrackerScoreRow | undefined, key: string) => {
  const v = row?.[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
};

export function ComparePanel({ runs, a, b, onA, onB, onSwap, colors }: ComparePanelProps) {
  const rowA = a.detail.metrics.trackers.find((s) => s.tracker === a.tracker);
  const rowB = b.detail?.metrics.trackers.find((s) => s.tracker === b.tracker);
  const sameRun = b.runId === a.detail.id;

  return (
    <section>
      <div className="compare-pick">
        <span className="swatch line" style={{ background: colors[0] }} />
        <label className="field grow">
          <span>A: this run</span>
          <select value={a.tracker} onChange={(e) => onA(e.target.value)}>
            {a.detail.metrics.trackers.map((s) => (
              <option key={s.tracker}>{s.tracker}</option>
            ))}
          </select>
        </label>
      </div>
      <div className="compare-pick">
        <span className="swatch line" style={{ background: colors[1] }} />
        <div className="grow">
          <label className="field">
            <span>B: run</span>
            <select value={b.runId} onChange={(e) => onB({ runId: e.target.value, tracker: "" })}>
              <option value={a.detail.id}>this run</option>
              {runs
                .filter((r) => r.id !== a.detail.id)
                .map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.scenario} · seed {r.seed} · {r.created.slice(5, 16).replace("T", " ")}
                  </option>
                ))}
            </select>
          </label>
          <label className="field">
            <span>B: tracker</span>
            <select value={b.tracker} onChange={(e) => onB({ runId: b.runId, tracker: e.target.value })} disabled={!b.detail}>
              {(b.detail?.metrics.trackers ?? []).map((s) => (
                <option key={s.tracker}>{s.tracker}</option>
              ))}
            </select>
          </label>
        </div>
      </div>
      <div className="row actions">
        <button onClick={onSwap} disabled={!sameRun} title={sameRun ? "Swap A and B" : "Swapping needs both from this run"}>
          Swap A and B
        </button>
      </div>
      {!sameRun && (
        <p className="muted small">
          B is another run: its sensors and detections differ, so the maps can differ in more than the tracker.
        </p>
      )}
      <table className="scores compare-table">
        <thead>
          <tr>
            <th>metric</th>
            <th className="num">A</th>
            <th className="num">B</th>
          </tr>
        </thead>
        <tbody>
          {ROWS.map((r) => {
            // Compare what is shown: equal-looking values are a tie.
            const shown = (v: number | null) => (v === null ? null : Number(v.toFixed(r.digits)));
            const va = shown(value(rowA, r.key));
            const vb = shown(value(rowB, r.key));
            const win =
              r.better && va !== null && vb !== null && va !== vb
                ? (r.better === "lower") === va < vb
                  ? "a"
                  : "b"
                : null;
            return (
              <tr key={r.key} title={r.title}>
                <td>{r.label}</td>
                <td className={`num ${win === "a" ? "better" : ""}`}>
                  {va === null ? "–" : va.toFixed(r.digits)}
                  {win === "a" && <span className="sr-only"> (better)</span>}
                </td>
                <td className={`num ${win === "b" ? "better" : ""}`}>
                  {vb === null ? "–" : vb.toFixed(r.digits)}
                  {win === "b" && <span className="sr-only"> (better)</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted small">Bold: the better of the two.</p>
    </section>
  );
}
