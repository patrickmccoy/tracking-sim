# Progress

What is done and what is next. Updated at the end of every working session.
The full design lives in [Sensor_simulation_playground_design.md](Sensor_simulation_playground_design.md);
the reasons behind choices are in [DECISIONS.md](DECISIONS.md).

## Milestone 1: truth

Plan (written at the start of the milestone; item 4 was revised by D11):

1. Repo scaffold: `pyproject.toml` (uv, src layout, optional extras), ruff, mypy,
   pytest, GitHub Actions CI, this file and `DECISIONS.md`.
2. `TruthRecord` and the `TruthSource` protocol; the `recording` adapter
   (Parquet and JSONL, reader and writer); a seeded synthetic fixture in `tests/data/`.
3. `sbs` adapter (SBS-1 BaseStation over TCP and from captured files) and the
   `record` CLI command that pipes a live source into a recording.
4. ~~`planespotter` adapter as the optional `planespotter` extra~~. Replaced
   by an SBS exporter in the planespotter repository, written to the
   contract in `tools/README.md`: truth now enters only as SBS (D11).
5. Truth builder: dedupe, segment, speed-gate outliers, merge position and
   velocity, ENU conversion, smoothing with `state_at(t)`, export to Stone Soup
   `GroundTruthPath`.
6. Tooling to settle the planespotter altitude reference (source comparison).
7. A notebook that plots an hour of flights and compares two sources.

Status:

- [x] Repo scaffold, `PROGRESS.md`, `DECISIONS.md`, CI running tests and lint
- [x] `TruthRecord`, `TruthSource` protocol, `recording` adapter, and a fixture file
- [x] `sbs` adapter and the `record` CLI command (plus `export`, `info`, `compare`)
- [x] SBS contract for exporters (`tools/README.md`); the planespotter
  adapter was removed and its exporter belongs in the planespotter
  repository (D11)
- [x] Truth builder through to Stone Soup `GroundTruthPath`
- [ ] Resolve the altitude reference question for planespotter: **tooling done,
  verdict pending**. Needs an SBS export from planespotter; see DECISIONS.md D10.

Done-when check: `notebooks/01_truth_playground.ipynb` plots an hour of
flights and shows the same aircraft from two sources agreeing within the
30 m truth sigma (about 9 m median). Today that runs on synthetic data: the
recording is the synthetic fixture and the second source is a synthetic
stand-in. Pointing `TRACKING_SIM_SECOND` at a planespotter SBS export runs
it on real data, which has not been tried yet because no database was
reachable.

### Open: closing milestone 1 on real data

These wait on real recordings and the planespotter SBS exporter:

1. Write the SBS exporter in the planespotter repository, following
   `tools/README.md`.
2. Record an hour from the receiver with `tracking-sim record sbs`, export the
   same hour from planespotter, convert it with
   `tracking-sim export sbs --altitude-field alt_unknown_ft`, and run
   `tracking-sim compare ps.parquet ref.parquet --altitude` (D10 has the
   commands). Pick an hour when the altimeter setting is well away from
   29.92 inHg.
3. Record the altitude verdict in DECISIONS.md D10.
4. Run the notebook with `TRACKING_SIM_RECORDING` and `TRACKING_SIM_SECOND` set.
5. Confirm which Dump1090 fork and SBS time zone the receiver uses (open question 2).

## Milestone 2: one sensor

Plan (approved at the start of the milestone):

1. `SensorSpec` and YAML loading: Pydantic models for the design doc's spec,
   a discriminated union on `type` (only `rotating_radar_2d` for now), and
   an example spec at KBJC.
2. Sensor geometry: a per-sensor local frame, slant range, bearing and
   elevation from the antenna, and a 4/3-earth radar horizon.
3. Beam-crossing timing: solve when the rotating beam crosses each
   aircraft, and sample truth at that moment.
4. Detections: range, sector, elevation and horizon gates, constant Pd,
   range and bearing noise, Stone Soup `TrueDetection`s carrying a
   slant-range measurement model.
5. Resolution: merge targets closer than the range-by-bearing cell.
6. False alarms: Poisson clutter over the coverage area, as `Clutter`.
7. Seeded replay engine: scans in time order across sensors, per-sensor
   random streams from the run seed, detections and opportunities to
   Parquet, determinism and achieved-Pd tests.
8. Notebook `02_one_sensor.ipynb`: detections over truth, achieved Pd
   against range, false alarms, determinism.
9. Docs: this file, DECISIONS.md, README.

Decided with the plan: the radar measures **slant range** (as real 2D
radars do), not ground range, so trackers see the real altitude ambiguity.
No CLI command yet; `tracking-sim run` waits for scenario files (milestone 4).

Status:

- [x] `SensorSpec` and YAML loading (`scenarios/sensors/radar-1.yaml` is the example)
- [x] Rotating 2D radar with beam-crossing timing, slant range and bearing
  noise, constant Pd, resolution, false alarms, radar horizon
- [x] Seeded replay engine, with detections and opportunities to Parquet

Done-when check: `notebooks/02_one_sensor.ipynb` plots an hour of
detections over truth; the achieved Pd is 0.893 against the configured 0.9
(95% interval 0.881-0.905, and flat across range); false alarms average
38.0 per scan against 38.0 expected; and the same seed reproduces every
detection. Tests assert the same with a 99.9% interval. An hour of traffic
simulates in about two seconds. As with milestone 1, this is on the
synthetic fixture until real recordings are available.

Decisions are D12-D17 in DECISIONS.md.

## Milestone 3: tracking and metrics

Plan (approved at the start of the milestone):

1. Tracker spec and YAML loading: a Pydantic model with enumerated choices
   per component (motion model: constant velocity, or constant velocity
   plus coordinated turn for multiple models; filter: EKF, UKF or
   multiple-model particle; association: GNN with Mahalanobis gating;
   initiation: assumed height and its uncertainty, detections to confirm;
   deletion), and example specs for the three trackers.
2. Tracker factory: Stone Soup components from a spec and the sensor's
   measurement model. EKF and UKF track `[x, vx, y, vy, z, vz]`; the
   multiple-model particle filter tracks `[x, vx, y, vy, w, z, vz]` (turn
   rate placed so Stone Soup's model mappings work). Tentative tracks use a
   cheap EKF in every tracker; the multiple-model tracker samples particles
   only on confirmation, through a small initiator wrapper.
3. Running trackers: detections from the replay engine, one scan per batch
   in report-time order, each predicted to its own measurement time. The
   runner seeds numpy's global random state from the run seed and tracker
   id before each tracker (Stone Soup's particle filter draws from it) and
   runs trackers one after another. Tracks to Parquet.
4. Scoring truth: truth paths cut to the times each aircraft was in
   coverage, from the milestone 2 opportunities.
5. Metrics: OSPA, GOSPA and SIAP on horizontal position (Stone Soup), track
   latency, and a comparison table with run time.
6. Minimal scenario file and CLI: `tracking-sim run scenario.yaml [--out DIR]`,
   a subset of the design's scenario format (name, origin, truth path and
   window, sensor and tracker spec files, seed). Sensors run once and every
   tracker gets the same detections.
7. Notebook `03_tracking.ipynb`: tracks over truth, OSPA and GOSPA over
   time, the comparison table, one aircraft through a turn.
8. Docs: this file, DECISIONS.md, README.

Decided with the plan: the third tracker is Stone Soup's multiple-model
particle filter (1.9.1 has no Kalman IMM); trackers carry altitude in a 6D
(or 7D) state with a vague height prior rather than tracking only
horizontally; scoring uses truth only while in coverage; the command takes
a minimal scenario YAML.

Defaults: OSPA/GOSPA cut-off 1 km with p = 1; SIAP association threshold
1 km; 1,000 particles per confirmed track; assumed height 5,000 m ± 3,000 m
above the antenna; confirm after 3 associated detections; delete after 3
consecutive missed scans.

Risk: about 38 false alarms a scan spawn many tentative tracks. They stay
on the cheap EKF, but if an hour runs too slowly that is reported, not
hidden by lowering the clutter.

Carried over from milestone 2: detections are `[bearing, slant range]`
with `CartesianToBearingSlantRange`, which needs a height guess
(`inverse_height_m`) to initiate; each detection has a measurement time
(`timestamp`) and a report time (`metadata["report_time"]`).

Status:

- [x] Tracker factory from config, with the baseline tracker
- [x] A second and third tracker (unscented, multiple-model) for comparison
- [x] OSPA, GOSPA, and SIAP metrics (plus track latency)

Done-when check: `uv run tracking-sim run scenarios/kbjc-synthetic.yaml`
runs the hour fixture past radar-1 with all three trackers in about a
minute and prints:

```
tracker     run s  tracks  OSPA m  GOSPA   loc  missed  false  complete  ambig  spurious  pos acc m  longest  latency s  never
ekf-cv-gnn   15.3      65     445   1532  1106     201    226      0.89   1.00      0.12        341     0.66         10      2
ukf-cv-gnn   16.0      59     470   1653  1163     232    258      0.87   1.00      0.14        363     0.65         12      1
pf-mm-gnn    23.7      56     542   1854  1255     320    278      0.82   1.00      0.16        415     0.67         15      1
```

`notebooks/03_tracking.ipynb` shows the same with plots. Synthetic data
again, so read the numbers as a working pipeline rather than a verdict on
the filters. What they show so far:

- The constant-velocity EKF has the lowest OSPA on this traffic; the UKF is
  close behind.
- The particle filter makes no false tracks (versus 10 each) and loses the
  manoeuvring aircraft less often (8 tracks on it versus 11-12), but its
  localisation is worse: 1,000 particles over a 7D state with a 3 km height
  spread is thin.
- Every tracker has a few hundred metres of error even in straight flight,
  mostly ground-range bias from the assumed 5,000 m height (D19).
- All three fragment tracks in turns: tuning (process noise, gate,
  particle count) is the obvious next experiment, and is now a config
  change.

Decisions are D18-D24 in DECISIONS.md. Plan changes made along the way:
particle draws are seeded by content rather than through numpy's global
state (D20), and a geometric pre-gate keeps clutter affordable (D21).

The test suite now takes about three minutes with the notebook extra,
mostly notebook 03 running the full hour.

## Milestone 4: scenarios and realism

Plan (approved at the start of the milestone):

1. Scenario format and the immutable run store: a `terrain:` section and
   inline sensors in the scenario; runs written once to `runs/<run-id>/`
   with a manifest (resolved config, input digests, code version, seed);
   an identical re-run reuses the stored run; `tracking-sim runs list`,
   `runs show`, `runs compare A B`.
2. Terrain masking from a DEM: Copernicus GLO-30 tiles downloaded once and
   cached under `~/.cache/tracking-sim/` (optional `terrain` extra);
   per-sensor terrain horizon (max terrain elevation angle per azimuth bin
   out to each range, 4/3 earth) so a check is one lookup; a `terrain`
   gate after the radar horizon; a synthetic ridge for tests.
3. Radar-equation Pd with RCS classes: reference SNR at a reference range
   for 1 m^2, SNR ~ 1/R^4, Swerling 1 single-pulse Pd (Swerling 0 via
   scipy's non-central chi-squared as an option); RCS classes light,
   small, large, heavy, rotorcraft and unknown.
4. `aircraft_json` adapter: readsb/dump1090-fa `aircraft.json`, live polling
   and saved snapshots; position time = poll time - `seen_pos`, repeats
   dropped; geometric altitude, NACp and emitter category (`TruthRecord`
   and recordings gain a nullable `category`; old recordings still read);
   segment truth sigma from NACp; `record`/`export aircraft-json`.
5. Passive bearing-only sensor (`type: passive_bearing`): staring, every
   target in coverage once per period (1 s default), azimuth only via
   Stone Soup's `Cartesian2DToBearing`, bearing noise, Pd falling with
   range, false bearings, horizon and terrain gates. Detections only;
   fusion and bearing-only tracking wait for milestone 6.
6. Notebook `04_scenarios_and_terrain.ipynb`: coverage with and without
   terrain, the radar moved behind a ridge, Pd by range and RCS class, the
   passive sensor, the run store.
7. Docs.

Decided with the plan:

- DEM: Copernicus GLO-30 (global 30 m, free, no login), read with tifffile.
- Ridge demo: real terrain downloaded on first run (the Front Range west of
  KBJC), with a synthetic ridge when offline; the notebook test uses the
  synthetic one, so CI needs no network.
- RCS class: ADS-B emitter category when present, then an optional user
  CSV of icao24 to class, then a configurable default.
- Run id: a hash of the resolved config, input digests, seed and code
  version (package version, git commit, and a flag for uncommitted
  changes), so a code change never silently reuses old results.

Carried over from milestone 3: the scenario format is a subset to extend,
not replace; the scoring grid uses the first sensor's scans (D22), which a
second sensor type needs a rule for; `aircraft_json` geometric altitude is a
chance to score heights.

Status:

- [x] Scenario files and the immutable run store
- [x] Terrain masking from a DEM (Copernicus GLO-90 by default)
- [x] Radar equation Pd with RCS classes
- [x] `aircraft_json` adapter, bringing geometric altitude and NACp
- [x] A second sensor type (passive bearing-only)

Done-when check: `notebooks/04_scenarios_and_terrain.ipynb` runs the same
radar, traffic and seed at KBJC and at a site 15 km west, behind the first
foothills ridge. On real Copernicus terrain the second site makes 63
low-altitude (below 3,000 m) detections on bearings 45-135 deg against
KBJC's 356, and terrain hides 1,198 of its looks; with the synthetic ridge
the notebook test uses, 6 against 348. Radar-equation and one-way Pd match
their expected values. `scenarios/kbjc-terrain.yaml` combines everything
and runs in about 75 s once the tiles are cached; behind the ridge the
trackers' OSPA rises to 615-664 m (from 445-542 m at KBJC with constant
Pd), with completeness 0.54-0.64.

Plan changes, all in DECISIONS.md: GLO-90 rather than GLO-30 by default
(D26: I had understated the tile sizes), run ids hash the package source
rather than a git commit (D25), and the `aircraft_json` adapter came before
the radar equation (D28). Decisions are D25-D30.

## Between milestones 4 and 5: Stone Soup walkthrough notebook

Asked for before starting milestone 5: a notebook that runs Stone Soup's
data association, filtering and prediction algorithms on this project's
truth and detections, built up step by step like the Stone Soup tutorials,
with easily changed parameters and a replay of any one aircraft. Decisions
in D31.

- [x] `DetectionAdapter` (tracking.adapt): detections remapped for any state
      layout, public, so notebooks can use acceleration and turn-rate states
- [x] Scoring grid from look times (`grid_from_looks`) and `ScoringGrid.sample`
      for plain Stone Soup tracks of any layout
- [x] `TrackingData` (sim.dataset): truth plus one radar's detections,
      simulated or loaded from a stored run; per-aircraft table, subsets
      near one aircraft, clipping, scoring
- [x] Pre-gate accepts a bare state (multi-frame assignment)
- [x] Replay player (viz.replay) with an aircraft picker; ipywidgets and
      ortools in the `notebook` extra
- [x] `notebooks/05_stone_soup_walkthrough.ipynb`: EKF, UKF, particle
      filter, CA, Singer, coordinated turn, multiple model, RTS smoothing,
      replay, NN, GNN, PDA, JPDA, MFA, comparison
- [x] Docs

Checked on the synthetic hour, which runs in about 10 s and is what the
notebook test uses, and on the roof capture, both simulated
(`TRACKING_SIM_RECORDING`) and loaded from a stored run
(`TRACKING_SIM_RUN`). On the capture, 41 aircraft fall within 40 km of the
chosen one, and every association method takes 1-2 s on that subset.

Follow-up: the particle filters sometimes veered kilometres off course.
Their particles collapsed in turns with the Kalman filters' process noise;
they now use their own, `PF_Q = 50` (D31). The `pf-mm-gnn` tracker spec
uses 1.0 and probably suffers the same way; not changed yet, since that
changes the scenario's results.

## Next: milestone 5, map playground

From the design: a FastAPI service over scenarios and runs; a map UI to
place and edit sensors, show coverage, and replay truth, detections and
tracks with a time slider; side-by-side tracker comparison. Done when a
sensor can be dropped on the map and a replay run from the browser with no
file editing. Write the plan and get it approved before coding. Things
milestone 4 leaves for it:

- The run store (`eval.runstore`) is the natural backend for a runs API;
  runs are immutable and addressed by id.
- `TerrainHorizon.lowest_visible_height` gives coverage maps directly.
- Scenarios accept inline specs, so the UI can write a scenario without
  separate spec files.
- Notebook tests now take a few minutes; worth trimming if CI time grows.

## Notes for the next session

- Truth enters only through Dump1090-family outputs (D11): SBS today,
  `aircraft_json` and `beast` later. Other data sources, such as databases,
  get an exporter to SBS that lives with that system and follows
  `tools/README.md`, not an adapter in the package.
- The synthetic fixture and all examples are centred on Rocky Mountain
  Metropolitan Airport (KBJC), not a real receiver site.
- GCV spline fitting is about 50 ms per axis per segment. Fine for an hour of
  traffic; worth revisiting if milestone 2 replays many hours at once (D8).
