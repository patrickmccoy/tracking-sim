# Exporters: getting other data into tracking-sim

tracking-sim reads truth only in the formats a Dump1090-family decoder
(dump1090, dump1090-fa, readsb) produces. Today that is **SBS-1
BaseStation text**; `aircraft.json` and Beast frames are planned (see the
roadmap in the top-level README). Data held anywhere else, such as a
database of historical flights, reaches tracking-sim through an
**exporter** that writes SBS.

Exporters live with the system that owns the data, not in this repository:
a database exporter belongs in that database's project, next to the schema
it reads. This directory holds the contract they write to. Small, generic
conversion helpers may live here later. Nothing in `tools/` is part of the
`tracking_sim` package, and the package never imports from it.

## What an exporter produces

A text file of SBS-1 `MSG` lines, as Dump1090 serves on TCP port 30003.

- **Files:** plain text (`.sbs`) or gzip (`.sbs.gz`). ASCII, one message per
  line, `\r\n` (as Dump1090 writes) or `\n` line endings. Several files can
  be passed at once.
- **Order:** time order is recommended but not required. tracking-sim sorts
  records by timestamp on replay.
- **Other lines:** anything that doesn't start with `MSG` (`SEL`, `ID`,
  `AIR`, `STA`, `CLK`, comments) is ignored, as are malformed `MSG` lines.

### Line layout

Each line has 22 comma-separated fields. Fields 1 to 10 identify the
message and fields 11 to 22 carry data. Empty fields are fine and mean
"not in this message". Field numbers below are 1-based, as in most SBS
documentation.

| # | Field | Format | Used by tracking-sim |
| --- | --- | --- | --- |
| 1 | Message type | `MSG` | Required |
| 2 | Transmission type | `1` to `8` | See below |
| 3 | Session ID | integer | Ignored; write `1` |
| 4 | Aircraft ID | integer | Ignored; write `1` |
| 5 | Hex ident | 24-bit ICAO address, 6 hex digits | Required. Any case; stored lower case |
| 6 | Flight ID | integer | Ignored; write `1` |
| 7 | Date generated | `YYYY/MM/DD` | Required: the record's time |
| 8 | Time generated | `HH:MM:SS.fff` (or `HH:MM:SS`) | Required: the record's time |
| 9 | Date logged | `YYYY/MM/DD` | Used only with `time_field="logged"`; write the same as field 7 |
| 10 | Time logged | `HH:MM:SS.fff` | As field 9 |
| 11 | Callsign | up to 8 characters, trailing spaces trimmed | `callsign` |
| 12 | Altitude | feet | See [Altitude](#altitude) |
| 13 | Ground speed | knots | `gs_kt` |
| 14 | Track | degrees true, 0 to 360 | `track_deg` |
| 15 | Latitude | decimal degrees, WGS84 | `lat` (both 15 and 16, or neither) |
| 16 | Longitude | decimal degrees, WGS84 | `lon` |
| 17 | Vertical rate | feet per minute, climb positive | `vrate_fpm` |
| 18 | Squawk | 4 octal digits | Ignored |
| 19 | Alert flag | | Ignored |
| 20 | Emergency flag | | Ignored |
| 21 | SPI flag | | Ignored |
| 22 | On ground | `-1` (or `1`) on ground, `0` airborne, empty unknown | `on_ground` |

Use these transmission types, so the output looks like a real Dump1090 feed
and other SBS tools can read it too:

| Type | Use for | Fields to fill |
| --- | --- | --- |
| `1` | Identification | 11 (callsign). Write one when a callsign first appears or changes |
| `2` | Surface position | 12, 13, 14, 15, 16, 22 = `-1` |
| `3` | Airborne position | 12, 15, 16, 22 = `0` |
| `4` | Airborne velocity | 13, 14, 17 |
| `5` | Altitude only | 12 |
| `8` | On-ground flag only | 22 |

tracking-sim reads whichever data fields are present, whatever the
transmission type, so a combined row (position and velocity together) may go
in one line. Keeping them apart matches what a decoder sends and is safer
for other tools.

Example (an airborne position, then a velocity, for one aircraft):

```
MSG,3,1,1,A1B2C3,1,2026/11/01,23:00:00.250,2026/11/01,23:00:00.250,,12000,,,39.912345,-105.123456,,,,,,0
MSG,4,1,1,A1B2C3,1,2026/11/01,23:00:00.700,2026/11/01,23:00:00.700,,,250.0,90.0,,,-640,,,,,
```

### Timestamps

SBS timestamps carry no time zone. Dump1090 writes the receiver host's local
time, so tracking-sim takes the zone as a setting (`--tz`, or
`TRACKING_SIM_SBS_TZ`; the default is UTC).

- **Write UTC** unless there is a reason not to, and say so in the
  exporter's documentation. The reader then needs no `--tz` at all.
- Write milliseconds (`HH:MM:SS.fff`). Truncate rather than round, as
  Dump1090 does.
- Use the time the message was received or generated, not the time of the
  export.

### Altitude

Field 12 is in feet. In a live Dump1090 feed it is barometric (pressure)
altitude, so tracking-sim stores it as `alt_baro_ft` by default.

- If the exported altitude **is barometric**, there is nothing to do.
- If it is **geometric** (GNSS) height, or the exporter **can't tell**, say
  so in the exporter's documentation. Readers then pass
  `--altitude-field alt_geom_ft` or `--altitude-field alt_unknown_ft`.
- Convert from metres if the source stores metres. Don't convert between
  barometric and geometric altitude: that needs the local pressure.

`tracking-sim compare EXPORT RECEIVER --altitude` checks which reference an
exported altitude follows, by comparing it with a receiver's own recording
of the same hour; see DECISIONS.md, D10.

### Units and values

| Quantity | Unit in SBS | Convert from |
| --- | --- | --- |
| Altitude | feet | metres: divide by 0.3048 |
| Ground speed | knots | m/s: × 3600/1852; km/h: ÷ 1.852; mph: × 1609.344/1852 |
| Track | degrees true | |
| Vertical rate | ft/min | m/s: × 196.85 |
| Position | decimal degrees, WGS84 | Write at least 5 decimals (about 1 m); Dump1090 writes 5 or 6 |

Leave out any value you don't have; don't write placeholders such as `0` or
`-1`, which would be read as real values. Positions outside ±90°/±180° are
dropped.

## What an exporter should do

- **Take a time window** (`--start`, `--end`, ISO 8601, start inclusive and
  end exclusive), so its output lines up with a receiver recording of the
  same hour.
- **Never modify the source.** For a database, connect as a read-only user
  and issue only `SELECT`s.
- **Keep credentials and site details out of code**: take them from
  environment variables or a config file that isn't committed.
- **Export what was received, not what was smoothed.** tracking-sim does its
  own deduplication, outlier rejection and smoothing, and it needs the raw
  scatter to estimate truth quality. If the source only has smoothed or
  resampled data, say so in its documentation.
- **Document its output**: the time zone of the timestamps, the altitude
  reference, and any fields it never fills.

## Checking an export

Read the export with tracking-sim. A clean conversion and sensible counts
are a good first check:

```sh
uv run tracking-sim export sbs export.sbs.gz --receiver mysource \
    --altitude-field alt_unknown_ft -o data/mysource.parquet
uv run tracking-sim info data/mysource.parquet
```

If you have a receiver recording of the same hour, compare the two. The same
aircraft should agree within the truth sigma, and `--altitude` reports the
altitude reference:

```sh
uv run tracking-sim compare data/mysource.parquet data/receiver.parquet --altitude
```

The notebook takes an export directly as its second source:
`TRACKING_SIM_SECOND=export.sbs.gz`.

## Formats to come

When the `aircraft_json` (milestone 4) and `beast` (milestone 6) adapters
exist, an exporter may write those formats instead, to carry what SBS can't:
separate barometric and geometric altitude, NIC and NACp, or raw Mode S
frames. This document will describe them then. Until they exist, write SBS.
