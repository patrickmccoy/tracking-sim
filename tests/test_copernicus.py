import io
import urllib.error
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tracking_sim.geo import copernicus
from tracking_sim.geo.copernicus import area_around, load_area, read_tile, tile_name, tile_url

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("imagecodecs")

SCALE = 1.0 / 120  # a coarse 30 arc-second test tile: 120 x 120 pixels a degree


def write_tile(path: Path, lat: int, lon: int, heights: np.ndarray, point: bool = True) -> None:
    """A GeoTIFF shaped like a Copernicus tile (floating-point predictor, deflate)."""
    geokeys = (1, 1, 0, 1, 1025, 0, 1, 2 if point else 1)  # GTRasterTypeGeoKey
    tie = (0.0, 0.0, 0.0, float(lon), float(lat + 1), 0.0)
    if not point:
        tie = (0.0, 0.0, 0.0, lon - SCALE / 2, lat + 1 + SCALE / 2, 0.0)
    tifffile.imwrite(
        path,
        heights.astype(np.float32),
        compression="zlib",
        predictor=3,
        extratags=[
            (33550, "d", 3, (SCALE, SCALE, 0.0), True),
            (33922, "d", 6, tie, True),
            (34735, "H", len(geokeys), geokeys, True),
        ],
    )


def test_tile_names_and_urls() -> None:
    assert tile_name(39, -106) == "Copernicus_DSM_COG_30_N39_00_W106_00_DEM"
    assert tile_name(-5, 7, 30) == "Copernicus_DSM_COG_10_S05_00_E007_00_DEM"
    assert tile_url(39, -106).startswith("https://copernicus-dem-90m.s3.amazonaws.com/")
    assert tile_url(39, -106, 30).endswith("Copernicus_DSM_COG_10_N39_00_W106_00_DEM.tif")


@pytest.mark.parametrize("point", [True, False])
def test_read_tile_geometry(tmp_path: Path, point: bool) -> None:
    heights = np.arange(120 * 120, dtype=np.float32).reshape(120, 120)
    write_tile(tmp_path / "t.tif", 39, -106, heights, point=point)
    data, north, west, dlat, dlon = read_tile(tmp_path / "t.tif")
    assert np.array_equal(data, heights)
    assert (north, west) == pytest.approx((40.0, -106.0))
    assert (dlat, dlon) == pytest.approx((SCALE, SCALE))


def test_load_area_mosaics_cached_tiles_and_treats_missing_as_sea(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "glo90"
    cache.mkdir()
    write_tile(cache / f"{tile_name(39, -106)}.tif", 39, -106, np.full((120, 120), 2000.0))
    write_tile(cache / f"{tile_name(39, -105)}.tif", 39, -105, np.full((120, 120), 1600.0))
    write_tile(cache / f"{tile_name(40, -106)}.tif", 40, -106, np.full((120, 120), 3000.0))
    requested: list[str] = []

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        requested.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO())  # type: ignore[arg-type]

    monkeypatch.setattr(copernicus.urllib.request, "urlopen", fake_urlopen)
    dem = load_area(39.2, 40.5, -105.9, -104.5, cache=cache)
    assert dem.shape == (240, 240) and dem.source == "copernicus-glo90"
    assert dem.height(39.5, -105.5) == pytest.approx(2000.0)
    assert dem.height(39.5, -104.5) == pytest.approx(1600.0)
    assert dem.height(40.5, -105.5) == pytest.approx(3000.0)
    assert dem.height(40.5, -104.5) == pytest.approx(0.0)  # the missing tile: sea level
    assert len(requested) == 1 and "N40_00_W105" in requested[0]
    # The 404 is remembered; nothing is requested again.
    load_area(39.2, 40.5, -105.9, -104.5, cache=cache)
    assert len(requested) == 1


def test_download_writes_into_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "remote.tif"
    write_tile(src, 39, -106, np.full((120, 120), 1234.0))

    class Response(io.BytesIO):
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    seen: dict[str, str] = {}

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        seen["agent"] = request.get_header("User-agent")
        return Response(src.read_bytes())

    monkeypatch.setattr(copernicus.urllib.request, "urlopen", fake_urlopen)
    cache = tmp_path / "cache"
    path = copernicus.fetch_tile(39, -106, cache=cache)
    assert path is not None and path.exists() and not list(cache.glob("*.part"))
    assert "tracking-sim" in seen["agent"]
    assert read_tile(path)[0][0, 0] == pytest.approx(1234.0)
    with pytest.raises(FileNotFoundError):
        copernicus.fetch_tile(10, 10, cache=cache, download=False)


def test_area_around() -> None:
    south, north, west, east = area_around([(39.9, -105.1, 110_000.0)], margin_m=0.0)
    assert north - south == pytest.approx(2 * 110_000 / 111_320, rel=1e-6)
    assert east - west > north - south  # longitude degrees are shorter
