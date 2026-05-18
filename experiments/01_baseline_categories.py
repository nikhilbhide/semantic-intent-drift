"""Experiment 1: baseline drift profile across prompt categories.

Submits 6 prompts spanning factual / definitional / reasoning / ambiguous /
opinion / creative, with num_variants=5 each, and prints per-category drift.

Original finding: the "factual" prompt scored highest drift, driven by format
variation (`"Paris"` vs `"The capital of France is Paris."`) rather than
semantic divergence — see experiment 02 for the follow-up.
"""

from __future__ import annotations

from _client import poll_until_done, submit_batch

PROMPTS = [
    ("factual",      "What is the capital of France?"),
    ("definitional", "What is entropy in thermodynamics?"),
    ("reasoning",    "If a train travels at 60 mph for 2.5 hours, how far does it go?"),
    ("ambiguous",    "What makes a good leader?"),
    ("opinion",      "Is privacy more important than convenience?"),
    ("creative",     "Describe the color blue to someone who has never seen it."),
]


def main() -> None:
    jobs = [{"category": c, "prompt": p, "num_variants": 5} for c, p in PROMPTS]
    print(f"submitting {len(jobs)} runs", flush=True)
    submit_batch(jobs)
    for j in jobs:
        print(f"  {j['category']:13s} {j['run_id']}")
    poll_until_done(jobs)

    print(f"\n{'category':14s} {'mean':>8s} {'max':>8s}  prompt")
    for j in jobs:
        s = j["state"]
        md = s.get("mean_drift")
        mx = s.get("max_drift")
        md_s = f"{md:.4f}" if isinstance(md, (int, float)) else "—"
        mx_s = f"{mx:.4f}" if isinstance(mx, (int, float)) else "—"
        print(f"{j['category']:14s} {md_s:>8s} {mx_s:>8s}  {j['prompt']}")


if __name__ == "__main__":
    main()
