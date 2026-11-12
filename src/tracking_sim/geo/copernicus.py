"""Copernicus DEM tiles: download once, cache, and mosaic into a ``GridDEM``.

The Copernicus GLO-90 and GLO-30 digital elevation models are free, global
and need no login: 1 x 1 degree Cloud-Optimised GeoTIFF tiles on AWS open
data. GLO-90 (3 arc-seconds, about 90 m, ~5 MB a tile) is the default and
plenty for masking terrain at radar ranges; GLO-30 (1 arc-second, about
30 m, ~38 MB a tile) is available for short-range or low-angle work.
Heights are metres above the EGM2008 geoid, i.e. mean sea level.

Reading the tiles needs the ``terrain`` extra (``tifffile`` and
``imagecodecs``, for the tiles' floating-point predictor)::

    uv sync --extra terrain

Tiles are cached under ``$TRACKING_SIM_CACHE/dem/glo90`` (default
``~/.cache/tracking-sim``). Tiles that don't exist (open ocean) are
remembered as sea level and not requested again.
"""

from __future__ import annotations

import math
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal

import numpy as np

from tracking_sim.geo.terrain import GridDEM

__all__ = ["area_around", "cache_dir", "fetch_tile", "load_area", "read_tile", "tile_name"]

Resolution = Literal[30, 90]
_BUCKET = "https://copernicus-dem-{res}m.s3.amazonaws.com/{name}/{name}.tif"
_USER_AGENT = "tracking-sim (terrain tiles)"


def tile_name(lat: int, lon: int, resolution_m: Resolution = 90) -> str:
    """The Copernicus name of the tile whose south-west corner is (lat, lon)."""
    arcsec = {30: 10, 90: 30}[resolution_m]
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_{arcsec}_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def tile_url(lat: int, lon: int, resolution_m: Resolution = 90) -> str:
    return _BUCKET.format(res=resolution_m, name=tile_name(lat, lon, resolution_m))


def cache_dir(resolution_m: Resolution = 90, root: str | Path | None = None) -> Path:
    base = Path(root or os.environ.get("TRACKING_SIM_CACHE") or Path.home() / ".cache/tracking-sim")
    return base / "dem" / f"glo{resolution_m}"


def fetch_tile(
    lat: int,
    lon: int,
    resolution_m: Resolution = 90,
    *,
    cache: Path | None = None,
    download: bool = True,
) -> Path | None:
    """The cached tile for (lat, lon), downloading it if needed; None if it doesn't exist."""
    cache = cache or cache_dir(resolution_m)
    name = tile_name(lat, lon, resolution_m)
    path = cache / f"{name}.tif"
    missing = cache / f"{name}.missing"
    if path.exists():
        return path
    if missing.exists():
        return None
    if not download:
        raise FileNotFoundError(f"{path} is not cached and downloading is off")
    cache.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        tile_url(lat, lon, resolution_m), headers={"User-Agent": _USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
    except urllib.error.HTTPError as err:
        if err.code in (403, 404):  # no tile here: open ocean
            missing.touch()
            return None
        raise
    fd, tmp = tempfile.mkstemp(dir=cache, suffix=".part")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return path


def _tifffile() -> Any:
    try:
        import tifffile
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise ImportError(
            "reading DEM tiles needs the 'terrain' extra: uv sync --extra terrain"
        ) from exc
    return tifffile


def read_tile(path: str | Path) -> tuple[np.ndarray, float, float, float, float]:
    """Heights (rows north to south) and the centre of pixel (0, 0) and spacing.

    Returns ``(heights, north, west, dlat, dlon)`` from the GeoTIFF's own
    pixel-scale and tie-point tags, honouring pixel-is-area versus
    pixel-is-point.
    """
    tifffile = _tifffile()
    with tifffile.TiffFile(path) as tif:
        page = tif.pages[0]
        heights = page.asarray().astype(np.float32)
        sx, sy, _ = page.tags["ModelPixelScaleTag"].value
        _, _, _, x0, y0, _ = page.tags["ModelTiepointTag"].value
        geo = tif.geotiff_metadata or {}
        raster_type = geo.get("GTRasterTypeGeoKey", 1)
    point = int(raster_type) == 2 or str(raster_type).endswith("Point")
    # Pixel-is-area tie points are pixel corners; shift to the pixel centre.
    north = float(y0) if point else float(y0) - sy / 2
    west = float(x0) if point else float(x0) + sx / 2
    return heights, north, west, float(sy), float(sx)


def load_area(
    south: float,
    north: float,
    west: float,
    east: float,
    *,
    resolution_m: Resolution = 90,
    cache: Path | None = None,
    download: bool = True,
) -> GridDEM:
    """A mosaic of every tile touching the box, as one ``GridDEM``."""
    lats = range(math.floor(south), math.floor(north) + 1)
    lons = range(math.floor(west), math.floor(east) + 1)
    tiles: dict[tuple[int, int], tuple[np.ndarray, float, float, float, float] | None] = {}
    for lat in lats:
        for lon in lons:
            path = fetch_tile(lat, lon, resolution_m, cache=cache, download=download)
            tiles[(lat, lon)] = read_tile(path) if path is not None else None
    present = [t for t in tiles.values() if t is not None]
    if not present:
        raise ValueError("no terrain tiles cover this area (all ocean?)")
    rows, cols = present[0][0].shape
    dlat, dlon = present[0][3], present[0][4]
    if any(t[0].shape != (rows, cols) for t in present):
        raise NotImplementedError("tiles of different widths (above 50 degrees latitude)")
    mosaic = np.zeros((rows * len(lats), cols * len(lons)), dtype=np.float32)
    for i, lat in enumerate(reversed(lats)):  # north first
        for j, lon in enumerate(lons):
            tile = tiles[(lat, lon)]
            if tile is None:
                continue
            heights, t_north, t_west, _, _ = tile
            if abs(t_north - (lat + 1)) > dlat / 2 or abs(t_west - lon) > dlon / 2:
                raise ValueError(f"tile {lat},{lon} is not where its name says")
            mosaic[i * rows : (i + 1) * rows, j * cols : (j + 1) * cols] = heights
    return GridDEM(
        heights=mosaic,
        north=float(max(lats) + 1),
        west=float(min(lons)),
        dlat=dlat,
        dlon=dlon,
        source=f"copernicus-glo{resolution_m}",
    )


def area_around(
    points: list[tuple[float, float, float]], margin_m: float = 2000.0
) -> tuple[float, float, float, float]:
    """(south, north, west, east) covering circles of ``(lat, lon, radius_m)``."""
    south = north = west = east = float("nan")
    for lat, lon, radius in points:
        r = radius + margin_m
        dlat = r / 111_320.0
        dlon = r / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
        box = (lat - dlat, lat + dlat, lon - dlon, lon + dlon)
        south = min(south, box[0]) if not math.isnan(south) else box[0]
        north = max(north, box[1]) if not math.isnan(north) else box[1]
        west = min(west, box[2]) if not math.isnan(west) else box[2]
        east = max(east, box[3]) if not math.isnan(east) else box[3]
    return south, north, west, east
