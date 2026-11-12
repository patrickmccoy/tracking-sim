# Decisions

Each design decision with its reason. Newest last.

## D1. Packaging: uv, hatchling, src layout, Python 3.11+

`uv` manages the environment and lockfile; `hatchling` builds the wheel from
`src/tracking_sim`. Optional features are extras so the core installs with
one command; today the only extra is `notebook` (a `planespotter` extra
existed until D11 removed it). Dev tools live in a
`dev` dependency group that `uv sync` installs by default.

## D2. Stone Soup pinned to 1.9.x

The design warns that Stone Soup class names were written from memory.
`pyproject.toml` allows `>=1.9,<1.10` and `uv.lock` pins 1.9.1, the version
every class used here was checked against.

## D3. CI runs the core tests without extras

The `test-core` job installs no extras, so any test that needs Jupyter must
skip itself when the dependency is missing. A separate job runs
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

## D7. Planespotter schema lives in config, altitude goes to `alt_unknown_ft`

*Superseded by D11: the adapter was removed, and planespotter data now
arrives as SBS from an exporter in the planespotter repository. The
read-only rule carries over to exporters (tools/README.md).*

The planespotter schema (table and column names, units, timestamp semantics)
was not available when the adapter was written, so `PlanespotterConfig`
describes it and the code assumes nothing. Until the altitude reference is
verified, `altitude_reference: unknown` writes it to `alt_unknown_ft`.

Read-only is enforced three ways: the adapter only builds `SELECT`
statements, MySQL sessions are set `READ ONLY` on connect, and the docs
require a dedicated read-only user. Credentials come from `${VAR}`
references in the YAML.

Live mode polls the database for rows at least `live_delay_s` old instead of
using `/flights/stream`, whose format has not been confirmed. Swap it in once
it is.

## D8. Truth smoothing: per-axis cubic smoothing splines chosen by GCV

Each segment's ENU axes are fitted with `scipy.interpolate.make_smoothing_spline`,
with the smoothing parameter picked by generalised cross-validation unless
`smoothing_lam` is set. On the synthetic fixture this cuts the median
horizontal error from about 9.4 m (raw positions, 8 m noise per axis) to about
6.5 m and gives speeds within 0.5 m/s median, with no hand tuning per
segment. Velocity is the spline derivative; reported ADS-B velocity is kept
alongside for checking, not fused. A Kalman smoother that also uses the
velocity reports is a possible later upgrade. `smoothing: linear` is
available for debugging, and segments with fewer than five points always
interpolate linearly. Trajectories never extrapolate past their segment.

## D9. Outlier gate compares each point with both neighbours

A point is dropped only when the steps to both neighbours exceed
`max_speed_mps * dt + slack` while the neighbours agree with each other, so a
single bad decode can't take its good neighbours with it. Two consecutive
bad decodes that agree with each other survive. That is rare and is left
for now.

## D10. Settling the planespotter altitude reference: tooling now, verdict pending

The design asks milestone 1 to resolve whether planespotter's stored
altitude is barometric or geometric. That needs either the decoder code path
planespotter reads, or the database next to a live receiver, and neither
was available in the session that built milestone 1. What exists instead:

* `tracking_sim.truth.compare.assess_altitude_reference` compares a
  candidate source's altitude with a reference receiver's barometric and
  geometric altitude for the same aircraft and times, and returns `baro`,
  `geom` or `inconclusive` with the numbers behind it.
* `tracking-sim compare CANDIDATE REFERENCE --altitude` runs it on two
  recordings.

Procedure, about an hour of wall time (updated for D11):

1. Record a reference from the same receiver planespotter uses:
   `tracking-sim record sbs --host <receiver> --tz <zone> -o data/ref.parquet --duration 3600`.
2. Export the same hour as SBS with planespotter's exporter (in the
   planespotter repository) and convert it, reading the altitude as unknown:
   `tracking-sim export sbs data/ps.sbs.gz --receiver planespotter --altitude-field alt_unknown_ft -o data/ps.parquet`.
3. `tracking-sim compare data/ps.parquet data/ref.parquet --altitude`.

SBS only carries barometric altitude, so the reference has no geometric
altitude, and a `baro` verdict is only convincing on a day when the local
altimeter setting is well away from 29.92 inHg: then a geometric source
would differ from the receiver's barometric altitude by hundreds of feet. On
a day near standard pressure, both answers fit. Once the `aircraft_json`
adapter exists (milestone 4), record that as the reference instead to get
geometric altitude too. When settled, record the result here; if it is
barometric, read planespotter exports with the default
`--altitude-field alt_baro_ft`.

## D11. Truth enters only through Dump1090 outputs; other data arrives via exporters

The planespotter adapter tied the package to one site's database: a schema
config, SQLAlchemy and a MySQL driver, and a polling live mode. Now package
adapters exist only for formats a Dump1090-family decoder produces, so the
framework is usable by anyone with a receiver: SBS-1 BaseStation text today
(live over TCP or replayed from captured files), with `aircraft_json`
(milestone 4) and `beast` (milestone 6) still planned.

Data held anywhere else reaches the framework as SBS, written by an
exporter that lives with the system that owns the data. Planespotter's
exporter belongs in the planespotter repository, next to the schema it
reads, so this project carries no database code, schema config or
credentials handling at all. What this project owns is the contract:
`tools/README.md` specifies the SBS lines exporters must write (layout,
transmission types, time zone, altitude reference, units), how they should
behave (time window, read-only, raw rather than smoothed data, documented
output) and how to check an export with `tracking-sim compare`.

Consequences:

* An SBS export carries one altitude column and no NIC or NACp, so the
  planespotter altitude check against a receiver's SBS needs a day with
  non-standard pressure (D10) until `aircraft_json` brings geometric
  altitude.
* Live mode for planespotter (database polling) is gone; live truth comes
  from the receiver.
