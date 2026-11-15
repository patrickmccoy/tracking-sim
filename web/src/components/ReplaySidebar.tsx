import type { RunDetail, TrackData, TruthData } from "../api";
import { positionAt } from "../replay/frame";
import type { LayerOptions, Selection } from "../replay/ReplayLayers";
import { lowerBound } from "../replay/frame";

interface ReplaySidebarProps {
  detail: RunDetail;
  tracker: string;
  onTracker: (tracker: string) => void;
  options: LayerOptions;
  onOptions: (options: LayerOptions) => void;
  selection: Selection | null;
  truth: TruthData | null;
  tracks: Record<string, TrackData>;
  t: number;
  onClose: () => void;
  onEdit: () => void;
}

const fmt = (v: unknown, digits = 0) => (typeof v === "number" ? v.toFixed(digits) : "–");

export function ReplaySidebar(props: ReplaySidebarProps) {
  const { detail, tracker, onTracker, options, onOptions, selection, truth, tracks, t, onClose, onEdit } = props;
  const scenario = detail.manifest.scenario;
  const toggle = (key: keyof LayerOptions) => (e: React.ChangeEvent<HTMLInputElement>) =>
    onOptions({ ...options, [key]: e.target.checked });

  return (
    <>
      <div className="row actions">
        <button onClick={onClose}>← Back to editing</button>
        <button onClick={onEdit} title="Load this run's scenario into the editor">
          Edit this run
        </button>
      </div>
      <h2>Run</h2>
      <dl className="facts">
        <dt>Id</dt>
        <dd title={detail.id}>{detail.id}</dd>
        <dt>Scenario</dt>
        <dd>
          {scenario.name}, seed {scenario.seed}
        </dd>
        <dt>Sensors</dt>
        <dd>{detail.sensors.join(", ")}</dd>
        <dt>Created</dt>
        <dd>{detail.created.replace("T", " ").slice(0, 16)}</dd>
      </dl>

      <h2>Trackers</h2>
      <table className="scores">
        <thead>
          <tr>
            <th />
            <th>tracker</th>
            <th title="Mean OSPA, metres: lower is better">OSPA m</th>
            <th title="SIAP completeness: share of truth tracked">compl.</th>
            <th title="Tracks the tracker made">tracks</th>
          </tr>
        </thead>
        <tbody>
          {detail.metrics.trackers.map((s) => (
            <tr key={s.tracker} className={s.tracker === tracker ? "active" : ""} onClick={() => onTracker(s.tracker)}>
              <td>
                <input type="radio" name="tracker" checked={s.tracker === tracker} onChange={() => onTracker(s.tracker)} />
              </td>
              <td>{s.tracker}</td>
              <td className="num">{fmt(s.ospa_m)}</td>
              <td className="num">{fmt(s.completeness, 2)}</td>
              <td className="num">{fmt(s.tracks)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>Show</h2>
      <div className="grid2">
        <label className="field check">
          <input type="checkbox" checked={options.truth} onChange={toggle("truth")} />
          <span className="swatch truth" /> Truth
        </label>
        <label className="field check">
          <input type="checkbox" checked={options.tracks} onChange={toggle("tracks")} />
          <span className="swatch track" /> Tracks
        </label>
        <label className="field check">
          <input type="checkbox" checked={options.targets} onChange={toggle("targets")} />
          <span className="swatch dot detection" /> Detections
        </label>
        <label className="field check">
          <input type="checkbox" checked={options.clutter} onChange={toggle("clutter")} />
          <span className="swatch dot clutter" /> False alarms
        </label>
        <label className="field">
          <span>Trails</span>
          <select value={options.trailS} onChange={(e) => onOptions({ ...options, trailS: Number(e.target.value) })}>
            {[0, 30, 60, 120, 300, 600].map((s) => (
              <option key={s} value={s}>
                {s ? `${s / 60 >= 1 ? `${s / 60} min` : `${s} s`}` : "none"}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Detections last</span>
          <select value={options.persistS} onChange={(e) => onOptions({ ...options, persistS: Number(e.target.value) })}>
            {[5, 15, 30, 60].map((s) => (
              <option key={s} value={s}>
                {s} s
              </option>
            ))}
          </select>
        </label>
      </div>

      {selection && <SelectionDetails selection={selection} truth={truth} tracks={tracks} t={t} />}
    </>
  );
}

function SelectionDetails({ selection, truth, tracks, t }: { selection: Selection; truth: TruthData | null; tracks: Record<string, TrackData>; t: number }) {
  if (selection.kind === "truth") {
    const seg = truth?.segments.find((s) => s.id === selection.id);
    if (!seg) return null;
    const i = Math.min(lowerBound(seg.t, t), seg.t.length - 1);
    const alt = seg.alt_m[i];
    return (
      <>
        <h2>Aircraft</h2>
        <dl className="facts">
          <dt>Callsign</dt>
          <dd>{seg.callsign?.trim() || "–"}</dd>
          <dt>ICAO</dt>
          <dd>{seg.icao24}</dd>
          <dt>Segment</dt>
          <dd>{seg.id}</dd>
          <dt>Altitude</dt>
          <dd>{typeof alt === "number" ? `${Math.round(alt / 0.3048).toLocaleString()} ft (${Math.round(alt)} m)` : "unknown"}</dd>
          <dt>Speed</dt>
          <dd>{fmt((seg.speed_mps[i] ?? NaN) / 0.514444)} kt</dd>
          <dt>Course</dt>
          <dd>{fmt(seg.course_deg[i])}°</dd>
          <dt>In view</dt>
          <dd>{positionAt(seg, t) ? "yes" : "not now"}</dd>
        </dl>
      </>
    );
  }
  const track = tracks[selection.tracker ?? ""]?.tracks.find((k) => k.id === selection.id);
  if (!track) return null;
  const i = Math.min(lowerBound(track.t, t), track.t.length - 1);
  return (
    <>
      <h2>Track</h2>
      <dl className="facts">
        <dt>Track</dt>
        <dd>{track.id}</dd>
        <dt>Tracker</dt>
        <dd>{selection.tracker}</dd>
        <dt>Mostly fed by</dt>
        <dd>{track.segment_id ?? "false alarms"}</dd>
        <dt>Speed</dt>
        <dd>{fmt((track.speed_mps[i] ?? NaN) / 0.514444)} kt</dd>
        <dt>Course</dt>
        <dd>{fmt(track.course_deg[i])}°</dd>
        <dt>Position sigma</dt>
        <dd>{fmt(track.sigma_h_m[i])} m</dd>
        <dt>Lifetime</dt>
        <dd>{Math.round((track.t[track.t.length - 1]! - track.t[0]!) / 60)} min</dd>
      </dl>
    </>
  );
}
