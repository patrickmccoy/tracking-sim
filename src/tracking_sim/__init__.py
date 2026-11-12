"""tracking_sim: replay ADS-B truth, simulate sensors, and score Stone Soup trackers."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tracking-sim")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
