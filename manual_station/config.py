"""Configuration parsing and offline validation. This module never imports GPIO."""
from __future__ import annotations

import json
import math
from pathlib import Path

CHANNELS = ("front-a", "front-b", "rear-a", "rear-b")
MAX_DUTY = 40.0
COMMON_CHECKS = (
    "wiring_verified", "logic_power_and_common_ground_verified", "enable_off_verified",
    "area_clear", "physical_stop_verified", "other_controllers_stopped",
)
WHEEL_CHECKS = ("wheels_unloaded",)
LIFT_CHECKS = ("lift_holding_verified", "lift_limits_verified", "lift_inputs_healthy")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds finite range")
    return number


def load_config(path) -> dict:
    path = Path(path)
    if path.stat().st_size > 131072:
        raise ValueError("Configuration exceeds 128 KiB")
    result = json.loads(path.read_text(encoding="utf-8-sig"),
                        object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
                        parse_float=_finite_float)
    if not isinstance(result, dict):
        raise ValueError("Configuration must be a JSON object")
    return result


def duty_limit(config):
    value = config.get("max_duty_percent", MAX_DUTY)
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= MAX_DUTY:
        raise ValueError("max_duty_percent must be finite and within 1..40")
    return float(value)


def validate_real(config, scope) -> list[str]:
    """Return all commissioning/configuration errors without touching devices.

    Wheel side/polarity may remain unknown for electrical single-channel tests.
    The controller must reject vehicle movement until those fields are known.
    Configured pins in inactive scopes still participate in collision checks.
    """
    errors = []
    if not isinstance(config, dict):
        return ["Configuration must be an object"]
    if scope not in ("wheels", "lift"):
        return ["scope must be wheels or lift"]
    if type(config.get("schema_version")) is not int or config.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if config.get("numbering") != "BCM":
        errors.append("numbering must be BCM (RP1 line offsets, not physical pins)")
    try:
        duty_limit(config)
    except ValueError as exc:
        errors.append(str(exc))
    checks = config.get("safety_checks", {})
    if not isinstance(checks, dict):
        checks = {}
    required = COMMON_CHECKS + (WHEEL_CHECKS if scope == "wheels" else LIFT_CHECKS)
    for name in required:
        if checks.get(name) is not True:
            errors.append(f"safety_checks.{name} must be explicitly confirmed true")

    reserved = config.get("reserved_pins", [])
    if not isinstance(reserved, list) or any(type(p) is not int or not 0 <= p <= 27 for p in reserved):
        errors.append("reserved_pins must contain BCM integers in 0..27")
        reserved = []
    seen = {}

    def pin(value, label, required=False):
        if value is None and not required:
            return
        if type(value) is not int or not 2 <= value <= 27:
            errors.append(f"{label}: a confirmed BCM pin in 2..27 is required")
            return
        if value in reserved:
            errors.append(f"{label}: BCM{value} is reserved by another device")
        if value in seen:
            errors.append(f"{label}: BCM{value} duplicates {seen[value]}")
        seen[value] = label

    wheels = config.get("wheels")
    if not isinstance(wheels, dict) or set(wheels) != set(CHANNELS):
        errors.append("wheels must contain exactly front-a, front-b, rear-a, rear-b")
        wheels = wheels if isinstance(wheels, dict) else {}
    for name in CHANNELS:
        wheel = wheels.get(name)
        if not isinstance(wheel, dict):
            errors.append(f"wheels.{name} must be an object")
            continue
        if scope == "wheels" and wheel.get("confirmed") is not True:
            errors.append(f"wheels.{name}.confirmed must be true")
        if wheel.get("side") not in (None, "left", "right"):
            errors.append(f"wheels.{name}.side must be null, left or right")
        sign = wheel.get("forward_sign")
        if sign is not None and (type(sign) is not int or sign not in (-1, 1)):
            errors.append(f"wheels.{name}.forward_sign must be null, -1 or 1")
        for key in ("in1", "in2", "enable"):
            pin(wheel.get(key), f"wheels.{name}.{key}", scope == "wheels")

    lift = config.get("lift")
    if not isinstance(lift, dict):
        errors.append("lift must be an object")
        lift = {}
    if type(lift.get("enabled")) is not bool:
        errors.append("lift.enabled must be a boolean")
    if scope == "lift":
        if lift.get("enabled") is not True or lift.get("confirmed") is not True:
            errors.append("lift must be explicitly enabled and confirmed")
        if type(lift.get("up_sign")) is not int or lift.get("up_sign") not in (-1, 1):
            errors.append("lift.up_sign must be confirmed as -1 or 1")
        if lift.get("driver") not in ("dual_pwm", "enable_pwm"):
            errors.append("lift.driver must be dual_pwm or enable_pwm")
    for key in ("in1", "in2"):
        pin(lift.get(key), f"lift.{key}", scope == "lift")
    pin(lift.get("enable"), "lift.enable", scope == "lift" and lift.get("driver") == "enable_pwm")
    if lift.get("driver") == "dual_pwm" and lift.get("enable") is not None:
        errors.append("dual_pwm requires enable=null; its fixed-enable wiring needs enable_off_verified")
    for name in ("upper_limit", "lower_limit"):
        limit = lift.get(name)
        if not isinstance(limit, dict):
            errors.append(f"lift.{name} must be an object")
            continue
        pin(limit.get("pin"), f"lift.{name}.pin", scope == "lift")
        if type(limit.get("active_level")) is not int or limit.get("active_level") not in (0, 1):
            errors.append(f"lift.{name}.active_level must be 0 or 1")
    return errors
