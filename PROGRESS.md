# Progress

What is done and what is next. Updated at the end of every working session.
The full design lives in [Sensor_simulation_playground_design.md](Sensor_simulation_playground_design.md);
the reasons behind choices are in [DECISIONS.md](DECISIONS.md).

## Milestone 1: truth

Plan (written at the start of the milestone):

1. Repo scaffold: `pyproject.toml` (uv, src layout, optional extras), ruff, mypy,
   pytest, GitHub Actions CI, this file and `DECISIONS.md`.
2. `TruthRecord` and the `TruthSource` protocol; the `recording` adapter
   (Parquet and JSONL, reader and writer); a seeded synthetic fixture in `tests/data/`.
3. `sbs` adapter (SBS-1 BaseStation over TCP and from captured files) and the
   `record` CLI command that pipes a live source into a recording.
4. `planespotter` adapter as the optional `planespotter` extra, read-only, with
   table and column names supplied by config.
5. Truth builder: dedupe, segment, speed-gate outliers, merge position and
   velocity, ENU conversion, smoothing with `state_at(t)`, export to Stone Soup
   `GroundTruthPath`.
6. Tooling to settle the planespotter altitude reference (source comparison).
7. A notebook that plots an hour of flights and compares two sources.

Status:

- [x] Repo scaffold, `PROGRESS.md`, `DECISIONS.md`, CI running tests and lint
- [x] `TruthRecord`, `TruthSource` protocol, `recording` adapter, and a fixture file
- [x] `sbs` adapter and the `record` CLI command (plus `export`, `info`, `compare`)
- [x] `planespotter` adapter as an optional extra
- [x] Truth builder through to Stone Soup `GroundTruthPath`
- [ ] Resolve the altitude reference question for planespotter: **tooling done,
  verdict pending**. Needs a run against the real database; see DECISIONS.md D10.

Done-when check: `notebooks/01_truth_playground.ipynb` plots an hour of
flights and shows the same aircraft from two sources agreeing within the
30 m truth sigma (about 9 m median). Today that runs on synthetic data: the
recording is the synthetic fixture and the second source is a synthetic
planespotter stand-in. The notebook switches to the real planespotter
database when `TRACKING_SIM_PLANESPOTTER` points at a config, which has not
been tried yet because no database was reachable.

## Next

To close milestone 1 on real data:

1. Fill in `scenarios/truth/planespotter.yaml` from the real schema (copy the
   example). This also answers the open question about table and column
   names, units and timestamp semantics.
2. Record an hour from the receiver with `tracking-sim record sbs`, export the
   same hour from planespotter, and run
   `tracking-sim compare ps.parquet ref.parquet --altitude`.
3. Record the altitude verdict in DECISIONS.md D10 and set
   `altitude_reference` in the planespotter config.
4. Run the notebook with `TRACKING_SIM_RECORDING` and `TRACKING_SIM_PLANESPOTTER` set.
5. Confirm which Dump1090 fork and SBS time zone the receiver uses (open question 2).

Then milestone 2: `SensorSpec` and YAML loading, the rotating 2D radar, and
the seeded replay engine.

## Notes for the next session

- The planespotter `/flights/stream` endpoint is not used; live mode polls the
  database instead (D7).
- GCV spline fitting is about 50 ms per axis per segment. Fine for an hour of
  traffic; worth revisiting if milestone 2 replays many hours at once (D8).
