# Progress

What is done and what is next. Updated at the end of every working session.
The full design lives in [Sensor_simulation_playground_design.md](Sensor_simulation_playground_design.md).

## Current milestone: 1 (truth)

Plan:

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

- [ ] Repo scaffold, `PROGRESS.md`, `DECISIONS.md`, CI running tests and lint
- [ ] `TruthRecord`, `TruthSource` protocol, `recording` adapter, and a fixture file
- [ ] `sbs` adapter and the `record` CLI command
- [ ] `planespotter` adapter as an optional extra
- [ ] Truth builder through to Stone Soup `GroundTruthPath`
- [ ] Resolve the altitude reference question for planespotter
