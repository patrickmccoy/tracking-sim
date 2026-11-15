import { useEffect, useMemo, useState } from "react";
import { api, type Config, type ScenarioEntry, type ScenarioResponse } from "./api";
import { GeoJsonLayer } from "./map/GeoJsonLayer";
import { MapView } from "./map/MapView";
import { colors } from "./theme";

export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [scenarios, setScenarios] = useState<ScenarioEntry[]>([]);
  const [key, setKey] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<ScenarioResponse | null>(null);
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

  useEffect(() => {
    if (!key) return;
    api
      .scenario(key)
      .then(setLoaded)
      .catch((e: Error) => setError(e.message));
  }, [key]);

  const scenario = loaded?.scenario;
  const sensors = useMemo<GeoJSON.FeatureCollection>(
    () => ({
      type: "FeatureCollection",
      features: (scenario?.sensors ?? []).map((s) => ({
        type: "Feature",
        properties: { id: s.id, type: s.type },
        geometry: { type: "Point", coordinates: [s.position.lon, s.position.lat] },
      })),
    }),
    [scenario],
  );

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
        {config && <span className="muted version">v{config.version}</span>}
      </header>
      <aside className="sidebar">
        {error && <p className="error">{error}</p>}
        {scenario && (
          <section>
            <h2>Scenario</h2>
            <dl className="facts">
              <dt>Truth</dt>
              <dd title={scenario.truth.path}>{scenario.truth.path.split("/").pop()}</dd>
              <dt>Window</dt>
              <dd>{scenario.truth.window ? scenario.truth.window.map((w) => w.slice(0, 16).replace("T", " ")).join(" to ") : "all"}</dd>
              <dt>Terrain</dt>
              <dd>{scenario.terrain ? String(scenario.terrain.source) : "none"}</dd>
              <dt>Seed</dt>
              <dd>{scenario.seed}</dd>
            </dl>
            <h2>Sensors</h2>
            <ul className="plain">
              {scenario.sensors.map((s) => (
                <li key={s.id}>
                  {s.id} <span className="muted">{s.type.replaceAll("_", " ")}</span>
                </li>
              ))}
            </ul>
            <h2>Trackers</h2>
            <ul className="plain">
              {scenario.trackers.map((t) => (
                <li key={t.id}>{t.id}</li>
              ))}
            </ul>
          </section>
        )}
      </aside>
      <main className="main">
        {config && loaded && (
          <MapView styleUrl={config.map_style} center={[loaded.frame.lon, loaded.frame.lat]} zoom={8}>
            <GeoJsonLayer
              id="sensors"
              data={sensors}
              layers={[
                {
                  id: "sensors-dot",
                  type: "circle",
                  paint: {
                    "circle-radius": 7,
                    "circle-color": colors.sensor,
                    "circle-stroke-color": colors.halo,
                    "circle-stroke-width": 2,
                  },
                },
                {
                  id: "sensors-label",
                  type: "symbol",
                  layout: {
                    "text-field": ["get", "id"],
                    "text-offset": [0, 1.3],
                    "text-anchor": "top",
                    "text-size": 12,
                  },
                  paint: { "text-color": colors.truthLabel, "text-halo-color": colors.halo, "text-halo-width": 1.5 },
                },
              ]}
            />
          </MapView>
        )}
      </main>
    </div>
  );
}
