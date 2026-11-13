"""Detections re-expressed for a tracker whose state is laid out differently.

The sensors build their measurement models for the 6D state
``[x, vx, y, vy, z, vz]`` (positions at 0, 2, 4). A tracker with another
state, such as constant acceleration ``[x, vx, ax, y, vy, ay, z, vz, az]``
(positions at 0, 3, 6) or a turn-rate state, needs the same model with its
own size and position indices. ``DetectionAdapter`` makes those copies.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from stonesoup.types.detection import Clutter, Detection, TrueDetection

from tracking_sim.sensors.measurement import CartesianToBearingSlantRange

__all__ = ["DetectionAdapter", "remap_model"]


def remap_model(
    model: CartesianToBearingSlantRange, ndim_state: int, mapping: tuple[int, int, int]
) -> CartesianToBearingSlantRange:
    """``model`` for a state of ``ndim_state`` with positions at ``mapping``."""
    if not isinstance(model, CartesianToBearingSlantRange):
        raise TypeError(f"can't remap a {type(model).__name__}")
    return CartesianToBearingSlantRange(
        ndim_state=ndim_state,
        mapping=mapping,
        noise_covar=model.covar(),
        translation_offset=model.translation_offset,
        frame_rotation=model.rotation_matrix,
        inverse_height_m=model.inverse_height_m,
    )


class DetectionAdapter:
    """Copies of detections whose models fit a state of ``ndim_state``.

    With the sensors' own layout (6D, positions at 0, 2, 4) detections pass
    through unchanged. Otherwise each gets a copy with a remapped model;
    copies are cached, so the same input gives the same object every time
    (trackers and metrics compare detections by identity).
    """

    def __init__(self, ndim_state: int, position_mapping: tuple[int, int, int]) -> None:
        self.ndim_state = ndim_state
        self.position_mapping = tuple(position_mapping)
        self._models: dict[int, Any] = {}
        # id(original) -> (original, copy); holding the original keeps its id unique.
        self._detections: dict[int, tuple[Detection, Detection]] = {}

    @property
    def passes_through(self) -> bool:
        return self.ndim_state == 6 and self.position_mapping == (0, 2, 4)

    def __call__(self, detections: Iterable[Detection]) -> set[Detection]:
        if self.passes_through:
            return set(detections)
        return {self.one(d) for d in detections}

    def one(self, detection: Detection) -> Detection:
        if self.passes_through:
            return detection
        cached = self._detections.get(id(detection))
        if cached is not None:
            return cached[1]
        model = self.model_for(detection.measurement_model)
        if isinstance(detection, TrueDetection):
            copy: Detection = TrueDetection(
                state_vector=detection.state_vector,
                timestamp=detection.timestamp,
                measurement_model=model,
                groundtruth_path=detection.groundtruth_path,
                metadata=detection.metadata,
            )
        elif isinstance(detection, Clutter):
            copy = Clutter(
                state_vector=detection.state_vector,
                timestamp=detection.timestamp,
                measurement_model=model,
                metadata=detection.metadata,
            )
        else:
            copy = Detection(
                state_vector=detection.state_vector,
                timestamp=detection.timestamp,
                measurement_model=model,
                metadata=detection.metadata,
            )
        self._detections[id(detection)] = (detection, copy)
        return copy

    def model_for(self, model: Any) -> CartesianToBearingSlantRange:
        cached = self._models.get(id(model))
        if cached is None:
            cached = remap_model(model, self.ndim_state, self.position_mapping)  # type: ignore[arg-type]
            self._models[id(model)] = (model, cached)
            return cached
        remapped: CartesianToBearingSlantRange = cached[1]
        return remapped
