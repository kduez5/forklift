from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from integration_contracts.observations import (
    LiftReference, ObservationMonitor, fixture_packet, fixture_spec, run_fixture_validation,
)


class ObservationContractTests(unittest.TestCase):
    def monitor(self, specs=None, references=()):
        return ObservationMonitor(specs or [fixture_spec()], clock_domain="fixture_clock",
                                  session_id="fixture_boot_1", lift_references=references)

    def test_registered_fresh_evidence_preserves_uncertainty_without_motion_permission(self):
        monitor = self.monitor()
        self.assertEqual(monitor.ingest(fixture_packet(), now_monotonic_s=10.03).status, "VALID")
        snapshot = monitor.snapshot(now_monotonic_s=10.03)
        self.assertEqual(snapshot["sources"][0]["distance_interval_m"], [.38, .42000000000000004])
        self.assertTrue(snapshot["input_evidence_ready"])
        self.assertFalse(snapshot["actual_motion_permission"])
        self.assertFalse(snapshot["action_readiness"])

    def test_missing_fields_and_unknown_source_do_not_become_valid(self):
        packet = fixture_packet(); del packet["sample_monotonic_s"]
        self.assertEqual(self.monitor().ingest(packet, now_monotonic_s=10.03).status, "UNKNOWN")
        self.assertEqual(self.monitor().ingest(fixture_packet(source_id="wrong"), now_monotonic_s=10.03).status, "UNKNOWN")
        malformed = fixture_packet(); malformed[5] = "unexpected"
        self.assertEqual(self.monitor().ingest(malformed, now_monotonic_s=10.03).status, "UNKNOWN")

    def test_frame_units_mount_and_calibration_mismatches_conflict(self):
        for changes in ({"frame_id": "other"}, {"units": "cm"}, {"physical_mount_id": "rear"},
                        {"calibration_version": "unknown"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.monitor().ingest(fixture_packet(**changes), now_monotonic_s=10.03).status, "CONFLICT")
        with self.assertRaises(ValueError):
            self.monitor([fixture_spec(units="cm")])
        camera = self.monitor([fixture_spec(sensor_kind="camera", units="pixel")])
        self.assertEqual(camera.ingest(fixture_packet(sensor_kind="camera", units="pixel", raw_evidence_id="frame1"), now_monotonic_s=10.03).status, "CONFLICT")

    def test_nan_infinity_boolean_and_negative_ranges_are_not_measurements(self):
        for value in (float("nan"), float("inf"), True, -.1):
            with self.subTest(value=value):
                result = self.monitor().ingest(fixture_packet(distance_m=value), now_monotonic_s=10.03)
                self.assertEqual(result.status, "UNKNOWN")
                json.dumps(result.to_dict(), allow_nan=False)

    def test_no_return_is_unknown_and_zero_substitution_is_rejected(self):
        result = self.monitor().ingest(fixture_packet(validity="no_return", distance_m=None), now_monotonic_s=10.03)
        self.assertEqual(result.status, "UNKNOWN")
        for value in (0.0, 4.0):
            self.assertEqual(self.monitor().ingest(fixture_packet(validity="no_return", distance_m=value), now_monotonic_s=10.03).status, "CONFLICT")

    def test_clock_order_future_and_invalid_clock_values(self):
        for changes in ({"sample_monotonic_s": 11}, {"receive_monotonic_s": 11},
                        {"sample_monotonic_s": -1}, {"receive_monotonic_s": float("nan")}):
            self.assertEqual(self.monitor().ingest(fixture_packet(**changes), now_monotonic_s=10.03).status, "CONFLICT")

    def test_clock_domain_and_boot_epoch_are_not_mixed(self):
        for changes in ({"clock_domain": "wall_clock"}, {"session_id": "rebooted"}):
            result = self.monitor().ingest(fixture_packet(**changes), now_monotonic_s=10.03)
            self.assertEqual(result.status, "CONFLICT")
            self.assertIsNone(result.age_s)

    def test_duplicate_out_of_order_and_time_regression_invalidate_latest(self):
        for changes in ({"sequence": 2}, {"sequence": 1}, {"sequence": 3, "sample_monotonic_s": 9.9}):
            monitor = self.monitor()
            monitor.ingest(fixture_packet(sequence=2), now_monotonic_s=10.03)
            self.assertEqual(monitor.ingest(fixture_packet(**changes), now_monotonic_s=10.04).status, "CONFLICT")
            self.assertFalse(monitor.snapshot(now_monotonic_s=10.04)["input_evidence_ready"])

    def test_stale_snapshot_does_not_refresh_and_future_snapshot_clock_conflicts(self):
        monitor = self.monitor(); monitor.ingest(fixture_packet(), now_monotonic_s=10.03)
        self.assertEqual(monitor.snapshot(now_monotonic_s=11)["status"], "STALE")
        self.assertEqual(monitor.snapshot(now_monotonic_s=11)["sources"][0]["age_s"], 1.0)
        self.assertEqual(monitor.snapshot(now_monotonic_s=9)["status"], "CONFLICT")

    def test_direction_fov_and_registration_verification_are_required(self):
        for changes in ({"direction_rad": None}, {"fov_rad": None}):
            self.assertEqual(self.monitor().ingest(fixture_packet(**changes), now_monotonic_s=10.03).status, "UNKNOWN")
        monitor = self.monitor([fixture_spec(calibration_verified=False)])
        self.assertEqual(monitor.ingest(fixture_packet(), now_monotonic_s=10.03).status, "UNKNOWN")
        self.assertEqual(self.monitor().ingest(fixture_packet(direction_rad=3.14), now_monotonic_s=10.03).status, "CONFLICT")
        for changes in ({"direction_rad": float("nan")}, {"fov_rad": float("inf")},
                        {"max_age_s": float("nan")}, {"registration_verified": "false"}):
            with self.assertRaises(ValueError):
                self.monitor([fixture_spec(**changes)])
        self.assertEqual(self.monitor().ingest(fixture_packet(raw_evidence_id=1), now_monotonic_s=10.03).status, "UNKNOWN")

    def test_conflicting_same_target_intervals_are_not_averaged(self):
        monitor = self.monitor([fixture_spec(), fixture_spec("second")])
        monitor.ingest(fixture_packet(), now_monotonic_s=10.03)
        monitor.ingest(fixture_packet("second", distance_m=.9), now_monotonic_s=10.03)
        snapshot = monitor.snapshot(now_monotonic_s=10.03)
        self.assertEqual(snapshot["status"], "CONFLICT")
        self.assertEqual(len(snapshot["conflicts"]), 1)
        self.assertFalse(snapshot["distance_averaging_applied"])
        self.assertEqual([row["observation"]["distance_m"] for row in snapshot["sources"]], [.4, .9])

    def test_unsynchronized_or_unregistered_comparison_basis_is_unknown(self):
        monitor = self.monitor([fixture_spec(), fixture_spec("second", comparison_basis_verified=False)])
        monitor.ingest(fixture_packet(), now_monotonic_s=10.03)
        monitor.ingest(fixture_packet("second"), now_monotonic_s=10.03)
        self.assertEqual(monitor.snapshot(now_monotonic_s=10.03)["status"], "UNKNOWN")
        monitor = self.monitor([fixture_spec(), fixture_spec("second")])
        monitor.ingest(fixture_packet(), now_monotonic_s=10.11)
        monitor.ingest(fixture_packet("second", sample_monotonic_s=10.1, receive_monotonic_s=10.11), now_monotonic_s=10.12)
        self.assertEqual(monitor.snapshot(now_monotonic_s=10.12)["status"], "UNKNOWN")

    def test_common_mount_raw_data_and_power_group_do_not_multiply_evidence(self):
        for second_spec, packet_changes in (
                (fixture_spec("second"), {}),
                (fixture_spec("second", physical_mount_id="other"), {"physical_mount_id": "other", "raw_evidence_id": "shared_scan"}),
                (fixture_spec("second", physical_mount_id="other", correlation_groups=("power:supply1",)), {"physical_mount_id": "other"})):
            monitor = self.monitor([fixture_spec(correlation_groups=("power:supply1",)), second_spec])
            monitor.ingest(fixture_packet(raw_evidence_id="shared_scan"), now_monotonic_s=10.03)
            monitor.ingest(fixture_packet("second", **packet_changes), now_monotonic_s=10.03)
            snapshot = monitor.snapshot(now_monotonic_s=10.03)
            self.assertEqual(snapshot["evidence_group_count"], 1)
            self.assertFalse(snapshot["statistical_independence_verified"])

    def test_lift_needs_verified_same_session_reference_and_limit_evidence(self):
        spec = fixture_spec(sensor_kind="lift")
        packet = fixture_packet(sensor_kind="lift", distance_m=None, height_m=.05,
                                lift_reference_id="home", measurement_basis="estimated")
        self.assertEqual(self.monitor([spec]).ingest(packet, now_monotonic_s=10.03).status, "UNKNOWN")
        reference = LiftReference("home", "fixture_clock", "fixture_boot_1", 9, 0, "fixture_limit_edge", True)
        with self.assertRaises(ValueError):
            self.monitor([spec], [replace(reference, reference_id={})])
        for invalid in (replace(reference, verified=False), replace(reference, limit_evidence_id=None), replace(reference, session_id="older")):
            self.assertEqual(self.monitor([spec], [invalid]).ingest(packet, now_monotonic_s=10.03).status, "UNKNOWN")
        valid = self.monitor([spec], [reference]).ingest(packet, now_monotonic_s=10.03)
        self.assertEqual(valid.status, "VALID")
        self.assertEqual(valid.observation.measurement_basis, "estimated")

    def test_missing_required_source_and_fixture_report(self):
        monitor = self.monitor([fixture_spec(), fixture_spec("missing")])
        monitor.ingest(fixture_packet(), now_monotonic_s=10.03)
        self.assertEqual(monitor.snapshot(now_monotonic_s=10.03)["status"], "UNKNOWN")
        self.assertFalse(monitor.snapshot(now_monotonic_s=10.03)["input_evidence_ready"])
        for kind in ("camera", "pose"):
            with self.subTest(kind=kind):
                monitor = self.monitor([fixture_spec(sensor_kind=kind)])
                result = monitor.ingest(fixture_packet(sensor_kind=kind, distance_m=None), now_monotonic_s=10.03)
                self.assertEqual(result.status, "UNKNOWN")
        with tempfile.TemporaryDirectory() as directory:
            report = run_fixture_validation(Path(directory))
            self.assertEqual(report["total"], 15)
            self.assertTrue(report["all_passed"])
            self.assertTrue(Path(report["artifact"]).exists())
            self.assertFalse(report["hardware_accessed"])


if __name__ == "__main__":
    unittest.main()
