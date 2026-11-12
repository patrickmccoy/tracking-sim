"""The ``tracking-sim`` command line.

Hosts, ports and other site-specific settings come from options or from
environment variables, never from code:

==============================  =======================================
Variable                        Used for
==============================  =======================================
``TRACKING_SIM_SBS_HOST``       Dump1090 host for ``record sbs``
``TRACKING_SIM_SBS_PORT``       BaseStation port (default 30003)
``TRACKING_SIM_SBS_TZ``         Receiver time zone of SBS timestamps
``TRACKING_SIM_RECEIVER``       Receiver name used in ``source_id``
==============================  =======================================
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from tracking_sim.geo import LocalFrame
from tracking_sim.truth.record import TruthRecord, ensure_utc
from tracking_sim.truth.recorder import export_replay, record_stream, summarize
from tracking_sim.truth.source import TruthSource
from tracking_sim.truth.sources.recording import RecordingWriter, read_records
from tracking_sim.truth.sources.sbs import SbsSource

__all__ = ["main"]


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


def parse_time(value: str) -> datetime:
    """Parse an ISO 8601 time; a trailing ``Z`` and naive values mean UTC."""
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--out",
        "-o",
        type=Path,
        required=True,
        help="recording file to write (.parquet or .jsonl)",
    )


def _add_record_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    parser.add_argument("--max-records", type=int, help="stop after this many records")
    parser.add_argument(
        "--flush-every", type=float, default=10.0, help="seconds between flushes (default 10)"
    )


def _sbs_source(args: argparse.Namespace) -> SbsSource:
    if not args.host:
        raise SystemExit("record sbs: give --host or set TRACKING_SIM_SBS_HOST")
    return SbsSource(
        host=args.host,
        port=args.port,
        receiver=args.receiver,
        tz=args.tz,
        use_arrival_time=args.arrival_time,
        altitude_field=args.altitude_field,
    )


def _run_record(args: argparse.Namespace, make_source: Callable[[], TruthSource]) -> int:
    source = make_source()
    with RecordingWriter(args.out) as writer:
        print(f"recording {source.source_id} to {args.out} (Ctrl-C to stop)", file=sys.stderr)
        # Ctrl-C cancels the stream; the writer still closes cleanly.
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(
                record_stream(
                    source,
                    writer,
                    duration_s=args.duration,
                    max_records=args.max_records,
                    flush_every_s=args.flush_every,
                )
            )
    print(f"wrote {writer.count} records to {args.out}", file=sys.stderr)
    return 0


def _cmd_record_sbs(args: argparse.Namespace) -> int:
    return _run_record(args, lambda: _sbs_source(args))


def _cmd_export(args: argparse.Namespace) -> int:
    source = SbsSource(
        path=args.paths,
        receiver=args.receiver,
        tz=args.tz,
        altitude_field=args.altitude_field,
    )
    start = parse_time(args.start) if args.start else None
    end = parse_time(args.end) if args.end else None
    with RecordingWriter(args.out) as writer:
        count = export_replay(source, writer, start, end)
    print(f"wrote {count} records to {args.out}", file=sys.stderr)
    return 0


def _parse_origin(value: str | None, records: list[TruthRecord]) -> LocalFrame:
    if value:
        parts = [float(x) for x in value.split(",")]
        if len(parts) not in (2, 3):
            raise SystemExit("--origin takes LAT,LON or LAT,LON,ALT_M")
        return LocalFrame(*parts)
    lats = [r.lat for r in records if r.lat is not None]
    lons = [r.lon for r in records if r.lon is not None]
    if not lats:
        raise SystemExit("compare: no positions in the reference recording")
    return LocalFrame(sum(lats) / len(lats), sum(lons) / len(lons))


def _cmd_compare(args: argparse.Namespace) -> int:
    from tracking_sim.truth.builder import TruthBuilderConfig, build_truth
    from tracking_sim.truth.compare import assess_altitude_reference, compare_truth

    start = parse_time(args.start) if args.start else None
    end = parse_time(args.end) if args.end else None
    candidate = read_records(args.candidate, start, end)
    reference = read_records(args.reference, start, end)
    frame = _parse_origin(args.origin, reference)
    config = TruthBuilderConfig(default_horizontal_sigma_m=args.sigma)
    report = compare_truth(
        build_truth(candidate, frame, config),
        build_truth(reference, frame, config),
        step_s=args.step,
    )
    print(f"a = {args.candidate}\nb = {args.reference}\n")
    print(report.table())
    if args.altitude:
        print("\naltitude reference of a:")
        print(assess_altitude_reference(candidate, reference, frame, config=config).summary())
    return 0 if report.agrees else 1


def _cmd_info(args: argparse.Namespace) -> int:
    start = parse_time(args.start) if args.start else None
    end = parse_time(args.end) if args.end else None
    summary = summarize(read_records(args.paths, start, end))
    print("\n".join(summary.lines()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracking-sim",
        description="Sensor and tracking simulation playground.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log progress")
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser("record", help="record a live truth source into a recording file")
    record_sources = record.add_subparsers(dest="source", required=True)

    sbs = record_sources.add_parser("sbs", help="Dump1090 SBS-1 BaseStation feed over TCP")
    sbs.add_argument("--host", default=_env("TRACKING_SIM_SBS_HOST"), help="Dump1090 host")
    sbs.add_argument(
        "--port", type=int, default=int(_env("TRACKING_SIM_SBS_PORT", "30003") or 30003)
    )
    _add_sbs_options(sbs)
    _add_output_options(sbs)
    _add_record_limits(sbs)
    sbs.set_defaults(func=_cmd_record_sbs)

    export = commands.add_parser(
        "export", help="convert a time window of captured SBS files into a recording file"
    )
    export_sources = export.add_subparsers(dest="source", required=True)
    export_sbs = export_sources.add_parser("sbs", help="captured SBS-1 text files")
    export_sbs.add_argument("paths", nargs="+", type=Path, help="SBS files (plain or .gz)")
    _add_sbs_options(export_sbs)
    export_sbs.add_argument("--start", help="ISO 8601 start time (inclusive, default: open)")
    export_sbs.add_argument("--end", help="ISO 8601 end time (exclusive, default: open)")
    _add_output_options(export_sbs)
    export_sbs.set_defaults(func=_cmd_export)

    compare = commands.add_parser(
        "compare",
        help="check two recordings agree on the same aircraft (and test altitude reference)",
    )
    compare.add_argument(
        "candidate",
        help="recording under test, e.g. from a second receiver or an exported database",
    )
    compare.add_argument("reference", help="reference recording, e.g. from record sbs")
    compare.add_argument(
        "--origin", help="LAT,LON[,ALT_M] of the local frame (default: reference centroid)"
    )
    compare.add_argument("--start", help="ISO 8601 start time (inclusive)")
    compare.add_argument("--end", help="ISO 8601 end time (exclusive)")
    compare.add_argument("--step", type=float, default=5.0, help="sample spacing, seconds")
    compare.add_argument(
        "--sigma", type=float, default=30.0, help="horizontal truth sigma, metres (default 30)"
    )
    compare.add_argument(
        "--altitude",
        action="store_true",
        help="also test whether the candidate's altitude is barometric or geometric",
    )
    compare.set_defaults(func=_cmd_compare)

    info = commands.add_parser("info", help="summarize recording files")
    info.add_argument("paths", nargs="+", help="recording files, directories or globs")
    info.add_argument("--start", help="ISO 8601 start time (inclusive)")
    info.add_argument("--end", help="ISO 8601 end time (exclusive)")
    info.set_defaults(func=_cmd_info)
    return parser


def _add_sbs_options(sbs: argparse.ArgumentParser) -> None:
    sbs.add_argument(
        "--receiver",
        default=_env("TRACKING_SIM_RECEIVER", "default"),
        help="receiver name; records get source_id sbs:<receiver>",
    )
    sbs.add_argument(
        "--tz",
        default=_env("TRACKING_SIM_SBS_TZ", "UTC"),
        help="IANA time zone of the BaseStation timestamps (Dump1090 uses host local time)",
    )
    sbs.add_argument(
        "--arrival-time",
        action="store_true",
        help="stamp records with arrival time (UTC) instead of the message timestamp",
    )
    sbs.add_argument(
        "--altitude-field",
        choices=["alt_baro_ft", "alt_geom_ft", "alt_unknown_ft"],
        default="alt_baro_ft",
        help="TruthRecord field for the SBS altitude column (default alt_baro_ft)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
