# tracking-sim

Sensor and tracking simulation playground: replay real ADS-B traffic from Dump1090 as ground truth, simulate radar and other sensor detections, and compare [Stone Soup](https://stonesoup.readthedocs.io/) tracking filters.

You define a sensor (range, accuracy, scan rate, detection probability), place it on the map, and replay traffic past it. The output is detections, tracks, and scores against truth, so different filters can be compared on identical traffic.

> **Status: milestone 1 (truth) is implemented.** Truth ingestion, cleaning and export to Stone Soup work, and a notebook plots and cross-checks an hour of traffic. Sensors, trackers and scoring come in milestones 2 and 3. See [PROGRESS.md](PROGRESS.md) for exactly what is done and what is next.

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

```
 ┌───────────────┐   TruthRecord   ┌───────────────┐  GroundTruthPath  ┌──────────────┐  Detection  ┌──────────┐  Track  ┌────────────────┐
 │ Truth sources │ ──────────────▶ │ Truth builder │ ────────────────▶ │ Sensor       │ ──────────▶ │ Trackers │ ──────▶ │ Scoring and UI │
 │ sbs, recording│                 │ clean, smooth │                   │ simulator    │             │ (Stone   │         │ metrics, runs, │
 │ planespotter  │                 │ ENU, state_at │ ─────────┐        │ (milestone 2)│             │  Soup)   │         │ map            │
 └───────────────┘                 └───────────────┘          │        └──────────────┘             └──────────┘         └────────────────┘
       built                              built               └────────────── truth for scoring ─────────────────────────────────▲
```

1. **Truth sources** emit canonical `TruthRecord`s. Every source implements the same `TruthSource` protocol (`replay(start, end)` and `stream()`), so a raw Dump1090 feed, a recording file and planespotter are interchangeable.
2. **The truth builder** turns sparse records into clean per-flight segments that can be sampled at any time.
3. **The sensor simulator** samples those paths at each sensor's scan times and emits noisy detections, misses and false alarms. *(Milestone 2.)*
4. **Trackers** are Stone Soup pipelines assembled from config. *(Milestone 3.)*
5. **Scoring and UI** compare tracks with truth, store runs and display them. *(Milestones 3 to 5.)*

### Truth sources

| Adapter | Input | Modes | Notes |
| --- | --- | --- | --- |
| `recording` | Parquet or JSONL files of TruthRecords | replay | The repeatable fixture format. `tracking-sim record` writes it. |
| `sbs` | Dump1090 SBS-1 BaseStation text (TCP port 30003) or captured files | live, replay | Position, barometric altitude, speed, track, vertical rate, callsign. |
| `planespotter` | A planespotter MySQL/Percona database, read-only | replay, live (polling) | Optional extra. Schema comes from a YAML config. |
| `aircraft_json`, `beast` | readsb / dump1090-fa `aircraft.json`, Beast frames | | Milestones 4 and 6. |

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

- Sources without accuracy fields (SBS, planespotter) get a configurable horizontal truth sigma, 30 m by default.
- Barometric altitude can differ from true height by hundreds of feet depending on weather, so every segment records which altitude reference it uses.
- Planespotter's stored altitude is believed to be barometric but has not been verified, so it is stored as `alt_unknown_ft`. `tracking-sim compare --altitude` is the tool for settling it; see [DECISIONS.md](DECISIONS.md) (D10).

### Package layout

```
src/tracking_sim/
  cli.py                 # the tracking-sim command
  config.py              # YAML -> Pydantic loading with ${ENV_VAR} expansion
  units.py               # unit constants (SI internally)
  geo/frames.py          # LocalFrame: WGS84 <-> ENU
  truth/
    record.py            # TruthRecord
    source.py            # TruthSource protocol
    sources/
      recording.py       # Parquet / JSONL reader and writer
      sbs.py             # Dump1090 SBS-1 BaseStation
      planespotter.py    # optional, read-only database adapter
    builder.py           # dedupe, segment, gate, merge, ENU -> TruthSet
    trajectory.py        # smoothing splines, state_at(t)
    compare.py           # source agreement and altitude reference checks
    recorder.py          # record / export / summary helpers
    synthetic.py         # seeded synthetic traffic for tests and demos
notebooks/               # 01_truth_playground.ipynb
scenarios/truth/         # example source configs
scripts/make_fixture.py  # regenerates tests/data
tests/                   # pytest suite; tests/data holds the synthetic fixture
```

Later milestones add `sensors/`, `sim/`, `tracking/`, `eval/` and `api/` alongside `truth/`, as laid out in the [design document](Sensor_simulation_playground_design.md).

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
uv sync --extra notebook
```

This creates `.venv/` with the package, its pinned dependencies (from `uv.lock`), Jupyter and matplotlib.

**3. Open the notebook:**

```sh
uv run jupyter lab notebooks/01_truth_playground.ipynb
```

Run all cells. With no other setup it uses the synthetic one-hour fixture in `tests/data/`, so it works offline with no receiver or database. You'll see:

1. A summary of the recording: 14 aircraft and about 11,000 sparse records from two receivers.
2. The truth builder's statistics: duplicates dropped, bad decodes rejected, and segments built (one aircraft is split by a two-minute coverage gap).
3. A map of an hour of flights with range rings, plus their altitude profiles.
4. A close-up of a segment showing the position the speed gate rejected, and the smoothed ground speed against the ADS-B velocity reports.
5. The same aircraft checked against a second source, with per-aircraft agreement against the 30 m truth sigma.
6. An assessment of whether the second source's altitude is barometric or geometric.
7. The Stone Soup `GroundTruthPath` export.

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

Check what you recorded:

```sh
uv run tracking-sim info data/2026-11-01.parquet
```

### Point the notebook at it

```sh
export TRACKING_SIM_RECORDING=data/2026-11-01.parquet
export TRACKING_SIM_ORIGIN=39.98,-105.13,1650        # receiver lat, lon, height (m)
export TRACKING_SIM_WINDOW=2026-11-01T23:00Z,2026-11-02T00:00Z   # optional
uv run jupyter lab notebooks/01_truth_playground.ipynb
```

Without a second source configured, the notebook stops at section 5 for your own recording; set up planespotter (below) to compare against it.

### Planespotter (optional)

Planespotter is supported as an optional, read-only truth source. Install the extra and describe your schema:

```sh
uv sync --extra notebook --extra planespotter
cp scenarios/truth/planespotter.example.yaml scenarios/truth/planespotter.yaml
# edit table and column names, units and timestamp kind to match your database
export PLANESPOTTER_DB_HOST=db.local PLANESPOTTER_DB_USER=tracking_sim_ro PLANESPOTTER_DB_PASSWORD=...
```

Connect as a dedicated read-only database user. The adapter only issues `SELECT`s and also sets MySQL sessions read-only. Credentials stay in environment variables, referenced from the YAML as `${VAR}`.

```sh
# copy a window into a recording
uv run tracking-sim export planespotter --config scenarios/truth/planespotter.yaml \
    --start 2026-11-01T23:00Z --end 2026-11-02T00:00Z -o data/ps.parquet

# check it against your own receiver for the same hour, including its altitude reference
uv run tracking-sim compare data/ps.parquet data/2026-11-01.parquet --altitude
```

Set `TRACKING_SIM_PLANESPOTTER=scenarios/truth/planespotter.yaml` to make the notebook use planespotter as its second source.

## Command line

```
tracking-sim record sbs           record a live Dump1090 feed into a recording file
tracking-sim record planespotter  poll a planespotter database into a recording file
tracking-sim export sbs           convert captured SBS text files into a recording
tracking-sim export planespotter  copy a time window from planespotter into a recording
tracking-sim compare A B          check two recordings agree on the same aircraft (--altitude too)
tracking-sim info FILES           summarize recordings
```

Run `uv run tracking-sim <command> --help` for every option. `compare` exits non-zero when the sources disagree, so it can be scripted.

## Python API

```python
from tracking_sim.geo import LocalFrame
from tracking_sim.truth import build_truth
from tracking_sim.truth.sources.recording import RecordingSource

source = RecordingSource("tests/data/synthetic_1h.parquet")
records = list(source.replay())  # or replay(start, end) with aware datetimes

frame = LocalFrame(lat=39.98, lon=-105.13, alt_m=1650.0)  # your receiver or scenario origin
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
| `TRACKING_SIM_SBS_TZ` | `record sbs` | Time zone of SBS timestamps (default UTC) |
| `TRACKING_SIM_RECEIVER` | `record sbs` | Receiver name; records get `source_id` `sbs:<name>` |
| `TRACKING_SIM_PLANESPOTTER` | CLI, notebook | Path to a planespotter YAML config |
| `TRACKING_SIM_RECORDING` | notebook | Recording file, directory or glob |
| `TRACKING_SIM_ORIGIN` | notebook | `LAT,LON[,ALT_M]` of the local frame |
| `TRACKING_SIM_WINDOW` | notebook | `START,END` in ISO 8601 |

Truth builder settings live in `TruthBuilderConfig` (a Pydantic model): segment gap, speed gate, default truth sigma, altitude preference, smoothing method, and whether to drop aircraft on the ground.

## Development

```sh
uv sync --all-extras          # everything, including dev tools
uv run pytest                 # tests (notebook test runs when the notebook extra is installed)
uv run ruff check             # lint
uv run ruff format            # format
uv run mypy                   # strict type check of src/
uv run python scripts/make_fixture.py   # regenerate tests/data (deterministic)
```

CI (GitHub Actions) runs lint and mypy, the core tests on Python 3.11 to 3.13 with **no extras**, and the full suite with all extras.

Conventions, from the design document:

- The core package must install and pass tests with no extras, no database and no network. Tests for optional adapters skip themselves when their extra is missing.
- Every simulation path takes an explicit seed; identical config and seed give identical output.
- Units are SI internally. Conversion from feet, knots and degrees happens only in the truth builder and config loading.
- Work one milestone at a time, and keep [PROGRESS.md](PROGRESS.md) and [DECISIONS.md](DECISIONS.md) current at the end of each session.

## Roadmap

| Milestone | Delivers |
| --- | --- |
| 1. Truth | Truth sources, truth builder, Stone Soup ground truth, notebook. **Implemented** (altitude verdict pending real data). |
| 2. One sensor | `SensorSpec` YAML, rotating 2D radar with beam-crossing timing, noise, Pd, false alarms and radar horizon; seeded replay engine. |
| 3. Tracking and metrics | Tracker factory from config (EKF + GNN baseline, UKF, multiple-model); OSPA, GOSPA and SIAP; one-command comparison table. |
| 4. Scenarios and realism | Scenario files and an immutable run store, terrain masking, radar-equation Pd, `aircraft_json` adapter, passive bearing-only sensor. |
| 5. Map playground | FastAPI service and a MapLibre map UI to place sensors and replay runs. |
| 6. Live and multi-sensor | Live engine, `beast` adapter, multi-sensor fusion. |

## License

[MIT](LICENSE)
