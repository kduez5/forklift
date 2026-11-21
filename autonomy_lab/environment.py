"""Assumed planar forklift approach/alignment task, with synthetic observations.

This is deliberately not a calibrated digital twin. No GPIO, camera, serial,
network, lift, SLAM or hardware driver is imported or used here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import numpy as np


ACTIONS = ("STOP", "FORWARD", "REVERSE", "LEFT", "RIGHT")
ACTION_COMMANDS = ((0.0, 0.0), (0.22, 0.0), (-0.15, 0.0), (0.0, 1.3), (0.0, -1.3))
ASSUMPTIONS = [
    "SIMULATION ONLY: a 1.8 m square planar test area; dimensions are unmeasured assumptions.",
    "Conservative body+fork rectangle; no lift, payload, contact force, tipping or motor electrical model.",
    "Synthetic goal-relative metric pose is provided; this is not real camera depth, SLAM or calibrated sensor fusion.",
    "Synthetic ray ranges and 2D camera labels are generated from the assumed scene, not physical sensors or images.",
    "Friction/response/noise/dropout variation is assumed, not identified from the Raspberry Pi hardware.",
    "A simulation supervisor rejects sampled swept-envelope wall/obstacle conflicts; this is not a physical safety proof.",
]


def wrap_angle(value: float) -> float:
    return (value + math.pi) % (2 * math.pi) - math.pi


@dataclass(frozen=True)
class EnvironmentConfig:
    width_m: float = 1.8
    height_m: float = 1.8
    dt_s: float = 0.25
    max_steps: int = 160
    body_length_m: float = 0.26
    body_width_m: float = 0.18
    fork_extension_m: float = 0.10
    geometry_margin_m: float = 0.015
    goal_tolerance_m: float = 0.09
    heading_tolerance_rad: float = 0.25
    observation_dropout: float = 0.025
    gamma: float = 0.99
    obstacles: tuple = ()

    def validate(self):
        if self.width_m < 1.2 or self.height_m < 1.2:
            raise ValueError("Assumed world must be at least 1.2 m on each side")
        if not 1 <= self.max_steps <= 2000 or not 0 < self.dt_s <= 0.5:
            raise ValueError("Invalid horizon or simulation time step")
        if not 0 <= self.observation_dropout < 0.5 or not 0 < self.gamma <= 1:
            raise ValueError("Invalid observation/dropout or discount setting")
        for value in (self.body_length_m, self.body_width_m, self.fork_extension_m,
                      self.geometry_margin_m, self.goal_tolerance_m, self.heading_tolerance_rad):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Geometry and tolerances must be finite positive values")
        for obstacle in self.obstacles:
            if len(obstacle) != 4 or not all(math.isfinite(float(x)) for x in obstacle):
                raise ValueError("An obstacle requires finite xmin,ymin,xmax,ymax")
            if obstacle[0] >= obstacle[2] or obstacle[1] >= obstacle[3]:
                raise ValueError("Invalid obstacle rectangle")


def _polygons_overlap(first, second):
    for polygon in (first, second):
        for i, point in enumerate(polygon):
            other = polygon[(i + 1) % len(polygon)]
            axis = (-(other[1] - point[1]), other[0] - point[0])
            aa = [x * axis[0] + y * axis[1] for x, y in first]
            bb = [x * axis[0] + y * axis[1] for x, y in second]
            if max(aa) < min(bb) or max(bb) < min(aa):
                return False
    return True


class ForkliftEnvironment:
    """Small partially observed task; state is a discretized *observation*.

    Continuous pose is evaluator truth. The policy receives noisy synthetic
    distance/bearing/heading only, not the truth pose or randomized dynamics.
    """

    DISTANCE_EDGES = (0.09, 0.16, 0.28, 0.45, 0.70)
    ANGLE_BINS = 16
    NUM_STATES = (len(DISTANCE_EDGES) + 1) * ANGLE_BINS + 1
    INVALID_STATE = NUM_STATES - 1

    def __init__(self, config: EnvironmentConfig | None = None):
        self.config = config or EnvironmentConfig()
        self.config.validate()
        self.done = True

    def envelope(self, pose=None):
        x, y, theta = self.pose if pose is None else pose
        c, s = math.cos(theta), math.sin(theta)
        margin = self.config.geometry_margin_m
        rear = -self.config.body_length_m / 2 - margin
        front = self.config.body_length_m / 2 + self.config.fork_extension_m + margin
        half_width = self.config.body_width_m / 2 + margin
        return [(x + c * px - s * py, y + s * px + c * py)
                for px, py in ((rear, -half_width), (front, -half_width),
                               (front, half_width), (rear, half_width))]

    def collision(self, pose=None):
        polygon = self.envelope(pose)
        if any(x <= 0 or x >= self.config.width_m or y <= 0 or y >= self.config.height_m
               for x, y in polygon):
            return True
        for x1, y1, x2, y2 in self.config.obstacles:
            if _polygons_overlap(polygon, [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]):
                return True
        return False

    def reset(self, seed: int):
        self.seed = int(seed)
        rng = np.random.default_rng(self.seed)
        self.dynamics = {"linear_gain": float(rng.uniform(.72, 1.0)),
                         "angular_gain": float(rng.uniform(.78, 1.0)),
                         "response": float(rng.uniform(.65, .95))}
        for _ in range(2000):
            self.pose = [float(rng.uniform(.32, self.config.width_m - .32)),
                         float(rng.uniform(.32, self.config.height_m - .32)),
                         float(rng.uniform(-math.pi, math.pi))]
            goal = [float(rng.uniform(.34, self.config.width_m - .34)),
                    float(rng.uniform(.34, self.config.height_m - .34)),
                    float(rng.uniform(-math.pi, math.pi))]
            if .35 <= math.hypot(goal[0] - self.pose[0], goal[1] - self.pose[1]) <= 1.05:
                if not self.collision(self.pose) and not self.collision(goal):
                    self.goal = goal
                    break
        else:
            raise ValueError("Could not sample a collision-free start and goal")
        # Exogenous noise/dropouts are indexed by time, independent of actions.
        self._noise = rng.normal(0, [.002, .012, .012], (self.config.max_steps + 2, 3))
        self._valid = rng.random(self.config.max_steps + 2) >= self.config.observation_dropout
        self._valid[0] = True
        self.steps = 0
        self.v = self.omega = 0.0
        self.last_action = 0
        self.blocked_count = 0
        self.done = False
        self._last_valid_step = 0
        self.observation = self._observe()
        return self.state_index(self.observation), self.observation

    def truth_errors(self):
        dx, dy = self.goal[0] - self.pose[0], self.goal[1] - self.pose[1]
        return (math.hypot(dx, dy), wrap_angle(math.atan2(dy, dx) - self.pose[2]),
                wrap_angle(self.goal[2] - self.pose[2]))

    def _observe(self):
        valid = bool(self._valid[self.steps])
        if valid:
            self._last_valid_step = self.steps
        distance, bearing, heading = self.truth_errors()
        noise = self._noise[self.steps]
        return {"valid": valid, "age_s": (self.steps - self._last_valid_step) * self.config.dt_s,
                "distance_m": max(0.0, distance + float(noise[0])) if valid else None,
                "bearing_rad": wrap_angle(bearing + float(noise[1])) if valid else None,
                "heading_error_rad": wrap_angle(heading + float(noise[2])) if valid else None,
                "source": "synthetic_goal_relative_observation"}

    @classmethod
    def state_index(cls, observation):
        if not observation["valid"]:
            return cls.INVALID_STATE
        distance_bin = sum(observation["distance_m"] >= edge for edge in cls.DISTANCE_EDGES)
        # Zero is at the centre of a bin, avoiding a left/right discontinuity.
        def angle_bin(value):
            return int(math.floor((wrap_angle(value) + math.pi / cls.ANGLE_BINS) /
                                  (2 * math.pi / cls.ANGLE_BINS))) % cls.ANGLE_BINS
        # Approach needs bearing; only the goal region needs final heading.
        # Omitting the irrelevant angle prevents sparse nuisance-state tables.
        angle = (observation["heading_error_rad"] if distance_bin == 0
                 else observation["bearing_rad"])
        return distance_bin * cls.ANGLE_BINS + angle_bin(angle)

    def _potential(self):
        distance, bearing, heading = self.truth_errors()
        angle_cost = abs(heading) if distance <= self.config.goal_tolerance_m else abs(bearing) * .55
        return -3.0 * distance - .40 * angle_cost

    def step(self, requested_action: int):
        if self.done:
            raise RuntimeError("Episode ended; reset before stepping")
        if type(requested_action) is not int or not 0 <= requested_action < len(ACTIONS):
            raise ValueError("Action must be a known integer action index")
        previous_potential = self._potential()
        previous_pose = list(self.pose)
        blocked_reason = None
        applied_action = requested_action
        if not self.observation["valid"]:
            applied_action = 0
            if requested_action != 0:
                blocked_reason = "observation_unavailable"
        target_v, target_w = ACTION_COMMANDS[applied_action]
        alpha = self.dynamics["response"]
        if applied_action == 0:
            # Instant simulated STOP is an explicit idealized assumption.
            v, omega = 0.0, 0.0
        else:
            v = self.v + alpha * (target_v * self.dynamics["linear_gain"] - self.v)
            omega = self.omega + alpha * (target_w * self.dynamics["angular_gain"] - self.omega)
        dt = self.config.dt_s
        proposed = [self.pose[0] + math.cos(self.pose[2] + .5 * omega * dt) * v * dt,
                    self.pose[1] + math.sin(self.pose[2] + .5 * omega * dt) * v * dt,
                    wrap_angle(self.pose[2] + omega * dt)]
        # Fine swept-envelope sampling prevents a single endpoint check from
        # skipping narrow obstacles. It remains a discrete simulation guard.
        samples = max(1, math.ceil(abs(v * dt) / .008), math.ceil(abs(omega * dt) / .025))
        blocked_collision = False
        theta_delta = wrap_angle(proposed[2] - self.pose[2])
        for i in range(1, samples + 1):
            f = i / samples
            candidate = [self.pose[0] + f * (proposed[0] - self.pose[0]),
                         self.pose[1] + f * (proposed[1] - self.pose[1]),
                         wrap_angle(self.pose[2] + f * theta_delta)]
            if self.collision(candidate):
                blocked_collision = True
                blocked_reason = "predicted_envelope_collision"
                applied_action, v, omega = 0, 0.0, 0.0
                proposed = previous_pose
                break
        self.pose, self.v, self.omega = proposed, v, omega
        self.steps += 1
        if blocked_reason:
            self.blocked_count += 1
        distance, _, heading = self.truth_errors()
        success = (not blocked_reason and applied_action == 0 and self.observation["valid"]
                   and distance <= self.config.goal_tolerance_m
                   and abs(heading) <= self.config.heading_tolerance_rad)
        terminated = bool(success or blocked_collision)
        truncated = self.steps >= self.config.max_steps and not terminated
        self.done = terminated or truncated
        next_potential = 0.0 if terminated else self._potential()
        components = {
            "potential_progress": self.config.gamma * next_potential - previous_potential,
            "verified_success": 8.0 if success else 0.0,
            "time_cost": -.08,
            "command_variation_cost": -.004 * (applied_action != self.last_action),
            "rejected_request_cost": -8.0 if blocked_collision else (-.12 if blocked_reason else 0.0),
        }
        reward = float(sum(components.values()))
        self.last_action = applied_action
        self.observation = self._observe()
        info = {"success": bool(success), "requested_action": ACTIONS[requested_action],
                "applied_action": ACTIONS[applied_action], "requested_action_index": requested_action,
                "applied_action_index": applied_action, "blocked": blocked_reason is not None,
                "blocked_reason": blocked_reason, "reward": reward, "reward_components": components,
                "terminated": terminated, "truncated": truncated,
                "reason": "goal_verified" if success else (blocked_reason or ("time_limit" if truncated else "running")),
                "distance_error_m": distance, "heading_error_rad": abs(heading),
                "actual_velocity": {"linear_mps": v, "angular_radps": omega},
                "physical_collision": self.collision()}
        return self.state_index(self.observation), reward, terminated, truncated, info

    def _ray_distance(self, relative_angle):
        angle = self.pose[2] + relative_angle
        dx, dy = math.cos(angle), math.sin(angle)
        candidates = []
        for edge in (0.0, self.config.width_m):
            if abs(dx) > 1e-10:
                t = (edge - self.pose[0]) / dx
                y = self.pose[1] + t * dy
                if t >= 0 and 0 <= y <= self.config.height_m:
                    candidates.append(t)
        for edge in (0.0, self.config.height_m):
            if abs(dy) > 1e-10:
                t = (edge - self.pose[1]) / dy
                x = self.pose[0] + t * dx
                if t >= 0 and 0 <= x <= self.config.width_m:
                    candidates.append(t)
        for x1, y1, x2, y2 in self.config.obstacles:
            for edge in (x1, x2):
                if abs(dx) > 1e-10:
                    t = (edge - self.pose[0]) / dx
                    if t >= 0 and y1 <= self.pose[1] + t * dy <= y2:
                        candidates.append(t)
            for edge in (y1, y2):
                if abs(dy) > 1e-10:
                    t = (edge - self.pose[1]) / dy
                    if t >= 0 and x1 <= self.pose[0] + t * dx <= x2:
                        candidates.append(t)
        return min(candidates) if candidates else None

    def frame(self, info=None):
        info = info or {"requested_action": "STOP", "applied_action": "STOP", "reward": 0.0,
                        "reward_components": {}, "terminated": False, "truncated": False, "reason": "reset"}
        valid, age = self.observation["valid"], self.observation["age_s"]
        points = []
        if valid:
            for i in range(36):
                angle = i * math.tau / 36
                distance = self._ray_distance(angle)
                if distance is not None:
                    points.append([self.pose[0] + distance * math.cos(self.pose[2] + angle),
                                   self.pose[1] + distance * math.sin(self.pose[2] + angle)])
        ultrasonic = []
        for name, angle in (("front", 0), ("left", math.pi / 2), ("rear", math.pi), ("right", -math.pi / 2)):
            ultrasonic.append({"name": name, "distance_m": self._ray_distance(angle) if valid else None,
                               "angle_rad": angle, "valid": valid, "age_s": age,
                               "source": "synthetic_single_ray_not_real_ultrasound"})
        detections = []
        if valid and abs(self.observation["bearing_rad"]) < math.pi / 3:
            detections.append({"label": "goal_marker", "bearing_rad": self.observation["bearing_rad"],
                               "distance_m": self.observation["distance_m"], "synthetic": True})
        return {"step": self.steps, "time_s": self.steps * self.config.dt_s,
                "pose": dict(zip(("x", "y", "theta"), self.pose)),
                "goal": dict(zip(("x", "y", "theta"), self.goal)),
                "envelope": self.envelope(), "observation": dict(self.observation), **info,
                "sensors": {"lidar": {"valid": valid, "age_s": age, "points": points,
                                      "source": "synthetic_raycast"}, "ultrasonic": ultrasonic,
                            "camera": {"mode": "synthetic", "valid": valid, "age_s": age,
                                       "image_available": False, "detections": detections,
                                       "label": "2D virtual observation; not camera video"}}}

    def trace_header(self):
        return {"schema_version": 1, "mode": "simulation", "seed": self.seed,
                "world": {"width_m": self.config.width_m, "height_m": self.config.height_m,
                          "obstacles": [list(x) for x in self.config.obstacles]},
                "geometry": {"body_length_m": self.config.body_length_m,
                             "body_width_m": self.config.body_width_m,
                             "fork_extension_m": self.config.fork_extension_m,
                             "margin_m": self.config.geometry_margin_m},
                "environment_config": asdict(self.config), "sampled_dynamics": self.dynamics,
                "source_assumptions": ASSUMPTIONS + ["STOP is idealized as instantaneous zero velocity in this simulator."]}


def baseline_action(observation):
    """Untrained geometric controller, with no access to randomized physics/truth."""
    if not observation["valid"]:
        return 0
    if observation["distance_m"] > .075:
        bearing = observation["bearing_rad"]
        if abs(bearing) > .16:
            return 3 if bearing > 0 else 4
        return 1
    heading = observation["heading_error_rad"]
    if abs(heading) > .18:
        return 3 if heading > 0 else 4
    return 0
