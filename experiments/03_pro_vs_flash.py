"""Experiment 3: model comparison — gemini-2.5-pro vs gemini-2.5-flash.

Same 8-prompt set, num_variants=5, paired by prompt. Model swap happens by
`kubectl set env` on the deployment, so this script needs kubectl context
pointed at the SID cluster.

Result on a sample run: flash trended lower drift on 5/7 prompts (mean Δ
= -0.024, n=7, n.s.). The dramatic case was "What is the capital of France?"
— flash answered "The capital of France is **Paris**." in 4/5 variants, while
pro produced 4 different phrasings including a terse "Paris" (drift 0.29) and
a verbose "The seat of the French government is Paris..." (drift 0.26). Pro's
extra "drift" is rhetorical variation, not semantic instability — strong
evidence that the SID metric is partially a *templated-ness* score.
"""

from __future__ import annotations

import statistics
import subprocess
import time

from _client import active_model, poll_until_done, submit_batch

NUM_VARIANTS = 5
MODELS = ("gemini-2.5-pro", "gemini-2.5-flash")

PROMPTS = [
    ("factual",      "What is the capital of France?"),
    ("factual",      "What is the chemical symbol for gold?"),
    ("reasoning",    "If a train travels at 60 mph for 2.5 hours, how far does it go?"),
    ("reasoning",    "If 3 apples cost $1.20, how much do 7 apples cost?"),
    ("definitional", "What is entropy in thermodynamics?"),
    ("definitional", "What is photosynthesis?"),
    ("open_ended",   "What makes a good leader?"),
    ("open_ended",   "Describe the taste of saltiness."),
]


def swap_model(target: str) -> None:
    print(f"\n>>> setting VERTEX_MODEL={target} and waiting for rollout", flush=True)
    subprocess.run(
        ["kubectl", "set", "env", "deployment/sid-benchmark", f"VERTEX_MODEL={target}"],
        check=True,
    )
    subprocess.run(
        ["kubectl", "rollout", "status", "deployment/sid-benchmark", "--timeout=5m"],
        check=True,
    )
    for _ in range(20):
        if active_model() == target:
            print(f"    active model: {target}", flush=True)
            return
        time.sleep(3)
    raise RuntimeError(f"timed out waiting for active model={target}")


def main() -> None:
    all_jobs: list[dict] = []
    for model in MODELS:
        swap_model(model)
        jobs = [{"model": model, "category": c, "prompt": p, "num_variants": NUM_VARIANTS}
                for c, p in PROMPTS]
        print(f"--- submitting {len(jobs)} runs on {model} ---", flush=True)
        submit_batch(jobs)
        for j in jobs:
            print(f"  {j['category']:13s} {j['run_id']}")
        poll_until_done(jobs)
        all_jobs.extend(jobs)

    by_prompt: dict[str, dict[str, float]] = {}
    for j in all_jobs:
        md = j["state"].get("mean_drift")
        if isinstance(md, (int, float)):
            by_prompt.setdefault(j["prompt"], {})[j["model"]] = md

    print(f"\n{'prompt':70s} {'pro':>8s} {'flash':>8s}  Δ(flash-pro)")
    deltas: list[float] = []
    for p, vals in by_prompt.items():
        pro = vals.get("gemini-2.5-pro")
        flash = vals.get("gemini-2.5-flash")
        if pro is None or flash is None:
            continue
        d = flash - pro
        deltas.append(d)
        print(f"{p[:70]:70s} {pro:8.4f} {flash:8.4f}  {d:+8.4f}")
    if deltas:
        m = statistics.mean(deltas)
        sd = statistics.stdev(deltas) if len(deltas) > 1 else 0.0
        print(f"\nn={len(deltas)}  mean Δ={m:+.4f}  sd={sd:.4f}")


if __name__ == "__main__":
    main()
