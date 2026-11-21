"""Safety behavior with an injected clock/backend; no GPIO imports or devices."""
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from manual_station.controller import Controller, CHANNELS


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Backend:
    def __init__(self):
        self.calls = []
        self.limit_states = {"upper": False, "lower": False}
        self.fail = None

    def open(self, scope):
        self.calls.append(("open", scope))
        if self.fail == "open":
            raise OSError("partial open")

    def apply(self, outputs, deadline):
        self.calls.append(("apply", dict(outputs), deadline))
        if self.fail == "apply":
            raise OSError("output failure")

    def stop(self):
        self.calls.append(("stop",))
        if self.fail == "stop":
            raise OSError("stop failure")

    def close(self):
        self.calls.append(("close",))
        if self.fail == "close":
            raise OSError("close failure")

    def limits(self):
        self.calls.append(("limits",))
        return dict(self.limit_states)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.backend = Backend()
        self.path = Path(self.directory.name) / "events.sqlite3"
        self.controller = Controller({"wheels": {}, "lift": {}}, self.backend,
                                     db_path=self.path, clock=self.clock, start_watchdog=False)
        self.lease = self.controller.request("claim", {"label": "operator"})["lease"]
        self.sequence = 0

    def tearDown(self):
        try:
            self.controller.close()
        except RuntimeError:
            pass
        self.directory.cleanup()

    def arm(self, scope="wheels"):
        lifecycle = self.controller.snapshot(self.lease)["lifecycle_permit"]
        return self.controller.request("arm", {"lease": self.lease, "scope": scope, "ack": True,
                                               "lifecycle_permit": lifecycle})

    def reset(self):
        lifecycle = self.controller.snapshot(self.lease)["lifecycle_permit"]
        return self.controller.request("reset", {"lease": self.lease, "lifecycle_permit": lifecycle})

    def command(self, command="forward", duty=20, **extra):
        permit = self.controller.snapshot(self.lease)["permit"]
        self.sequence += 1
        return self.controller.request("command", {"lease": self.lease, "permit": permit,
                "seq": self.sequence, "command": command, "duty": duty, **extra})

    def test_startup_and_arm_never_apply_output(self):
        self.assertEqual(self.backend.calls, [])
        status = self.arm()
        self.assertEqual(status["state"], "ARMED")
        self.assertEqual(self.backend.calls, [("open", "wheels")])
        self.assertTrue(all(value == 0 for value in status["outputs"].values()))

    def test_claim_is_exclusive_and_observers_have_no_secrets(self):
        with self.assertRaises(ValueError):
            self.controller.request("claim", {"label": "second"})
        self.arm()
        observer = self.controller.snapshot()
        self.assertIsNone(observer["permit"])
        self.assertNotIn(self.lease, json.dumps(observer))
        self.assertFalse(observer["owns_control"])

    def test_all_mixing_obeys_duty_limit(self):
        for command in ("forward", "reverse", "left", "right"):
            self.arm()
            status = self.command(command, 40)
            self.assertEqual({abs(status["outputs"][c]) for c in CHANNELS}, {40})
            self.controller.request("stop", {})
            self.clock.advance(.201)

    def test_single_channel_uses_electrical_direction(self):
        self.arm()
        status = self.command("channel", 12, channel="rear-a", direction=-1)
        self.assertEqual(status["outputs"]["rear-a"], -12)
        self.assertEqual(sum(v != 0 for v in status["outputs"].values()), 1)

    def test_invalid_duty_and_sequence_do_not_apply(self):
        self.arm()
        for duty in (True, False, math.nan, math.inf, -1, 0, 40.01, "20", None):
            with self.subTest(duty=duty), self.assertRaises(ValueError):
                self.command(duty=duty)
        for sequence in (True, -1, 1.2, "1", 2**53):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                self.controller.request("command", {"lease": self.lease, "permit": self.controller.snapshot(self.lease)["permit"],
                        "seq": sequence, "command": "forward"})
        self.assertFalse(any(call[0] == "apply" for call in self.backend.calls))

    def test_polling_does_not_extend_command_deadline(self):
        self.arm()
        self.command()
        deadline = next(c[2] for c in self.backend.calls if c[0] == "apply")
        for _ in range(3):
            self.clock.advance(.08)
            self.controller.snapshot(self.lease)
        deadlines = [c[2] for c in self.backend.calls if c[0] == "apply"]
        self.assertTrue(all(value == deadline for value in deadlines))
        self.clock.advance(.061)
        status = self.controller.snapshot(self.lease)
        self.assertEqual(status["state"], "DISARMED")
        self.assertIn("deadline", status["reason"])
        self.assertEqual(self.backend.calls[-2:], [("stop",), ("close",)])

    def test_delayed_request_keeps_remaining_permit_time_only(self):
        permit = self.arm()["permit"]
        expiry = self.clock.value + .3
        self.clock.advance(.25)
        self.controller.request("command", {"lease": self.lease, "permit": permit, "seq": 1, "command": "forward"})
        self.assertAlmostEqual(self.backend.calls[-1][2], expiry)
        self.clock.advance(.051)
        self.assertEqual(self.controller.snapshot(self.lease)["state"], "DISARMED")

    def test_expired_duplicate_and_old_generation_commands_rejected(self):
        permit = self.arm()["permit"]
        self.clock.advance(.301)
        with self.assertRaises(ValueError):
            self.controller.request("command", {"lease": self.lease, "permit": permit, "seq": 1, "command": "forward"})
        permit = self.controller.snapshot(self.lease)["permit"]
        body = {"lease": self.lease, "permit": permit, "seq": 1, "command": "forward"}
        self.controller.request("command", body)
        for sequence in (1, 2):
            with self.assertRaises(ValueError):
                self.controller.request("command", {**body, "seq": sequence})
        old = self.controller.snapshot(self.lease)["permit"]
        self.controller.request("stop", {})
        self.clock.advance(.201)
        self.arm()
        with self.assertRaises(ValueError):
            self.controller.request("command", {**body, "permit": old, "seq": 3})

    def test_refreshing_input_cannot_extend_two_second_burst(self):
        self.arm()
        self.command()
        for _ in range(19):
            self.clock.advance(.1)
            self.command()
        self.clock.advance(.101)
        status = self.controller.snapshot(self.lease)
        self.assertEqual(status["state"], "DISARMED")
        self.assertIn("burst", status["reason"])

    def test_direction_change_requires_stop_delay_arm_and_new_input(self):
        self.arm()
        self.command("forward")
        with self.assertRaises(ValueError):
            self.command("reverse")
        self.assertEqual(self.controller.snapshot(self.lease)["state"], "DISARMED")
        with self.assertRaises(ValueError):
            self.arm()
        self.clock.advance(.201)
        self.arm()
        self.assertEqual(self.controller.snapshot(self.lease)["outputs"]["front-a"], 0)
        self.command("reverse")

    def test_stop_does_not_refresh_old_output(self):
        self.arm()
        self.command()
        count = sum(c[0] == "apply" for c in self.backend.calls)
        self.clock.advance(.06)
        self.controller.request("stop", {})
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), count)

    def test_estop_is_global_and_reset_does_not_arm(self):
        self.arm()
        self.command()
        self.controller.request("estop", {})
        self.assertEqual(self.controller.request("stop", {})["state"], "ESTOP")
        with self.assertRaises(ValueError):
            self.arm()
        self.assertEqual(self.reset()["state"], "DISARMED")
        with self.assertRaises(ValueError):
            self.command()

    def test_global_stop_cannot_renew_an_already_expired_owner(self):
        self.clock.advance(3.001)
        result = self.controller.request("stop", {"lease": self.lease})
        self.assertIsNone(result["owner"])
        self.assertFalse(result["owns_control"])
        with self.assertRaises(ValueError):
            self.arm()

    def test_delayed_arm_captured_before_stop_cannot_rearm(self):
        captured = {"lease": self.lease, "scope": "wheels", "ack": True,
                    "lifecycle_permit": self.controller.snapshot(self.lease)["lifecycle_permit"]}
        self.controller.request("stop", {})
        self.clock.advance(.201)
        with self.assertRaises(ValueError):
            self.controller.request("arm", captured)
        self.assertEqual(self.controller.state, "DISARMED")
        self.assertFalse(any(call[0] == "open" for call in self.backend.calls))
        self.arm()  # Only a new explicit request with a post-STOP permit works.

    def test_old_reset_cannot_clear_a_newer_estop_latch(self):
        self.controller.request("estop", {})
        captured = {"lease": self.lease,
                    "lifecycle_permit": self.controller.snapshot(self.lease)["lifecycle_permit"]}
        self.controller.request("estop", {})  # Same state, new safety generation.
        with self.assertRaises(ValueError):
            self.controller.request("reset", captured)
        self.assertEqual(self.controller.state, "ESTOP")
        self.reset()
        self.assertEqual(self.controller.state, "DISARMED")

    def test_consumed_reset_cannot_be_replayed_after_another_estop(self):
        self.controller.request("estop", {})
        captured = {"lease": self.lease,
                    "lifecycle_permit": self.controller.snapshot(self.lease)["lifecycle_permit"]}
        self.controller.request("reset", captured)
        self.controller.request("estop", {})
        with self.assertRaises(ValueError):
            self.controller.request("reset", captured)
        self.assertEqual(self.controller.state, "ESTOP")

    def test_lifecycle_permits_required_and_separate_from_motion_permits(self):
        with self.assertRaises(ValueError):
            self.controller.request("arm", {"lease": self.lease, "scope": "wheels", "ack": True})
        before_arm = self.controller.snapshot(self.lease)["lifecycle_permit"]
        armed = self.arm()
        self.assertIsNone(armed["lifecycle_permit"])
        with self.assertRaises(ValueError):
            self.controller.request("command", {"lease": self.lease, "permit": before_arm,
                                                 "seq": 1, "command": "forward"})
        self.controller.request("stop", {})
        self.clock.advance(.201)
        with self.assertRaises(ValueError):
            self.controller.request("arm", {"lease": self.lease, "scope": "wheels", "ack": True,
                                             "lifecycle_permit": armed["permit"]})
        self.controller.request("estop", {})
        with self.assertRaises(ValueError):
            self.controller.request("reset", {"lease": self.lease})

    def test_lifecycle_permit_expires_without_any_safety_transition(self):
        captured = {"lease": self.lease, "scope": "wheels", "ack": True,
                    "lifecycle_permit": self.controller.snapshot(self.lease)["lifecycle_permit"]}
        self.clock.advance(1.001)
        with self.assertRaises(ValueError):
            self.controller.request("arm", captured)
        self.assertEqual(self.backend.calls, [])

    def test_lifecycle_registry_bounded_and_observer_gets_no_token(self):
        for _ in range(100):
            self.controller.snapshot(self.lease)
        self.assertLessEqual(len(self.controller._lifecycle_permits), 64)
        self.assertIsNone(self.controller.snapshot()["lifecycle_permit"])

    def test_lease_expiry_and_release_stop_and_revoke(self):
        self.arm()
        self.clock.advance(3.001)
        self.assertIsNone(self.controller.snapshot()["owner"])
        with self.assertRaises(ValueError):
            self.arm()
        self.lease = self.controller.request("claim", {"label": "new"})["lease"]
        self.clock.advance(.201)
        self.arm()
        self.command()
        self.controller.request("release", {"lease": self.lease})
        self.assertIsNone(self.controller.snapshot()["owner"])
        self.assertEqual(self.backend.calls[-2:], [("stop",), ("close",)])

    def test_partial_open_and_apply_errors_cleanup_and_latch_fault(self):
        self.backend.fail = "open"
        with self.assertRaises(RuntimeError):
            self.arm()
        self.assertEqual(self.controller.snapshot(self.lease)["state"], "FAULT")
        self.assertEqual(self.backend.calls[-2:], [("stop",), ("close",)])
        self.backend.fail = None
        self.reset()
        self.clock.advance(.201)
        self.arm()
        self.backend.fail = "apply"
        with self.assertRaises(RuntimeError):
            self.command()
        self.assertEqual(self.controller.snapshot(self.lease)["state"], "FAULT")
        self.assertEqual(self.backend.calls[-2:], [("stop",), ("close",)])

    def test_stop_failure_still_attempts_close_and_blocks_reset(self):
        self.arm()
        self.command()
        self.backend.fail = "stop"
        with self.assertRaises(RuntimeError):
            self.controller.request("stop", {})
        self.assertEqual(self.backend.calls[-2:], [("stop",), ("close",)])
        self.assertEqual(self.controller.snapshot(self.lease)["state"], "FAULT")
        with self.assertRaises(RuntimeError):
            self.reset()

    def test_audit_failure_stops_before_accepting_another_command(self):
        self.arm()
        self.command()
        count = sum(c[0] == "apply" for c in self.backend.calls)
        self.controller._db.close()
        with self.assertRaises(RuntimeError):
            self.command()
        self.assertEqual(self.controller.state, "FAULT")
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), count)
        self.assertEqual(self.backend.calls[-2:], [("stop",), ("close",)])

    def test_clock_regression_stops_and_latches_fault(self):
        self.arm()
        self.command()
        self.clock.advance(-.01)
        with self.assertRaises(RuntimeError):
            self.controller.snapshot(self.lease)
        self.assertEqual(self.controller.state, "FAULT")
        self.assertEqual(self.backend.calls[-2:], [("stop",), ("close",)])

    def test_lift_limit_stops_and_both_limits_fault(self):
        self.arm("lift")
        self.command("lift_up")
        self.backend.limit_states["upper"] = True
        self.assertEqual(self.controller.snapshot(self.lease)["state"], "DISARMED")
        self.clock.advance(.201)
        self.backend.limit_states["lower"] = True
        with self.assertRaises(RuntimeError):
            self.arm("lift")
        self.assertEqual(self.controller.state, "FAULT")

    def test_wheel_lift_scopes_are_exclusive(self):
        self.arm()
        with self.assertRaises(ValueError):
            self.command("lift_up")
        with self.assertRaises(ValueError):
            self.arm("lift")
        self.assertFalse(any(c[0] == "apply" for c in self.backend.calls))

    def test_real_rejects_unconfirmed_config_before_open(self):
        self.controller.mode = "real"
        with patch("manual_station.controller.validate_real", return_value=["unconfirmed wiring"]):
            with self.assertRaises(ValueError):
                self.arm()
        self.assertEqual(self.backend.calls, [])

    def test_real_body_requires_mapping_but_channel_does_not(self):
        self.controller.mode = "real"
        with patch("manual_station.controller.validate_real", return_value=[]):
            self.arm()
            with self.assertRaises(ValueError):
                self.command("forward")
            status = self.command("channel", channel="front-a", direction=1)
            self.assertEqual(status["outputs"]["front-a"], 20)
            self.assertFalse(status["mock_demo_mapping"])

    def test_note_is_observation_only_and_database_contains_no_tokens(self):
        self.controller.request("note", {"lease": self.lease, "text": "wheel label observed", "logistics_id": "007"})
        db = sqlite3.connect(self.path)
        try:
            rows = db.execute("SELECT kind,details_json FROM events").fetchall()
        finally:
            db.close()
        text = json.dumps(rows)
        self.assertNotIn(self.lease, text)
        self.assertIn("operator_observation_only", text)
        self.assertIn("007", text)
        self.assertFalse(any(c[0] == "apply" for c in self.backend.calls))

    def test_permit_expiring_during_audit_never_reaches_backend(self):
        self.arm()
        original = self.controller._audit

        def delayed_audit(kind, details):
            original(kind, details)
            if kind == "command":
                self.clock.advance(.301)

        with patch.object(self.controller, "_audit", side_effect=delayed_audit):
            with self.assertRaises(ValueError):
                self.command()
        self.assertEqual(self.controller.state, "DISARMED")
        self.assertFalse(any(c[0] == "apply" for c in self.backend.calls))

    def test_arm_audit_expiring_lifecycle_permit_never_opens_backend(self):
        original = self.controller._audit

        def delayed_audit(kind, details):
            original(kind, details)
            if kind == "arm":
                self.clock.advance(1.001)

        with patch.object(self.controller, "_audit", side_effect=delayed_audit):
            with self.assertRaises(ValueError):
                self.arm()
        self.assertEqual(self.controller.state, "DISARMED")
        self.assertEqual(self.backend.calls, [])

    def test_arm_audit_expiring_owner_lease_never_opens_backend(self):
        original = self.controller._audit

        def delayed_audit(kind, details):
            original(kind, details)
            if kind == "arm":
                self.clock.advance(4.0)

        with patch.object(self.controller, "_audit", side_effect=delayed_audit):
            with self.assertRaises(ValueError):
                self.arm()
        self.assertIsNone(self.controller.snapshot()["owner"])
        self.assertEqual(self.backend.calls, [])

    def test_reset_audit_expiring_lifecycle_permit_does_not_clear_estop(self):
        self.controller.request("estop", {})
        original = self.controller._audit

        def delayed_audit(kind, details):
            original(kind, details)
            if kind == "reset":
                self.clock.advance(1.001)

        with patch.object(self.controller, "_audit", side_effect=delayed_audit):
            with self.assertRaises(ValueError):
                self.reset()
        self.assertEqual(self.controller.state, "ESTOP")

    def test_active_deadline_expiring_during_audit_cannot_be_revived(self):
        self.arm()
        self.command()
        self.clock.advance(.25)
        original = self.controller._audit

        def delayed_audit(kind, details):
            original(kind, details)
            if kind == "command":
                self.clock.advance(.06)

        before = sum(c[0] == "apply" for c in self.backend.calls)
        with patch.object(self.controller, "_audit", side_effect=delayed_audit):
            with self.assertRaises(ValueError):
                self.command()
        self.assertEqual(self.controller.state, "DISARMED")
        # The helper's status poll may refresh the old, still-valid deadline,
        # but no application is permitted after the audit crossed that expiry.
        self.assertLessEqual(sum(c[0] == "apply" for c in self.backend.calls) - before, 1)
        self.assertEqual(self.backend.calls[-2:], [("stop",), ("close",)])

    def test_slow_backend_open_losing_lease_cannot_arm(self):
        original = self.backend.open

        def delayed_open(scope):
            original(scope)
            self.clock.advance(3.001)

        with patch.object(self.backend, "open", side_effect=delayed_open):
            with self.assertRaises(RuntimeError):
                self.arm()
        self.assertIsNone(self.controller.snapshot()["owner"])
        self.assertEqual(self.controller.state, "FAULT")
        self.assertEqual(self.backend.calls[-2:], [("stop",), ("close",)])

    def test_stop_cooldown_measured_after_cleanup_finishes(self):
        self.arm()
        self.command()
        original = self.backend.stop

        def delayed_stop():
            original()
            self.clock.advance(.5)

        with patch.object(self.backend, "stop", side_effect=delayed_stop):
            self.controller.request("stop", {})
        with self.assertRaises(ValueError):
            self.arm()
        self.clock.advance(.201)
        self.arm()

    def test_configured_duty_ceiling_and_scope_outputs(self):
        self.controller.config["max_duty_percent"] = 15
        self.arm()
        with self.assertRaises(ValueError):
            self.command(duty=16)
        self.command(duty=15)
        self.assertEqual(set(self.backend.calls[-1][1]), set(CHANNELS))
        self.controller.request("stop", {})
        self.clock.advance(.201)
        self.arm("lift")
        self.command("lift_up", 15)
        self.assertEqual(set([call for call in self.backend.calls if call[0] == "apply"][-1][1]), {"lift"})

    def test_independent_watchdog_stops_without_any_http_poll(self):
        self.controller.close()
        stopped = threading.Event()
        original = self.backend.stop

        def report_stop():
            original()
            stopped.set()

        self.backend.stop = report_stop
        self.controller = Controller({"wheels": {}, "lift": {}}, self.backend,
                                     db_path=self.path, clock=self.clock, start_watchdog=True)
        self.lease = self.controller.request("claim", {"label": "watchdog-test"})["lease"]
        self.arm()
        self.command()
        self.clock.advance(.301)
        self.assertTrue(stopped.wait(.5), "watchdog failed to stop without client polling")
        with self.controller._lock:
            self.assertEqual(self.controller.state, "DISARMED")


if __name__ == "__main__":
    unittest.main()
