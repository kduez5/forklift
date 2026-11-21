"""Offline sensor-evidence contracts; no acquisition or actuation capability.

Registrations and reference evidence are supplied by the caller. A valid packet
means it satisfies this input contract, not that a sensor or machine is safe.
"""
from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, fields
import json
import math
from pathlib import Path
from typing import Mapping


SCHEMA_VERSION = 1
STATUSES = ("VALID", "STALE", "UNKNOWN", "CONFLICT")
KINDS = ("range", "lidar", "camera", "pose", "lift")


@dataclass(frozen=True)
class Observation:
    schema_version: int
    source_id: str
    sensor_kind: str
    physical_mount_id: str
    frame_id: str
    sequence: int
    sample_monotonic_s: float
    receive_monotonic_s: float
    clock_domain: str
    session_id: str
    units: str
    validity: str
    quality: float | None
    quality_kind: str
    calibration_version: str
    distance_m: float | None = None
    uncertainty_m: float | None = None
    direction_rad: float | None = None
    fov_rad: float | None = None
    raw_evidence_id: str | None = None
    height_m: float | None = None
    lift_reference_id: str | None = None
    measurement_basis: str = "unverified"


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    sensor_kind: str
    physical_mount_id: str
    frame_id: str
    calibration_version: str
    units: str = "m"
    required: bool = True
    registration_verified: bool = False
    calibration_verified: bool = False
    max_age_s: float = .30
    direction_rad: float | None = None
    fov_rad: float | None = None
    min_distance_m: float = .02
    max_distance_m: float = 4.0
    min_quality: float = .5
    # Only set this for an explicitly normalized, same-target quantity.
    # Raw ranges from different sensor origins are NOT comparable by default.
    comparison_key: str | None = None
    comparison_basis_verified: bool = False
    correlation_groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiftReference:
    reference_id: str
    clock_domain: str
    session_id: str
    observed_monotonic_s: float
    position_m: float
    limit_evidence_id: str | None
    verified: bool = False


def _json_safe(value):
    """Keep invalid numeric evidence explicit; never replace it with zero."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_numeric": repr(value)}
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"unsupported_type": type(value).__name__}


@dataclass(frozen=True)
class ValidationResult:
    source_id: str | None
    status: str
    reasons: tuple[str, ...]
    observation: Observation | None
    raw_evidence: dict
    age_s: float | None = None

    def to_dict(self):
        return _json_safe(asdict(self))


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _angular_difference(a, b):
    return abs((a - b + math.pi) % math.tau - math.pi)


class ObservationMonitor:
    """Stateful ingestion, read-only freshness aggregation.

    ``snapshot`` never advances sequence counters or refreshes sample ages.
    A new session requires a new monitor; old-epoch samples are not subtracted
    from the new monotonic clock and are rejected as conflicts.
    """

    def __init__(self, sources, *, clock_domain: str, session_id: str,
                 lift_references=(), clock_skew_tolerance_s=.02,
                 comparison_time_tolerance_s=.05, comparison_distance_tolerance_m=.01):
        if not _nonempty(clock_domain) or not _nonempty(session_id):
            raise ValueError("An explicit clock domain and boot/session ID are required")
        tolerances = (clock_skew_tolerance_s, comparison_time_tolerance_s, comparison_distance_tolerance_m)
        if any(not _finite_number(x) or x < 0 for x in tolerances):
            raise ValueError("Tolerances must be finite nonnegative numbers")
        self.clock_domain, self.session_id = clock_domain, session_id
        self.clock_skew_tolerance_s = clock_skew_tolerance_s
        self.comparison_time_tolerance_s = comparison_time_tolerance_s
        self.comparison_distance_tolerance_m = comparison_distance_tolerance_m
        self.sources = {}
        for spec in sources:
            if not isinstance(spec, SourceSpec) or not _nonempty(spec.source_id):
                raise ValueError("Sources must be SourceSpec registrations with a source ID")
            if spec.source_id in self.sources:
                raise ValueError("Duplicate source registration")
            if any(not _nonempty(getattr(spec, name)) for name in ("physical_mount_id", "frame_id", "calibration_version", "units")):
                raise ValueError("Registration requires mount, frame, calibration and unit identities")
            if any(type(getattr(spec, name)) is not bool for name in ("required", "registration_verified", "calibration_verified", "comparison_basis_verified")):
                raise ValueError("Verification flags must be booleans, not truthy strings")
            if spec.sensor_kind not in KINDS or not _finite_number(spec.max_age_s) or spec.max_age_s <= 0:
                raise ValueError("Invalid sensor kind or maximum age")
            if spec.sensor_kind in ("range", "lidar", "lift") and spec.units != "m":
                raise ValueError("Metric measurement contracts require canonical metre units")
            if (any(not _finite_number(x) for x in (spec.min_quality, spec.min_distance_m, spec.max_distance_m))
                    or not (0 <= spec.min_quality <= 1) or not (0 <= spec.min_distance_m < spec.max_distance_m)):
                raise ValueError("Invalid quality or distance bounds")
            if (spec.direction_rad is not None and not _finite_number(spec.direction_rad)
                    or spec.fov_rad is not None and (not _finite_number(spec.fov_rad) or not 0 < spec.fov_rad <= math.tau)):
                raise ValueError("Registered direction/FOV must be known finite values or explicitly None")
            if spec.comparison_key is not None and not _nonempty(spec.comparison_key):
                raise ValueError("Invalid comparison key")
            if not isinstance(spec.correlation_groups, (tuple, list)) or any(not _nonempty(x) for x in spec.correlation_groups):
                raise ValueError("Correlation groups must contain explicit string identifiers")
            self.sources[spec.source_id] = spec
        self.lift_references = {}
        for reference in lift_references:
            if (not isinstance(reference, LiftReference) or not _nonempty(reference.reference_id)
                    or reference.reference_id in self.lift_references):
                raise ValueError("Invalid or duplicate lift reference")
            self.lift_references[reference.reference_id] = reference
        self._history = {}
        self._latest = {}

    def ingest(self, packet, *, now_monotonic_s: float) -> ValidationResult:
        raw = asdict(packet) if isinstance(packet, Observation) else dict(packet) if isinstance(packet, Mapping) else {}
        source = raw.get("source_id") if _nonempty(raw.get("source_id")) else None
        observation = None

        def finish(status, *reasons, age=None):
            result = ValidationResult(source, status, tuple(reasons), observation, _json_safe(raw), age)
            # Invalid latest evidence must not leave an old healthy reading
            # looking current. Ordering history itself only advances below.
            self._latest[source or "<unidentified>"] = result
            return result

        if not _finite_number(now_monotonic_s) or now_monotonic_s < 0:
            return finish("CONFLICT", "invalid_monitor_clock")
        if not isinstance(packet, (Observation, Mapping)):
            return finish("UNKNOWN", "packet_is_not_an_observation")
        if any(not isinstance(key, str) for key in raw):
            return finish("UNKNOWN", "schema_keys_must_be_strings")
        required = {f.name for f in fields(Observation) if f.default is MISSING and f.default_factory is MISSING}
        missing = sorted(required - raw.keys())
        unexpected = sorted(raw.keys() - {f.name for f in fields(Observation)})
        if missing or unexpected:
            return finish("UNKNOWN", "missing_fields:" + ",".join(missing) if missing else "unexpected_fields:" + ",".join(unexpected))
        try:
            observation = packet if isinstance(packet, Observation) else Observation(**raw)
        except (TypeError, ValueError):
            return finish("UNKNOWN", "schema_construction_failed")
        obs = observation
        if type(obs.schema_version) is not int or obs.schema_version != SCHEMA_VERSION:
            return finish("CONFLICT", "unsupported_schema_version")
        for name in ("source_id", "sensor_kind", "physical_mount_id", "frame_id", "clock_domain",
                     "session_id", "units", "validity", "quality_kind", "calibration_version"):
            if not _nonempty(getattr(obs, name)):
                return finish("UNKNOWN", "missing_identity:" + name)
        for name in ("raw_evidence_id", "lift_reference_id"):
            if getattr(obs, name) is not None and not _nonempty(getattr(obs, name)):
                return finish("UNKNOWN", "invalid_optional_identity:" + name)
        if obs.source_id not in self.sources:
            return finish("UNKNOWN", "unregistered_source")
        spec = self.sources[obs.source_id]
        for name in ("sensor_kind", "physical_mount_id", "frame_id", "units"):
            if getattr(obs, name) != getattr(spec, name):
                return finish("CONFLICT", "registration_mismatch:" + name)
        if obs.calibration_version != spec.calibration_version:
            return finish("CONFLICT", "unrecognized_calibration_version")
        if obs.clock_domain != self.clock_domain or obs.session_id != self.session_id:
            return finish("CONFLICT", "mixed_clock_domain_or_session_epoch")
        if type(obs.sequence) is not int or obs.sequence < 0:
            return finish("CONFLICT", "invalid_sequence")
        if any(not _finite_number(t) or t < 0 for t in (obs.sample_monotonic_s, obs.receive_monotonic_s)):
            return finish("CONFLICT", "invalid_sample_or_receive_clock")
        if obs.sample_monotonic_s > obs.receive_monotonic_s + self.clock_skew_tolerance_s:
            return finish("CONFLICT", "sample_after_receive")
        if obs.receive_monotonic_s > now_monotonic_s + self.clock_skew_tolerance_s:
            return finish("CONFLICT", "receive_in_future")
        previous = self._history.get(source)
        if previous is not None:
            sequence, sample, received = previous
            if obs.sequence <= sequence:
                return finish("CONFLICT", "duplicate_sequence" if obs.sequence == sequence else "out_of_order_sequence")
            if obs.sample_monotonic_s < sample or obs.receive_monotonic_s < received:
                return finish("CONFLICT", "monotonic_time_regression")
        self._history[source] = (obs.sequence, obs.sample_monotonic_s, obs.receive_monotonic_s)
        age = max(0.0, now_monotonic_s - obs.sample_monotonic_s)
        if not spec.registration_verified or not spec.calibration_verified:
            return finish("UNKNOWN", "registration_or_calibration_unverified", age=age)
        if obs.validity not in ("valid", "unknown", "no_return", "invalid"):
            return finish("UNKNOWN", "unrecognized_validity", age=age)
        for name in ("distance_m", "uncertainty_m", "direction_rad", "fov_rad", "height_m"):
            value = getattr(obs, name)
            if value is not None and not _finite_number(value):
                return finish("UNKNOWN", "nonfinite_or_nonnumeric:" + name, age=age)
        if obs.validity != "valid":
            if obs.validity == "no_return" and obs.distance_m is not None:
                return finish("CONFLICT", "no_return_must_not_be_replaced_by_a_distance", age=age)
            return finish("UNKNOWN", "observation_" + obs.validity, age=age)
        if obs.measurement_basis not in ("measured", "estimated", "derived", "replay", "synthetic", "synthetic_fixture"):
            return finish("UNKNOWN", "measurement_basis_unverified", age=age)
        if obs.quality_kind not in ("score", "probability") or not _finite_number(obs.quality) or not 0 <= obs.quality <= 1:
            return finish("UNKNOWN", "quality_unavailable_or_invalid", age=age)
        if obs.quality < spec.min_quality:
            return finish("UNKNOWN", "quality_below_registered_minimum", age=age)
        if obs.sensor_kind == "pose":
            return finish("UNKNOWN", "pose_payload_contract_not_implemented", age=age)
        if obs.sensor_kind == "camera" and not _nonempty(obs.raw_evidence_id):
            return finish("UNKNOWN", "camera_frame_evidence_reference_missing", age=age)
        if obs.sensor_kind in ("range", "lidar", "camera"):
            if any(x is None for x in (spec.direction_rad, spec.fov_rad, obs.direction_rad, obs.fov_rad)):
                return finish("UNKNOWN", "mount_direction_or_fov_unknown", age=age)
            if not 0 < obs.fov_rad <= math.tau or not 0 < spec.fov_rad <= math.tau:
                return finish("CONFLICT", "invalid_fov", age=age)
            if _angular_difference(obs.direction_rad, spec.direction_rad) > 1e-6 or abs(obs.fov_rad - spec.fov_rad) > 1e-6:
                return finish("CONFLICT", "direction_or_fov_registration_mismatch", age=age)
        if obs.sensor_kind in ("range", "lidar") and obs.distance_m is None:
            return finish("UNKNOWN", "range_measurement_missing", age=age)
        if obs.distance_m is not None:
            if obs.units != "m":
                return finish("CONFLICT", "distance_m_requires_metre_units", age=age)
            if obs.distance_m < 0:
                return finish("UNKNOWN", "negative_distance", age=age)
            if not spec.min_distance_m <= obs.distance_m <= spec.max_distance_m:
                return finish("UNKNOWN", "distance_outside_registered_range", age=age)
            if obs.uncertainty_m is None or obs.uncertainty_m < 0:
                return finish("UNKNOWN", "distance_uncertainty_missing_or_negative", age=age)
        if obs.sensor_kind == "lift":
            reference = self.lift_references.get(obs.lift_reference_id)
            if (reference is None or reference.verified is not True or not _nonempty(reference.limit_evidence_id)
                    or reference.clock_domain != self.clock_domain or reference.session_id != self.session_id
                    or not _finite_number(reference.observed_monotonic_s) or reference.observed_monotonic_s < 0
                    or reference.observed_monotonic_s > obs.sample_monotonic_s
                    or not _finite_number(reference.position_m) or reference.position_m < 0):
                return finish("UNKNOWN", "lift_reference_or_limit_evidence_unverified", age=age)
            if obs.height_m is None or obs.height_m < 0 or obs.measurement_basis not in ("measured", "estimated"):
                return finish("UNKNOWN", "lift_position_evidence_missing", age=age)
            if obs.uncertainty_m is None or obs.uncertainty_m < 0:
                return finish("UNKNOWN", "lift_position_uncertainty_missing", age=age)
        if age > spec.max_age_s:
            return finish("STALE", "sample_age_exceeded", age=age)
        return finish("VALID", "input_contract_satisfied_only", age=age)

    def snapshot(self, *, now_monotonic_s: float):
        """Return evidence and conflicts without averaging or authorizing motion."""
        rows = []
        aggregate_reasons = []
        if not _finite_number(now_monotonic_s) or now_monotonic_s < 0:
            aggregate_reasons.append("invalid_monitor_clock")
        for source_id, spec in self.sources.items():
            if source_id not in self._latest and spec.required:
                rows.append({"source_id": source_id, "status": "UNKNOWN", "reasons": ["required_source_missing"],
                             "observation": None, "age_s": None})
        for source_id, result in self._latest.items():
            row = result.to_dict()
            obs, spec = result.observation, self.sources.get(source_id)
            if obs is not None and spec is not None and result.status in ("VALID", "STALE") and not aggregate_reasons:
                if now_monotonic_s + self.clock_skew_tolerance_s < obs.receive_monotonic_s:
                    row["status"], row["reasons"] = "CONFLICT", ["snapshot_clock_before_receive"]
                else:
                    row["age_s"] = max(0.0, now_monotonic_s - obs.sample_monotonic_s)
                    if row["age_s"] > spec.max_age_s:
                        row["status"], row["reasons"] = "STALE", ["sample_age_exceeded"]
            if obs is not None and obs.distance_m is not None and _finite_number(obs.distance_m) and _finite_number(obs.uncertainty_m):
                row["distance_interval_m"] = [obs.distance_m - obs.uncertainty_m, obs.distance_m + obs.uncertainty_m]
            rows.append(row)
        valid_rows = [row for row in rows if row["status"] == "VALID"]
        conflicts, incomparable = [], []
        for i, first in enumerate(valid_rows):
            a, sa = first["observation"], self.sources[first["source_id"]]
            if not sa.comparison_key or a["distance_m"] is None:
                continue
            for second in valid_rows[i + 1:]:
                b, sb = second["observation"], self.sources[second["source_id"]]
                if sb.comparison_key != sa.comparison_key or b["distance_m"] is None:
                    continue
                pair = [a["source_id"], b["source_id"]]
                if (not sa.comparison_basis_verified or not sb.comparison_basis_verified
                        or a["frame_id"] != b["frame_id"] or a["units"] != b["units"]
                        or abs(a["sample_monotonic_s"] - b["sample_monotonic_s"]) > self.comparison_time_tolerance_s):
                    incomparable.append({"sources": pair, "reason": "comparison_basis_or_time_not_verified"})
                    continue
                ai, bi = first["distance_interval_m"], second["distance_interval_m"]
                gap = max(ai[0] - bi[1], bi[0] - ai[1], 0)
                if gap > self.comparison_distance_tolerance_m:
                    conflicts.append({"sources": pair, "comparison_key": sa.comparison_key,
                                      "reason": "nonoverlapping_distance_evidence", "interval_gap_m": gap,
                                      "intervals_m": [ai, bi], "averaging_applied": False})
        # Connected components of shared mounts/raw evidence/configured causes.
        groups = []
        for row in valid_rows:
            obs, spec = row["observation"], self.sources[row["source_id"]]
            tags = {"mount:" + obs["physical_mount_id"], *spec.correlation_groups}
            if obs["raw_evidence_id"]:
                tags.add("raw:" + obs["session_id"] + ":" + obs["raw_evidence_id"])
            connected = [group for group in groups if group["_tags"] & tags]
            members = [obs["source_id"]]
            for group in connected:
                tags |= group["_tags"]
                members.extend(group["sources"])
                groups.remove(group)
            groups.append({"sources": members, "_tags": tags})
        statuses = {row["status"] for row in rows}
        if conflicts or aggregate_reasons or "CONFLICT" in statuses:
            status = "CONFLICT"
        elif incomparable or "UNKNOWN" in statuses or not rows:
            status = "UNKNOWN"
        elif "STALE" in statuses:
            status = "STALE"
        else:
            status = "VALID"
        required = [spec.source_id for spec in self.sources.values() if spec.required]
        verified = {row["source_id"] for row in valid_rows}
        return {"schema_version": SCHEMA_VERSION, "mode": "offline_evidence_validation",
                "status": status, "reasons": aggregate_reasons, "clock_domain": self.clock_domain,
                "session_id": self.session_id, "now_monotonic_s": _json_safe(now_monotonic_s),
                "sources": rows, "conflicts": conflicts, "incomparable": incomparable,
                "required_sources": required,
                "input_evidence_ready": bool(required) and status == "VALID" and all(s in verified for s in required),
                "action_readiness": False, "actual_motion_permission": False,
                "evidence_group_count": len(groups),
                "evidence_groups": [{"sources": sorted(g["sources"]), "correlation_tags": sorted(g["_tags"])} for g in groups],
                "statistical_independence_verified": False, "distance_averaging_applied": False,
                "limitations": ["Input validation is not sensor calibration or a movement authorization.",
                                "Camera validation covers packet metadata/frame references, not image content; pose payload is not implemented.",
                                "Only registered normalized same-target quantities are compared.",
                                "Configured correlation groups are incomplete evidence, not proof of independence.",
                                "No camera, LiDAR, GPIO, serial, network or actuator was accessed."]}


def fixture_spec(source_id="ultrasonic_front", **changes):
    """Synthetic, explicitly verified fixture registration; never real wiring."""
    values = dict(source_id=source_id, sensor_kind="range", physical_mount_id="fixture_front",
                  frame_id="fixture_base", calibration_version="fixture-cal-1", registration_verified=True,
                  calibration_verified=True, direction_rad=0.0, fov_rad=.4,
                  comparison_key="fixture_normalized_same_wall_distance", comparison_basis_verified=True)
    values.update(changes)
    return SourceSpec(**values)


def fixture_packet(source_id="ultrasonic_front", **changes):
    values = dict(schema_version=1, source_id=source_id, sensor_kind="range", physical_mount_id="fixture_front",
                  frame_id="fixture_base", sequence=1, sample_monotonic_s=10.0, receive_monotonic_s=10.01,
                  clock_domain="fixture_clock", session_id="fixture_boot_1", units="m", validity="valid",
                  quality=.9, quality_kind="score", calibration_version="fixture-cal-1", distance_m=.4,
                  uncertainty_m=.02, direction_rad=0.0, fov_rad=.4, measurement_basis="synthetic_fixture")
    values.update(changes)
    return values


def run_fixture_validation(output: Path | None = None):
    """Run offline fault fixtures; optionally save a reviewable JSON report."""
    cases = []

    def monitor(specs=None):
        return ObservationMonitor(specs or [fixture_spec()], clock_domain="fixture_clock", session_id="fixture_boot_1")

    def single(case_id, packet, expected, specs=None):
        checked = monitor(specs).ingest(packet, now_monotonic_s=10.03)
        cases.append({"id": case_id, "expected_status": expected, "actual_status": checked.status,
                      "passed": checked.status == expected, "evidence": checked.to_dict()})

    single("O01_valid_fixture", fixture_packet(), "VALID")
    missing = fixture_packet(); del missing["sample_monotonic_s"]
    single("O02_missing_timestamp", missing, "UNKNOWN")
    single("O03_unknown_source", fixture_packet(source_id="unregistered"), "UNKNOWN")
    single("O04_frame_mismatch", fixture_packet(frame_id="unverified_frame"), "CONFLICT")
    single("O05_nonfinite_distance", fixture_packet(distance_m=float("inf")), "UNKNOWN")
    single("O06_negative_distance", fixture_packet(distance_m=-.1), "UNKNOWN")
    single("O07_clock_skew", fixture_packet(sample_monotonic_s=11), "CONFLICT")
    single("O08_mixed_epoch", fixture_packet(session_id="another_boot"), "CONFLICT")
    ordered = monitor(); ordered.ingest(fixture_packet(), now_monotonic_s=10.03)
    duplicate = ordered.ingest(fixture_packet(), now_monotonic_s=10.04)
    cases.append({"id": "O09_duplicate_sequence", "expected_status": "CONFLICT", "actual_status": duplicate.status,
                  "passed": duplicate.status == "CONFLICT", "evidence": duplicate.to_dict()})
    stale_monitor = monitor(); stale_monitor.ingest(fixture_packet(), now_monotonic_s=10.03)
    stale = stale_monitor.snapshot(now_monotonic_s=11)
    cases.append({"id": "O10_stale_snapshot", "expected_status": "STALE", "actual_status": stale["status"],
                  "passed": stale["status"] == "STALE", "evidence": stale})
    single("O11_direction_unknown", fixture_packet(direction_rad=None), "UNKNOWN")
    single("O12_no_return", fixture_packet(validity="no_return", distance_m=None), "UNKNOWN")
    pairs = monitor([fixture_spec(), fixture_spec("derived_front")])
    pairs.ingest(fixture_packet(), now_monotonic_s=10.03)
    pairs.ingest(fixture_packet("derived_front", distance_m=.8), now_monotonic_s=10.03)
    conflict = pairs.snapshot(now_monotonic_s=10.03)
    cases.append({"id": "O13_conflicting_ranges", "expected_status": "CONFLICT", "actual_status": conflict["status"],
                  "passed": conflict["status"] == "CONFLICT" and not conflict["distance_averaging_applied"], "evidence": conflict})
    correlated = monitor([fixture_spec(), fixture_spec("derived_front")])
    correlated.ingest(fixture_packet(), now_monotonic_s=10.03)
    correlated.ingest(fixture_packet("derived_front", distance_m=.41), now_monotonic_s=10.03)
    grouped = correlated.snapshot(now_monotonic_s=10.03)
    cases.append({"id": "O14_shared_mount_correlation", "expected_status": "VALID", "actual_status": grouped["status"],
                  "passed": grouped["status"] == "VALID" and grouped["evidence_group_count"] == 1, "evidence": grouped})
    single("O15_lift_without_reference", fixture_packet(sensor_kind="lift", distance_m=None, height_m=.05),
           "UNKNOWN", [fixture_spec(sensor_kind="lift")])
    report = {"schema_version": SCHEMA_VERSION, "mode": "offline_synthetic_fixtures", "cases": cases,
              "total": len(cases), "passed": sum(case["passed"] for case in cases),
              "all_passed": all(case["passed"] for case in cases), "sample_snapshot": grouped,
              "hardware_accessed": False, "actual_motion_permission": False,
              "statement": "These are synthetic contract tests, not live sensor or physical safety validation."}
    if output is not None:
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        destination = output / "observation_contract_validation.json"
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        report["artifact"] = str(destination.resolve())
    return report
