from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def fixture_path() -> Path:
    return DATA / "synthetic_1h.parquet"
