"""Phase-memory rules plus learned Q control; isolated, assumed 2D simulation.

This is a hybrid candidate, not an isolated demonstration of an RL improvement.
The frozen v2 environment/reward and original learning module are not modified.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .environment import ACTIONS, EnvironmentConfig, ForkliftEnvironment
from .learning import evaluate as evaluate_original


ALGORITHM = 'phase_memory_masked_q_learning_v1'
POLICY_LABEL = 'phase-memory rules + Q-learning; synthetic approach/alignment only'
APPROACH, SETTLE, ALIGN = 'APPROACH', 'SETTLE', 'ALIGN'


@dataclass(frozen=True)
class PhaseConfig:
    enter_alignment_m: float = .065
    leave_alignment_m: float = .12

    def validate(self, environment):
        if not (0 < self.enter_alignment_m < environment.goal_tolerance_m < self.leave_alignment_m):
            raise ValueError('Require enter distance < goal tolerance < exit distance')


class PhaseMemoryEnvironment:
    NUM_STATES = 116
    INVALID = {APPROACH: 113, SETTLE: 114, ALIGN: 115}

    def __init__(self, config=None, phase_config=None):
        self.config = config or EnvironmentConfig()
        self.phase_config = phase_config or PhaseConfig()
        self.phase_config.validate(self.config)
        self.base = ForkliftEnvironment(self.config)
        self.phase = APPROACH

    @property
    def observation(self):
        return self.base.observation

    def reset(self, seed):
        self.base.reset(seed)
        self.phase = APPROACH
        self._update_phase()
        return self.state(), self.observation

    def _update_phase(self):
        if not self.observation['valid']:
            return
        distance = self.observation['distance_m']
        if self.phase == APPROACH and distance <= self.phase_config.enter_alignment_m:
            self.phase = SETTLE
        elif self.phase in (SETTLE, ALIGN) and distance >= self.phase_config.leave_alignment_m:
            self.phase = APPROACH

    def allowed_actions(self):
        if not self.observation['valid'] or self.phase == SETTLE:
            return (0,)
        return (0, 3, 4) if self.phase == ALIGN else (0, 1, 2, 3, 4)

    def state(self):
        obs = self.observation
        if not obs['valid']:
            return self.INVALID[self.phase]
        if self.phase == SETTLE:
            return 96
        angle = obs['heading_error_rad'] if self.phase == ALIGN else obs['bearing_rad']
        angle_bin = int(math.floor((angle + math.pi / 16) / (math.tau / 16))) % 16
        if self.phase == ALIGN:
            return 97 + angle_bin
        distance_bin = sum(obs['distance_m'] >= edge for edge in self.base.DISTANCE_EDGES)
        return distance_bin * 16 + angle_bin

    def step(self, action):
        if action not in self.allowed_actions():
            raise ValueError('Requested action is excluded by the explicit phase/validity mask')
        previous_phase = self.phase
        _, reward, terminated, truncated, info = self.base.step(action)
        if previous_phase == SETTLE and self.base.observation['valid']:
            self.phase = ALIGN
        self._update_phase()
        info.update(control_phase=self.phase, phase_before=previous_phase,
                    allowed_actions=[ACTIONS[a] for a in self.allowed_actions()], policy_label=POLICY_LABEL)
        return self.state(), reward, terminated, truncated, info

    def frame(self, info=None):
        return {**self.base.frame(info), 'control_phase': self.phase, 'policy_label': POLICY_LABEL,
                'allowed_actions': [ACTIONS[a] for a in self.allowed_actions()]}

    def trace_header(self):
        return {**self.base.trace_header(), 'candidate_phase_config': asdict(self.phase_config),
                'policy_label': POLICY_LABEL,
                'candidate_assumptions': [
                    '0.065 m entry and 0.12 m exit are simulation design assumptions, not measured hardware limits.',
                    'A mandatory STOP settles idealized linear/angular velocity before alignment.',
                    'During alignment only STOP/LEFT/RIGHT are legal; no physical actuator is connected.',
                    'Rules and phase state changed together with learning; effects cannot be attributed to RL alone.']}


def masked_greedy(q, state, allowed):
    return max(allowed, key=lambda action: q[state, action])


def masked_q_update(q, state, action, reward, next_state, terminated, next_allowed, alpha=.16, gamma=.99):
    future = 0.0 if terminated else max(float(q[next_state, a]) for a in next_allowed)
    td = reward + gamma * future - q[state, action]
    q[state, action] += alpha * td
    return float(td)


def phase_rule_action(env):
    obs = env.observation
    if not obs['valid'] or env.phase == SETTLE:
        return 0
    angle = obs['heading_error_rad'] if env.phase == ALIGN else obs['bearing_rad']
    if abs(angle) > .18:
        return 3 if angle > 0 else 4
    return 0 if env.phase == ALIGN else 1


def evaluate_candidate(q, config, phase_config, seeds, policy='learned', include_trace=False):
    if not seeds:
        raise ValueError('Evaluation requires at least one seed')
    before = hashlib.sha256(q.tobytes()).hexdigest()
    rows, representative = [], None
    for seed in seeds:
        env = PhaseMemoryEnvironment(config, phase_config)
        state, _ = env.reset(seed)
        frames = [env.frame()] if include_trace else None
        total, switches, previous = 0.0, 0, None
        while True:
            action = phase_rule_action(env) if policy == 'phase_rule' else masked_greedy(q, state, env.allowed_actions())
            state, reward, terminated, truncated, info = env.step(action)
            total += reward
            if previous is not None and action != previous:
                switches += 1
            previous = action
            if frames is not None:
                frames.append(env.frame(info))
            if terminated or truncated:
                break
        row = {'seed': seed, 'return': total, 'success': info['success'], 'steps': env.base.steps,
               'blocked': env.base.blocked_count, 'reason': info['reason'],
               'distance_error_m': info['distance_error_m'], 'heading_error_rad': info['heading_error_rad'],
               'physical_collisions': int(info['physical_collision']), 'action_switches': switches,
               'final_phase': env.phase}
        rows.append(row)
        if frames is not None and (representative is None or (row['success'] and not representative['result']['success'])):
            representative = {**env.trace_header(), 'frames': frames, 'policy': policy, 'result': row,
                              'selection': 'first_success_else_first'}
    after = hashlib.sha256(q.tobytes()).hexdigest()
    if before != after:
        raise RuntimeError('Evaluation mutated Q')
    n = len(rows)
    metrics = {'policy': policy, 'episodes': n, 'successes': sum(r['success'] for r in rows),
               'success_rate': sum(r['success'] for r in rows)/n,
               'mean_return': sum(r['return'] for r in rows)/n,
               'mean_steps': sum(r['steps'] for r in rows)/n,
               'mean_blocked': sum(r['blocked'] for r in rows)/n,
               'mean_distance_error_m': sum(r['distance_error_m'] for r in rows)/n,
               'physical_collisions': sum(r['physical_collisions'] for r in rows),
               'q_sha256_before': before, 'q_sha256_after': after, 'evaluation_learning_disabled': True,
               'episodes_detail': rows}
    return metrics, representative


def _write(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def run_candidate_experiment(output, episodes=2500, seed=20261121, eval_episodes=40,
                             eval_seed_start=2000000, evaluation_label='development_validation',
                             config=None, phase_config=None):
    if type(episodes) is not int or not 1 <= episodes <= 100000:
        raise ValueError('episodes must be within 1..100000')
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError('seed must be uint32')
    if not 1 <= eval_episodes <= 1000 or not 1000000 <= eval_seed_start < 2**32-eval_episodes:
        raise ValueError('Invalid evaluation range; training seeds are below 1000000')
    if evaluation_label not in ('development_validation', 'regression', 'final_holdout'):
        raise ValueError('Unknown evaluation label')
    config, phase_config = config or EnvironmentConfig(), phase_config or PhaseConfig()
    phase_config.validate(config)
    source = Path(__file__)
    settings = {'algorithm': ALGORITHM, 'seed': seed, 'environment': asdict(config),
                'phase_config': asdict(phase_config), 'alpha': .16, 'epsilon_start': .90,
                'epsilon_end': .05, 'epsilon_decay_episodes': 3500, 'state_encoding': 'phase_memory_v1',
                'candidate_source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'environment_source_sha256': hashlib.sha256(source.with_name('environment.py').read_bytes()).hexdigest(),
                'reward_source': 'unchanged frozen v2 environment',
                'learning_source_sha256': hashlib.sha256(source.with_name('learning.py').read_bytes()).hexdigest()}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    checkpoint, log_path = output/'checkpoint.json', output/'training.jsonl'
    q = np.zeros((PhaseMemoryEnvironment.NUM_STATES, len(ACTIONS)), dtype=np.float64)
    rng = np.random.default_rng(seed)
    completed = updates = 0
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding='utf-8'))
        if saved['settings_sha256'] != _hash(settings):
            raise ValueError('Candidate checkpoint settings/source changed; use a separate experiment')
        filename = saved['q_file']
        if Path(filename).name != filename:
            raise ValueError('Invalid checkpoint path')
        qfile = output/filename
        if hashlib.sha256(qfile.read_bytes()).hexdigest() != saved['q_file_sha256']:
            raise ValueError('Candidate checkpoint checksum mismatch')
        with np.load(qfile, allow_pickle=False) as archive:
            restored = archive['q']
            if restored.shape != q.shape or not np.isfinite(restored).all():
                raise ValueError('Candidate Q table is invalid')
            q[:] = restored
        completed, updates = saved['completed_episodes'], saved['q_updates']
        rng.bit_generator.state = saved['rng_state']
        if episodes < completed:
            raise ValueError('Requested episodes precede checkpoint')
        lines = log_path.read_text(encoding='utf-8').splitlines()
        committed = lines[:completed]
        if len(committed) != completed or any(json.loads(line)['episode'] != i+1 for i,line in enumerate(committed)):
            raise ValueError('Committed training log is incomplete')
        if len(lines) != completed:
            log_path.write_text('\n'.join(committed)+'\n', encoding='utf-8')
    elif log_path.exists():
        raise ValueError('Incomplete experiment without checkpoint; use a new directory')

    def save():
        generation = hashlib.sha256(q.tobytes()).hexdigest()[:16]
        qfile = output/f'q_table_e{completed:08d}_{generation}.npz'
        if not qfile.exists():
            tmp = qfile.with_name(qfile.stem+'.tmp.npz')
            np.savez_compressed(tmp, q=q)
            tmp.replace(qfile)
        _write(checkpoint, {'schema_version': 1, 'algorithm': ALGORITHM, 'settings': settings,
                           'settings_sha256': _hash(settings), 'completed_episodes': completed,
                           'q_updates': updates, 'rng_state': rng.bit_generator.state,
                           'q_file': qfile.name, 'q_file_sha256': hashlib.sha256(qfile.read_bytes()).hexdigest()})
        tmp = output/'q_table.npz.tmp'
        tmp.write_bytes(qfile.read_bytes())
        tmp.replace(output/'q_table.npz')

    resumed = completed
    env = PhaseMemoryEnvironment(config, phase_config)
    with log_path.open('a', encoding='utf-8') as log:
        for episode in range(completed, episodes):
            episode_seed = int(rng.integers(0, 1000000))
            state, _ = env.reset(episode_seed)
            epsilon = .05 + .85*math.exp(-episode/3500)
            total = td_total = 0.0
            while True:
                allowed = env.allowed_actions()
                action = int(rng.choice(allowed)) if rng.random() < epsilon else masked_greedy(q, state, allowed)
                next_state, reward, terminated, truncated, info = env.step(action)
                td_total += abs(masked_q_update(q, state, action, reward, next_state, terminated,
                                               env.allowed_actions(), gamma=config.gamma))
                updates += 1
                state = next_state
                total += reward
                if terminated or truncated:
                    break
            completed = episode+1
            log.write(json.dumps({'episode': completed, 'seed': episode_seed, 'return': total,
                                  'success': info['success'], 'steps': env.base.steps,
                                  'blocked': env.base.blocked_count, 'epsilon': epsilon,
                                  'absolute_td_update': td_total, 'reason': info['reason'],
                                  'terminated': terminated, 'truncated': truncated})+'\n')
            if completed % 250 == 0:
                log.flush()
                save()
        log.flush()
    save()
    seeds = list(range(eval_seed_start, eval_seed_start+eval_episodes))
    learned, trace = evaluate_candidate(q, config, phase_config, seeds, include_trace=True)
    untrained, _ = evaluate_candidate(np.zeros_like(q), config, phase_config, seeds, policy='untrained')
    phase_rule, _ = evaluate_candidate(np.zeros_like(q), config, phase_config, seeds, policy='phase_rule')
    original_q = np.zeros((ForkliftEnvironment.NUM_STATES, len(ACTIONS)))
    baseline, _ = evaluate_original(original_q, config, seeds, policy='baseline')
    result = {'schema_version': 1, 'mode': 'simulation', 'algorithm': ALGORITHM, 'policy_label': POLICY_LABEL,
              'status': 'HYBRID_SIMULATION_CANDIDATE_HARDWARE_UNTESTED', 'settings': settings,
              'completed_episodes': completed, 'episodes': completed, 'resumed_from_episode': resumed,
              'q_updates': updates, 'q_nonzero_entries': int(np.count_nonzero(q)),
              'evaluation_label': evaluation_label, 'evaluation_seeds': seeds,
              'training_seed_range': [0,999999], 'eval_episodes': eval_episodes,
              'learned': learned, 'untrained': untrained, 'phase_rule': phase_rule, 'baseline': baseline,
              'hardware_accessed': False, 'physical_lift_or_sensor_learning_completed': False,
              'causal_limit': 'Rules, phase memory, action mask and RL are combined; no isolated RL superiority claim.',
              'artifacts': {'evaluation': str((output/'evaluation.json').resolve()),
                            'trace': str((output/'representative_trace.json').resolve()),
                            'checkpoint': str(checkpoint.resolve()), 'training': str(log_path.resolve())}}
    _write(output/'evaluation.json', result)
    _write(output/'representative_trace.json', trace)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--episodes', type=int, default=2500)
    parser.add_argument('--seed', type=int, default=20261121)
    parser.add_argument('--eval-episodes', type=int, default=40)
    parser.add_argument('--eval-seed-start', type=int, default=2000000)
    parser.add_argument('--evaluation-label', default='development_validation')
    args = parser.parse_args()
    result = run_candidate_experiment(args.output, args.episodes, args.seed, args.eval_episodes,
                                      args.eval_seed_start, args.evaluation_label)
    print(json.dumps({key: result[key] for key in ('algorithm','episodes','q_updates','evaluation_label')}))
    print(json.dumps({key: {m:result[key][m] for m in ('success_rate','mean_steps','mean_return','mean_blocked')}
                      for key in ('learned','phase_rule','baseline','untrained')}))


if __name__ == '__main__':
    main()
