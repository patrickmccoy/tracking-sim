"""Replay one aircraft: its truth, the radar's detections and filter estimates.

``Replay`` draws one moment of a single aircraft's flight, scan by scan:

* a map of the neighbourhood, with the aircraft's true path, its
  detections so far (as the radar display shows them), the latest scan's
  other detections and false alarms, and each estimate's track with its
  2-sigma position ellipse;
* each estimate's horizontal position error against truth over the whole
  flight, with a cursor at the moment shown.

``Replay.widget()`` wraps it in an ipywidgets player (play, step, scrub),
optionally with an aircraft picker that recomputes the estimates through a
callback. Drawing needs matplotlib, the player ipywidgets (both in the
``notebook`` extra).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray
from stonesoup.types.detection import TrueDetection
from stonesoup.types.track import Track

if TYPE_CHECKING:
    from tracking_sim.sim.dataset import TrackingData

__all__ = ["SERIES", "Estimate", "Replay", "horizontal_error"]

FloatArray = NDArray[np.float64]

# The reference data-viz palette's categorical slots, in their fixed order.
SERIES = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
)
INK, GREY, LIGHT = "#52514e", "#8a8984", "#c9c8c2"


@dataclass(frozen=True)
class Estimate:
    """One filter's or tracker's track, and where position sits in its state."""

    label: str
    track: Track
    position_mapping: tuple[int, int, int] = (0, 2, 4)

    def positions(self) -> tuple[FloatArray, FloatArray, FloatArray]:
        """POSIX times (n,), east-north means (n, 2) and covariances (n, 2, 2).

        Covariance is NaN for states that carry none (for example a stored
        run's tracks). Particle and mixture states give their mean and
        spread.
        """
        ix, iy = self.position_mapping[0], self.position_mapping[1]
        t, xy, cov = [], [], []
        for state in self.track.states:
            mean = np.asarray(getattr(state, "mean", state.state_vector), dtype=float).ravel()
            t.append(state.timestamp.timestamp())
            xy.append(mean[[ix, iy]])
            c = getattr(state, "covar", None)
            if c is None:
                cov.append(np.full((2, 2), np.nan))
            else:
                c = np.asarray(c, dtype=float)
                cov.append(c[np.ix_([ix, iy], [ix, iy])])
        return np.array(t), np.array(xy).reshape(-1, 2), np.array(cov).reshape(-1, 2, 2)


def horizontal_error(
    data: TrackingData, segment_id: str, estimate: Estimate
) -> tuple[FloatArray, FloatArray]:
    """An estimate's horizontal distance from truth at each of its states (m)."""
    t, xy, _ = estimate.positions()
    pos, _, ok = data.truth.segment(segment_id).evaluate(t)
    err = np.hypot(xy[:, 0] - pos[:, 0], xy[:, 1] - pos[:, 1])
    err[~ok] = np.nan
    return t, err


def _ellipse(cov: FloatArray, n_sigma: float = 2.0, points: int = 48) -> FloatArray:
    """(points, 2) offsets of the n-sigma ellipse of a 2x2 covariance."""
    vals, vecs = np.linalg.eigh((cov + cov.T) / 2)
    vals = np.clip(vals, 0.0, None)
    a = np.linspace(0, 2 * np.pi, points)
    circle = np.column_stack([np.cos(a), np.sin(a)])
    offsets: FloatArray = n_sigma * circle * np.sqrt(vals) @ vecs.T
    return offsets


class Replay:
    """One aircraft's replay, scan by scan.

    ``estimates`` are drawn in the palette's fixed order. The map stays on
    the aircraft's detected path plus ``margin_m``; frames are the radar's
    scan ends while the aircraft was detected.
    """

    def __init__(
        self,
        data: TrackingData,
        segment_id: str,
        estimates: Sequence[Estimate] = (),
        *,
        margin_m: float = 8000.0,
    ) -> None:
        self.data = data
        self.segment_id = segment_id
        self.estimates = list(estimates)
        if len(self.estimates) > len(SERIES):
            raise ValueError(f"at most {len(SERIES)} estimates fit the palette")
        self.mine = data.detections_of(segment_id)
        if not self.mine:
            raise ValueError(f"the radar never detected {segment_id}")
        t0, t1 = self.mine[0].timestamp, self.mine[-1].timestamp
        self.frames = [s.end for s in data.scans if t0 < s.end and s.start <= t1]
        self.mine_xy = data.plot_positions(self.mine)
        self.mine_t = np.array([d.timestamp.timestamp() for d in self.mine])

        segment = data.truth.segment(segment_id)
        tt = np.arange(segment.t[0], segment.t[-1], 1.0)
        pos, _, ok = segment.evaluate(tt)
        self.truth_t, self.truth_xy = tt[ok], pos[ok][:, :2]

        lo = self.mine_xy.min(axis=0) - margin_m
        hi = self.mine_xy.max(axis=0) + margin_m
        centre, half = (lo + hi) / 2, max(hi - lo) / 2
        self.extent = (centre - half, centre + half)

        self.tracks = [e.positions() for e in self.estimates]
        self.errors = [horizontal_error(data, segment_id, e) for e in self.estimates]
        by_end = {s.end: s for s in data.scans}
        self._scan_at = by_end

    # -- one frame -----------------------------------------------------------

    def summary(self, frame: int) -> str:
        """The frame's time and each estimate's current error, as text."""
        t = self.frames[frame].timestamp()
        parts = [f"{self.frames[frame]:%H:%M:%S}"]
        for est, (et, err) in zip(self.estimates, self.errors, strict=True):
            k = int(np.searchsorted(et, t, side="right")) - 1
            value = f"{err[k]:.0f} m" if k >= 0 and np.isfinite(err[k]) else "-"
            parts.append(f"{est.label} {value}")
        return "  |  ".join(parts)

    def draw(self, frame: int = -1, fig: Any = None) -> Any:
        """Draw frame ``frame`` (an index into ``frames``) and return the figure."""
        import matplotlib.pyplot as plt

        frame = frame % len(self.frames)
        now = self.frames[frame]
        t = now.timestamp()
        if fig is None:
            fig = plt.figure(figsize=(11, 5.2))
        fig.clear()
        grid = fig.add_gridspec(1, 2, width_ratios=[1.15, 1])
        ax, ax_err = fig.add_subplot(grid[0]), fig.add_subplot(grid[1])
        km = 1e-3

        # The neighbourhood this scan: everything else the radar reported.
        scan = self._scan_at.get(now)
        if scan is not None:
            others = [
                d
                for d in scan.detections
                if not (
                    isinstance(d, TrueDetection) and d.metadata.get("segment_id") == self.segment_id
                )
            ]
            xy = self.data.plot_positions(others)
            if len(xy):
                ax.scatter(
                    *(xy * km).T,
                    s=14,
                    marker="x",
                    color=LIGHT,
                    lw=1,
                    label="other detections, this scan",
                    zorder=1,
                )

        # Truth: the whole path faint, flown so far solid.
        ax.plot(*(self.truth_xy * km).T, color=LIGHT, lw=1, ls="--", zorder=1)
        flown = self.truth_t <= t
        ax.plot(*(self.truth_xy[flown] * km).T, color=INK, lw=1.5, label="truth", zorder=2)
        if flown.any():
            ax.scatter(*(self.truth_xy[flown][-1] * km), s=40, color=INK, zorder=5)

        seen = self.mine_t <= t
        ax.scatter(*(self.mine_xy[seen] * km).T, s=9, color=GREY, label="its detections", zorder=3)

        for i, (est, (et, exy, ecov)) in enumerate(zip(self.estimates, self.tracks, strict=True)):
            colour = SERIES[i]
            upto = et <= t
            if not upto.any():
                continue
            ax.plot(*(exy[upto] * km).T, color=colour, lw=1.5, label=est.label, zorder=4)
            last = np.flatnonzero(upto)[-1]
            ax.scatter(*(exy[last] * km), s=30, color=colour, edgecolor="white", lw=1, zorder=6)
            if np.all(np.isfinite(ecov[last])):
                ring = (exy[last] + _ellipse(ecov[last])) * km
                ax.plot(*ring.T, color=colour, lw=1, alpha=0.8, zorder=4)

        (x0, y0), (x1, y1) = self.extent
        ax.set(xlim=(x0 * km, x1 * km), ylim=(y0 * km, y1 * km), aspect="equal")
        ax.set(xlabel="east (km)", ylabel="north (km)")
        callsign = self.data.truth.segment(self.segment_id).callsign or ""
        ax.set_title(f"{self.segment_id} {callsign}  {now:%H:%M:%S}", loc="left", fontsize=10)
        ax.legend(loc="upper left", fontsize=8, frameon=False)

        # Error against truth over the whole flight, cursor at now.
        start = self.mine_t[0]
        for i, (est, (et, err)) in enumerate(zip(self.estimates, self.errors, strict=True)):
            ax_err.plot((et - start) / 60, err, color=SERIES[i], lw=1.5, label=est.label)
        ax_err.axvline((t - start) / 60, color=GREY, lw=1)
        ax_err.set(xlabel="minutes since first detection", ylabel="horizontal error (m)")
        ax_err.set_ylim(bottom=0)
        if self.estimates:
            top = np.nanpercentile(np.concatenate([e for _, e in self.errors]), 98)
            if np.isfinite(top) and top > 0:
                ax_err.set_ylim(0, top * 1.15)
        ax_err.set_title("position error against truth", loc="left", fontsize=10)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
            ax_err.spines[side].set_visible(False)
        fig.tight_layout()
        return fig

    # -- the player ------------------------------------------------------------

    def widget(
        self,
        choose: Callable[[str], Sequence[Estimate]] | None = None,
        aircraft: Sequence[str] | None = None,
        *,
        interval_ms: int = 300,
    ) -> Any:
        """An ipywidgets player for this replay.

        With ``choose`` (segment id -> estimates) and ``aircraft`` (the ids to
        offer), a picker switches aircraft and calls ``choose`` to make the
        new one's estimates.
        """
        import ipywidgets as widgets
        import matplotlib.pyplot as plt
        from IPython.display import display

        state = {"replay": self}
        out = widgets.Output()
        status = widgets.Label()
        play = widgets.Play(
            value=len(self.frames) - 1, min=0, max=len(self.frames) - 1, interval=interval_ms
        )
        slider = widgets.IntSlider(
            value=len(self.frames) - 1,
            min=0,
            max=len(self.frames) - 1,
            description="scan",
            continuous_update=False,
            layout=widgets.Layout(width="420px"),
        )
        widgets.jslink((play, "value"), (slider, "value"))

        def show(frame: int) -> None:
            replay = state["replay"]
            with out:
                out.clear_output(wait=True)
                fig = replay.draw(frame)
                display(fig)  # type: ignore[no-untyped-call]
                plt.close(fig)
            status.value = replay.summary(frame)

        slider.observe(lambda change: show(change["new"]), names="value")
        controls: list[Any] = [play, slider]
        if choose is not None and aircraft:
            picker = widgets.Dropdown(
                options=list(aircraft), value=self.segment_id, description="aircraft"
            )

            def switch(change: Any) -> None:
                segment = change["new"]
                status.value = f"running filters on {segment}..."
                replay = Replay(self.data, segment, choose(segment))
                state["replay"] = replay
                last = len(replay.frames) - 1
                play.max = slider.max = last
                if slider.value == last:
                    show(last)
                else:
                    slider.value = last

            picker.observe(switch, names="value")
            controls.insert(0, picker)
        show(slider.value)
        return widgets.VBox([widgets.HBox(controls), status, out])
