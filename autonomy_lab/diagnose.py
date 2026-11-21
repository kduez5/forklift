"""Replay failed frozen simulation episodes without training or hardware access."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import fields
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .environment import ACTIONS, EnvironmentConfig, ForkliftEnvironment


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def diagnose(run_dir):
    run_dir = Path(run_dir).resolve()
    evaluation = json.loads((run_dir / 'evaluation.json').read_text(encoding='utf-8'))
    saved = json.loads((run_dir / 'checkpoint.json').read_text(encoding='utf-8'))
    filename = saved['q_file']
    if Path(filename).name != filename:
        raise ValueError('Invalid checkpoint filename')
    q_path = run_dir / filename
    if _sha(q_path) != saved['q_file_sha256']:
        raise ValueError('Checkpoint checksum mismatch')
    source = Path(__file__).with_name('environment.py')
    if _sha(source) != saved['settings']['environment_source_sha256']:
        raise ValueError('Environment source changed after policy freeze')
    protected = [run_dir / n for n in ('evaluation.json', 'checkpoint.json', 'training.jsonl', 'q_table.npz')]
    protected.append(q_path)
    before = {p.name: _sha(p) for p in protected}
    with np.load(q_path, allow_pickle=False) as archive:
        q = archive['q'].copy()
    q_hash = hashlib.sha256(q.tobytes()).hexdigest()
    if q_hash != evaluation['learned']['q_sha256_after']:
        raise ValueError('Evaluation and checkpoint policies differ')
    valid_names = {f.name for f in fields(EnvironmentConfig)}
    config = EnvironmentConfig(**{k:v for k,v in saved['settings']['environment'].items() if k in valid_names})
    target = run_dir / 'diagnostics'
    target.mkdir(exist_ok=True)
    cases = []
    for original in evaluation['learned']['episodes_detail']:
        if original['success']:
            continue
        env = ForkliftEnvironment(config)
        state, obs = env.reset(original['seed'])
        frames, transitions, total = [env.frame()], [], 0.0
        while True:
            old_state, old_obs = state, dict(obs)
            action = int(np.argmax(q[state]))
            state, reward, terminated, truncated, info = env.step(action)
            obs = env.observation
            total += reward
            frames.append(env.frame(info))
            transitions.append({'step': env.steps, 'state': old_state, 'observation': old_obs,
                                'requested': ACTIONS[action], 'applied': info['applied_action'],
                                'q_values': q[old_state].tolist(), 'pose': list(env.pose)})
            if terminated or truncated:
                break
        if (env.steps != original['steps'] or info['reason'] != original['reason']
                or abs(total-original['return']) > 1e-9
                or abs(info['distance_error_m']-original['distance_error_m']) > 1e-12
                or abs(info['heading_error_rad']-original['heading_error_rad']) > 1e-12):
            raise RuntimeError(f"Frozen replay differs for seed {original['seed']}")
        tail = transitions[-40:]
        counts = Counter(x['requested'] for x in tail)
        linear = [x['requested'] for x in tail if x['requested'] in ('FORWARD', 'REVERSE')]
        linear_switches = sum(a != b for a,b in zip(linear, linear[1:]))
        turns = [x['requested'] for x in tail if x['requested'] in ('LEFT', 'RIGHT')]
        turn_switches = sum(a != b for a,b in zip(turns, turns[1:]))
        trailing_stops = 0
        for x in reversed(transitions):
            if x['requested'] != 'STOP':
                break
            trailing_stops += 1
        state_ranges = []
        for s, count in Counter(x['state'] for x in tail).most_common(4):
            selected = [x for x in tail if x['state']==s and x['observation']['valid']]
            if selected:
                state_ranges.append({'state': s, 'count': count, 'action': ACTIONS[int(np.argmax(q[s]))],
                                     'distance_min_m': min(x['observation']['distance_m'] for x in selected),
                                     'distance_max_m': max(x['observation']['distance_m'] for x in selected),
                                     'heading_min_rad': min(x['observation']['heading_error_rad'] for x in selected),
                                     'heading_max_rad': max(x['observation']['heading_error_rad'] for x in selected)})
        if info['reason']=='predicted_envelope_collision':
            classification, diagnosis = 'supervisor_rejected_envelope_conflict', '가상 차체·지게 외곽의 충돌 예측으로 감독기가 요청을 차단함'
        elif len(linear)>=30 and linear_switches>=8:
            classification, diagnosis = 'forward_reverse_oscillation', '목표 근처에서 전진·후진을 반복하여 방향 정렬을 완료하지 못함'
        elif trailing_stops>=20:
            classification, diagnosis = 'persistent_stop', 'STOP 요청이 지속되어 시간 제한에 도달함'
        elif len(turns)>=25 and turn_switches>=8:
            classification, diagnosis = 'left_right_oscillation', '좌우 회전 요청이 반복되어 시간 제한에 도달함'
        else:
            classification, diagnosis = 'mixed_actions_timeout', '여러 행동이 반복되어 시간 제한에 도달함'
        trace_file = f"trace_{original['seed']}.json"
        _write(target / trace_file, {**env.trace_header(), 'policy': 'learned', 'selection': 'failed_final_holdout_frozen_replay',
                                    'frames': frames, 'result': original, 'q_sha256': q_hash,
                                    'learning_performed': False, 'hardware_accessed': False})
        cases.append({'seed': original['seed'], 'reason': info['reason'], 'diagnosis': diagnosis,
                      'classification': classification, 'trace_file': trace_file,
                      'result_matches_original': True, 'steps': env.steps,
                      'final_distance_m': info['distance_error_m'],
                      'final_heading_degrees': math.degrees(info['heading_error_rad']),
                      'tail_requested_counts': dict(counts), 'tail_linear_switches': linear_switches,
                      'tail_turn_switches': turn_switches, 'trailing_stop_steps': trailing_stops,
                      'tail_state_ranges': state_ranges, 'last_40_transitions': tail,
                      'hypotheses_not_proven': [
                          '거리 0.09m 경계에서 정책의 각도 입력이 목표 방위에서 최종 방향 오차로 전환됨',
                          '근거리 상태는 목표 방위를, 원거리 상태는 최종 방향을 보존하지 않으며 속도·경계 거리도 상태에 없음',
                          '상태 이산화·부분 관측·제한된 학습으로 같은 상태에 서로 다른 최적 행동이 섞였을 가능성; 개입 비교 필요']})
    after = {p.name: _sha(p) for p in protected}
    if before != after or hashlib.sha256(q.tobytes()).hexdigest() != q_hash:
        raise RuntimeError('Frozen experiment evidence changed during diagnosis')
    summary = {'schema_version': 1, 'mode': 'post_hoc_frozen_simulation_policy_replay',
               'learning_performed': False, 'hardware_accessed': False,
               'scope': 'Synthetic planar goal approach/alignment only; no physical fault diagnosis',
               'evidence_sha256_before': before, 'evidence_sha256_after': after, 'q_sha256': q_hash,
               'failure_count': len(cases), 'classifications': dict(Counter(c['classification'] for c in cases)),
               'cases': cases}
    _write(target / 'failure_summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    args = parser.parse_args()
    result = diagnose(args.run_dir)
    print(json.dumps({'failure_count': result['failure_count'], 'classifications': result['classifications'],
                      'summary': str((args.run_dir / 'diagnostics/failure_summary.json').resolve())}, ensure_ascii=False))


if __name__ == '__main__':
    main()
