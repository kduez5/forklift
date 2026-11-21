"""Mock output and opt-in finite-pulse lgpio backend.

No hardware imports occur until RealBackend.open passes offline validation,
host/cwd checks and the shared process lease. Duties are electrical commands,
not measured speed. The caller maps logical vehicle/lift polarity.
"""
from __future__ import annotations

import copy
import importlib
import math
import os
from pathlib import Path
import platform
import re
import stat
import threading
import time

from .config import CHANNELS, duty_limit, validate_real

PERIOD_US = 5000  # 200 Hz, integer pulse quantization rounds duty down.
MAX_PULSE_SECONDS = 0.12
REVERSAL_SECONDS = 0.2


def _outputs(outputs, scope, maximum):
    allowed = set(CHANNELS) if scope == "wheels" else {"lift"}
    if not isinstance(outputs, dict) or not set(outputs) <= allowed:
        raise ValueError("Output keys must belong to the opened scope")
    result = {}
    for name, value in outputs.items():
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > maximum:
            raise ValueError(f"{name}: duty must be finite within +/-{maximum:g}%")
        result[name] = float(value)
    return result


def _deadline(deadline):
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= time.monotonic():
        raise ValueError("Command deadline expired or invalid")


class MockBackend:
    """Deterministic no-device backend; log entries are explicitly mock."""
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.maximum = duty_limit(config)
        self.scope = None
        self.commands = []
        self.outputs = {}
        self.limit_states = {"upper": False, "lower": False}

    def open(self, scope):
        if scope not in ("wheels", "lift") or self.scope is not None:
            raise ValueError("Invalid scope or backend already open")
        self.scope = scope
        self.commands.append({"event": "open", "scope": scope, "mock": True})

    def apply(self, outputs, deadline):
        try:
            if self.scope is None:
                raise RuntimeError("Backend is not open")
            values = _outputs(outputs, self.scope, self.maximum)
            _deadline(deadline)
            self.outputs = values
            self.commands.append({"event": "apply", "outputs": dict(values), "deadline": deadline, "mock": True})
        except BaseException:
            self.stop()
            raise

    def stop(self):
        self.outputs = {}
        self.commands.append({"event": "stop", "mock": True})

    def close(self):
        self.stop()
        self.scope = None

    def limits(self):
        return dict(self.limit_states)


class RealBackend:
    def __init__(self, config, lock_path=None):
        self.config = copy.deepcopy(config)
        self.lock_path = Path(lock_path or Path(__file__).parent / "runtime/controller.lock").absolute()
        self.scope = None
        self._gpio = None
        self._handle = None
        self._lock_fd = None
        self._fcntl = None
        self._claimed = []
        self._output_pins = []
        self._pwm_pins = []
        self._active = {}
        self._last_sign = {}
        self._stopped_at = {}
        self._mutex = threading.RLock()

    @staticmethod
    def _check_host():
        if platform.system() != "Linux" or platform.machine() not in ("aarch64", "arm64"):
            raise RuntimeError("Real output requires Linux ARM64 Raspberry Pi 5")
        model = Path("/proc/device-tree/model").read_bytes().rstrip(b"\0\n")
        if not model.startswith(b"Raspberry Pi 5 "):
            raise RuntimeError("Host is not identified as Raspberry Pi 5")
        cwd = Path.cwd().stat()
        if cwd.st_uid != os.getuid() or stat.S_IMODE(cwd.st_mode) & 0o077:
            raise RuntimeError("Real startup requires a private user-owned mode700 working directory for lgpio")

    def _take_lock(self):
        # Do not unlink this file: all team users must lock the same inode.
        import fcntl
        parent = self.lock_path.parent
        parent_info = parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_mode & 0o002:
            raise RuntimeError("Shared lock directory must exist and must not be world-writable")
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        created = False
        try:
            fd = os.open(self.lock_path, flags | os.O_CREAT | os.O_EXCL, 0o660)
            created = True
        except FileExistsError:
            fd = os.open(self.lock_path, flags)
        try:
            if created:
                os.fchmod(fd, 0o660)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o007:
                raise RuntimeError("Controller lock must be a non-public regular file with one link")
            if info.st_gid != parent_info.st_gid:
                raise RuntimeError("Controller lock must inherit the shared runtime directory group (setgid required)")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another team controller already owns the output lease") from exc
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd, self._fcntl = fd, fcntl

    def _check_lock(self):
        opened, current = os.fstat(self._lock_fd), self.lock_path.lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) or opened.st_nlink != 1:
            raise RuntimeError("Controller lock file was replaced or removed")

    @staticmethod
    def _load_gpio():
        return importlib.import_module("lgpio")

    @staticmethod
    def _checked(result, operation):
        if type(result) is not int or result < 0:
            raise RuntimeError(f"{operation} failed: {result!r}")
        return result

    def _open_chip(self):
        failures = []
        for device in sorted(Path("/dev").glob("gpiochip[0-9]*")):
            match = re.fullmatch(r"gpiochip(\d+)", device.name)
            if not match:
                continue
            handle = None
            try:
                handle = self._checked(self._gpio.gpiochip_open(int(match[1])), "gpiochip_open")
                status, lines, _name, label = self._gpio.gpio_get_chip_info(handle)
                self._checked(status, "gpio_get_chip_info")
                if isinstance(label, bytes):
                    label = label.decode()
                if label == "pinctrl-rp1" and lines >= 28:
                    return handle
            except Exception as exc:
                failures.append(str(exc))
            if handle is not None:
                try:
                    self._checked(self._gpio.gpiochip_close(handle), "gpiochip_close")
                except BaseException:
                    self._handle = handle  # Let open's abort path retry cleanup.
                    raise
        raise RuntimeError("No accessible Pi5 pinctrl-rp1 chip: " + "; ".join(failures))

    def open(self, scope):
        with self._mutex:
            if self.scope is not None:
                raise RuntimeError("Backend already open")
            errors = validate_real(self.config, scope)
            if errors:
                raise ValueError("; ".join(errors))
            self._check_host()
            try:
                self._take_lock()
                self._gpio = self._load_gpio()
                self._handle = self._open_chip()
                self.scope = scope
                if scope == "wheels":
                    wheels = self.config["wheels"]
                    enables = [wheels[name]["enable"] for name in CHANNELS]
                    directions = [wheels[name][key] for name in CHANNELS for key in ("in1", "in2")]
                    self._pwm_pins = list(enables)
                    ordered = enables + directions  # ALL EN low before any direction claim.
                else:
                    lift = self.config["lift"]
                    if lift["driver"] == "enable_pwm":
                        self._pwm_pins = [lift["enable"]]
                        ordered = [lift["enable"], lift["in1"], lift["in2"]]
                    else:
                        self._pwm_pins = [lift["in1"], lift["in2"]]
                        ordered = list(self._pwm_pins)
                for pin in ordered:
                    self._checked(self._gpio.gpio_claim_output(self._handle, pin, 0), f"claim GPIO{pin}")
                    self._claimed.append(pin)
                    self._output_pins.append(pin)
                if scope == "lift":
                    for name in ("upper_limit", "lower_limit"):
                        limit = self.config["lift"][name]
                        # A disconnected input is pulled toward its asserted/stop state.
                        flags = self._gpio.SET_PULL_DOWN if limit["active_level"] == 0 else self._gpio.SET_PULL_UP
                        self._checked(self._gpio.gpio_claim_input(self._handle, limit["pin"], flags), "claim limit")
                        self._claimed.append(limit["pin"])
                    self._read_limits()
                self.stop()
            except BaseException as exc:
                self._abort(exc)

    def _read_limits(self):
        if self.scope != "lift":
            return {"upper": False, "lower": False}
        result = {}
        for name in ("upper", "lower"):
            config = self.config["lift"][name + "_limit"]
            level = self._gpio.gpio_read(self._handle, config["pin"])
            if type(level) is not int or level not in (0, 1):
                raise RuntimeError(f"{name} limit input is unhealthy")
            result[name] = level == config["active_level"]
        if all(result.values()):
            raise RuntimeError("Both lift limits asserted: wiring/input fault")
        return result

    def limits(self):
        with self._mutex:
            try:
                return self._read_limits()
            except BaseException as exc:
                self._abort(exc)

    def _write(self, pin, value, deadline):
        _deadline(deadline)
        self._checked(self._gpio.gpio_write(self._handle, pin, value), f"write GPIO{pin}")
        _deadline(deadline)

    def _pulse(self, pin, duty, deadline):
        _deadline(deadline)
        now = time.monotonic()
        # Reserve one natural-cycle alignment and one submission margin. A slow
        # syscall is detected after return and causes immediate best-effort OFF.
        available = min(MAX_PULSE_SECONDS, deadline - now) - 2 * PERIOD_US / 1e6
        cycles = math.floor(available * 1e6 / PERIOD_US)
        on_us = math.floor(PERIOD_US * duty / 100)
        if cycles < 1 or on_us < 1:
            raise ValueError("Insufficient finite pulse budget or sub-resolution duty")
        _deadline(deadline)
        self._checked(self._gpio.tx_pulse(self._handle, pin, on_us, PERIOD_US - on_us,
                                         pulse_offset=0, pulse_cycles=cycles), "finite PWM")
        if time.monotonic() + (cycles + 1) * PERIOD_US / 1e6 > deadline:
            raise RuntimeError("PWM submission consumed its remaining deadline budget")

    def apply(self, outputs, deadline):
        with self._mutex:
            try:
                if self.scope is None or self._handle is None:
                    raise RuntimeError("Backend is not open")
                values = _outputs(outputs, self.scope, duty_limit(self.config))
                _deadline(deadline)
                self._check_lock()
                self.stop()  # Cancel active AND queued pulses before fresh output.
                limits = self._read_limits()
                for name, value in values.items():
                    if not value:
                        continue
                    sign = 1 if value > 0 else -1
                    if self._last_sign.get(name, sign) != sign and time.monotonic() - self._stopped_at.get(name, 0) < REVERSAL_SECONDS:
                        raise ValueError("Direction reversal requires at least 200ms confirmed OFF")
                    if name == "lift":
                        mapping = self.config["lift"]
                        upward = sign == mapping["up_sign"]
                        if limits["upper" if upward else "lower"]:
                            raise ValueError("Requested lift direction is at its limit")
                    else:
                        mapping = self.config["wheels"][name]
                    self._last_sign[name] = sign
                    self._active[name] = value
                    if name == "lift" and mapping["driver"] == "dual_pwm":
                        self._pulse(mapping["in1" if sign > 0 else "in2"], abs(value), deadline)
                    else:
                        self._write(mapping["in1"], int(sign > 0), deadline)
                        self._write(mapping["in2"], int(sign < 0), deadline)
                        self._pulse(mapping["enable"], abs(value), deadline)
                _deadline(deadline)
            except BaseException as exc:
                self._abort(exc)

    def _off_errors(self):
        errors = []
        if self._handle is None:
            return errors
        for pin in self._pwm_pins:
            if pin not in self._claimed:
                continue
            errors.extend(self._cancel_pwm(pin))
        for pin in self._output_pins:
            try:
                self._checked(self._gpio.gpio_write(self._handle, pin, 0), f"LOW GPIO{pin}")
            except Exception as exc:
                errors.append(str(exc))
        for pin in self._output_pins:
            try:
                if self._gpio.gpio_read(self._handle, pin) != 0:
                    raise RuntimeError(f"GPIO{pin} LOW unconfirmed")
            except Exception as exc:
                errors.append(str(exc))
        now = time.monotonic()
        for name, value in self._active.items():
            if value:
                self._stopped_at[name] = now
        self._active = {}
        return errors

    def _cancel_pwm(self, pin):
        """Cancel a live queue, including its finite-completion race.

        lgpio rejects zero/zero on an already inactive pin. Its BAD_PWM_MICROS
        race is benign only when a subsequent busy query confirms no PWM.
        An initial query failure never suppresses the cancellation attempt.
        """
        errors = []
        try:
            busy = self._checked(self._gpio.tx_busy(self._handle, pin, self._gpio.TX_PWM), "tx_busy")
            if busy == 0:
                return errors
            if busy != 1:
                errors.append(f"GPIO{pin} unexpected PWM busy state {busy}")
        except Exception as exc:
            errors.append(str(exc))
        bad_code = getattr(self._gpio, "BAD_PWM_MICROS", None)
        race_error = None
        try:
            result = self._gpio.tx_pulse(self._handle, pin, 0, 0)
            if bad_code is not None and result == bad_code:
                race_error = f"GPIO{pin} cancellation reported BAD_PWM_MICROS"
            else:
                self._checked(result, f"cancel GPIO{pin}")
        except Exception as exc:
            value = getattr(exc, "value", None)
            expected_text = self._gpio.error_text(bad_code) if bad_code is not None and hasattr(self._gpio, "error_text") else None
            if bad_code is not None and (value == bad_code or (expected_text is not None and value == expected_text)):
                race_error = str(exc)
            else:
                errors.append(str(exc))
        try:
            if self._checked(self._gpio.tx_busy(self._handle, pin, self._gpio.TX_PWM), "tx_busy after cancel") != 0:
                errors.append(race_error or f"GPIO{pin} PWM cancellation unconfirmed")
        except Exception as exc:
            errors.append(str(exc))
            if race_error:
                errors.append(race_error)
        return errors

    def stop(self):
        with self._mutex:
            errors = self._off_errors()
            if errors:
                raise RuntimeError("Output OFF not confirmed: " + "; ".join(errors))

    def close(self):
        with self._mutex:
            errors = self._off_errors()
            if self._handle is not None:
                for pin in reversed(self._claimed):
                    try:
                        self._checked(self._gpio.gpio_free(self._handle, pin), f"free GPIO{pin}")
                    except Exception as exc:
                        errors.append(str(exc))
                try:
                    self._checked(self._gpio.gpiochip_close(self._handle), "gpiochip_close")
                except Exception as exc:
                    errors.append(str(exc))
            self._handle = None
            self.scope = None
            self._claimed, self._output_pins, self._pwm_pins = [], [], []
            self._active, self._last_sign, self._stopped_at = {}, {}, {}
            if self._lock_fd is not None:
                try:
                    os.close(self._lock_fd)
                except OSError as exc:
                    errors.append(str(exc))
                self._lock_fd = None
            if errors:
                raise RuntimeError("Output cleanup failed; physical stop required: " + "; ".join(errors))

    def _abort(self, failure):
        try:
            self.close()
        except BaseException as cleanup:
            raise RuntimeError(f"{failure}; cleanup: {cleanup}") from failure
        raise failure
