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
``TRACKING_SIM_PLANESPOTTER``   Path to a planespotter YAML config
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

from tracking_sim.truth.record import ensure_utc
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


def _planespotter_source(args: argparse.Namespace) -> TruthSource:
    if not args.config:
        raise SystemExit("planespotter: give --config or set TRACKING_SIM_PLANESPOTTER")
    from tracking_sim.truth.sources.planespotter import PlanespotterSource

    return PlanespotterSource.from_yaml(args.config)


def _cmd_record_sbs(args: argparse.Namespace) -> int:
    return _run_record(args, lambda: _sbs_source(args))


def _cmd_record_planespotter(args: argparse.Namespace) -> int:
    return _run_record(args, lambda: _planespotter_source(args))


def _cmd_export(args: argparse.Namespace) -> int:
    if args.source == "sbs":
        source: TruthSource = SbsSource(
            path=args.paths,
            receiver=args.receiver,
            tz=args.tz,
            altitude_field=args.altitude_field,
        )
    else:
        source = _planespotter_source(args)
    start = parse_time(args.start) if args.start else None
    end = parse_time(args.end) if args.end else None
    with RecordingWriter(args.out) as writer:
        count = export_replay(source, writer, start, end)
    print(f"wrote {count} records to {args.out}", file=sys.stderr)
    return 0


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

    live_ps = record_sources.add_parser(
        "planespotter", help="poll a planespotter database (needs the planespotter extra)"
    )
    _add_planespotter_options(live_ps)
    _add_output_options(live_ps)
    _add_record_limits(live_ps)
    live_ps.set_defaults(func=_cmd_record_planespotter)

    export = commands.add_parser(
        "export", help="copy a time window from a replay source into a recording file"
    )
    export_sources = export.add_subparsers(dest="source", required=True)
    export_sbs = export_sources.add_parser("sbs", help="captured SBS-1 text files")
    export_sbs.add_argument("paths", nargs="+", type=Path, help="SBS files (plain or .gz)")
    _add_sbs_options(export_sbs)
    export_ps = export_sources.add_parser("planespotter", help="a planespotter database")
    _add_planespotter_options(export_ps)
    for sub in (export_sbs, export_ps):
        sub.add_argument("--start", help="ISO 8601 start time (inclusive, default: open)")
        sub.add_argument("--end", help="ISO 8601 end time (exclusive, default: open)")
        _add_output_options(sub)
        sub.set_defaults(func=_cmd_export)

    info = commands.add_parser("info", help="summarize recording files")
    info.add_argument("paths", nargs="+", help="recording files, directories or globs")
    info.add_argument("--start", help="ISO 8601 start time (inclusive)")
    info.add_argument("--end", help="ISO 8601 end time (exclusive)")
    info.set_defaults(func=_cmd_info)
    return parser


def _add_planespotter_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=_env("TRACKING_SIM_PLANESPOTTER"),
        help="planespotter YAML config (see scenarios/truth/planespotter.example.yaml)",
    )


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
