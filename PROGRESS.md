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

- [ ] Tracker factory from config, with the baseline tracker
- [ ] A second and third tracker (unscented, multiple-model) for comparison
- [ ] OSPA, GOSPA, and SIAP metrics

Done when one command runs a scenario with three trackers and prints a
comparison table.

## Notes for the next session

- Truth enters only through Dump1090-family outputs (D11): SBS today,
  `aircraft_json` and `beast` later. Other data sources, such as databases,
  get an exporter to SBS that lives with that system and follows
  `tools/README.md`, not an adapter in the package.
- The synthetic fixture and all examples are centred on Rocky Mountain
  Metropolitan Airport (KBJC), not a real receiver site.
- GCV spline fitting is about 50 ms per axis per segment. Fine for an hour of
  traffic; worth revisiting if milestone 2 replays many hours at once (D8).
