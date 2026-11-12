# Sensor simulation playground: Claude Code handoff

Nov 11, 2026 · @Patrick McCoy

## Purpose and scope

This project is a standalone Python playground that replays real aircraft traffic from ADS-B as ground truth, simulates how a configurable sensor would detect that traffic, and runs Stone Soup trackers against the simulated detections. The working repo name is tracking-sim, with Python package tracking\_sim.

A user defines a sensor (range, accuracy, scan rate, detection probability), places it at a map position, and replays traffic past it. The output is detections, tracks, and scores against truth, so different filters can be compared on identical traffic.

In scope:

- Ingesting ADS-B truth from several interchangeable sources, including a raw Dump1090 feed, so anyone with a receiver can use it
- Sensor models for radar and other sensor types with real-world characteristics
- Stone Soup tracker pipelines that can be swapped by config
- Scoring, a run store for comparing experiments, and later a map UI

Out of scope:

- Changes to planespotter. It is one optional truth source, read-only, with no shared code
- Decoding RF. Dump1090 or a compatible decoder (readsb, dump1090-fa) does that
- Real sensor hardware integration

## Architecture overview

The system is a one-way pipeline of five stages, and each stage talks to the next only through a defined data type. The core never imports a specific truth source.

1. **Truth sources** emit canonical truth records from Dump1090, planespotter, or a recording.
2. **Truth builder** turns records into clean per-flight paths that can be sampled at any time.
3. **Sensor simulator** samples those paths at each sensor's scan times and emits noisy detections, misses, and false alarms.
4. **Trackers** are Stone Soup pipelines that consume detections and emit tracks.
5. **Scoring and UI** compare tracks to truth, store the run, and display it.

Truth also feeds stage 5 directly, since scoring needs it.

Proposed package layout:

```
src/tracking_sim/
  truth/
    record.py        # canonical TruthRecord
    source.py        # TruthSource protocol
    sources/         # sbs.py, beast.py, aircraft_json.py,
                     # recording.py, planespotter.py (optional extra)
    builder.py       # segment, smooth, resample
  geo/               # frames (WGS84 <-> ENU), horizon, terrain LOS
  sensors/
    spec.py          # SensorSpec loaded from YAML
    models/          # rotating radar, 3D radar, passive, EO/IR
    detection.py     # Pd, noise, clutter, resolution
  sim/               # scenario, clock, seeded RNG, replay and live engines
  tracking/          # Stone Soup pipeline factory from config
  eval/              # metrics, run store
  api/               # FastAPI (milestone 5)
scenarios/           # YAML scenario and sensor definitions
notebooks/
tests/
```

## Truth sources

Every source implements one `TruthSource` protocol and emits the same `TruthRecord`, so a raw Dump1090 feed and planespotter are interchangeable. The Dump1090 adapters are the primary, public path; the planespotter adapter is an optional extra installed separately.

| Adapter | Input | Mode | What it provides | Milestone |
| --- | --- | --- | --- | --- |
| `recording` | Parquet or JSONL files of TruthRecords | Replay | Whatever was recorded; the repeatable test fixture | 1 |
| `sbs` | Dump1090 SBS-1 BaseStation text, TCP port 30003 | Live, record | Position, altitude, speed, track, callsign. Simplest to parse | 1 |
| `planespotter` | Read-only Percona user on the position and velocity tables; `/flights/stream` for live | Replay, live | Lat, lon, altitude, velocity history | 1 |
| `aircraft_json` | `aircraft.json` polled from readsb or dump1090-fa | Live, record | Adds separate `alt_baro` and `alt_geom`, plus NIC and NACp | 4 |
| `beast` | Raw Mode S frames, Beast TCP port 30005, decoded with pyModeS | Live, record | Everything in the messages, with per-message timestamps | 6 |

Port numbers and field availability differ between Dump1090 forks. Confirm against the decoder actually running before coding each adapter.

The protocol has two methods: `replay(start, end)` returning an iterator of records in time order, and `stream()` returning an async iterator for live use. A `record` CLI command pipes any live source into the `recording` format, which lets any user build their own history without a database.

Canonical `TruthRecord` fields, with everything beyond the first five nullable:

| Field | Type | Notes |
| --- | --- | --- |
| `icao24` | str | Hex address, lower case |
| `ts` | datetime (UTC) | Receiver time of the message |
| `lat`, `lon` | float, degrees | WGS84 |
| `source_id` | str | Which adapter and receiver produced it |
| `alt_baro_ft` | float | Pressure altitude |
| `alt_geom_ft` | float | GNSS height |
| `alt_unknown_ft` | float | Altitude whose reference is not known |
| `gs_kt`, `track_deg`, `vrate_fpm` | float | From velocity messages |
| `callsign`, `on_ground` | str, bool |  |
| `nic`, `nacp` | int | Position integrity and accuracy categories |

Position and velocity arrive as separate messages. Adapters emit sparse records and the truth builder merges them per aircraft.

## Truth quality

ADS-B is good truth horizontally and weaker vertically, and the design treats both limits explicitly instead of assuming perfect truth.

**Altitude reference.** Planespotter's stored altitude is most likely barometric. Standard airborne position messages carry pressure altitude, and only a rarely used message type carries GNSS height. Pressure altitude can differ from true height by several hundred feet depending on weather. Until verified, the planespotter adapter writes its altitude to `alt_unknown_ft`. Verification is a milestone 1 task: read the decoder code path planespotter consumes, or compare stored values against `alt_baro` and `alt_geom` from `aircraft.json` for the same aircraft and time.

**No NIC or NACp from planespotter.** The builder takes a configurable default horizontal position sigma for sources lacking accuracy fields. Start with 30 m as an assumption and revisit once the `aircraft_json` adapter shows real NACp values for local traffic.

**Consequences for scoring:**

- Horizontal metrics are the default. Vertical metrics run only on flights that have `alt_geom_ft`
- Results are trustworthy when simulated sensor error is well above truth error. That holds for surveillance radar at range. Flag runs where a precise sensor's noise approaches the truth sigma
- Altitude error has little effect on a 2D radar, which measures slant range and bearing

**Truth builder steps:**

1. Deduplicate records that arrive from more than one receiver stream.
2. Group by `icao24` and split into flight segments on a time gap (default 60 s).
3. Reject outliers such as position jumps from bad decodes, using a speed gate.
4. Merge sparse position and velocity records.
5. Convert to a local ENU frame centred on the scenario origin.
6. Smooth each segment and expose a `state_at(t)` function for interpolation.
7. Export Stone Soup `GroundTruthPath` objects for trackers and metrics.

No path is extrapolated beyond its segment. Live mode runs a configurable delay behind real time (default 10 s) so sampling always interpolates.

## Sensor simulator

A sensor is a YAML spec plus a model class, and placing it on the map means setting its position. Use Stone Soup's own sensor classes where one fits and write a thin custom model where none does.

Example spec (values are illustrative):

```yaml
id: radar-1
type: rotating_radar_2d      # or radar_3d, passive_bearing, eo_ir
position: {lat: 39.98, lon: -105.13, alt_m: 1650, mast_m: 15}
coverage:
  range_min_m: 500
  range_max_m: 110000
  azimuth_deg: [0, 360]
  elevation_deg: [-1, 30]
scan:
  period_s: 5.0              # one rotation
  beamwidth_deg: 1.5
noise:
  range_sigma_m: 60
  bearing_sigma_deg: 0.2
detection:
  model: constant            # or radar_equation
  pd: 0.9
  false_alarm_density: 1.0e-9   # per m^2 per scan
resolution:
  range_m: 150
  bearing_deg: 1.5
latency_s: 0.5
```

For each scan, the engine does the following per sensor:

1. Compute when the beam crosses each aircraft's bearing and sample truth at that time, not at the scan start.
2. Apply geometry gates: range limits, field of view, radar horizon.
3. Draw a detection with probability Pd.
4. Add measurement noise in the sensor's native coordinates (range, bearing, elevation).
5. Merge targets closer together than the resolution cell.
6. Add false alarms across the coverage volume.
7. Emit Stone Soup `Detection` objects carrying the measurement model, plus the truth link for scoring.

All randomness comes from one seeded generator per run, so a scenario replays identically.

Realism layers, added one at a time behind config flags:

| Layer | What it adds | Milestone |
| --- | --- | --- |
| Radar horizon | Line of sight on a 4/3 earth from mast height and target altitude | 2 |
| Terrain masking | Line of sight against a DEM (SRTM or USGS 3DEP tiles, cached locally) | 4 |
| Radar equation Pd | Pd from SNR, with RCS classed by aircraft type and a Swerling fluctuation model | 4 |
| Other sensor types | Passive bearing-only, EO/IR with angular field of view | 4 onward |

Aircraft type for the RCS class comes from an ICAO address lookup table shipped as an optional data file, so the core does not depend on planespotter's reference tables.

## Tracking and scoring

A tracker is a Stone Soup pipeline assembled from config, so comparing two filters is a config diff on the same scenario and seed.

A tracker config names each component:

- **Transition model:** constant velocity, constant acceleration, coordinated turn, or a multiple-model set
- **Predictor and updater:** Kalman, extended, unscented, or particle
- **Data association:** nearest neighbour, global nearest neighbour, or JPDA
- **Initiator and deleter:** how tracks start and when they are dropped

The baseline for milestone 3 is an extended Kalman filter with a constant velocity model and global nearest neighbour association.

A scenario file ties a run together:

```yaml
name: front-range-evening
origin: {lat: 39.98, lon: -105.13}
truth:
  source: recording
  path: data/2026-11-01.parquet
  window: ["2026-11-01T23:00:00Z", "2026-11-02T01:00:00Z"]
sensors: [sensors/radar-1.yaml]
trackers: [trackers/ekf-gnn.yaml, trackers/ukf-gnn.yaml]
seed: 42
```

Each run writes one directory under `runs/` holding the resolved config, detections, tracks, and metrics as Parquet and JSON. Runs are immutable and addressed by a hash of config plus seed.

Metrics, using Stone Soup's metric generators where available:

- OSPA and GOSPA over time for overall tracking error
- SIAP measures for completeness, ambiguity, spuriousness, and track continuity
- Per-sensor detection statistics: achieved Pd by range, false alarm count
- Track latency: time from first detection opportunity to confirmed track

Stone Soup class and metric names here come from memory of its documentation. Check each against the installed version before building on it, and pin the Stone Soup version in the lockfile.

## Tech stack and working conventions

The core is Python because Stone Soup is a Python library; everything else is chosen to keep the core small and installable by other people with one command.

| Concern | Choice |
| --- | --- |
| Language and packaging | Python 3.11 or newer, `uv`, `pyproject.toml` with optional extras |
| Tracking | Stone Soup, version pinned |
| Numerics and data | NumPy, SciPy, Polars or pandas, PyArrow for Parquet |
| Geodesy | `pymap3d` for WGS84 and ENU conversion |
| Mode S decoding | `pyModeS`, only in the `beast` extra |
| Config | Pydantic models loaded from YAML |
| Database access | SQLAlchemy with a MySQL driver, only in the `planespotter` extra |
| Tests and lint | pytest, ruff, type hints checked with mypy or pyright |
| API and UI (milestone 5) | FastAPI, React, MapLibre GL |
| Deployment | Docker Compose |

Rules for Claude Code sessions:

- Work one milestone at a time. Write a short plan for the milestone and get it approved before writing code
- Keep `PROGRESS.md` (what is done, what is next) and `DECISIONS.md` (each design decision with its reason) current at the end of every session
- The core package must install and pass tests with no extras, no database, and no network. A small recorded fixture in `tests/data/` makes that possible
- Nothing site-specific in code. Receiver location, hosts, ports, and credentials come from config or environment variables
- The planespotter adapter never writes to the database and uses a dedicated read-only user
- Every simulation path takes an explicit seed. Tests assert that identical config and seed give identical output
- Units are SI internally (metres, seconds, radians). Conversion from feet, knots, and degrees happens only in the truth builder and config loading; TruthRecord keeps the source's native units

## Milestones

Six milestones, each ending in something runnable; milestones 1 to 3 give a working notebook playground before any UI exists.

**Milestone 1: truth**

- [ ] Repo scaffold, `PROGRESS.md`, `DECISIONS.md`, CI running tests and lint
- [ ] `TruthRecord`, `TruthSource` protocol, `recording` adapter, and a fixture file
- [ ] `sbs` adapter and the `record` CLI command
- [ ] `planespotter` adapter as an optional extra
- [ ] Truth builder through to Stone Soup `GroundTruthPath`
- [ ] Resolve the altitude reference question for planespotter

Done when a notebook plots an hour of flights from both a recording and planespotter, and the same aircraft from both sources agree within the truth sigma.

**Milestone 2: one sensor**

- [ ] `SensorSpec` and YAML loading
- [ ] Rotating 2D radar with beam-crossing timing, range and bearing noise, constant Pd, false alarms, radar horizon
- [ ] Seeded replay engine

Done when detections plot over truth, achieved Pd matches the configured value within sampling error, and two runs with one seed are identical.

**Milestone 3: tracking and metrics**

- [ ] Tracker factory from config, with the baseline tracker
- [ ] A second and third tracker (unscented, multiple-model) for comparison
- [ ] OSPA, GOSPA, and SIAP metrics

Done when one command runs a scenario with three trackers and prints a comparison table.

**Milestone 4: scenarios and realism**

- [ ] Scenario files and the immutable run store
- [ ] Terrain masking from a DEM
- [ ] Radar equation Pd with RCS classes
- [ ] `aircraft_json` adapter, bringing geometric altitude and NACp
- [ ] A second sensor type (passive bearing-only)

Done when moving a radar behind a ridge visibly removes low-altitude detections on that bearing.

**Milestone 5: map playground**

- [ ] FastAPI service over scenarios and runs
- [ ] Map UI: place and edit sensors, show coverage, replay truth, detections, and tracks with a time slider
- [ ] Side-by-side tracker comparison

Done when a sensor can be dropped on the map and a replay run from the browser with no file editing.

**Milestone 6: live and multi-sensor**

- [ ] Live engine running behind real time on any streaming source
- [ ] `beast` adapter
- [ ] Multiple sensors feeding one tracker, with fusion

Done when the map shows live simulated tracks from two sensors against live truth.

## Open questions

None of these block milestone 1, but the first three should be settled during it.

- [ ] Is planespotter's stored altitude barometric or geometric? Expected barometric; verify as described under Truth quality
- [ ] Which Dump1090 fork and output format does the receiver run, and which port does planespotter read? This decides whether `sbs` or `aircraft_json` is the first public adapter
- [ ] What are the exact table and column names, units, and timestamp semantics in the planespotter position and velocity tables? Provide the schema to the first Claude Code session
- [ ] Final project name (working name: tracking-sim, package tracking\_sim)
- [ ] Licence, if the repo will be public
- [ ] Is the 30 m default truth sigma reasonable for local traffic? Check against real NACp values in milestone 4
- [ ] Which DEM source and resolution for terrain masking?
- [ ] Should the map UI live in this repo or a separate one?
