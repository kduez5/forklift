"""Fail-closed manual control, independent deadline monitoring and local audit.

This module never imports a GPIO package. All outputs are electrical signed duty
percentages; physical direction is applied only from confirmed configuration.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import getpass
import json
import math
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from .config import duty_limit, validate_real


CHANNELS = ("front-a", "front-b", "rear-a", "rear-b")
OUTPUTS = CHANNELS + ("lift",)
LEASE_TTL = 3.0
INPUT_TTL = 0.3
LIFECYCLE_TTL = 1.0
MAX_BURST = 2.0
REARM_DELAY = 0.2
REFRESH_INTERVAL = 0.05
WATCHDOG_INTERVAL = 0.02
MAX_DUTY = 40.0


class Controller:
    def __init__(self, config, backend, *, mode="mock", db_path=Path("manual-events.sqlite3"),
                 clock=time.monotonic, start_watchdog=True):
        if mode not in ("mock", "real"):
            raise ValueError("mode must be mock or real")
        self.config, self.backend, self.mode = config, backend, mode
        self._clock = clock
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._thread = None
        self._closed = False
        self.state, self.reason = "DISARMED", "startup: explicit ARM and new input required"
        self._owner = None
        self._owner_deadline = 0.0
        self._seq = -1
        self._generation = 0
        self._permits = {}
        self._lifecycle_permits = {}
        self._scope = None
        self._backend_open = False
        self._cleanup_failed = False
        self._outputs = dict.fromkeys(OUTPUTS, 0.0)
        self._direction = None
        self._logical_command = None
        self._deadline = None
        self._burst_start = None
        self._last_apply = None
        self._limits = {"upper": False, "lower": False}
        self._recent = deque(maxlen=20)
        self._log_failed = False
        self._last_now = float(clock())
        if not math.isfinite(self._last_now):
            raise ValueError("clock must return finite monotonic seconds")
        self._last_stop = self._last_now - REARM_DELAY
        path = Path(db_path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = sqlite3.connect(str(path), timeout=0.1, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, monotonic REAL NOT NULL,
                wall_time TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
                mode TEXT NOT NULL, details_json TEXT NOT NULL)""")
            self._db.commit()
            self._audit("state", {"reason": self.reason, "linux_user": getpass.getuser()})
        except Exception as exc:
            if hasattr(self, "_db"):
                self._db.close()
            raise RuntimeError("audit database unavailable; controller not started") from exc
        if start_watchdog:
            self._thread = threading.Thread(target=self._watchdog, name="manual-watchdog", daemon=True)
            self._thread.start()

    def _now(self):
        try:
            now = float(self._clock())
            if not math.isfinite(now) or now < self._last_now:
                raise ValueError("nonmonotonic clock")
        except Exception as exc:
            self._fault("monotonic clock failure", audit=True)
            raise RuntimeError("monotonic clock failure") from exc
        self._last_now = now
        return now

    def _audit(self, kind, details):
        try:
            if self._log_failed:
                raise RuntimeError("previous audit failure")
            timestamp = datetime.now(timezone.utc).isoformat()
            encoded = json.dumps(details, ensure_ascii=False, allow_nan=False)
            cursor = self._db.execute(
                "INSERT INTO events(monotonic,wall_time,kind,state,mode,details_json) VALUES(?,?,?,?,?,?)",
                (self._last_now, timestamp, kind, self.state, self.mode, encoded))
            self._db.commit()
            self._recent.append({"id": cursor.lastrowid, "time": timestamp,
                                 "kind": kind, "state": self.state, "mode": self.mode,
                                 "details": details})
        except Exception as exc:
            self._log_failed = True
            self._fault("audit logging failed", audit=False)
            raise RuntimeError("audit logging failed; outputs stopped and FAULT latched") from exc

    def _cleanup(self):
        errors = []
        if self._backend_open:
            for method in (self.backend.stop, self.backend.close):
                try:
                    method()
                except Exception as exc:
                    errors.append(f"{method.__name__}: {type(exc).__name__}: {exc}")
        # The cooldown starts after cleanup returns, including slow device I/O.
        try:
            stopped_at = float(self._clock())
            if not math.isfinite(stopped_at) or stopped_at < self._last_now:
                raise ValueError("nonmonotonic clock during cleanup")
            self._last_now = stopped_at
        except Exception as exc:
            errors.append(f"clock: {exc}")
        self._backend_open = False
        self._cleanup_failed = self._cleanup_failed or bool(errors)
        self._outputs = dict.fromkeys(OUTPUTS, 0.0)
        self._scope = self._direction = self._logical_command = None
        self._deadline = self._burst_start = self._last_apply = None
        self._generation += 1
        self._permits.clear()
        self._lifecycle_permits.clear()
        self._last_stop = self._last_now
        return errors

    def _fault(self, reason, *, audit=True):
        errors = self._cleanup()
        self.state = "FAULT"
        self.reason = reason + ("; cleanup: " + "; ".join(errors) if errors else "")
        if audit and not self._log_failed:
            self._audit("state", {"reason": self.reason})

    def _disarm(self, reason, *, target="DISARMED"):
        errors = self._cleanup()
        if errors:
            self.state = "FAULT"
            self.reason = reason + "; cleanup failed: " + "; ".join(errors)
        else:
            self.state, self.reason = target, reason
        self._audit("state", {"reason": self.reason})
        if errors:
            raise RuntimeError(self.reason)

    def _read_limits(self):
        try:
            limits = self.backend.limits()
            if not isinstance(limits, dict) or any(type(limits.get(k)) is not bool for k in ("upper", "lower")):
                raise ValueError("limits must contain upper/lower booleans")
            self._limits = {k: limits[k] for k in ("upper", "lower")}
        except Exception as exc:
            self._fault("lift limit read failed")
            raise RuntimeError("lift limit read failed") from exc
        if all(self._limits.values()):
            self._fault("both lift limits are active")
            raise RuntimeError(self.reason)

    def _tick(self, now, *, refresh=True):
        if self._closed:
            return
        if self._owner and now >= self._owner_deadline:
            target = self.state if self.state in ("ESTOP", "FAULT") else "DISARMED"
            self._disarm("operator lease expired", target=target)
            self._owner = None
            self._seq = -1
        self._permits = {p: v for p, v in self._permits.items() if v[0] > now}
        self._lifecycle_permits = {p: v for p, v in self._lifecycle_permits.items() if v[0] > now}
        if self._scope == "lift" and self._backend_open:
            self._read_limits()
            blocked = ((self._logical_command == "lift_up" and self._limits["upper"]) or
                       (self._logical_command == "lift_down" and self._limits["lower"]))
            if blocked:
                self._disarm("lift travel limit reached")
                return
        if self.state != "MOVING":
            return
        if now >= self._burst_start + MAX_BURST:
            self._disarm("maximum continuous burst reached; new ARM required")
        elif now >= self._deadline:
            self._disarm("input deadline expired; new ARM required")
        elif refresh and (self._last_apply is None or now - self._last_apply >= REFRESH_INTERVAL):
            self._apply(now)

    def _apply(self, now):
        # The backend receives the ORIGINAL command deadline, never now+TTL.
        if self.state != "MOVING" or self._deadline is None or now >= self._deadline:
            return
        try:
            keys = CHANNELS if self._scope == "wheels" else ("lift",)
            self.backend.apply({key: self._outputs[key] for key in keys}, self._deadline)
            self._last_apply = now
        except Exception as exc:
            self._fault(f"output failed: {type(exc).__name__}: {exc}")
            raise RuntimeError(self.reason) from exc

    def _watchdog(self):
        while not self._shutdown.wait(WATCHDOG_INTERVAL):
            with self._lock:
                try:
                    self._tick(self._now())
                except Exception:
                    # Helpers already latch FAULT and attempt all cleanup paths.
                    if self.state != "FAULT":
                        try:
                            self._fault("watchdog failure")
                        except Exception:
                            pass

    @staticmethod
    def _text(value, name, maximum, *, optional=False):
        if optional and value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f"invalid {name}")
        return value.strip()

    def _owner_check(self, body):
        lease = body.get("lease")
        if not isinstance(lease, str) or self._owner is None or not secrets.compare_digest(lease, self._owner["lease"]):
            raise ValueError("valid operator lease required")
        return lease

    def _validation(self):
        if self.mode == "mock":
            return {"wheels": [], "lift": []}
        return {scope: list(validate_real(self.config, scope)) for scope in ("wheels", "lift")}

    def _consume_lifecycle(self, body, now):
        token = body.get("lifecycle_permit")
        issued = self._lifecycle_permits.pop(token, None) if isinstance(token, str) else None
        if issued is None:
            raise ValueError("expired, consumed or invalid lifecycle permit")
        self._recheck_lifecycle(issued, body["lease"], now)
        return issued

    def _recheck_lifecycle(self, issued, lease, now):
        # Validate again after synchronous audit I/O, before opening hardware or
        # clearing a latch. STOP/ESTOP always advance generation, even at rest.
        self._owner_check({"lease": lease})
        if (issued[0] <= now or issued[1] != self._generation or
                issued[2] != self.state or now >= self._owner_deadline):
            raise ValueError("expired or obsolete lifecycle permit; make a fresh explicit request")

    def _mix(self, body):
        command = body.get("command")
        if command not in ("forward", "reverse", "left", "right", "channel", "lift_up", "lift_down"):
            raise ValueError("invalid command")
        duty = body.get("duty", 20)
        ceiling = duty_limit(self.config)
        if isinstance(duty, bool) or not isinstance(duty, (int, float)) or not math.isfinite(duty) or not 1 <= duty <= ceiling:
            raise ValueError(f"duty must be a finite number from 1 through {ceiling:g}")
        outputs = dict.fromkeys(OUTPUTS, 0.0)
        if command.startswith("lift_"):
            if self._scope != "lift":
                raise ValueError("ARM lift scope first")
            up_sign = self.config.get("lift", {}).get("up_sign")
            if self.mode == "mock" and up_sign not in (-1, 1):
                up_sign = 1
            if type(up_sign) is not int or up_sign not in (-1, 1):
                raise ValueError("lift up_sign is unconfirmed")
            self._read_limits()
            if self._limits["upper" if command == "lift_up" else "lower"]:
                self._disarm("lift travel limit reached")
                raise ValueError("requested lift direction is at its limit")
            outputs["lift"] = duty * up_sign * (1 if command == "lift_up" else -1)
        else:
            if self._scope != "wheels":
                raise ValueError("ARM wheels scope first")
            if command == "channel":
                channel, direction = body.get("channel"), body.get("direction")
                if channel not in CHANNELS or type(direction) is not int or direction not in (-1, 1):
                    raise ValueError("channel requires a known channel and electrical direction +1 or -1")
                outputs[channel] = duty * direction
            else:
                wheels = self.config.get("wheels", {})
                mapping = {}
                for index, channel in enumerate(CHANNELS):
                    item = wheels.get(channel, {})
                    side, sign = item.get("side"), item.get("forward_sign")
                    if self.mode == "mock":
                        side = side if side in ("left", "right") else ("left" if index % 2 == 0 else "right")
                        sign = sign if type(sign) is int and sign in (-1, 1) else 1
                    if side not in ("left", "right") or type(sign) is not int or sign not in (-1, 1):
                        raise ValueError("vehicle movement requires confirmed sides and forward signs")
                    mapping[channel] = (side, sign)
                if sum(side == "left" for side, _ in mapping.values()) != 2:
                    raise ValueError("vehicle movement requires exactly two left and two right wheels")
                for channel, (side, sign) in mapping.items():
                    logical = 1 if command == "forward" else -1
                    if command in ("left", "right"):
                        logical = -1 if side == command else 1
                    outputs[channel] = duty * sign * logical
        return command, outputs

    def request(self, action: str, body: dict) -> dict:
        with self._lock:
            if self._closed:
                raise RuntimeError("controller closed")
            try:
                now = self._now()
                if not isinstance(action, str) or not isinstance(body, dict):
                    raise ValueError("action must be a string and body must be an object")
                allowed = {
                    "claim": {"label"}, "release": {"lease"}, "arm": {"lease", "scope", "ack", "lifecycle_permit"},
                    "command": {"lease", "permit", "seq", "command", "channel", "direction", "duty"},
                    "stop": {"lease"}, "estop": {"lease"}, "reset": {"lease", "lifecycle_permit"},
                    "note": {"lease", "text", "logistics_id"}}
                if action not in allowed or any(key not in allowed[action] for key in body):
                    raise ValueError("unknown action or unexpected request field")
                # Global STOP must not refresh an old output before disabling it.
                if action not in ("stop", "estop"):
                    self._tick(now, refresh=False)
                if action == "claim":
                    label = self._text(body.get("label"), "operator label", 64)
                    if self._owner is not None:
                        raise ValueError("another operator holds control")
                    self._audit("claim", {"label": label})
                    now = self._now()
                    lease = secrets.token_urlsafe(32)
                    self._owner = {"label": label, "lease": lease}
                    self._owner_deadline, self._seq = now + LEASE_TTL, -1
                    result = self._snapshot_locked(lease, now)
                    result["lease"] = lease
                    return result
                if action in ("stop", "estop"):
                    target = "ESTOP" if action == "estop" else "DISARMED"
                    if self.state == "FAULT":
                        target = "FAULT"
                    elif self.state == "ESTOP":
                        target = "ESTOP"
                    self._disarm("operator " + action, target=target)
                    now = self._now()
                    self._tick(now, refresh=False)
                    return self._snapshot_locked(body.get("lease"), now)
                lease = self._owner_check(body)
                if action == "release":
                    target = self.state if self.state in ("ESTOP", "FAULT") else "DISARMED"
                    self._disarm("operator released control", target=target)
                    self._owner, self._seq = None, -1
                    return self._snapshot_locked(None, now)
                if action == "reset":
                    if self.state not in ("ESTOP", "FAULT"):
                        raise ValueError("RESET requires latched ESTOP or FAULT")
                    if self._log_failed:
                        raise RuntimeError("audit failure requires service restart after storage recovery")
                    if self._cleanup_failed:
                        raise RuntimeError("output cleanup failure requires service restart and inspection")
                    lifecycle = self._consume_lifecycle(body, now)
                    self._audit("reset", {"label": self._owner["label"]})
                    now = self._now()
                    self._tick(now, refresh=False)
                    self._recheck_lifecycle(lifecycle, lease, now)
                    self._disarm("operator RESET; new ARM required")
                elif action == "arm":
                    if self.state != "DISARMED":
                        raise ValueError("ARM requires DISARMED state")
                    if body.get("ack") is not True:
                        raise ValueError("explicit ARM acknowledgement required")
                    if now - self._last_stop < REARM_DELAY - 1e-9:
                        raise ValueError("wait at least 200ms after stop before ARM")
                    scope = body.get("scope")
                    if scope not in ("wheels", "lift"):
                        raise ValueError("scope must be wheels or lift")
                    errors = self._validation()[scope]
                    if errors:
                        raise ValueError("real configuration rejected: " + "; ".join(errors))
                    lifecycle = self._consume_lifecycle(body, now)
                    self._audit("arm", {"scope": scope, "label": self._owner["label"]})
                    now = self._now()
                    self._tick(now, refresh=False)
                    self._recheck_lifecycle(lifecycle, lease, now)
                    self._generation += 1
                    self._permits.clear()
                    self._lifecycle_permits.clear()
                    self._backend_open = True  # Include partial-open failures in cleanup.
                    try:
                        self.backend.open(scope)
                        self._scope = scope
                        if scope == "lift":
                            self._read_limits()
                        now = self._now()
                        self._tick(now, refresh=False)
                        if self._owner is None or not self._backend_open:
                            raise RuntimeError("operator lease expired during ARM")
                    except Exception as exc:
                        self._fault(f"backend ARM failed: {type(exc).__name__}: {exc}")
                        raise RuntimeError(self.reason) from exc
                    self.state, self.reason = "ARMED", "armed at zero output; new input required"
                    self._audit("state", {"reason": self.reason, "scope": scope})
                elif action == "command":
                    if self.state not in ("ARMED", "MOVING"):
                        raise ValueError("new ARM required before movement")
                    sequence, permit = body.get("seq"), body.get("permit")
                    if type(sequence) is not int or not 0 <= sequence <= 2**53 - 1 or sequence <= self._seq:
                        raise ValueError("command sequence must strictly increase")
                    issued = self._permits.get(permit) if isinstance(permit, str) else None
                    if issued is None or issued[0] <= now or issued[1] != self._generation:
                        raise ValueError("expired, consumed or invalid command permit")
                    command, outputs = self._mix(body)
                    direction = tuple(0 if outputs[c] == 0 else (1 if outputs[c] > 0 else -1) for c in OUTPUTS)
                    if self.state == "MOVING" and direction != self._direction:
                        self._disarm("direction or channel change; new ARM required")
                        raise ValueError("stop and ARM again before changing direction or channel")
                    self._audit("command", {"command": command, "sequence": sequence,
                                             "outputs": outputs, "label": self._owner["label"]})
                    # Logging or device reads may block. A captured time is not a
                    # fresh authorization after those calls return.
                    now = self._now()
                    self._tick(now, refresh=False)
                    if self.state not in ("ARMED", "MOVING"):
                        raise ValueError("control expired while processing command; new ARM required")
                    if issued[0] <= now or issued[1] != self._generation:
                        self._disarm("command permit expired during processing; new ARM required")
                        raise ValueError("command permit expired during processing")
                    self._permits.pop(permit, None)
                    self._seq = sequence
                    if self._burst_start is None:
                        self._burst_start = now
                    self._deadline = min(issued[0], self._burst_start + MAX_BURST)
                    self._outputs, self._direction, self._logical_command = outputs, direction, command
                    self.state, self.reason = "MOVING", "manual input active"
                    if self._last_apply is None or now - self._last_apply >= REFRESH_INTERVAL:
                        self._apply(now)
                elif action == "note":
                    value = self._text(body.get("text"), "observation text", 2000)
                    logistics = self._text(body.get("logistics_id"), "logistics ID", 128, optional=True)
                    self._audit("note", {"text": value, "logistics_id": logistics,
                                         "classification": "operator_observation_only", "label": self._owner["label"]})
                now = self._now()
                self._tick(now, refresh=False)
                return self._snapshot_locked(lease, now)
            except (ValueError, TypeError) as exc:
                self._audit("denial", {"action": action if isinstance(action, str) else "invalid",
                                       "reason": str(exc)})
                raise ValueError(str(exc)) from exc

    def _snapshot_locked(self, lease, now):
        owns = (isinstance(lease, str) and self._owner is not None and
                secrets.compare_digest(lease, self._owner["lease"]))
        permit = lifecycle_permit = None
        if owns:
            self._owner_deadline = now + LEASE_TTL
            if self.state in ("ARMED", "MOVING"):
                permit = secrets.token_urlsafe(24)
                self._permits[permit] = (now + INPUT_TTL, self._generation)
                while len(self._permits) > 64:
                    self._permits.pop(next(iter(self._permits)))
            elif self.state in ("DISARMED", "ESTOP", "FAULT"):
                lifecycle_permit = secrets.token_urlsafe(24)
                self._lifecycle_permits[lifecycle_permit] = (now + LIFECYCLE_TTL, self._generation, self.state)
                while len(self._lifecycle_permits) > 64:
                    self._lifecycle_permits.pop(next(iter(self._lifecycle_permits)))
        validation = self._validation()
        wait = max(0.0, self._last_stop + REARM_DELAY - now)
        return {"state": self.state, "mode": self.mode, "reason": self.reason, "monotonic_s": now,
                "outputs": dict(self._outputs), "owner": {"label": self._owner["label"]} if self._owner else None,
                "owns_control": owns, "scope": self._scope,
                "scopes_available": {key: not errors for key, errors in validation.items()},
                "validation_errors": validation, "can_arm": bool(owns and self.state == "DISARMED" and wait <= 1e-9 and any(not errors for errors in validation.values())),
                "arm_wait_s": wait, "permit": permit, "permit_expires_in_s": INPUT_TTL if permit else 0,
                "lifecycle_permit": lifecycle_permit,
                "lifecycle_permit_expires_in_s": LIFECYCLE_TTL if lifecycle_permit else 0,
                "command_expires_in_s": max(0.0, self._deadline - now) if self._deadline is not None else 0,
                "burst_remaining_s": max(0.0, self._burst_start + MAX_BURST - now) if self._burst_start is not None else MAX_BURST,
                "limits": dict(self._limits) if self._scope == "lift" and self._backend_open else {"upper": None, "lower": None},
                "mock_demo_mapping": self.mode == "mock",
                "recent_events": list(self._recent)}

    def snapshot(self, lease=None):
        with self._lock:
            if self._closed:
                raise RuntimeError("controller closed")
            now = self._now()
            self._tick(now)
            return self._snapshot_locked(lease, now)

    def close(self):
        self._shutdown.set()
        with self._lock:
            if self._closed:
                return
            try:
                self._disarm("controller closed", target="CLOSED")
            finally:
                self._closed = True
                self._owner = None
                self._db.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
