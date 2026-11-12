"""Unit conversions. The package is SI internally (metres, seconds, radians).

Convert from feet, knots and degrees only in the truth builder and config
loading; ``TruthRecord`` keeps the source's native units.
"""

from __future__ import annotations

import math

FT_TO_M = 0.3048
KT_TO_MPS = 1852.0 / 3600.0
FPM_TO_MPS = FT_TO_M / 60.0
DEG_TO_RAD = math.pi / 180.0

__all__ = ["DEG_TO_RAD", "FPM_TO_MPS", "FT_TO_M", "KT_TO_MPS"]
