from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

sqlalchemy = pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import mysql  # noqa: E402

from tracking_sim.config import load_yaml_model  # noqa: E402
from tracking_sim.truth import TruthSource  # noqa: E402
from tracking_sim.truth.sources.planespotter import (  # noqa: E402
    PlanespotterConfig,
    PlanespotterSource,
    set_session_read_only,
)

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)


def make_db(kind: str, url: str = "sqlite://") -> sqlalchemy.Engine:
    """A sqlite stand-in for planespotter with deliberately odd names."""
    engine = sqlalchemy.create_engine(url)
    ts_type = "TIMESTAMP" if kind == "datetime" else "REAL"
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE TABLE pos_hist (hex TEXT, seen {ts_type}, la REAL, lo REAL, alt_m REAL)"
        )
        conn.exec_driver_sql(
            f"CREATE TABLE vel_hist (hex TEXT, seen {ts_type}, spd REAL, hdg REAL, vs REAL)"
        )

        def stamp(seconds: float) -> object:
            ts = T0 + timedelta(seconds=seconds)
            if kind == "epoch_s":
                return ts.timestamp()
            # Naive local (UTC-7) wall time, as a MySQL DATETIME would hold it.
            local = (ts - timedelta(hours=7)).replace(tzinfo=None)
            return local.isoformat(sep=" ", timespec="microseconds")

        conn.exec_driver_sql(
            "INSERT INTO pos_hist VALUES (?, ?, ?, ?, ?)",
            [
                ("ABC123", stamp(0), 40.0, -105.0, 3048.0),
                ("ABC123", stamp(10), 40.01, -105.0, None),
                ("DEF456", stamp(5), 39.0, -104.0, 1000.0),
                ("DEF456", stamp(20), None, None, 1000.0),
            ],
        )
        conn.exec_driver_sql(
            "INSERT INTO vel_hist VALUES (?, ?, ?, ?, ?)",
            [("ABC123", stamp(2), 128.6111, 90.0, 5.08), ("ABC123", stamp(30), None, None, None)],
        )
    return engine


def config(kind: str) -> PlanespotterConfig:
    return PlanespotterConfig.model_validate(
        {
            "url": "sqlite://",
            "receiver": "test",
            "positions": {
                "name": "pos_hist",
                "columns": {
                    "icao24": "hex",
                    "ts": "seen",
                    "lat": "la",
                    "lon": "lo",
                    "altitude": "alt_m",
                },
            },
            "velocities": {
                "name": "vel_hist",
                "columns": {
                    "icao24": "hex",
                    "ts": "seen",
                    "gs": "spd",
                    "track": "hdg",
                    "vrate": "vs",
                },
            },
            "time": {"kind": kind, "timezone": "Etc/GMT+7"},
            "units": {"altitude": "m", "speed": "mps", "vrate": "mps"},
        }
    )


@pytest.mark.parametrize("kind", ["datetime", "epoch_s"])
def test_replay_maps_schema_units_and_time(kind: str) -> None:
    source = PlanespotterSource(config(kind), engine=make_db(kind))
    assert isinstance(source, TruthSource)
    records = list(source.replay())
    assert [(r.icao24, (r.ts - T0).total_seconds()) for r in records] == [
        ("abc123", 0.0),
        ("abc123", 2.0),
        ("def456", 5.0),
        ("abc123", 10.0),
    ]
    first, velocity = records[0], records[1]
    assert first.source_id == "planespotter:test"
    assert first.alt_unknown_ft == pytest.approx(10_000.0)
    assert first.alt_baro_ft is None and first.alt_geom_ft is None
    assert velocity.gs_kt == pytest.approx(250.0, abs=0.01)
    assert velocity.vrate_fpm == pytest.approx(1000.0, abs=0.1)
    assert records[3].altitude_ft is None


def test_window_and_altitude_reference() -> None:
    cfg = config("datetime").model_copy(update={"altitude_reference": "baro"})
    source = PlanespotterSource(cfg, engine=make_db("datetime"))
    records = list(source.replay(T0 + timedelta(seconds=1), T0 + timedelta(seconds=10)))
    assert [(r.ts - T0).total_seconds() for r in records] == [2.0, 5.0]
    assert records[1].alt_baro_ft == pytest.approx(1000.0 / 0.3048)


def test_velocity_table_is_optional() -> None:
    cfg = config("datetime").model_copy(update={"velocities": None})
    records = list(PlanespotterSource(cfg, engine=make_db("datetime")).replay())
    assert all(r.has_position for r in records)


def test_queries_are_select_only() -> None:
    source = PlanespotterSource(config("datetime"), engine=make_db("datetime"))
    for query in (source.position_query(T0, None), source.velocity_query(None, T0)):
        sql = str(query.compile(dialect=mysql.dialect())).lstrip().upper()
        assert sql.startswith("SELECT")
        assert not any(word in sql for word in ("INSERT", "UPDATE", "DELETE", "DROP"))


def test_mysql_sessions_are_read_only() -> None:
    pytest.importorskip("pymysql")
    cfg = config("datetime").model_copy(update={"url": "mysql+pymysql://u@h/db"})
    engine = PlanespotterSource(cfg).engine  # creating the engine does not connect
    assert sqlalchemy.event.contains(engine, "connect", set_session_read_only)

    class FakeCursor:
        executed: ClassVar[list[str]] = []

        def execute(self, sql: str) -> None:
            self.executed.append(sql)

        def close(self) -> None:
            pass

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    set_session_read_only(FakeConnection())
    assert FakeCursor.executed == ["SET SESSION TRANSACTION READ ONLY"]


def test_example_config_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANESPOTTER_DB_PASSWORD", "secret")
    cfg = load_yaml_model(PlanespotterConfig, ROOT / "scenarios/truth/planespotter.example.yaml")
    assert "secret" in cfg.url and cfg.altitude_reference == "unknown"
    monkeypatch.delenv("PLANESPOTTER_DB_PASSWORD")
    with pytest.raises(KeyError):
        load_yaml_model(PlanespotterConfig, ROOT / "scenarios/truth/planespotter.example.yaml")


def test_cli_export_writes_a_recording(tmp_path: Path) -> None:
    from tracking_sim.cli import main
    from tracking_sim.truth.sources.recording import read_records

    db = tmp_path / "ps.sqlite"
    make_db("epoch_s", f"sqlite:///{db}").dispose()
    cfg = config("epoch_s").model_dump()
    cfg["url"] = f"sqlite:///{db}"
    path = tmp_path / "ps.yaml"
    path.write_text(yaml.safe_dump(cfg))
    out = tmp_path / "ps.parquet"
    main(
        [
            "export",
            "planespotter",
            "--config",
            str(path),
            "--end",
            "2026-11-01T23:00:06Z",
            "-o",
            str(out),
        ]
    )
    assert [r.icao24 for r in read_records(out)] == ["abc123", "abc123", "def456"]
