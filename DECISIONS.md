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

## D12. The 2D radar measures slant range, in Stone Soup's conventions

Decided with the milestone 2 plan: real 2D surveillance radars measure the
straight-line distance to an aircraft, so the simulated one does too, and
trackers see the resulting altitude-dependent range error (about 500 m for
an aircraft 10 km up at 100 km) as they would with real data. Stone Soup's
`CartesianToBearingRange` measures ground range, so
`CartesianToBearingSlantRange` keeps its `[bearing, range]` layout and its
bearing convention (counter-clockwise from the sensor's east, a `Bearing`)
but measures 3D range from the antenna. Using Stone Soup's conventions
rather than compass azimuth means its filters, initiators and metrics work
unchanged in milestone 3; compass azimuth lives in metadata and tables for
people. The Jacobian is Stone Soup's numerical default.
`inverse_function` needs a height a 2D radar does not measure, so it takes
an assumed one (`inverse_height_m`).

## D13. Each sensor measures in its own frame, related to the scenario frame exactly

Bearings are measured from the sensor's own north. Far from the scenario
origin, meridian convergence rotates that north by a bearing-noise-sized
angle (about 0.3 degrees at 50 km east at 40 degrees N), so `SensorSite`
holds the exact rotation between the two ENU frames (both are rotations of
ECEF) instead of assuming they are parallel. The sensor's ground height
uses the same reference as truth altitudes (the builder treats reported
altitude as height above the ellipsoid); the geoid offset between that and
MSL is the same for both and cancels.

The radar horizon is the textbook 4/3-earth formula, with the target's
height above the site's ground taken from the sensor frame plus the
`d^2 / 2R` curvature drop. The elevation gate uses geometric elevation;
refraction raises apparent elevation by a few hundredths of a degree at
these ranges, which is ignored.

## D14. Observations are timed to the beam crossing

A rotating radar sees an aircraft when its beam crosses it, up to a full
period after the scan starts: over a kilometre at 250 m/s on a 5 s scan.
`BeamSchedule` finds crossings where the beam's phase relative to the
target is an integer, on a grid of the target's unwrapped azimuth, then
refines them by fixed-point iteration against the real trajectory. Scan
windows are fixed periods from the run start, so an aircraft near the
start azimuth moving against the beam can be crossed twice in one window
(and one moving with it, in none), as on a real radar. Crossings of the
same aircraft are always close to a period apart.

## D15. Detection draws, resolution and false alarms

- Pd and noise are drawn for every opportunity (a crossing that passed the
  geometry gates), detected or not, in time order, so one target's noise
  does not depend on another's detection.
- Achieved Pd counts opportunities, not detections: targets merged by the
  resolution cell each count as detected. That makes it directly comparable
  with the configured Pd, which is what milestone 2 checks.
- Unresolved targets (closer than the cell in both slant range and azimuth,
  single linkage on true values) give one plot at the mean of their noisy
  measurements, linked to the first of them, with all of them listed.
- False alarms are Poisson per scan with mean `density x area`, uniform over
  the coverage area in (azimuth, slant range), and timed to the beam.
- Segments without altitude are skipped, and the skips reported: slant
  range and the horizon depend on height, and the builder places such
  segments at the frame origin's height, which would be wrong for a radar.

## D16. Random streams per sensor and per purpose

`sensor_streams(seed, sensor_id)` seeds a `SeedSequence` with the run seed
and a CRC of the sensor id, and spawns separate generators for detection
draws, noise and clutter. The same seed reproduces a run; adding or
reordering sensors leaves each sensor's draws alone; and changing one
purpose's parameters (say, clutter density) leaves the other draws as they
were, so two configurations can be compared on common random numbers.

## D17. pandas joins the notebook extra

The notebooks do enough table work (grouping, binning) that pandas reads
more plainly than Arrow compute. It is only in the `notebook` extra; the
package itself stays on NumPy and PyArrow.

## D18. The multiple-model tracker is Stone Soup's particle filter

Decided with the milestone 3 plan. Stone Soup 1.9.1 has no Kalman-based
IMM; its multiple-model support is `MultiModelPredictor` and
`MultiModelParticleUpdater`. The tracker uses them with two models,
constant velocity and a coordinated turn (constant velocity vertically),
switching with probability 0.05 per update.

Stone Soup sets state dimensions a model doesn't map to zero, and
`ConstantTurn` works on `[x, vx, y, vy, w]`, so this tracker's state is
`[x, vx, y, vy, w, z, vz]`; the constant-velocity model maps every
dimension but `w`, so straight-flying particles hold zero turn rate.
Detections are re-wrapped with 7D copies of the sensor's measurement model
(`TrackerPipeline.adapt`). Tentative tracks run on an EKF with the same
layout, and particles (1,000) are sampled only when a track is confirmed,
which keeps the hundreds of clutter-born tentative tracks cheap.

## D19. Tracks carry height with a vague prior; metrics are horizontal

Decided with the plan: trackers keep a full 3D state although the 2D radar
measures no height. `SlantRangeInitiator` places a new track 5,000 m above
the antenna with 3,000 m of uncertainty and linearises position in
(bearing, slant range, height), so the height guess shows up as
ground-range uncertainty near the radar, where it matters, instead of
Stone Soup's default, which would give height almost no variance. Slant
range barely informs height afterwards, so it mostly stays near the prior,
and a wrong guess leaves a ground-range bias of a few hundred metres at
short range (visible in notebook 03). All metrics are therefore on
horizontal position.

## D20. Particle randomness is seeded by content, not call order

The plan said to seed numpy's global state before each tracker. That isn't
enough: Stone Soup's particle predictor, resampler and measurement
prediction draw from global state in call order, and in a multi-target
tracker that order follows set iteration over tracks, which depends on
memory addresses. `tracking.particle` gives every draw its own generator,
seeded from the run seed and the content being drawn for (particle state,
time), so tracks depend only on seed and data. The measurement prediction
used for gating is computed without a random noise draw, with the
measurement noise added to its covariance instead. Nothing uses numpy's
global random state.

## D21. A geometric pre-gate in front of Stone Soup's hypothesiser

`DistanceHypothesiser` evaluates every track-detection pair in Python;
with about 38 false alarms a scan, each starting a tentative track, that
made an hour of tracking take tens of minutes. `PreGatedHypothesiser`
first drops detections outside a generous box around each track's
extrapolated position in (bearing, slant range): 2 km, plus 3 sigma of the
track's position, plus 400 m/s times the time since its last state. The
exact gate is still Stone Soup's Mahalanobis distance, and a test shows
identical tracks with and without the pre-gate (about 30x faster with).

## D22. Scoring: coverage-only truth on a scan grid, reported tracks only

Decided with the plan: trackers are scored only on aircraft the radar
could see. Metrics need common timestamps, so both sides go on the end of
every scan of the first sensor:

- truth counts at a scan end if the aircraft had a detection opportunity
  (a crossing that passed every geometry gate) in that scan, at its true
  state then; each continuous stretch of visibility is a separate truth
  path (`<segment>#<n>`), because Stone Soup's SIAP assumes truth exists
  throughout an association, and an aircraft re-entering coverage is a new
  object to the radar;
- a track counts from its confirmation (the states before it belong to the
  tentative track) to its last state, reporting its latest state
  extrapolated at constant velocity to the scan end.

With several sensors the grid is still the first sensor's scans; revisit
in milestone 6.

## D23. Metric settings

OSPA and GOSPA use a 1 km cut-off with p = 1 (GOSPA's alpha = 2, so it
splits into localisation, missed and false parts). SIAP pairs tracks with
truth by `TrackToTruth` within 1 km for 3 consecutive grid times (ending
after 2 misses). Track latency is per visibility stretch, from its first
grid time to the start of its first association; stretches never
associated are counted as "never tracked" rather than given an infinite
latency. These are scenario settings (`metrics:`).

## D24. A minimal scenario file and `tracking-sim run`

Decided with the plan: the done-when command takes a scenario YAML using a
subset of the design's format (name, origin, truth source/path/window,
sensor and tracker spec files, seed, metrics), with paths relative to the
file, so milestone 4 extends it rather than replacing it. `--out` writes a
plain directory (resolved config, detections, opportunities, tracks per
tracker, metrics.json, the table); the immutable, hash-addressed run store
is milestone 4.
