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

## Next

To close milestone 1 on real data:

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

- [ ] `SensorSpec` and YAML loading
- [ ] Rotating 2D radar with beam-crossing timing, range and bearing noise,
  constant Pd, false alarms, radar horizon
- [ ] Seeded replay engine

Done when detections plot over truth, achieved Pd matches the configured
value within sampling error, and two runs with one seed are identical.

## Notes for the next session

- Truth enters only through Dump1090-family outputs (D11): SBS today,
  `aircraft_json` and `beast` later. Other data sources, such as databases,
  get an exporter to SBS that lives with that system and follows
  `tools/README.md`, not an adapter in the package.
- The synthetic fixture and all examples are centred on Rocky Mountain
  Metropolitan Airport (KBJC), not a real receiver site.
- GCV spline fitting is about 50 ms per axis per segment. Fine for an hour of
  traffic; worth revisiting if milestone 2 replays many hours at once (D8).
