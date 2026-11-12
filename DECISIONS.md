# Decisions

Each design decision with its reason. Newest last.

## D1. Packaging: uv, hatchling, src layout, Python 3.11+

`uv` manages the environment and lockfile; `hatchling` builds the wheel from
`src/tracking_sim`. Optional features (`planespotter`, `notebook`) are extras so
the core installs with one command and no database driver. Dev tools live in a
`dev` dependency group that `uv sync` installs by default.

## D2. Stone Soup pinned to 1.9.x

The design warns that Stone Soup class names were written from memory.
`pyproject.toml` allows `>=1.9,<1.10` and `uv.lock` pins 1.9.1, the version
every class used here was checked against.

## D3. CI runs the core tests without extras

The `test-core` job installs no extras, so any test that needs SQLAlchemy or
Jupyter must skip itself when the dependency is missing. A separate job runs
everything with all extras.

## D4. Timestamps are aware UTC datetimes everywhere

`TruthRecord.ts` is always an aware UTC datetime; naive inputs are taken as
UTC. Stone Soup accepts aware datetimes, so the same values flow into
`GroundTruthState` without conversion and mixed naive/aware comparisons can't
happen.

## D5. The fixture is synthetic and seeded

There is no shareable real recording yet, and the core tests must run with no
network or database. `tracking_sim.truth.synthetic` generates decoder-shaped
records with known defects (duplicates, bad decodes, a gap, a taxiing
aircraft) and keeps the noise-free trajectories, so tests can measure the
truth builder against a known answer. Replace or supplement it with a small
real recording once one can be published.

## D6. SBS time zone and altitude reference come from config

BaseStation lines carry no time zone, and Dump1090 writes the receiver host's
local time. `SbsSource` takes an IANA `tz` (default UTC) or can stamp live
records with arrival time. SBS altitude is pressure altitude in standard
Dump1090 output, so it maps to `alt_baro_ft`, with `altitude_field` as an
override for forks that differ.
