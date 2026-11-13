# tracking-sim

Sensor and tracking simulation playground: replay real ADS-B traffic from Dump1090 as ground truth, simulate radar and other sensor detections, and compare [Stone Soup](https://stonesoup.readthedocs.io/) tracking filters.

You define a sensor (range, accuracy, scan rate, detection probability), place it on the map, and replay traffic past it. The output is detections, tracks, and scores against truth, so different filters can be compared on identical traffic.

> **Status: milestones 1 to 4 are implemented.** Truth comes from Dump1090 SBS or readsb/dump1090-fa `aircraft.json`; a simulated rotating 2D radar turns an hour of traffic into realistic detections (noise, slant range, radar-equation Pd by aircraft size, unresolved targets, clutter, terrain masking from real elevation data); a passive bearing-only sensor runs alongside; three Stone Soup trackers (EKF, UKF, multiple-model particle filter) are scored with OSPA, GOSPA and SIAP; every run is stored once in an immutable run store; and a walkthrough notebook builds Stone Soup filters, motion models and data association (NN, GNN, PDA, JPDA, MFA) step by step on the same data, with a replay player for any aircraft. The map UI comes in milestone 5. See [PROGRESS.md](PROGRESS.md) for exactly what is done and what is next.

- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Using your own data](#using-your-own-data)
- [Command line](#command-line)
- [Python API](#python-api)
- [Configuration](#configuration)
- [Development](#development)
- [Roadmap](#roadmap)

## How it works

The system is a one-way pipeline of five stages. Each stage talks to the next only through a defined data type, and the core never imports a specific truth source.

Truth enters only through the outputs of a Dump1090-family decoder, so the framework works for anyone with a receiver. Today that is **SBS-1 BaseStation text**, live from a receiver or replayed from captured files; `aircraft.json` and Beast frames are on the roadmap. Data held elsewhere, such as a flight-history database, is converted to SBS by an exporter that lives with that system; [tools/README.md](tools/README.md) is the contract exporters write to.

```
 ┌───────────────┐   TruthRecord   ┌───────────────┐  GroundTruthPath  ┌──────────────┐  Detection  ┌──────────┐  Track  ┌────────────────┐
 │ Truth sources │ ──────────────▶ │ Truth builder │ ────────────────▶ │ Sensor       │ ──────────▶ │ Trackers │ ──────▶ │ Scoring and UI │
 │ sbs, recording│                 │ clean, smooth │                   │ simulator    │             │ (Stone   │         │ metrics, runs, │
 │               │                 │ ENU, state_at │ ─────────┐        │ 2D radar     │             │  Soup)   │         │ map            │
 └───────────────┘                 └───────────────┘          │        └──────────────┘             └──────────┘         └────────────────┘
                                                              └────────────── truth for scoring ──────────────────────────────────▲
       built                             built                              built                      built           metrics, runs
                                                                                                                       built; UI: 5
```

1. **Truth sources** emit canonical `TruthRecord`s. Every source implements the same `TruthSource` protocol (`replay(start, end)` and `stream()`), so a live Dump1090 feed, a captured SBS file and a recording are interchangeable.
2. **The truth builder** turns sparse records into clean per-flight segments that can be sampled at any time.
3. **The sensor simulator** samples those paths when each sensor's beam crosses each aircraft and emits noisy detections, misses and false alarms as Stone Soup `Detection`s.
4. **Trackers** are Stone Soup pipelines assembled from YAML specs, all fed the same detections.
5. **Scoring** compares tracks with the truth the sensors could see (OSPA, GOSPA, SIAP, track latency), and every run is kept in an immutable run store. *The map UI comes in milestone 5.*

### Truth sources

| Adapter | Input | Modes | Notes |
| --- | --- | --- | --- |
| `sbs` | Dump1090 SBS-1 BaseStation text (TCP port 30003) or captured files (plain or `.gz`) | live, replay | Position, barometric altitude, speed, track, vertical rate, callsign. |
| `recording` | Parquet or JSONL files of TruthRecords | replay | A parsed SBS feed saved for fast, repeatable replay. `tracking-sim record` writes it. |
| `aircraft_json` | readsb / dump1090-fa `aircraft.json`, polled over HTTP or saved snapshots (`.json`, `.jsonl`, `.gz`) | live, replay | Adds geometric altitude, NIC/NACp and the ADS-B emitter category. |
| `beast` | Beast frames | | Milestone 6: raw Mode S with per-message timestamps. |

`TruthRecord` keeps the source's native units (feet, knots, degrees). Position and velocity usually arrive as separate messages, so everything except `icao24`, `ts`, `source_id` and (for position messages) `lat`/`lon` is optional. Timestamps are always timezone-aware UTC.

### The truth builder

`build_truth(records, frame)` runs these steps:

1. **Deduplicate** identical messages heard by more than one receiver.
2. **Segment** each aircraft on a position gap (default 60 s).
3. **Reject outliers** such as bad CPR decodes, using a speed gate that compares each point with both neighbours.
4. **Merge** altitude and velocity onto positions. Altitude prefers geometric, then barometric, then unknown-reference, and a segment never mixes references.
5. **Convert to a local ENU frame** (`LocalFrame`) centred on the scenario origin, with exact WGS84 conversions from `pymap3d`. Everything from here on is SI.
6. **Smooth** each axis with a cubic smoothing spline (smoothing chosen by cross-validation). `state_at(t)` gives position and velocity anywhere inside a segment and `None` outside it: paths are never extrapolated.
7. **Export** Stone Soup `GroundTruthPath`s with `[x, vx, y, vy, z, vz]` states and per-segment metadata (ICAO address, callsign, altitude reference, truth sigma).

### Truth quality

ADS-B is good truth horizontally and weaker vertically, and the design treats both limits explicitly:

- SBS carries no accuracy fields, so truth gets a configurable horizontal sigma, 30 m by default. With `aircraft.json`, each segment's sigma comes from its NACp instead (NACp 9: about 12 m).
- Barometric altitude can differ from true height by hundreds of feet depending on weather, so every segment records which altitude reference it uses.
- A live SBS feed carries barometric altitude. An exported SBS file carries whatever its source stored, so until that is known, read it with `--altitude-field alt_unknown_ft`. `tracking-sim compare --altitude` is the tool for settling it; see [DECISIONS.md](DECISIONS.md) (D10).

### The sensor simulator

A sensor is a YAML spec plus a model class. Milestone 2 has one: a rotating 2D surveillance radar (`type: rotating_radar_2d`). The example at [scenarios/sensors/radar-1.yaml](scenarios/sensors/radar-1.yaml) is a terminal-area radar at Rocky Mountain Metropolitan Airport:

```yaml
id: radar-1
type: rotating_radar_2d
position: {lat: 39.9088, lon: -105.1172, alt_m: 1729, mast_m: 15}
coverage: {range_min_m: 500, range_max_m: 110000, azimuth_deg: [0, 360], elevation_deg: [-1, 30]}
scan: {period_s: 5.0, beamwidth_deg: 1.5, start_azimuth_deg: 0}
noise: {range_sigma_m: 60, bearing_sigma_deg: 0.2}
detection: {model: constant, pd: 0.9, false_alarm_density: 1.0e-9}   # per m² per scan
resolution: {range_m: 150, bearing_deg: 1.5}
latency_s: 0.5
```

`ReplayEngine(truth, [spec], seed=42).run()` replays truth past the radar. For each scan and aircraft it:

1. **Times the observation** to the moment the rotating beam crosses the aircraft, and samples truth then, not at the scan start (at 250 m/s a 5 s scan is over a kilometre of travel).
2. **Gates on geometry:** slant range limits, azimuth sector, elevation limits, and line of sight on a 4/3 earth from the mast height (the radar horizon).
3. **Draws a detection** with probability Pd.
4. **Adds noise** to the measured **slant range** and bearing. A 2D radar measures the straight-line distance to the aircraft, not the ground range, so a high aircraft appears farther out than it is. Trackers have to cope with that, as they would with a real radar.
5. **Merges** detected aircraft closer together than the resolution cell into one plot.
6. **Adds false alarms**, a Poisson number per scan spread uniformly over the coverage area.

Detections are Stone Soup `TrueDetection` (linked to the truth path) and `Clutter` objects with `[bearing, slant range]` measurements and a matching measurement model (`CartesianToBearingSlantRange`), so Stone Soup trackers can use them directly. Every beam crossing is also kept as a detection *opportunity*, so achieved Pd can be checked against the configured value. All randomness comes from the run seed, with separate streams per sensor and per purpose: the same seed reproduces a run exactly, adding a sensor doesn't change the others, and changing the clutter density doesn't change which aircraft are detected.

### Realism: terrain, radar physics and a second sensor

- **Terrain masking.** With a terrain model, every sensor also checks line of sight over the ground (on a 4/3 earth) and gates hidden aircraft as `terrain`. Real terrain comes from Copernicus GLO-90 (or GLO-30) elevation tiles, free and global, downloaded once and cached under `~/.cache/tracking-sim` (needs `uv sync --extra terrain`); a synthetic ridge or flat ground work offline.
- **Radar-equation Pd.** `detection.model: radar_equation` replaces a fixed Pd: you give the SNR a 1 m² target has at a reference range, and Pd follows from the radar equation (SNR falls as range⁴) for the aircraft's radar cross-section class, with a Swerling 1 (fluctuating) or 0 (steady) target. The class (light, small, large, heavy, rotorcraft) comes from an optional `icao24,class` CSV, then the ADS-B emitter category, then a default.
- **Passive bearing-only sensor.** `type: passive_bearing` is a staring receiver: every update (1 s by default) it reports a noisy bearing to each aircraft it hears, with Pd from a one-way link (SNR falls as range²) and some false bearings. Trackers can't use bearing-only detections yet (fusion is milestone 6), so scenarios feed trackers radar detections only.

```yaml
detection:
  model: radar_equation
  reference_snr_db: 13          # a 1 m^2 target at 50 km gives 13 dB
  reference_range_m: 50000
  pfa: 1.0e-6
  swerling: 1
  rcs_m2: {light: 1, small: 2, large: 20, heavy: 60, rotorcraft: 3, unknown: 5}
```

### Trackers and scoring

A tracker is a YAML spec naming each Stone Soup component, so comparing filters is a config diff on identical detections. Three examples live in [scenarios/trackers/](scenarios/trackers/):

| Spec | Filter | Motion model | State |
| --- | --- | --- | --- |
| `ekf-cv-gnn` (baseline) | extended Kalman | constant velocity | `[x, vx, y, vy, z, vz]` |
| `ukf-cv-gnn` | unscented Kalman | constant velocity | same |
| `pf-mm-gnn` | multiple-model particle filter (1,000 particles) | constant velocity or coordinated turn | adds turn rate |

```yaml
id: ekf-cv-gnn
filter: ekf                      # ekf, ukf or particle_mm
motion: {model: constant_velocity, acceleration_noise: 1.0, vertical_acceleration_noise: 0.1}
association: {method: gnn, gate: 3.0}          # global nearest neighbour, Mahalanobis gate
initiation: {min_points: 3, assumed_height_m: 5000, height_sigma_m: 3000}
deletion: {missed_scans: 3}
```

All trackers associate with global nearest neighbour and confirm a track after three detections. A 2D radar measures no height, so a new track starts at an assumed height above the antenna with a wide uncertainty; the height guess mostly shows up as ground-range error close to the radar. Detections reach the tracker one scan at a time in report order, each predicted to its own measurement time. Runs are reproducible: the particle filter's random draws are seeded from the run seed and the data, not from call order.

Scoring puts truth and tracks on a common grid (every scan end) and counts an aircraft only while the radar could see it, so no tracker is penalised for aircraft outside coverage or below the horizon. Tracks count from confirmation on. Metrics are on horizontal position:

- **OSPA** (per-aircraft average, 1 km cut-off) and **GOSPA** (a total, split into localisation, missed-aircraft and false-track parts);
- **SIAP**: completeness, ambiguity, spuriousness, position and velocity accuracy, longest track segment;
- **track latency**: from an aircraft becoming visible to its first track, plus a count of aircraft never tracked.

A **scenario** file ties a run together (truth, sensors, trackers, seed), and `tracking-sim run` runs it:

```sh
uv run tracking-sim run scenarios/kbjc-synthetic.yaml
```

```
tracker     run s  tracks  OSPA m  GOSPA   loc  missed  false  complete  ambig  spurious  pos acc m  longest  latency s  never
ekf-cv-gnn   15.3      65     445   1532  1106     201    226      0.89   1.00      0.12        341     0.66         10      2
ukf-cv-gnn   16.0      59     470   1653  1163     232    258      0.87   1.00      0.14        363     0.65         12      1
pf-mm-gnn    23.7      56     542   1854  1255     320    278      0.82   1.00      0.16        415     0.67         15      1
```

Each run is stored once in the **run store**, `runs/<name>-<hash>/`: the resolved configuration, a manifest, detections, every tracker's tracks (Parquet), `metrics.json` (summary and per-scan OSPA/GOSPA) and the table, all read-only. The hash covers the resolved scenario, the input data (truth, terrain, aircraft classes), the seed and the code (a digest of the package source and the Stone Soup, NumPy and SciPy versions), so running an identical scenario again reuses the stored run, and any change makes a new one. `tracking-sim runs list`, `runs show ID` and `runs compare A B` browse them; `--out DIR` writes a plain directory outside the store instead.

A scenario can also carry terrain, inline sensor and tracker specs, and an aircraft-class table; [scenarios/kbjc-terrain.yaml](scenarios/kbjc-terrain.yaml) uses all of them:

```yaml
terrain: {source: copernicus, resolution_m: 90}     # or synthetic (a ridge) or flat
truth: {path: ..., aircraft_classes: classes.csv}   # optional icao24,class table
sensors:
  - sensors/radar-1.yaml                             # a file...
  - {id: esm-1, type: passive_bearing, ...}          # ...or a spec written inline
```

### Package layout

```
src/tracking_sim/
  cli.py                 # the tracking-sim command
  config.py              # YAML -> Pydantic loading with ${ENV_VAR} expansion
  units.py               # unit constants (SI internally)
  geo/frames.py          # LocalFrame: WGS84 <-> ENU
  geo/site.py            # SensorSite (a sensor's own frame), 4/3-earth radar horizon
  geo/terrain.py         # elevation models, per-sensor terrain horizon
  geo/copernicus.py      # Copernicus DEM tiles: download, cache, mosaic
  truth/
    record.py            # TruthRecord
    source.py            # TruthSource protocol
    sources/
      recording.py       # Parquet / JSONL reader and writer
      sbs.py             # Dump1090 SBS-1 BaseStation
      aircraft_json.py   # readsb / dump1090-fa aircraft.json
    builder.py           # dedupe, segment, gate, merge, ENU -> TruthSet
    trajectory.py        # smoothing splines, state_at(t)
    compare.py           # source agreement and altitude reference checks
    recorder.py          # record / export / summary helpers
    synthetic.py         # seeded synthetic traffic for tests and demos
  sensors/
    spec.py              # SensorSpec: Pydantic models loaded from YAML
    base.py              # GroundSensor: site, geometry gates (range ... terrain), opportunities
    scan.py              # BeamSchedule: when a rotating beam crosses each target
    measurement.py       # slant-range and bearing-only Stone Soup measurement models
    detection.py         # RCS classes, radar equation, Swerling Pd
    radar.py             # RotatingRadar2D: Pd, noise, resolution, clutter
    passive.py           # PassiveBearingSensor: staring, bearing only
  sim/
    rng.py               # per-sensor, per-purpose random streams from the run seed
    engine.py            # ReplayEngine and SimulationResult (stats, tables, Parquet)
    scenario.py          # scenario files, run_scenario, ScenarioResult.write
    dataset.py           # TrackingData: truth + one radar's detections, simulated or from a stored run
  tracking/
    spec.py              # TrackerSpec: Pydantic models loaded from YAML
    factory.py           # Stone Soup components from a spec (EKF, UKF, particle MM)
    initiators.py        # slant-range initiation at an assumed height; particles on confirmation
    particle.py          # reproducible multiple-model particle filter components
    gating.py            # geometric pre-gate in front of Stone Soup's hypothesiser
    adapt.py             # detections remapped for other state layouts (acceleration, turn rate)
    runner.py            # detection batches, run_tracker, tracks to Parquet
  eval/
    scoring.py           # truth and tracks on a common grid, coverage-only truth
    metrics.py           # OSPA, GOSPA, SIAP, latency, comparison table
    runstore.py          # the immutable run store
  viz/replay.py          # single-aircraft replay: map, errors, ipywidgets player
notebooks/               # 01_truth_playground, 02_one_sensor, 03_tracking, 04_scenarios_and_terrain,
                         # 05_stone_soup_walkthrough
scenarios/               # kbjc-synthetic.yaml, kbjc-terrain.yaml; sensors/ and trackers/ specs
tools/README.md          # the SBS contract for exporters from other systems
scripts/make_fixture.py  # regenerates tests/data
tests/                   # pytest suite; tests/data holds the synthetic fixture
```

Later milestones add `api/` and more sensor models alongside these, as laid out in the [design document](Sensor_simulation_playground_design.md).

## Getting started

You need `git` and [uv](https://docs.astral.sh/uv/). uv installs the right Python (3.12, pinned in `.python-version`) by itself, so you don't need to install Python first.

**1. Install uv** (once per machine):

```sh
# macOS
brew install uv
# macOS or Linux, without Homebrew
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Clone and install** with the notebook extra:

```sh
git clone https://github.com/patrickmccoy/tracking-sim.git
cd tracking-sim
uv sync --extra notebook --extra terrain
```

This creates `.venv/` with the package, its pinned dependencies (from `uv.lock`), Jupyter, matplotlib, pandas, ipywidgets and OR-Tools (for Stone Soup's multi-frame assignment), and the GeoTIFF readers for terrain tiles. (`--extra terrain` is optional: without it, notebook 04 uses a synthetic ridge.)

**3. Open the notebooks:**

```sh
uv run jupyter lab notebooks/
```

Start with `01_truth_playground.ipynb`, then `02_one_sensor.ipynb`.

#### Notebook 01: truth playground

Run all cells. With no other setup it uses the synthetic one-hour fixture in `tests/data/`, so it works offline with no receiver or database. You'll see:

1. A summary of the recording: 14 aircraft around Rocky Mountain Metropolitan Airport and about 11,000 sparse records from two receivers.
2. The truth builder's statistics: duplicates dropped, bad decodes rejected, and segments built (one aircraft is split by a two-minute coverage gap).
3. A map of an hour of flights with range rings, plus their altitude profiles.
4. A close-up of a segment showing the position the speed gate rejected, and the smoothed ground speed against the ADS-B velocity reports.
5. The same aircraft checked against a second source, with per-aircraft agreement against the 30 m truth sigma.
6. An assessment of whether the second source's altitude is barometric or geometric.
7. The Stone Soup `GroundTruthPath` export.

#### Notebook 02: one sensor

It replays the same hour past the example radar (`scenarios/sensors/radar-1.yaml`) with seed 42. You'll see:

1. The run summary: scans, beam crossings, how many were gated and why, achieved Pd against the configured 0.9, and false alarms per scan.
2. A PPI view of an hour of detections over truth, with clutter, and one aircraft's measured slant range against its true slant and ground range.
3. Range and bearing errors against the configured noise.
4. Achieved Pd by range with confidence intervals.
5. False alarms per scan against their Poisson distribution.
6. The same seed reproducing every detection, and a different seed changing only the random draws.
7. The Stone Soup `TrueDetection` and `Clutter` objects a tracker will receive.

Try editing the radar spec (Pd, noise, clutter density, range, mast height) or setting `TRACKING_SIM_SENSOR` to your own spec and re-running.

#### Notebook 03: tracking

It runs `scenarios/kbjc-synthetic.yaml` (about a minute) and compares the three trackers on the same detections. You'll see:

1. The radar summary and the comparison table, with how to read each column.
2. Each tracker's confirmed tracks over truth, with false tracks highlighted.
3. OSPA over time and where each tracker's GOSPA comes from (localisation, missed aircraft, false tracks).
4. The most manoeuvring aircraft: every track each tracker held on it, and their error through the turns.

Edit a tracker spec in `scenarios/trackers/` (motion noise, gate, particle count, initiation) and re-run to see the effect on identical traffic.

#### Notebook 04: scenarios and terrain

It downloads (once, about 57 MB) real terrain for the Front Range, or uses a synthetic ridge offline, and shows:

1. The terrain, radar A at KBJC and radar B 15 km west, behind the first foothills ridge.
2. Coverage maps: the lowest height an aircraft can fly and still be seen, all round each radar.
3. The same radar, traffic and seed at A and at B: from B, low traffic to the east disappears (on real terrain, 63 low-altitude detections on bearings 45–135° instead of 356).
4. Radar-equation Pd by range and aircraft size class, against the model.
5. The passive sensor's bearings over time.
6. The run store reusing an identical run and storing a new one for another seed.

#### Notebook 05: Stone Soup walkthrough

Stone Soup components assembled by hand, one step at a time like Stone Soup's own tutorials, on this project's truth and radar detections. Each step starts with a short parameters cell: change a value and re-run from there. It runs in about ten seconds on the synthetic hour:

1. The data: simulated now (optionally with changes to the radar spec), or a stored run's exact detections with `TRACKING_SIM_RUN=<run id>`.
2. One aircraft (the most detected by default, or `TRACKING_SIM_AIRCRAFT`): its detections over truth and the radar's measurement errors.
3. Transition and measurement models, and the prior from one detection at an assumed height.
4. An extended Kalman filter, its error against its own 2-sigma, and what happens when height is left free (a 2D radar cannot see it).
5. An unscented Kalman filter.
6. A particle filter, warm-started from the EKF, and why it needs more process noise than the Kalman filters (its particle cloud collapses in a turn otherwise).
7. Motion models: constant acceleration, Singer, coordinated turn (EKF and UKF) and a multiple-model particle filter.
8. Rauch-Tung-Striebel smoothing of finished tracks.
9. A replay player: pick any aircraft, then play or scrub through its truth, its detections, the other detections of each scan, and every filter's track and error.
10. Many aircraft: nearest neighbour and global nearest neighbour, with initiation and deletion, on a subset of scans near the chosen aircraft.
11. PDA and JPDA.
12. Multi-frame assignment (MFA).
13. Every tracker, and a loaded run's own trackers, compared with OSPA, GOSPA and SIAP, then replayed on the chosen aircraft.

If you prefer VS Code or another editor, open the notebook there and choose the `.venv` interpreter in the repo as the kernel.

**4. Check everything works** (optional):

```sh
uv run pytest
```

> The fixture traffic is **synthetic**: generated by `tracking_sim.truth.synthetic` with a fixed seed to look like real decoder output, including its defects. It is good for learning the framework and for tests, not for drawing conclusions about real traffic.

## Using your own data

### Record from a Dump1090 receiver

Any decoder that serves SBS-1 BaseStation output works: dump1090, dump1090-fa or readsb, usually on TCP port 30003.

```sh
uv run tracking-sim record sbs --host 192.168.1.50 --tz America/Denver \
    --receiver roof -o data/2026-11-01.parquet --duration 3600
```

- `--tz` matters: BaseStation timestamps carry no time zone and Dump1090 writes the receiver host's **local** time. Use the receiver's IANA zone, or pass `--arrival-time` to stamp records with this machine's UTC clock instead.
- Stop early with Ctrl-C; the file is closed cleanly. Use a `.jsonl` output if you want every flushed batch to survive a crash.
- `data/` and `runs/` are git-ignored.

Already have a capture of port 30003 (for example from `nc receiver 30003 > capture.sbs`)? Convert it:

```sh
uv run tracking-sim export sbs capture.sbs --tz America/Denver -o data/capture.parquet
```

You can also skip the conversion: the notebook reads `.sbs` and `.sbs.gz` files directly.

If your decoder is readsb or dump1090-fa, `aircraft.json` gives more than SBS (geometric altitude, position accuracy, aircraft category):

```sh
uv run tracking-sim record aircraft-json --url http://192.168.1.50/data/aircraft.json \
    --receiver roof -o data/2026-11-01.parquet --duration 3600
uv run tracking-sim export aircraft-json snapshots/ -o data/snapshots.parquet   # saved polls
```

Check what you recorded:

```sh
uv run tracking-sim info data/2026-11-01.parquet
```

### Point the notebook at it

```sh
export TRACKING_SIM_RECORDING=data/2026-11-01.parquet   # or a .sbs / .sbs.gz capture
export TRACKING_SIM_SBS_TZ=America/Denver                # if the recording is SBS text
export TRACKING_SIM_ORIGIN=39.9088,-105.1172,1729        # receiver lat, lon, height (m)
export TRACKING_SIM_WINDOW=2026-11-01T23:00Z,2026-11-02T00:00Z   # optional
uv run jupyter lab notebooks/01_truth_playground.ipynb
```

The same variables run the later notebooks on your traffic: notebook 02 replays it past the example radar, notebook 03 runs its scenario's sensors and trackers on it, and notebook 04 uses it for the terrain, radar-equation and passive-sensor sections. SBS captures carry no ADS-B emitter category, so in notebook 04 every aircraft gets the default RCS class; record with `record aircraft-json` to keep the category. From the command line, point a copy of a scenario's `truth.path` at the recording instead.

Section 5 compares against a second source for the same hour: another receiver's recording or capture, or an SBS export from another system (below). Without one, the notebook stops there for your own recording.

```sh
export TRACKING_SIM_SECOND=data/export.sbs.gz   # its altitude is read as unknown-reference
```

### Data from other systems

Flight history held somewhere else, such as a database, comes in as SBS text written by an exporter. Exporters live with the system that owns the data, not in this repository; [tools/README.md](tools/README.md) describes exactly what they must write (line layout, time zone, altitude reference, units) and how to check the result. Once you have an export:

```sh
# convert it like any capture, keeping the altitude reference open until verified
uv run tracking-sim export sbs data/export.sbs.gz --receiver mysource \
    --altitude-field alt_unknown_ft -o data/export.parquet

# check it against your own receiver for the same hour, including its altitude reference
uv run tracking-sim compare data/export.parquet data/2026-11-01.parquet --altitude
```

## Command line

```
tracking-sim record sbs            record a live Dump1090 SBS feed into a recording file
tracking-sim record aircraft-json  poll readsb / dump1090-fa aircraft.json into a recording
tracking-sim export sbs            convert captured SBS text files into a recording
tracking-sim export aircraft-json  convert saved aircraft.json snapshots into a recording
tracking-sim compare A B           check two recordings agree on the same aircraft (--altitude too)
tracking-sim info FILES            summarize recordings
tracking-sim run SCENARIO          run a scenario into the run store (--seed N, --runs DIR, --out DIR)
tracking-sim runs list             stored runs
tracking-sim runs show ID          one run's manifest and results (ids or unique prefixes)
tracking-sim runs compare A B      configuration and metric differences between two runs
```

Run `uv run tracking-sim <command> --help` for every option. `compare` exits non-zero when the sources disagree, so it can be scripted.

## Python API

```python
from tracking_sim.geo import LocalFrame
from tracking_sim.truth import build_truth
from tracking_sim.truth.sources.recording import RecordingSource

source = RecordingSource("tests/data/synthetic_1h.parquet")
records = list(source.replay())  # or replay(start, end) with aware datetimes

frame = LocalFrame(lat=39.9088, lon=-105.1172, alt_m=1729.0)  # your receiver or scenario origin
truth = build_truth(records, frame)
print(truth.stats)

seg = truth.segments[0]
state = seg.state_at(seg.start + (seg.end - seg.start) / 2)  # smoothed ENU state, or None outside
print(seg.segment_id, seg.callsign, state.position, state.speed_mps)

paths = truth.ground_truth_paths(step_s=5.0)  # Stone Soup GroundTruthPath objects
```

To stream live records from a receiver yourself:

```python
import asyncio
from tracking_sim.truth.sources.sbs import SbsSource


async def main():
    async for record in SbsSource(host="192.168.1.50", tz="America/Denver").stream():
        print(record)


asyncio.run(main())
```

To simulate a radar:

```python
from tracking_sim.sensors import load_sensor_spec
from tracking_sim.sim import ReplayEngine

spec = load_sensor_spec("scenarios/sensors/radar-1.yaml")
result = ReplayEngine(truth, [spec], seed=42).run()  # or run(start, end)
print(result.summary())  # achieved Pd, gates, false alarms
detections = result.detections()  # Stone Soup TrueDetection / Clutter, in time order
result.write("runs/radar-1-seed-42")  # detections.parquet, opportunities.parquet
```

`ReplayEngine.scans()` yields the same thing one scan at a time, across all sensors in time order.

To run trackers and score them:

```python
from tracking_sim.sim.scenario import load_scenario, run_scenario

result = run_scenario(load_scenario("scenarios/kbjc-synthetic.yaml"))
print(result.table())  # the comparison table
result.scores[0].ospa  # OSPA per scan for the first tracker
result.runs[0].tracks_table()  # every track state as an Arrow table
result.write("runs/kbjc")  # everything --out writes
```

Or piece by piece: `tracking_sim.tracking.runner.run_tracker(spec, detection_batches(simulation.scans), seed=...)`, then `tracking_sim.eval.scoring_grid(truth, simulation)` and `score_run(run, grid)`.

To build Stone Soup trackers yourself, as notebook 05 does, start from `TrackingData`: truth plus one radar's detections, scan by scan, from a fresh simulation or read back from a stored run:

```python
from tracking_sim.sim.dataset import load_run, simulate

data = simulate(truth, spec, seed=42)   # or load_run("kbjc-synthetic-1a2b", store="runs")
data.batches()                          # (scan end, detections) for a Stone Soup tracker
data.detections_of("ac5044-0")          # one aircraft's TrueDetections
data.aircraft()                         # per-aircraft table: detections, speed, height, turn rate
part = data.subset(start, end, around="ac5044-0", radius_m=40_000)  # smaller, for JPDA or MFA
part.score("my tracker", tracks)        # OSPA, GOSPA and SIAP for any Stone Soup tracks
```

`tracking_sim.viz.replay.Replay(data, segment_id, estimates).widget()` replays one aircraft with any tracks (needs the `notebook` extra).

To generate synthetic traffic for experiments (any origin, any seed):

```python
from tracking_sim.truth.synthetic import ReceiverConfig, generate_traffic

traffic = generate_traffic(seed=7, origin=(51.47, -0.45, 25.0), receivers=(ReceiverConfig("rx"),))
traffic.records  # TruthRecords
traffic.flights  # the noise-free trajectories that produced them
```

## Configuration

Nothing site-specific lives in code. Hosts, ports, credentials and locations come from options, environment variables or YAML.

| Environment variable | Used by | Meaning |
| --- | --- | --- |
| `TRACKING_SIM_SBS_HOST` | `record sbs` | Dump1090 host |
| `TRACKING_SIM_SBS_PORT` | `record sbs` | BaseStation port (default 30003) |
| `TRACKING_SIM_SBS_TZ` | `record sbs`, `export sbs`, notebooks | Time zone of SBS timestamps (default UTC) |
| `TRACKING_SIM_RECEIVER` | `record sbs`, `export sbs` | Receiver name; records get `source_id` `sbs:<name>` |
| `TRACKING_SIM_RECORDING` | notebooks 01–05 | Recording (file, directory or glob) or SBS capture, in place of the synthetic hour |
| `TRACKING_SIM_SECOND` | notebook 01 | Second source for section 5, recording or SBS |
| `TRACKING_SIM_ORIGIN` | notebooks 01, 02 | `LAT,LON[,ALT_M]` of the local frame |
| `TRACKING_SIM_WINDOW` | notebooks 01, 03–05 | `START,END` in ISO 8601, part of the recording |
| `TRACKING_SIM_SENSOR` | notebooks 02, 05 | Sensor spec YAML (default `scenarios/sensors/radar-1.yaml`) |
| `TRACKING_SIM_SEED` | notebooks 02–05 | Run seed (default 42; notebook 03 overrides the scenario's) |
| `TRACKING_SIM_RUN` | notebook 05 | A stored run's id or prefix, to load its detections instead of simulating |
| `TRACKING_SIM_AIRCRAFT` | notebook 05 | Segment id of the aircraft to follow (default: the most detected) |
| `TRACKING_SIM_SCENARIO` | notebook 03 | Scenario YAML (default `scenarios/kbjc-synthetic.yaml`) |
| `TRACKING_SIM_TERRAIN` | notebook 04 | `copernicus` (default) or `synthetic` |
| `TRACKING_SIM_AIRCRAFT_JSON` | `record aircraft-json` | URL of `aircraft.json` |
| `TRACKING_SIM_RUNS` | `run`, `runs`, notebook 05 | Run store directory (default `runs`) |
| `TRACKING_SIM_CACHE` | terrain | Download cache (default `~/.cache/tracking-sim`) |

Sensors, trackers and scenarios are YAML files (above; every field is validated, `${VAR}` references are expanded, and scenario paths are relative to the scenario file). Truth builder settings live in `TruthBuilderConfig` (a Pydantic model): segment gap, speed gate, default truth sigma, altitude preference, smoothing method, and whether to drop aircraft on the ground.

## Development

```sh
uv sync --all-extras          # everything, including dev tools
uv run pytest                 # tests (~3 min with the notebook extra, which runs every notebook)
uv run ruff check             # lint
uv run ruff format            # format
uv run mypy                   # strict type check of src/
uv run python scripts/make_fixture.py   # regenerate tests/data (deterministic)
```

CI (GitHub Actions) runs lint and mypy, the core tests on Python 3.11 to 3.13 with **no extras**, and the full suite with all extras.

Conventions, from the design document:

- The core package must install and pass tests with no extras, no database and no network. The notebook test skips itself when the notebook extra is missing.
- Package adapters are only for Dump1090-family decoder outputs. Other data sources, such as databases, get an exporter to SBS that lives with that system and follows [tools/README.md](tools/README.md).
- Every simulation path takes an explicit seed; identical config and seed give identical output.
- Notebooks are committed without outputs, execution counts or local kernel metadata (outputs can carry local data). Clear outputs before committing, or restore the committed file with `git checkout -- notebooks/<name>.ipynb`; `tests/test_notebook_hygiene.py` fails otherwise.
- Units are SI internally. Conversion from feet, knots and degrees happens only in the truth builder and config loading.
- Work one milestone at a time, and keep [PROGRESS.md](PROGRESS.md) and [DECISIONS.md](DECISIONS.md) current at the end of each session.

## Roadmap

| Milestone | Delivers |
| --- | --- |
| 1. Truth | SBS ingest, exporter contract, truth builder, Stone Soup ground truth, notebook. **Implemented** (altitude verdict pending real data). |
| 2. One sensor | `SensorSpec` YAML, rotating 2D radar with beam-crossing timing, slant-range and bearing noise, Pd, resolution, false alarms and radar horizon; seeded replay engine; notebook. **Implemented.** |
| 3. Tracking and metrics | Tracker specs and factory (EKF + GNN baseline, UKF, multiple-model particle filter); OSPA, GOSPA, SIAP and latency; `tracking-sim run` comparison table; notebook. **Implemented.** |
| 4. Scenarios and realism | Scenario files and an immutable run store, terrain masking from Copernicus DEM tiles, radar-equation Pd with RCS classes, `aircraft_json` adapter, passive bearing-only sensor; notebook. **Implemented.** |
| 5. Map playground | FastAPI service and a MapLibre map UI to place sensors and replay runs. |
| 6. Live and multi-sensor | Live engine, `beast` adapter, multi-sensor fusion. |

## License

[MIT](LICENSE)
