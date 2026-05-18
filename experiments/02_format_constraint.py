"""Experiment 2: does prefixing "Answer in one concise sentence." reduce drift?

Hypothesis: format variation is a major component of measured drift, so forcing
a fixed shape should reduce it.

Result on gemini-2.5-pro (n=6 surviving pairs): mean Δ = +0.0086, t = +0.59 —
hypothesis *not* confirmed. Constraint reduces drift for short factual prompts
but *increases* it for reasoning/open-ended, because the natural verbose
template ("Distance = Speed × Time = 60 × 2.5 = 150 miles") is more stable
across paraphrases than compressed one-liners ("The train travels 150 miles."
vs "A train traveling at 60 mph for 2.5 hours covers 150 miles.").
"""

from __future__ import annotations

import statistics

from _client import poll_until_done, submit_batch

NUM_VARIANTS = 8
FORMAT_PREFIX = "Answer in one concise sentence. "

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


def build_jobs() -> list[dict]:
    jobs = []
    for cat, p in PROMPTS:
        jobs.append({"category": cat, "condition": "A_raw", "prompt": p, "sent": p,
                     "num_variants": NUM_VARIANTS})
        jobs.append({"category": cat, "condition": "B_constrained", "prompt": p,
                     "sent": FORMAT_PREFIX + p, "num_variants": NUM_VARIANTS})
    # _client.submit_batch dispatches via 'prompt' — swap in the actual sent text.
    for j in jobs:
        j["prompt_label"] = j["prompt"]
        j["prompt"] = j["sent"]
    return jobs


def main() -> None:
    jobs = build_jobs()
    print(f"submitting {len(jobs)} runs", flush=True)
    submit_batch(jobs)
    for j in jobs:
        print(f"  {j['category']:13s} {j['condition']:14s} {j['run_id']}")
    poll_until_done(jobs)

    print("\n--- per run ---")
    print(f"{'category':13s} {'condition':14s} {'mean':>8s} {'max':>8s}  prompt")
    for j in jobs:
        s = j["state"]
        md = s.get("mean_drift")
        mx = s.get("max_drift")
        md_s = f"{md:.4f}" if isinstance(md, (int, float)) else "—"
        mx_s = f"{mx:.4f}" if isinstance(mx, (int, float)) else "—"
        print(f"{j['category']:13s} {j['condition']:14s} {md_s:>8s} {mx_s:>8s}  {j['prompt_label']}")

    deltas: list[float] = []
    print("\n--- paired (B - A) by prompt ---")
    for i in range(0, len(jobs), 2):
        a, b = jobs[i], jobs[i + 1]
        a_m = a["state"].get("mean_drift")
        b_m = b["state"].get("mean_drift")
        if isinstance(a_m, (int, float)) and isinstance(b_m, (int, float)):
            d = b_m - a_m
            deltas.append(d)
            print(f"  {a['category']:13s} A={a_m:.4f}  B={b_m:.4f}  Δ={d:+.4f}  {a['prompt_label']}")
        else:
            print(f"  {a['category']:13s} skipped (one side missing)  {a['prompt_label']}")

    if len(deltas) >= 2:
        m = statistics.mean(deltas)
        sd = statistics.stdev(deltas)
        se = sd / len(deltas) ** 0.5
        print(f"\nn={len(deltas)}  mean Δ={m:+.4f}  sd={sd:.4f}  t={m/se if se else float('inf'):+.2f}")


if __name__ == "__main__":
    main()
