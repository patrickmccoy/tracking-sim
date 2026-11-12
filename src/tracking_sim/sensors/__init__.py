"""Sensors: specs loaded from YAML and the models that simulate them."""

from tracking_sim.sensors.spec import (
    RotatingRadar2DSpec,
    SensorSpec,
    load_sensor_spec,
    parse_sensor_spec,
)

__all__ = ["RotatingRadar2DSpec", "SensorSpec", "load_sensor_spec", "parse_sensor_spec"]
