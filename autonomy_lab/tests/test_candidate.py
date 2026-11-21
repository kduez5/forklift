import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from autonomy_lab.environment import EnvironmentConfig
from autonomy_lab.candidate import (APPROACH, SETTLE, ALIGN, PhaseMemoryEnvironment,
    PhaseConfig, evaluate_candidate, masked_greedy, masked_q_update, run_candidate_experiment)


class CandidateTests(unittest.TestCase):
    def env(self):
        env = PhaseMemoryEnvironment(EnvironmentConfig(observation_dropout=0))
        env.reset(49)
        env.base._noise[:] = 0
        env.base.goal = [.9, .9, 1.0]
        env.base.pose = [.84, .9, 0.0]
        env.base.observation = env.base._observe()
        return env

    def test_phase_entry_requires_stop_then_masks_translation(self):
        env = self.env()
        env.base.v = .18
        env._update_phase()
        self.assertEqual(env.phase, SETTLE)
        self.assertEqual(env.allowed_actions(), (0,))
        original = list(env.base.pose)
        env.step(0)
        self.assertEqual(env.phase, ALIGN)
        self.assertEqual(env.base.pose, original)
        self.assertEqual(env.base.v, 0)
        self.assertEqual(env.allowed_actions(), (0,3,4))
        with self.assertRaises(ValueError):
            env.step(1)
        with self.assertRaises(ValueError):
            env.step(2)

    def test_hysteresis_retains_alignment_until_exit(self):
        env = self.env()
        env.phase = ALIGN
        env.base.pose = [.80,.9,0.0]
        env.base.observation = env.base._observe()
        env._update_phase()
        self.assertEqual(env.phase, ALIGN)
        env.base.pose = [.77,.9,0.0]
        env.base.observation = env.base._observe()
        env._update_phase()
        self.assertEqual(env.phase, APPROACH)

    def test_invalid_observation_only_allows_stop_and_preserves_phase(self):
        env = self.env()
        env.phase = ALIGN
        env.base._valid[0] = False
        env.base.observation = env.base._observe()
        env._update_phase()
        self.assertEqual(env.phase, ALIGN)
        self.assertEqual(env.allowed_actions(), (0,))

    def test_masked_bootstrap_excludes_forbidden_high_value(self):
        q=np.zeros((2,5)); q[1]=[1,100,99,3,4]
        self.assertEqual(masked_greedy(q,1,(0,3,4)),4)
        masked_q_update(q,0,0,2,1,False,(0,3,4),alpha=1,gamma=.5)
        self.assertEqual(q[0,0],4)
        masked_q_update(q,0,3,2,1,True,(0,3,4),alpha=1,gamma=.5)
        self.assertEqual(q[0,3],2)

    def test_evaluation_is_immutable_and_same_seed_is_same_dynamics(self):
        q=np.zeros((PhaseMemoryEnvironment.NUM_STATES,5))
        before=hashlib.sha256(q.tobytes()).hexdigest()
        result,_=evaluate_candidate(q,EnvironmentConfig(max_steps=4),PhaseConfig(),[2100001])
        self.assertEqual(result['q_sha256_after'],before)
        a,b=PhaseMemoryEnvironment(),PhaseMemoryEnvironment()
        a.reset(77); b.reset(77)
        self.assertEqual(a.base.pose,b.base.pose)
        self.assertEqual(a.base.dynamics,b.base.dynamics)

    def test_resume_matches_uninterrupted_and_discards_partial_tail(self):
        config=EnvironmentConfig(max_steps=4)
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            run_candidate_experiment(a,episodes=6,eval_episodes=1,config=config)
            run_candidate_experiment(b,episodes=2,eval_episodes=1,config=config)
            with (Path(b)/'training.jsonl').open('a') as f:
                f.write('{"episode":')
            result=run_candidate_experiment(b,episodes=6,eval_episodes=1,config=config)
            self.assertEqual(result['resumed_from_episode'],2)
            with np.load(Path(a)/'q_table.npz') as x,np.load(Path(b)/'q_table.npz') as y:
                np.testing.assert_array_equal(x['q'],y['q'])
            self.assertEqual((Path(a)/'training.jsonl').read_bytes(),(Path(b)/'training.jsonl').read_bytes())


if __name__=='__main__':
    unittest.main()
