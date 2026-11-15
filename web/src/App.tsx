import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Config, type ScenarioDoc, type ScenarioEntry, type ScenarioResponse, type SensorSpec } from "./api";
import { CoverageLegend } from "./components/CoverageLegend";
import { ReplaySidebar } from "./components/ReplaySidebar";
import { RunPanel } from "./components/RunPanel";
import { ScenarioPanel } from "./components/ScenarioPanel";
import { SensorForm } from "./components/SensorForm";
import { CoverageLayer } from "./map/CoverageLayer";
import { MapClick } from "./map/MapClick";
import { MapView } from "./map/MapView";
import { SensorMarkers } from "./map/SensorMarkers";
import { useCoverage } from "./map/useCoverage";
import { ReplayLayers, type LayerOptions, type Selection } from "./replay/ReplayLayers";
import { TimeBar } from "./replay/TimeBar";
import { useClock } from "./replay/useClock";
import { useReplayData } from "./replay/useReplayData";
import { newSensor } from "./scenario/templates";
import { colors, coverageSteps } from "./theme";

type SensorType = SensorSpec["type"];

/** One scan of the first radar: the replay's step. */
function scanPeriod(sensors: SensorSpec[]): number {
  for (const s of sensors) {
    const period = (s.scan as { period_s?: number } | undefined)?.period_s;
    if (typeof period === "number") return period;
  }
  return 5;
}

export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [scenarios, setScenarios] = useState<ScenarioEntry[]>([]);
  const [key, setKey] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<ScenarioResponse | null>(null);
  const [draft, setDraft] = useState<ScenarioDoc | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [placing, setPlacing] = useState<SensorType | null>(null);
  const [heights, setHeights] = useState(coverageSteps.map((s) => s.height_m));
  const [showCoverage, setShowCoverage] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Replay: the open run, the tracker shown, and what is drawn.
  const [runId, setRunId] = useState<string | null>(null);
  const [tracker, setTracker] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [layerOptions, setLayerOptions] = useState<LayerOptions>({
    truth: true,
    targets: true,
    clutter: false,
    tracks: true,
    trailS: 120,
    persistS: 5,
  });

  useEffect(() => {
    Promise.all([api.config(), api.scenarios()])
      .then(([c, s]) => {
        setConfig(c);
        setScenarios(s);
        setKey((k) => k ?? s[0]?.key ?? null);
      })
      .catch((e: Error) => setError(`Can't reach the tracking-sim server: ${e.message}`));
  }, []);

  const load = useCallback((k: string) => {
    api
      .scenario(k)
      .then((r) => {
        setLoaded(r);
        setDraft(structuredClone(r.scenario));
        setSelected(null);
        setPlacing(null);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    if (key) load(key);
  }, [key, load]);

  const openRun = (id: string) => {
    setRunId(id);
    setPlacing(null);
    setShowCoverage(false);
  };

  const replay = useReplayData(runId, tracker ? [tracker] : []);
  const runScenario = replay.detail?.manifest.scenario ?? null;
  const window = replay.truth?.window ?? null;
  const clock = useClock(window);
  useEffect(() => {
    setSelection(null);
    setTracker(null);
  }, [runId]);
  useEffect(() => {
    if (replay.detail && !tracker) setTracker(replay.detail.metrics.trackers[0]?.tracker ?? null);
  }, [replay.detail, tracker]);

  /** The open run's scenario, as the editor holds one (its truth path is absolute). */
  const editRun = () => {
    if (!runScenario || !runId) return;
    const { frame, truth_path, ...rest } = runScenario as typeof runScenario & { truth_path: string };
    const doc = { ...rest, truth: { ...rest.truth, path: truth_path } } as ScenarioDoc;
    setLoaded({ key: `run:${runId}`, scenario: doc, frame });
    setDraft(structuredClone(doc));
    setKey(null);
    setSelected(null);
    setRunId(null);
  };

  const dirty = useMemo(
    () => !!loaded && !!draft && JSON.stringify(loaded.scenario) !== JSON.stringify(draft),
    [loaded, draft],
  );
  const replaying = runId !== null;
  const mapSensors = replaying ? (runScenario?.sensors ?? []) : (draft?.sensors ?? []);
  const coverage = useCoverage(mapSensors, (replaying ? runScenario?.terrain : draft?.terrain) ?? null);

  const updateSensor = (index: number, sensor: SensorSpec) =>
    setDraft((d) => d && { ...d, sensors: d.sensors.map((s, i) => (i === index ? sensor : s)) });

  // The ground under a moved or dropped sensor, when the scenario has terrain.
  const groundUnder = async (index: number, lat: number, lon: number) => {
    if (!draft?.terrain) return;
    try {
      const { heights_m } = await api.elevation(draft.terrain, [[lat, lon]]);
      const h = heights_m[0];
      if (h === null || h === undefined) return;
      setDraft(
        (d) =>
          d && {
            ...d,
            sensors: d.sensors.map((s, i) => (i === index ? { ...s, position: { ...s.position, alt_m: h } } : s)),
          },
      );
    } catch (e) {
      setError(`Ground height: ${(e as Error).message}`);
    }
  };

  const moveSensor = (index: number, lat: number, lon: number, done: boolean) => {
    const r = (x: number) => Math.round(x * 1e5) / 1e5;
    setDraft(
      (d) =>
        d && {
          ...d,
          sensors: d.sensors.map((s, i) => (i === index ? { ...s, position: { ...s.position, lat: r(lat), lon: r(lon) } } : s)),
        },
    );
    if (done) void groundUnder(index, lat, lon);
  };

  const place = (lat: number, lon: number) => {
    if (!draft || !placing) return;
    const sensor = newSensor(draft, placing, lat, lon);
    const index = draft.sensors.length;
    setDraft({ ...draft, sensors: [...draft.sensors, sensor] });
    setSelected(index);
    setPlacing(null);
    void groundUnder(index, sensor.position.lat, sensor.position.lon);
  };

  const removeSensor = (index: number) => {
    setDraft((d) => d && { ...d, sensors: d.sensors.filter((_, i) => i !== index) });
    setSelected(null);
  };

  const save = async () => {
    if (!draft) return;
    try {
      const blob = await api.exportScenario(draft);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${draft.name.replace(/[^A-Za-z0-9_.-]+/g, "-") || "scenario"}.yaml`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(`Can't save: ${(e as Error).message}`);
    }
  };

  const sensor = selected === null ? null : draft?.sensors[selected] ?? null;
  const radars = draft?.sensors.filter((s) => s.type === "rotating_radar_2d").length ?? 0;

  return (
    <div className="app">
      <header className="topbar">
        <h1>tracking-sim</h1>
        <label className="field inline">
          <span>Scenario</span>
          <select value={key ?? ""} onChange={(e) => setKey(e.target.value)} disabled={replaying}>
            {key === null && <option value="">{loaded?.key.startsWith("run:") ? `from run ${loaded.key.slice(4, 30)}…` : "…"}</option>}
            {scenarios.map((s) => (
              <option key={s.key} value={s.key}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        {replaying ? <span className="badge">replay</span> : dirty && <span className="badge">edited</span>}
        {config && <span className="muted version">v{config.version}</span>}
      </header>
      <aside className="sidebar">
        {error && (
          <p className="error" role="alert">
            {error} <button className="link" onClick={() => setError(null)}>dismiss</button>
          </p>
        )}
        {replaying && replay.error && <p className="error">{replay.error}</p>}
        {replaying && replay.loading && <p className="muted" role="status">Loading {replay.loading}…</p>}
        {replaying && replay.detail && tracker && (
          <ReplaySidebar
            detail={replay.detail}
            tracker={tracker}
            onTracker={setTracker}
            options={layerOptions}
            onOptions={setLayerOptions}
            selection={selection}
            truth={replay.truth}
            tracks={replay.tracks}
            t={clock.t}
            onClose={() => setRunId(null)}
            onEdit={editRun}
          />
        )}
        {!replaying && draft && loaded && (
          <>
            <h2>Run</h2>
            <RunPanel draft={draft} canRun={radars > 0 || draft.trackers.length === 0} onOpen={openRun} />
            <h2>Scenario</h2>
            <ScenarioPanel
              draft={draft}
              allTrackers={loaded.scenario.trackers}
              dirty={dirty}
              onChange={setDraft}
              onReset={() => (key ? load(key) : loaded.key.startsWith("run:") && setDraft(structuredClone(loaded.scenario)))}
              onSave={save}
            />
            <h2>Sensors</h2>
            <ul className="plain sensor-list">
              {draft.sensors.map((s, i) => (
                <li key={i}>
                  <button className={`list-item ${i === selected ? "active" : ""}`} onClick={() => setSelected(i === selected ? null : i)}>
                    <span>{s.id || "(no id)"}</span>
                    <span className="muted small">
                      {s.type === "rotating_radar_2d" ? "radar" : "passive"}
                      {coverage.errors[s.id] ? " · needs fixing" : ""}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <div className="row actions">
              <button className={placing === "rotating_radar_2d" ? "primary" : ""} onClick={() => setPlacing(placing === "rotating_radar_2d" ? null : "rotating_radar_2d")}>
                + Radar
              </button>
              <button className={placing === "passive_bearing" ? "primary" : ""} onClick={() => setPlacing(placing === "passive_bearing" ? null : "passive_bearing")}>
                + Passive sensor
              </button>
            </div>
            {radars === 0 && draft.trackers.length > 0 && (
              <p className="error small">Trackers need at least one radar.</p>
            )}
            {sensor && selected !== null && (
              <>
                <h2>{sensor.id || "Sensor"}</h2>
                <SensorForm
                  sensor={sensor}
                  takenIds={draft.sensors.filter((_, i) => i !== selected).map((s) => s.id)}
                  errors={coverage.errors[sensor.id] ?? {}}
                  onChange={(s) => updateSensor(selected, s)}
                  onDelete={() => removeSensor(selected)}
                />
              </>
            )}
          </>
        )}
      </aside>
      <main className="main">
        {config && loaded && draft && (
          <MapView
            styleUrl={config.map_style}
            center={replaying && runScenario ? [runScenario.frame.lon, runScenario.frame.lat] : [loaded.frame.lon, loaded.frame.lat]}
            zoom={8}
          >
            <CoverageLayer
              data={coverage.collection}
              heights={heights}
              visible={showCoverage}
              sensorId={replaying ? null : (sensor?.id ?? null)}
            />
            {replaying && (
              <ReplayLayers
                truth={replay.truth}
                detections={replay.detections}
                tracks={tracker ? [{ tracker, data: replay.tracks[tracker], color: colors.trackA }] : []}
                t={clock.t}
                options={layerOptions}
                selection={selection}
                onSelect={setSelection}
              />
            )}
            <SensorMarkers
              key={replaying ? `run-${runId}` : "edit"}
              sensors={mapSensors}
              selected={replaying ? null : selected}
              onMove={moveSensor}
              onSelect={replaying ? () => undefined : setSelected}
              draggable={!replaying}
            />
            {placing && !replaying && <MapClick onClick={place} cursor="crosshair" />}
          </MapView>
        )}
        {replaying && window && <TimeBar clock={clock} window={window} stepS={scanPeriod(runScenario?.sensors ?? [])} />}
        {placing && (
          <div className="map-hint">
            Click the map to place the {placing === "rotating_radar_2d" ? "radar" : "passive sensor"}.{" "}
            <button className="link" onClick={() => setPlacing(null)}>
              Cancel
            </button>
          </div>
        )}
        {draft && (
          <CoverageLegend
            sensorId={replaying ? null : (sensor?.id ?? null)}
            heights={heights}
            visible={showCoverage}
            busy={coverage.busy}
            onHeights={setHeights}
            onVisible={setShowCoverage}
          />
        )}
      </main>
    </div>
  );
}
