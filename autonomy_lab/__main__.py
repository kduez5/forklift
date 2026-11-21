"""Explicit simulation training entry point; never connects to the robot."""
import argparse
import json
from pathlib import Path

from .learning import run_experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--episodes", type=int, default=2500, help="Total episodes, including an existing matching checkpoint")
    parser.add_argument("--seed", type=int, default=20261121)
    parser.add_argument("--eval-episodes", type=int, default=40)
    parser.add_argument("--eval-seed-start", type=int, default=2000000)
    parser.add_argument("--evaluation-label", choices=("development_validation", "final_holdout"), default="development_validation")
    args = parser.parse_args()
    result = run_experiment(args.output, args.episodes, args.seed, args.eval_episodes,
                            eval_seed_start=args.eval_seed_start, evaluation_label=args.evaluation_label)
    print(json.dumps({"mode": result["mode"], "episodes": result["episodes"],
                      "q_updates": result["q_updates"], "evaluation_label": result["evaluation_label"],
                      "success_rates": {name: result[name]["success_rate"] for name in ("learned", "untrained", "baseline")},
                      "artifacts": result["artifacts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
