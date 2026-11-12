# Test fixtures

All files here are **synthetic**, produced by `tracking_sim.truth.synthetic`
with a fixed seed. They imitate decoder output (sparse position and velocity
messages, a second receiver's duplicates, three bad-decode position jumps, a
two-minute coverage gap, and a taxiing aircraft) but are not real traffic.

| File | Contents | Regenerate with |
| --- | --- | --- |
| `synthetic_1h.parquet` | One hour, 14 aircraft, receivers `synthetic:rx1` and `synthetic:rx2`, recording format | `uv run python scripts/make_fixture.py` |
