import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Config, type ScenarioDoc, type ScenarioEntry, type ScenarioResponse, type SensorSpec } from "./api";
import { CoverageLegend } from "./components/CoverageLegend";
import { ScenarioPanel } from "./components/ScenarioPanel";
import { SensorForm } from "./components/SensorForm";
import { CoverageLayer } from "./map/CoverageLayer";
import { MapClick } from "./map/MapClick";
import { MapView } from "./map/MapView";
import { SensorMarkers } from "./map/SensorMarkers";
import { useCoverage } from "./map/useCoverage";
import { newSensor } from "./scenario/templates";
import { coverageSteps } from "./theme";

type SensorType = SensorSpec["type"];

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

  const dirty = useMemo(
    () => !!loaded && !!draft && JSON.stringify(loaded.scenario) !== JSON.stringify(draft),
    [loaded, draft],
  );
  const coverage = useCoverage(draft?.sensors ?? [], draft?.terrain ?? null);

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
          <select value={key ?? ""} onChange={(e) => setKey(e.target.value)}>
            {scenarios.map((s) => (
              <option key={s.key} value={s.key}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        {dirty && <span className="badge">edited</span>}
        {config && <span className="muted version">v{config.version}</span>}
      </header>
      <aside className="sidebar">
        {error && (
          <p className="error" role="alert">
            {error} <button className="link" onClick={() => setError(null)}>dismiss</button>
          </p>
        )}
        {draft && loaded && (
          <>
            <h2>Scenario</h2>
            <ScenarioPanel
              draft={draft}
              allTrackers={loaded.scenario.trackers}
              dirty={dirty}
              onChange={setDraft}
              onReset={() => key && load(key)}
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
          <MapView styleUrl={config.map_style} center={[loaded.frame.lon, loaded.frame.lat]} zoom={8}>
            <CoverageLayer data={coverage.collection} heights={heights} visible={showCoverage} sensorId={sensor?.id ?? null} />
            <SensorMarkers sensors={draft.sensors} selected={selected} onMove={moveSensor} onSelect={setSelected} />
            {placing && <MapClick onClick={place} cursor="crosshair" />}
          </MapView>
        )}
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
            sensorId={sensor?.id ?? null}
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
