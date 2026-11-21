import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from autonomy_lab.environment import EnvironmentConfig, ForkliftEnvironment
from autonomy_lab.learning import evaluate, q_update, run_experiment


class LearningTests(unittest.TestCase):
    def test_termination_and_time_limit_bootstrap_differ(self):
        q = np.zeros((2, 2)); q[1] = 4
        q_update(q, 0, 0, 1, 1, False, 1, .9)
        self.assertAlmostEqual(q[0, 0], 4.6)
        q_update(q, 0, 1, 1, 1, True, 1, .9)
        self.assertEqual(q[0, 1], 1)

    def test_swept_collision_is_blocked_without_motion(self):
        env = ForkliftEnvironment(EnvironmentConfig(observation_dropout=0))
        env.reset(4)
        env.pose = [1.55, .9, 0.0]
        env.goal = [.5, .9, 0]
        env.observation = env._observe()
        original = list(env.pose)
        _, _, terminated, _, info = env.step(1)
        self.assertTrue(terminated)
        self.assertEqual(info["blocked_reason"], "predicted_envelope_collision")
        self.assertEqual(env.pose, original)
        self.assertEqual(info["applied_action"], "STOP")
        self.assertFalse(info["physical_collision"])

    def test_invalid_observation_forces_stop_and_keeps_no_fake_range(self):
        env = ForkliftEnvironment()
        env.reset(10)
        env._valid[0] = False; env._valid[1] = False
        env.observation = env._observe()
        old = list(env.pose)
        _, _, _, _, info = env.step(1)
        frame = env.frame(info)
        self.assertEqual(env.pose, old)
        self.assertEqual(info["applied_action"], "STOP")
        self.assertTrue(all(x["distance_m"] is None for x in frame["sensors"]["ultrasonic"]))
        self.assertFalse(frame["sensors"]["camera"]["image_available"])

    def test_success_requires_position_heading_and_explicit_stop(self):
        env = ForkliftEnvironment(EnvironmentConfig(observation_dropout=0))
        env.reset(2)
        env.pose = list(env.goal)
        env.observation = env._observe()
        _, _, done, _, info = env.step(0)
        self.assertTrue(done); self.assertTrue(info["success"])

    def test_evaluation_does_not_update_q(self):
        q = np.zeros((ForkliftEnvironment.NUM_STATES, 5))
        before = q.copy()
        result, _ = evaluate(q, EnvironmentConfig(max_steps=4), [2000000, 2000001])
        np.testing.assert_array_equal(q, before)
        self.assertEqual(result["success_rate"], 0)

    def test_resume_is_equal_to_uninterrupted_training(self):
        config = EnvironmentConfig(max_steps=8)
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            run_experiment(Path(a), episodes=8, seed=19, eval_episodes=2, config=config)
            run_experiment(Path(b), episodes=3, seed=19, eval_episodes=2, config=config)
            result = run_experiment(Path(b), episodes=8, seed=19, eval_episodes=2, config=config)
            with np.load(Path(a)/"q_table.npz", allow_pickle=False) as aa, np.load(Path(b)/"q_table.npz", allow_pickle=False) as bb:
                np.testing.assert_array_equal(aa["q"], bb["q"])
            self.assertEqual((Path(a)/"training.jsonl").read_text(), (Path(b)/"training.jsonl").read_text())
            self.assertEqual(result["resumed_from_episode"], 3)
            self.assertGreater(result["q_updates"], 0)
            with self.assertRaises(ValueError):
                run_experiment(Path(b), episodes=9, seed=20, eval_episodes=2, config=config)

    def test_checksum_rejects_modified_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            run_experiment(output, episodes=1, eval_episodes=1, config=EnvironmentConfig(max_steps=2))
            saved = json.loads((output / "checkpoint.json").read_text())
            with (output / saved["q_file"]).open("ab") as handle:
                handle.write(b"corruption")
            with self.assertRaises(ValueError):
                run_experiment(output, episodes=2, eval_episodes=1, config=EnvironmentConfig(max_steps=2))

    def test_resume_discards_torn_uncommitted_log_tail(self):
        config = EnvironmentConfig(max_steps=2)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            run_experiment(output, episodes=2, eval_episodes=1, config=config)
            with (output / "training.jsonl").open("a", encoding="utf-8") as handle:
                handle.write('{"episode": 3, "return":')
            result = run_experiment(output, episodes=3, eval_episodes=1, config=config)
            rows = [json.loads(line) for line in (output / "training.jsonl").read_text().splitlines()]
            self.assertEqual([row["episode"] for row in rows], [1, 2, 3])
            self.assertEqual(result["resumed_from_episode"], 2)


if __name__ == "__main__":
    unittest.main()
