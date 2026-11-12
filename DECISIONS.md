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
