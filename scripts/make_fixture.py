"""Regenerate the synthetic truth fixture in tests/data/.

    uv run python scripts/make_fixture.py

The output is deterministic for a given seed, so regenerating it should not
change the file unless the generator itself changed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tracking_sim.truth.sources.recording import write_records
from tracking_sim.truth.synthetic import generate_traffic

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--out", type=Path, default=ROOT / "tests" / "data" / "synthetic_1h.parquet"
    )
    args = parser.parse_args()

    traffic = generate_traffic(seed=args.seed)
    count = write_records(args.out, traffic.records)
    print(f"wrote {count} records for {len(traffic.flights)} aircraft to {args.out}")


if __name__ == "__main__":
    main()
