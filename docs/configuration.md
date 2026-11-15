# Configuration files

Runs are configured with three kinds of YAML file:

- a **scenario** ties a run together: truth, terrain, sensors, trackers, seed and metric settings;
- a **sensor** spec describes one sensor;
- a **tracker** spec describes one tracker.

The examples are in [`scenarios/`](../scenarios/), with sensor specs in `scenarios/sensors/` and tracker specs in `scenarios/trackers/`.

Every file is checked when it loads, by Pydantic models in the package. An unknown key, a missing required value or a value out of range stops the load with a message naming the field, so a typo never silently falls back to a default. `${VAR}` and `${VAR:-default}` in any value are replaced from the environment.

Units are the ones people think in: metres, seconds and degrees, converted inside the package. Below, *required* means there is no default. The source of truth for each table is the model named under it.

## Scenario

```yaml
name: kbjc-synthetic
origin: {lat: 39.9088, lon: -105.1172, alt_m: 1729}
truth:
  source: recording
  path: ../tests/data/synthetic_1h.parquet
  window: ["2026-11-01T23:00:00Z", "2026-11-02T00:00:00Z"]
terrain: {source: copernicus, resolution_m: 90}
sensors: [sensors/radar-1.yaml]
trackers: [trackers/ekf-cv-gnn.yaml, trackers/pf-mm-gnn.yaml]
seed: 42
metrics: {cutoff_m: 1000, p: 1, association_threshold_m: 1000}
```

Paths in a scenario are relative to the scenario file. Each entry under `sensors` and `trackers` is either a path to a spec file or the spec itself written inline as a mapping.

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | required | Names the run; stored runs are `<name>-<hash>`. |
| `origin` | the first sensor's site | Centre of the local east-north-up frame the trackers work in: `lat`, `lon`, `alt_m` (default 0). |
| `truth` | required | The traffic to replay (below). |
| `terrain` | none: a smooth earth | Ground for line-of-sight masking (below). |
| `sensors` | required, at least one | Sensor spec files or inline specs. |
| `trackers` | required, at least one | Tracker spec files or inline specs. Every tracker runs on the same detections. |
| `seed` | `0` | Seeds every random draw: detection, noise, clutter and particles. The same scenario and seed give the same run. |
| `metrics` | see below | Scoring settings. |

*Models: `tracking_sim.sim.scenario.ScenarioSpec` and `OriginSpec`.*

### `truth`

| Key | Default | Meaning |
| --- | --- | --- |
| `source` | `recording` | `recording` for a tracking-sim recording (`.parquet`, `.jsonl`, or a directory of them); `sbs` for captured Dump1090 BaseStation text (`.sbs`, `.sbs.gz`). |
| `path` | required | The file, relative to the scenario. |
| `window` | all of it | `[start, end]` in ISO 8601: replay only this part. |
| `tz` | `UTC` | `sbs` only: the time zone of the timestamps, which Dump1090 writes in the receiver host's clock. |
| `aircraft_classes` | none | An optional `icao24,class` CSV that gives aircraft a radar cross-section class for radar-equation detection. Classes are `light`, `small`, `large`, `heavy`, `rotorcraft` and `unknown`. |

*Model: `TruthSpec`.*

### `terrain`

| Key | Default | Meaning |
| --- | --- | --- |
| `source` | required | `copernicus`: the Copernicus elevation model, downloaded once per 1-degree tile; needs the `terrain` extra. `synthetic`: flat ground with one `ridge`. `flat`: level ground at `height_m`. |
| `resolution_m` | `90` | `copernicus` only: `90` (GLO-90) or `30` (GLO-30, much larger). |
| `ridge` | none | `synthetic` only, and required there. Keys: `lat`, `lon`, `azimuth_deg` (`0`), `height_m`, `width_m` (`1500`), `length_m` (`60000`), `base_m` (`0`). |
| `height_m` | `0` | `flat` only. |

*Models: `TerrainSpec` and `RidgeSpec`.*

### `metrics`

| Key | Default | Meaning |
| --- | --- | --- |
| `cutoff_m` | `1000` | OSPA and GOSPA cut-off. A missed or false track costs this much, and no error counts for more. |
| `p` | `1` | OSPA and GOSPA order. |
| `association_threshold_m` | `1000` | SIAP pairs a track with an aircraft when they are closer than this. |

*Model: `MetricsSpec`.*

## Sensors

`type` picks the kind of sensor. Two are implemented: `rotating_radar_2d` and `passive_bearing`.

### Common blocks

These two blocks are the same for both sensor types.

`position`: where the sensor stands.

| Key | Default | Meaning |
| --- | --- | --- |
| `lat`, `lon` | required | The site. |
| `alt_m` | `0` | Ground height at the site, in the same reference as the truth altitudes. The map playground fills it in from terrain. |
| `mast_m` | `10` | Antenna height above that ground. |

`coverage`: where the sensor can see.

| Key | Default | Meaning |
| --- | --- | --- |
| `range_min_m` | `0` | Nothing closer than this is seen. A radar's blind range. |
| `range_max_m` | required | Nothing farther than this is seen. Slant range. |
| `azimuth_deg` | `[0, 360]` | A compass sector `[from, to]` swept clockwise, so `[300, 60]` covers north. |
| `elevation_deg` | `[-90, 90]` | Limits on the elevation angle from the antenna. A low upper limit makes a cone of silence overhead. |
| `radar_horizon` | `true` | Hide targets below the horizon of an earth `refraction_k` times its real radius. |
| `refraction_k` | `4/3` | The standard-atmosphere value. |
| `terrain_masking` | `true` | Hide targets behind terrain, when the scenario has terrain. |

*Models: `tracking_sim.sensors.spec.PositionSpec` and `CoverageSpec`.*

### `rotating_radar_2d`

A rotating surveillance radar. It measures slant range and bearing, but not height.

```yaml
id: radar-1
type: rotating_radar_2d
position: {lat: 39.9088, lon: -105.1172, alt_m: 1729, mast_m: 15}
coverage: {range_min_m: 500, range_max_m: 110000, elevation_deg: [-1, 30]}
scan: {period_s: 5.0, beamwidth_deg: 1.5}
noise: {range_sigma_m: 60, bearing_sigma_deg: 0.2}
detection: {model: constant, pd: 0.9, false_alarm_density: 1.0e-9}
resolution: {range_m: 150, bearing_deg: 1.5}
latency_s: 0.5
```

The top-level keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `id` | required | Unique within the scenario. |
| `position`, `coverage` | required | See [Common blocks](#common-blocks). |
| `scan` | required | How the antenna turns (below). |
| `noise` | required | Measurement noise, one standard deviation (below). |
| `detection` | required | Detection probability and false alarms (below). |
| `resolution` | `0`, `0`: no merging | Targets closer than both `range_m` and `bearing_deg` merge into one detection. |
| `latency_s` | `0` | Detections are reported this long after they are measured. |

`scan`:

| Key | Default | Meaning |
| --- | --- | --- |
| `period_s` | required | Time for one full rotation. |
| `beamwidth_deg` | `1.5` | A target is looked at once per rotation, as the beam passes. |
| `start_azimuth_deg` | `0` | Where the beam points when the run starts (compass). |
| `direction` | `clockwise` | Or `counterclockwise`. |

`noise`:

| Key | Default | Meaning |
| --- | --- | --- |
| `range_sigma_m` | required | Slant-range noise. |
| `bearing_sigma_deg` | required | Bearing noise. |

`detection`:

| Key | Default | Meaning |
| --- | --- | --- |
| `model` | `constant` | `constant`: every look succeeds with probability `pd`. `radar_equation`: Pd comes from SNR, which scales with the aircraft's radar cross-section and falls with range to the fourth power. |
| `pd` | none | `constant` only, and required there. |
| `reference_snr_db`, `reference_range_m` | none | `radar_equation` only, and required there: a 1 m² target at this slant range has this SNR. |
| `pfa` | `1e-6` | `radar_equation`: the false-alarm probability that sets the detection threshold. |
| `swerling` | `1` | `radar_equation`: `1` for a fluctuating target, `0` for a steady one. |
| `rcs_m2` | `light 1`, `small 2`, `large 20`, `heavy 60`, `rotorcraft 3`, `unknown 5` | `radar_equation`: mean RCS per aircraft class. |
| `default_class` | `unknown` | `radar_equation`: the class for aircraft with no known class. |
| `false_alarm_density` | `0` | Clutter: expected false alarms per square metre of coverage per scan, for either model. |

*Models: `RotatingRadar2DSpec`, `ScanSpec`, `NoiseSpec`, `DetectionSpec`, `RcsSpec` and `ResolutionSpec`.*

### `passive_bearing`

A staring passive receiver, such as an ESM or a direction finder. It hears aircraft emissions and measures bearing only, every `update_period_s`. Trackers don't use its detections yet; fusion is milestone 6. They are simulated, stored and drawn on the map.

```yaml
id: esm-kbjc
type: passive_bearing
position: {lat: 39.9088, lon: -105.1172, alt_m: 1713, mast_m: 10}
coverage: {range_max_m: 150000, elevation_deg: [-2, 60]}
update_period_s: 1.0
noise: {bearing_sigma_deg: 1.0}
detection: {model: one_way, reference_snr_db: 20, reference_range_m: 60000, false_alarm_rate: 0.5}
```

The top-level keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `id`, `position`, `coverage` | required | As for the radar. |
| `update_period_s` | `1` | Time between bearing reports. |
| `noise` | required | `bearing_sigma_deg`. |
| `detection` | required | Below. |
| `latency_s` | `0` | As for the radar. |

`detection`:

| Key | Default | Meaning |
| --- | --- | --- |
| `model` | `constant` | `constant`: probability `pd`. `one_way`: Pd comes from SNR, which falls with range squared (a one-way link). |
| `pd` | none | `constant` only, and required there. |
| `reference_snr_db`, `reference_range_m` | none | `one_way` only, and required there. |
| `pfa` | `1e-6` | `one_way`: false-alarm probability. |
| `swerling` | `0` | `one_way`: emitters are steady by default. |
| `false_alarm_rate` | `0` | Mean false bearings per update, spread evenly over the sector. |

*Models: `PassiveBearingSpec`, `PassiveNoiseSpec` and `PassiveDetectionSpec`.*

## Trackers

A tracker spec picks [Stone Soup](https://stonesoup.readthedocs.io/) components from fixed lists, so comparing two trackers is a diff of two files on the same detections.

```yaml
id: ekf-cv-gnn
filter: ekf
motion: {model: constant_velocity, acceleration_noise: 1.0, vertical_acceleration_noise: 0.1}
association: {method: gnn, gate: 3.0}
initiation: {min_points: 3, assumed_height_m: 5000, height_sigma_m: 3000, speed_sigma_mps: 150, vertical_speed_sigma_mps: 10}
deletion: {missed_scans: 3}
```

The tracker's state is `[x, vx, y, vy, z, vz]` in the scenario's local frame, plus turn rate for the particle filter. Detections arrive one radar scan at a time, so "scans" below means tracker updates.

| Key | Default | Meaning |
| --- | --- | --- |
| `id` | required | Unique within the scenario. |
| `filter` | required | `ekf` (extended Kalman), `ukf` (unscented Kalman), or `particle_mm` (multiple-model particle filter). |
| `motion` | constant velocity | How targets are assumed to move (below). |
| `particles` | `count: 1000` | `particle_mm` only: `count`, the particles per confirmed track (at least 50). |
| `association` | `gnn`, gate `3.0` | Which detections update which track (below). |
| `initiation` | below | How tracks start and are confirmed. |
| `deletion` | `missed_scans` `3` | `missed_scans`: drop a confirmed track after this many scans in a row without a detection. |

*Model: `tracking_sim.tracking.spec.TrackerSpec`.*

### `motion`

| Key | Default | Meaning |
| --- | --- | --- |
| `model` | `constant_velocity` | `constant_velocity` for `ekf` and `ukf`. `cv_ct` for `particle_mm`, where it is required: each particle follows constant velocity or a coordinated turn, and may switch. |
| `acceleration_noise` | `1.0` | Horizontal process noise: the constant-velocity model's diffusion coefficient, in m²/s³. Larger values follow manoeuvres better but give noisier tracks and wider gates. |
| `vertical_acceleration_noise` | `0.1` | The same, vertically. |
| `turn_rate_noise_deg` | `1.0` | `cv_ct` only: turn-rate diffusion, in deg²/s³. |
| `switch_probability` | `0.05` | `cv_ct` only: the chance per update of switching between straight and turning. |

*Models: `MotionSpec` and `ParticleSpec`.*

### `association`

| Key | Default | Meaning |
| --- | --- | --- |
| `method` | `gnn` | `gnn` (global nearest neighbour), `pda` (probabilistic data association) or `jpda` (joint PDA). `pda` and `jpda` need `filter: ekf` or `ukf`. |
| `gate` | `3.0` | Mahalanobis distance, for every method: how many standard deviations a detection may be from a track's predicted measurement and still count for it. |
| `prob_detect` | from the radar | `pda`/`jpda` only: a fixed probability of detection instead of the radar's. |
| `clutter_density` | from the radar | `pda`/`jpda` only: fixed false alarms per radian of bearing per metre of range per scan, instead of the radar's. |

Each scan, every track is predicted to the time of the detections near it. A detection is a candidate for a track when its Mahalanobis distance is under `gate`. The distance is measured in (bearing, slant range), using the track's predicted uncertainty plus the radar's noise. What happens next depends on the method:

- **`gnn`** makes the one-to-one assignment of detections to tracks with the smallest total distance. "No detection" counts as a distance of `gate`, so a track takes a detection only when it beats that. A track gets one detection or none.
- **`pda`** doesn't choose. It weighs every candidate by how likely it is to be the aircraft's, against the chance that none is: from the distance, the radar's probability of detection and how much clutter to expect there. The track is updated with the weighted blend, and its uncertainty grows to cover the doubt. Each track is weighed on its own, so two tracks close together can both lean on the same detection.
- **`jpda`** weighs the joint possibilities across tracks that share candidates, so each detection is shared out consistently. It uses Stone Soup's `JPDAwithEHM`, which gives exact JPDA answers faster.

For `pda` and `jpda`, `gate` becomes the probability that the aircraft's own detection falls inside it (1 − e^(−gate²/2) for a 2D measurement: 3 is 98.9%, 4 is 99.97%). Unless `prob_detect` and `clutter_density` are set, both come from the scenario's radar for each track at its predicted range:
- **Clutter:** false alarms are spread evenly over ground area, so in (bearing, range) their density is `false_alarm_density` × range.
- **Probability of detection:** `pd` for a `constant` detection model; for `radar_equation`, the radar's Pd at that range for a `default_class` aircraft, since a tracker can't know each aircraft's size.

Detections left over start tentative tracks. Under `pda` and `jpda`, that means detections no track weighed above "no detection". Tentative tracks are always matched to new detections with `gnn` and the same gate while they are being confirmed.

Choosing a gate:
- Raising it keeps tracks through manoeuvres and missed scans, but lets clutter and crossing aircraft steal more updates (`gnn`) or blur more of them in (`pda`, `jpda`).
- Lowering it does the reverse.
- With `gnn`, a gate of 3 keeps about 99% of a track's true detections, if the track's uncertainty is right.
- A constant-velocity track lags a turning aircraft by more than its uncertainty admits, so `pda` and `jpda` usually want a wider gate.
- Process noise widens the gate too, because a noisier motion model predicts with more uncertainty. That is why `pf-mm-gnn-q50` associates more false alarms (DECISIONS.md, D33).

Before the exact test, a cheap geometric pre-gate drops detections that are plainly out of reach. Its box is 2 km, plus 3 standard deviations of the track's position (one more than `gate` when the gate is wider than 3), plus 400 m/s times the time since the track's last update. It changes speed, not results (D21).

Nearest neighbour and multi-frame assignment (MFA) are not tracker options. Notebook 05 builds them, PDA and JPDA step by step on the same data.

*Model: `AssociationSpec`; wired in `tracking_sim.tracking.factory`.*

### `initiation`

A 2D radar measures no height. A new track therefore starts at an assumed height above the antenna, with a wide uncertainty and zero mean velocity. Every unassociated detection starts a tentative track. A tentative track is confirmed once it has `min_points` associated detections, and dropped sooner than a confirmed track if it stops getting them.

| Key | Default | Meaning |
| --- | --- | --- |
| `min_points` | `3` | Associated detections, including the first, needed to confirm a track (at least 2). |
| `tentative_missed_scans` | `2` | Drop a tentative track after this many scans without a detection. |
| `assumed_height_m` | `5000` | Starting height above the antenna. |
| `height_sigma_m` | `3000` | Its uncertainty. |
| `speed_sigma_mps` | `150` | Prior spread of horizontal speed. |
| `vertical_speed_sigma_mps` | `10` | Prior spread of vertical speed. |
| `turn_rate_sigma_deg` | `3` | `particle_mm` only: prior spread of turn rate, in deg/s. |

*Models: `InitiationSpec` and `DeletionSpec`.*

### The examples

| Spec | Filter | Motion | Notes |
| --- | --- | --- | --- |
| `ekf-cv-gnn` | `ekf` | `constant_velocity` | The baseline. |
| `ukf-cv-gnn` | `ukf` | `constant_velocity` | The baseline with an unscented filter. |
| `pf-mm-gnn` | `particle_mm` | `cv_ct` | Follows turns. |
| `pf-mm-gnn-q50` | `particle_mm` | `cv_ct` | `acceleration_noise: 50`: its particle cloud stays spread in turns, and its gate is wider. |
