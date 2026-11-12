"""Regenerate the synthetic truth fixtures in tests/data/.

    uv run python scripts/make_fixture.py

The output is deterministic for a given seed, so regenerating it should not
change the files unless the generator itself changed.
"""

from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tracking_sim.truth.sources.recording import write_records
from tracking_sim.truth.sources.sbs import format_sbs_line
from tracking_sim.truth.synthetic import generate_traffic

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "tests" / "data")
    args = parser.parse_args()

    traffic = generate_traffic(seed=args.seed)
    recording = args.out_dir / "synthetic_1h.parquet"
    count = write_records(recording, traffic.records)
    print(f"wrote {count} records for {len(traffic.flights)} aircraft to {recording}")

    # Five minutes of one receiver as a Dump1090 SBS capture. BaseStation
    # timestamps are receiver-local, so write them in a non-UTC zone to keep
    # the time zone handling honest.
    end = traffic.epoch + timedelta(minutes=5)
    tz = ZoneInfo("America/Denver")
    lines = [
        format_sbs_line(r, tz=tz)
        for r in traffic.records
        if r.source_id == "synthetic:rx1" and r.ts < end
    ]
    sbs = args.out_dir / "synthetic_5min.sbs"
    sbs.write_text("\n".join(lines) + "\n", encoding="ascii")
    print(f"wrote {len(lines)} SBS lines to {sbs}")


if __name__ == "__main__":
    main()
