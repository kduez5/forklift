"""Offline safety checks: every GPIO operation uses FakeGPIO, never lgpio."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from manual_station import backends as b
from manual_station.config import CHANNELS, COMMON_CHECKS, load_config, validate_real

TEMPLATE = Path(__file__).resolve().parents[1] / "config.template.json"


def wheel_config():
    config = load_config(TEMPLATE)
    config["safety_checks"] = {name: True for name in config["safety_checks"]}
    for index, name in enumerate(CHANNELS):
        config["wheels"][name].update(in1=4 + 3 * index, in2=5 + 3 * index,
                                     enable=6 + 3 * index, confirmed=True)
    return config


def lift_config(driver="dual_pwm"):
    config = wheel_config()
    config["lift"].update(enabled=True, confirmed=True, driver=driver, in1=16, in2=17,
                          enable=18 if driver == "enable_pwm" else None, up_sign=1)
    config["lift"]["upper_limit"]["pin"] = 19
    config["lift"]["lower_limit"]["pin"] = 20
    return config


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeGPIO:
    TX_PWM = 0
    BAD_PWM_MICROS = -9
    SET_PULL_DOWN = 32
    SET_PULL_UP = 16

    def __init__(self, clock, fail=None):
        self.clock = clock
        self.calls = []
        self.levels = {}
        self.busy = {}
        self.fail = fail
        self.hook = None
        self.stuck_busy = None
        self.finish_during_cancel = False

    def record(self, name, *args):
        self.calls.append((name, *args))
        if self.hook:
            self.hook(name, args)
        if self.fail and self.fail(name, args):
            raise RuntimeError("injected " + name)
        return 0

    def gpiochip_open(self, index):
        self.record("open", index)
        return 100 + index

    def gpio_get_chip_info(self, handle):
        self.record("info", handle)
        return (0, 54, "fake", b"pinctrl-rp1" if handle == 104 else b"other")

    def gpiochip_close(self, handle):
        return self.record("close", handle)

    def gpio_claim_output(self, handle, pin, level=0):
        self.record("claim", pin, level)
        self.levels[pin] = level
        return 0

    def gpio_claim_input(self, handle, pin, flags):
        self.record("input", pin, flags)
        self.levels.setdefault(pin, 1)
        return 0

    def gpio_write(self, handle, pin, level):
        self.record("write", pin, level)
        self.levels[pin] = level
        return 0

    def gpio_read(self, handle, pin):
        self.record("read", pin)
        return self.levels[pin]

    def gpio_free(self, handle, pin):
        return self.record("free", pin)

    def tx_pulse(self, handle, pin, on, off, pulse_offset=0, pulse_cycles=0):
        self.record("pulse", pin, on, off, pulse_cycles, self.clock())
        if on or off:
            if self.busy.get(pin):
                raise RuntimeError("test detected queued PWM backlog")
            if pulse_cycles <= 0:
                raise RuntimeError("test detected infinite PWM")
            self.busy[pin] = True
        else:
            if self.finish_during_cancel:
                self.busy[pin] = False
            if not self.busy.get(pin):
                return self.BAD_PWM_MICROS
            self.busy[pin] = pin == self.stuck_busy
        return 4

    def tx_busy(self, handle, pin, kind):
        self.record("busy", pin)
        return int(self.busy.get(pin, False))


class ConfigTests(unittest.TestCase):
    def test_template_never_commissions_hardware(self):
        config = load_config(TEMPLATE)
        self.assertTrue(validate_real(config, "wheels"))
        self.assertTrue(validate_real(config, "lift"))
        self.assertTrue(all(w["in1"] is None and w["confirmed"] is False for w in config["wheels"].values()))
        self.assertFalse(config["lift"]["enabled"])

    def test_json_duplicate_nonfinite_and_nonobject_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for body in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}', '[]'):
                path.write_text(body)
                with self.subTest(body=body), self.assertRaises(ValueError):
                    load_config(path)

    def test_each_required_confirmation_and_invalid_numeric_rejected(self):
        config = wheel_config()
        self.assertEqual(validate_real(config, "wheels"), [])
        for name in COMMON_CHECKS + ("wheels_unloaded",):
            bad = copy.deepcopy(config)
            bad["safety_checks"][name] = 1  # Truthy is not explicit JSON true.
            self.assertTrue(validate_real(bad, "wheels"), name)
        for value in (True, 41, float("nan"), 0):
            bad = copy.deepcopy(config)
            bad["max_duty_percent"] = value
            self.assertTrue(validate_real(bad, "wheels"))

    def test_collisions_include_inactive_scope_and_reserved_pins(self):
        config = wheel_config()
        config["lift"]["in1"] = 4
        self.assertTrue(any("duplicates" in e for e in validate_real(config, "wheels")))
        config = wheel_config()
        config["reserved_pins"] = [4]
        self.assertTrue(any("reserved" in e for e in validate_real(config, "wheels")))
        config["wheels"]["front-a"]["in1"] = True
        self.assertTrue(validate_real(config, "wheels"))

    def test_unknown_sides_allowed_only_for_electrical_channel_mapping(self):
        config = wheel_config()
        self.assertEqual(validate_real(config, "wheels"), [])
        config["wheels"]["front-a"]["forward_sign"] = True
        self.assertTrue(validate_real(config, "wheels"))

    def test_lift_requires_topology_limits_and_confirmations(self):
        for driver in ("dual_pwm", "enable_pwm"):
            config = lift_config(driver)
            self.assertEqual(validate_real(config, "lift"), [])
            for key in ("lift_holding_verified", "lift_limits_verified", "lift_inputs_healthy"):
                bad = copy.deepcopy(config)
                bad["safety_checks"][key] = False
                self.assertTrue(validate_real(bad, "lift"))
            config["lift"]["upper_limit"]["pin"] = None
            self.assertTrue(validate_real(config, "lift"))


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.clock_patch = patch.object(b.time, "monotonic", self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    def opened(self, config=None, scope="wheels", gpio=None):
        gpio = gpio or FakeGPIO(self.clock)
        backend = b.RealBackend(config or wheel_config())
        for name, value in (("_check_host", None), ("_take_lock", None), ("_check_lock", None), ("_load_gpio", gpio)):
            mock = patch.object(backend, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        devices = patch.object(Path, "glob", return_value=[Path("/dev/gpiochip0"), Path("/dev/gpiochip4")])
        devices.start()
        self.addCleanup(devices.stop)
        backend.open(scope)
        self.addCleanup(lambda: backend.close() if backend._handle is not None else None)
        return backend, gpio

    def test_mock_imports_no_gpio_and_accepts_template(self):
        config = load_config(TEMPLATE)
        before = set(sys.modules)
        backend = b.MockBackend(config)
        with patch.object(b.importlib, "import_module", side_effect=AssertionError("No device import")):
            backend.open("wheels")
            backend.apply({"front-a": 20}, 1000.2)
            backend.close()
            backend.open("lift")
            backend.apply({"lift": -20}, 1000.2)
            backend.close()
        self.assertNotIn("lgpio", set(sys.modules) - before)
        self.assertFalse(config["wheels"]["front-a"]["confirmed"])
        self.assertTrue(all(event["mock"] for event in backend.commands))

    def test_real_validation_precedes_host_lock_and_import(self):
        backend = b.RealBackend(load_config(TEMPLATE))
        with patch.object(backend, "_check_host") as host, patch.object(backend, "_take_lock") as lock, patch.object(backend, "_load_gpio") as gpio:
            with self.assertRaises(ValueError):
                backend.open("wheels")
            host.assert_not_called()
            lock.assert_not_called()
            gpio.assert_not_called()

    def test_rp1_selection_all_enables_claimed_low_before_directions(self):
        backend, gpio = self.opened()
        claims = [x for x in gpio.calls if x[0] == "claim"]
        self.assertEqual([x[1] for x in claims[:4]], [6, 9, 12, 15])
        self.assertTrue(all(x[2] == 0 for x in claims))
        self.assertIn(("close", 100), gpio.calls)
        self.assertEqual(backend._handle, 104)
        self.assertFalse(any(x[0] == "pulse" and x[2] > 0 for x in gpio.calls))

    def test_finite_refresh_cancels_queue_and_stays_inside_deadline(self):
        backend, gpio = self.opened()
        backend.apply({name: 40 for name in CHANNELS}, 1000.076)
        self.clock.now += 0.05
        backend.apply({"front-a": 20}, 1000.141)
        pulses = [x for x in gpio.calls if x[0] == "pulse" and x[2]]
        self.assertEqual(len(pulses), 5)
        for index, call in enumerate(pulses):
            _, _pin, on, off, cycles, started = call
            self.assertGreater(cycles, 0)
            self.assertLessEqual(on / (on + off) * 100, 40)
            duration = (on + off) * cycles / 1e6
            self.assertLessEqual(duration, 0.12)
            self.assertLessEqual(started + duration + b.PERIOD_US / 1e6, 1000.076 if index < 4 else 1000.141)

    def test_expired_invalid_and_out_of_scope_commands_stop_everything(self):
        for outputs, deadline in (({"front-a": True}, 1000.2), ({"front-a": 41}, 1000.2),
                                  ({"front-a": float("nan")}, 1000.2), ({"lift": 20}, 1000.2),
                                  ({"front-a": 20}, 1000.0)):
            with self.subTest(outputs=outputs, deadline=deadline):
                backend, gpio = self.opened()
                backend.apply({"front-a": 20}, 1000.2)
                with self.assertRaises((ValueError, RuntimeError)):
                    backend.apply(outputs, deadline)
                self.assertIsNone(backend._handle)
                self.assertTrue(all(level == 0 for level in gpio.levels.values()))

    def test_deadline_checked_after_each_direction_write(self):
        backend, gpio = self.opened()
        def delay(name, args):
            if name == "write" and args[1] == 1:
                self.clock.now += 0.5
        gpio.hook = delay
        with self.assertRaises(ValueError):
            backend.apply({"front-a": 20}, 1000.1)
        self.assertFalse(any(x[0] == "pulse" and x[2] for x in gpio.calls))
        self.assertTrue(all(value == 0 for value in gpio.levels.values()))

    def test_slow_pwm_submission_aborts_and_cleans_up(self):
        backend, gpio = self.opened()
        gpio.hook = lambda name, args: setattr(self.clock, "now", self.clock.now + 0.04) if name == "pulse" and args[1] else None
        with self.assertRaises(RuntimeError):
            backend.apply({"front-a": 20}, 1000.08)
        self.assertIsNone(backend._handle)
        self.assertFalse(any(gpio.busy.values()))

    def test_reversal_rejected_without_off_dwell(self):
        backend, gpio = self.opened()
        backend.apply({"front-a": 20}, 1000.2)
        with self.assertRaises(ValueError):
            backend.apply({"front-a": -20}, 1000.2)
        self.assertIsNone(backend._handle)

    def test_reversal_after_explicit_stop_and_dwell(self):
        backend, gpio = self.opened()
        backend.apply({"front-a": 20}, 1000.2)
        backend.stop()
        self.clock.now += 0.201
        backend.apply({"front-a": -20}, 1000.5)
        self.assertEqual(gpio.levels[4], 0)
        self.assertEqual(gpio.levels[5], 1)

    def test_partial_initialization_cleans_only_claimed_pins(self):
        gpio = FakeGPIO(self.clock, fail=lambda name, args: name == "claim" and args[0] == 12)
        with self.assertRaises(RuntimeError):
            self.opened(gpio=gpio)
        self.assertEqual([x[1] for x in gpio.calls if x[0] == "free"], [9, 6])
        self.assertIn(("close", 104), gpio.calls)

    def test_chip_metadata_failure_never_claims_outputs(self):
        gpio = FakeGPIO(self.clock, fail=lambda name, args: name == "info")
        with self.assertRaises(RuntimeError):
            self.opened(gpio=gpio)
        self.assertFalse(any(x[0] == "claim" for x in gpio.calls))
        self.assertIn(("close", 100), gpio.calls)
        self.assertIn(("close", 104), gpio.calls)

    def test_failed_off_does_not_skip_other_channels_or_cleanup(self):
        backend, gpio = self.opened()
        gpio.fail = lambda name, args: name == "write" and args == (6, 0)
        with self.assertRaises(RuntimeError):
            backend.close()
        self.assertIn(("write", 15, 0), gpio.calls)
        self.assertIn(("free", 15), gpio.calls)
        self.assertIn(("close", 104), gpio.calls)

    def test_unconfirmed_cancel_does_not_submit_new_pwm(self):
        backend, gpio = self.opened()
        gpio.busy[6] = True
        gpio.stuck_busy = 6
        with self.assertRaises(RuntimeError):
            backend.apply({"front-a": 20}, 1000.2)
        self.assertFalse(any(x[0] == "pulse" and x[2] for x in gpio.calls))

    def test_inactive_pwm_is_not_cancelled_and_finite_completion_race_is_checked(self):
        backend, gpio = self.opened()
        self.assertFalse(any(x[0] == "pulse" for x in gpio.calls))
        backend.apply({"front-a": 20}, 1000.2)
        gpio.finish_during_cancel = True
        backend.stop()
        self.assertFalse(gpio.busy[6])
        self.assertEqual(gpio.levels[6], 0)
        self.assertIsNotNone(backend._handle)

    def test_busy_query_failure_still_attempts_cancel_and_all_low(self):
        backend, gpio = self.opened()
        backend.apply({"front-a": 20}, 1000.2)
        gpio.fail = lambda name, args: name == "busy" and args == (6,)
        with self.assertRaises(RuntimeError):
            backend.stop()
        self.assertFalse(gpio.busy[6])
        self.assertEqual(gpio.levels[15], 0)
        gpio.fail = None

    def test_lift_limits_are_fail_asserted_inputs_and_block_motion(self):
        backend, gpio = self.opened(lift_config(), "lift")
        self.assertIn(("input", 19, gpio.SET_PULL_DOWN), gpio.calls)
        gpio.levels[19] = 0
        self.assertEqual(backend.limits(), {"upper": True, "lower": False})
        with self.assertRaises(ValueError):
            backend.apply({"lift": 20}, 1000.2)
        self.assertFalse(any(x[0] == "pulse" and x[2] for x in gpio.calls))

    def test_both_limits_asserted_is_fault(self):
        backend, gpio = self.opened(lift_config(), "lift")
        gpio.levels[19] = gpio.levels[20] = 0
        with self.assertRaises(RuntimeError):
            backend.limits()
        self.assertIsNone(backend._handle)

    def test_lift_topologies_submit_finite_pwm_to_correct_pin(self):
        for driver, expected in (("dual_pwm", 16), ("enable_pwm", 18)):
            backend, gpio = self.opened(lift_config(driver), "lift")
            backend.apply({"lift": 20}, 1000.2)
            self.assertEqual([x[1] for x in gpio.calls if x[0] == "pulse" and x[2]], [expected])
            backend.close()

    @unittest.skipUnless(os.name == "posix", "POSIX flock is exercised on Linux/Pi")
    def test_process_lease_excludes_other_owner_and_keeps_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller.lock"
            first, second = b.RealBackend(wheel_config(), path), b.RealBackend(wheel_config(), path)
            first._take_lock()
            inode = path.stat().st_ino
            try:
                with self.assertRaisesRegex(RuntimeError, "Another team controller"):
                    second._take_lock()
            finally:
                first.close()
            self.assertEqual(path.stat().st_ino, inode)
            second._take_lock()
            second.close()


if __name__ == "__main__":
    unittest.main()
