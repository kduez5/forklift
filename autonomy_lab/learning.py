"""Reproducible tabular Q-learning and fixed-seed evaluation, simulation only."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .environment import ACTIONS, ASSUMPTIONS, EnvironmentConfig, ForkliftEnvironment, baseline_action


SCHEMA_VERSION = 1
ALGORITHM = "tabular_q_learning_v2"


def _json(path: Path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def q_update(q, state, action, reward, next_state, terminated, alpha, gamma):
    """Only true termination suppresses bootstrapping; time truncation does not."""
    target = reward if terminated else reward + gamma * float(np.max(q[next_state]))
    td_error = target - float(q[state, action])
    q[state, action] += alpha * td_error
    return float(td_error)


def _greedy(q, state):
    # Fixed tie order is reproducible. All-zero untrained policy chooses STOP.
    return int(np.argmax(q[state]))


def evaluate(q, config, seeds, policy="learned", include_trace=False):
    q_hash_before = hashlib.sha256(q.tobytes()).hexdigest()
    rows, representative = [], None
    for seed in seeds:
        env = ForkliftEnvironment(config)
        state, observation = env.reset(int(seed))
        frames = [env.frame()] if include_trace else None
        total, physical_collisions = 0.0, 0
        while True:
            action = baseline_action(observation) if policy == "baseline" else _greedy(q, state)
            state, reward, terminated, truncated, info = env.step(action)
            observation = env.observation
            total += reward
            physical_collisions += int(info["physical_collision"])
            if frames is not None:
                frames.append(env.frame(info))
            if terminated or truncated:
                break
        row = {"seed": int(seed), "return": total, "success": info["success"], "steps": env.steps,
               "blocked": env.blocked_count, "reason": info["reason"],
               "distance_error_m": info["distance_error_m"], "heading_error_rad": info["heading_error_rad"],
               "physical_collisions": physical_collisions}
        rows.append(row)
        if frames is not None and (representative is None or (info["success"] and not representative["result"]["success"])):
            representative = {**env.trace_header(), "policy": policy, "frames": frames, "result": row,
                              "selection": "first_success_else_first;not_a_population_summary"}
    q_hash_after = hashlib.sha256(q.tobytes()).hexdigest()
    if q_hash_after != q_hash_before:
        raise RuntimeError("Evaluation modified the learned policy")
    count = len(rows)
    metrics = {"policy": policy, "episodes": count, "successes": sum(x["success"] for x in rows),
               "success_rate": sum(x["success"] for x in rows) / count,
               "mean_return": sum(x["return"] for x in rows) / count,
               "mean_steps": sum(x["steps"] for x in rows) / count,
               "mean_blocked": sum(x["blocked"] for x in rows) / count,
               "physical_collisions": sum(x["physical_collisions"] for x in rows),
               "mean_distance_error_m": sum(x["distance_error_m"] for x in rows) / count,
               "evaluation_learning_disabled": True, "q_sha256_before": q_hash_before,
               "q_sha256_after": q_hash_after, "episodes_detail": rows}
    return metrics, representative


def _save_checkpoint(output, q, rng, completed, updates, settings):
    # Immutable generations let the previous manifest remain resumable even
    # when a process dies between writing new Q bytes and swapping metadata.
    generation = hashlib.sha256(q.tobytes()).hexdigest()[:16]
    q_path = output / f"q_table_e{completed:08d}_{generation}.npz"
    if not q_path.exists():
        temporary = q_path.with_name(q_path.stem + ".tmp.npz")
        np.savez_compressed(temporary, q=q)
        temporary.replace(q_path)
    metadata = {"schema_version": SCHEMA_VERSION, "algorithm": ALGORITHM,
                "completed_episodes": completed, "q_updates": updates, "settings": settings,
                "settings_sha256": _fingerprint(settings), "rng_state": rng.bit_generator.state,
                "q_shape": list(q.shape), "q_file": q_path.name,
                "q_file_sha256": hashlib.sha256(q_path.read_bytes()).hexdigest()}
    _json(output / "checkpoint.json", metadata)
    alias = output / "q_table.npz.tmp"
    alias.write_bytes(q_path.read_bytes())
    alias.replace(output / "q_table.npz")


def run_experiment(output: Path, episodes: int = 6000, seed: int = 20261121,
                   eval_episodes: int = 40, config: EnvironmentConfig | None = None,
                   eval_seed_start: int = 2000000, evaluation_label: str = "development_validation") -> dict:
    """Train to a total episode count, or resume a matching checkpoint.

    Repeating with a larger ``episodes`` continues the exact RNG/Q sequence.
    Changing config/seed rejects the checkpoint; evaluation never learns.
    """
    if type(episodes) is not int or not 1 <= episodes <= 1000000:
        raise ValueError("episodes must be an integer within 1..1000000")
    if type(eval_episodes) is not int or not 1 <= eval_episodes <= 1000:
        raise ValueError("eval_episodes must be within 1..1000")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a uint32 integer")
    if type(eval_seed_start) is not int or not 1000000 <= eval_seed_start < 2**32 - eval_episodes:
        raise ValueError("Evaluation seeds must be outside the training seed range")
    if evaluation_label not in ("development_validation", "final_holdout"):
        raise ValueError("Invalid evaluation label")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    config = config or EnvironmentConfig()
    config.validate()
    settings = {"algorithm": ALGORITHM, "schema_version": SCHEMA_VERSION, "seed": seed,
                "environment": asdict(config), "actions": list(ACTIONS), "alpha": .16,
                "epsilon_start": .90, "epsilon_end": .05, "epsilon_decay_episodes": 3500,
                "learning_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "environment_source_sha256": hashlib.sha256(Path(__file__).with_name("environment.py").read_bytes()).hexdigest(),
                "state_encoding": "distance6_relevant_angle16_invalid_v2",
                "reward_version": "potential_gamma099_time008_reject8_v2",
                "rejected_action_credit": "requested_action_with_observed_guarded_transition;no_forced_STOP_credit"}
    q = np.zeros((ForkliftEnvironment.NUM_STATES, len(ACTIONS)), dtype=np.float64)
    rng = np.random.default_rng(seed)
    completed, updates = 0, 0
    checkpoint = output / "checkpoint.json"
    log_path = output / "training.jsonl"
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if saved.get("settings_sha256") != _fingerprint(settings) or saved.get("algorithm") != ALGORITHM:
            raise ValueError("Checkpoint settings/seed/version do not match this experiment")
        filename = saved.get("q_file", "")
        if not filename or Path(filename).name != filename:
            raise ValueError("Invalid checkpoint data filename")
        q_path = output / filename
        if hashlib.sha256(q_path.read_bytes()).hexdigest() != saved.get("q_file_sha256"):
            raise ValueError("Checkpoint Q data checksum mismatch")
        with np.load(q_path, allow_pickle=False) as archive:
            restored = archive["q"]
            if restored.shape != q.shape or not np.isfinite(restored).all():
                raise ValueError("Invalid Q table shape or nonfinite values")
            q[:] = restored
        completed, updates = int(saved["completed_episodes"]), int(saved["q_updates"])
        rng.bit_generator.state = saved["rng_state"]
        if episodes < completed:
            raise ValueError("Requested total episodes is less than the saved checkpoint")
        # Incomplete rows after the last atomic checkpoint are discarded, so
        # exact replay of RNG state never duplicates episode evidence.
        if not log_path.exists():
            raise ValueError("Checkpoint is missing its training log")
        rows = log_path.read_text(encoding="utf-8").splitlines()
        # A crash can leave a torn final JSON line after the last checkpoint.
        # Only committed rows require parsing; uncommitted tail is discarded.
        committed = rows[:completed]
        if len(committed) != completed:
            raise ValueError("Training log is incomplete or inconsistent with checkpoint")
        if any(json.loads(line).get("episode") != index + 1 for index, line in enumerate(committed)):
            raise ValueError("Training log episode sequence does not match checkpoint")
        if len(rows) != completed:
            log_path.write_text("\n".join(committed) + "\n", encoding="utf-8")
    elif log_path.exists() or (output / "q_table.npz").exists():
        raise ValueError("Existing incomplete experiment has no valid checkpoint; use a new directory")
    resumed_from = completed
    env = ForkliftEnvironment(config)
    with log_path.open("a", encoding="utf-8") as log:
        for episode_index in range(completed, episodes):
            # Evaluation seeds are >= 2,000,000; training seeds are < 1,000,000.
            episode_seed = int(rng.integers(0, 1000000))
            state, _ = env.reset(episode_seed)
            epsilon = .05 + .85 * math.exp(-episode_index / 3500)
            total, absolute_td = 0.0, 0.0
            while True:
                action = int(rng.integers(len(ACTIONS))) if rng.random() < epsilon else _greedy(q, state)
                next_state, reward, terminated, truncated, info = env.step(action)
                actual = info["applied_action_index"]
                # The action space consists of policy requests. The guard is
                # part of the environment transition. Credit the request with
                # its ACTUALLY observed outcome and rejection cost, never an
                # imagined unexecuted motion. A forced STOP gets no STOP-action
                # credit unless the policy itself requested STOP.
                absolute_td += abs(q_update(q, state, action, reward, next_state,
                                             terminated, .16, config.gamma))
                updates += 1
                state = next_state
                total += reward
                if terminated or truncated:
                    break
            row = {"episode": episode_index + 1, "seed": episode_seed, "return": total,
                   "success": info["success"], "steps": env.steps, "blocked": env.blocked_count,
                   "epsilon": epsilon, "absolute_td_update": absolute_td,
                   "terminated": terminated, "truncated": truncated, "reason": info["reason"]}
            log.write(json.dumps(row, allow_nan=False) + "\n")
            completed = episode_index + 1
            if completed % 250 == 0:
                log.flush()
                _save_checkpoint(output, q, rng, completed, updates, settings)
        log.flush()
    _save_checkpoint(output, q, rng, completed, updates, settings)
    evaluation_seeds = list(range(eval_seed_start, eval_seed_start + eval_episodes))
    learned, trace = evaluate(q, config, evaluation_seeds, include_trace=True)
    untrained, _ = evaluate(np.zeros_like(q), config, evaluation_seeds, policy="untrained")
    baseline, _ = evaluate(np.zeros_like(q), config, evaluation_seeds, policy="baseline")
    result = {"schema_version": SCHEMA_VERSION, "mode": "simulation", "algorithm": ALGORITHM,
              "status": "SIMULATION_EXPERIMENT_COMPLETE_HARDWARE_UNTESTED", "completed_episodes": completed,
              "episodes": completed, "resumed_from_episode": resumed_from, "q_updates": updates,
              "q_nonzero_entries": int(np.count_nonzero(q)), "seed": seed, "eval_episodes": eval_episodes,
              "evaluation_seeds": evaluation_seeds, "training_seed_range": [0, 999999],
              "evaluation_label": evaluation_label,
              "settings": settings, "learned": learned, "untrained": untrained, "baseline": baseline,
              "improvement_over_untrained_success_rate": learned["success_rate"] - untrained["success_rate"],
              "improvement_over_baseline_success_rate": learned["success_rate"] - baseline["success_rate"],
              "source_assumptions": ASSUMPTIONS, "hardware_accessed": False,
              "physical_lift_or_sensor_learning_completed": False,
              "artifacts": {"training": str(log_path.resolve()), "checkpoint": str(checkpoint.resolve()),
                            "q_table": str((output / "q_table.npz").resolve()),
                            "evaluation": str((output / "evaluation.json").resolve()),
                            "trace": str((output / "representative_trace.json").resolve())}}
    _json(output / "evaluation.json", result)
    _json(output / "representative_trace.json", trace)
    return result
