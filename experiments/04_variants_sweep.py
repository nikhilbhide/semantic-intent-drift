"""Experiment 4: stability of the drift estimate vs num_variants.

Two prompts (one factual, one open-ended), num_variants ∈ {3, 5, 8, 12}, on
whichever model is currently active.

Observed patterns:
  - open-ended drift estimate converges fast — n=5 is already close to n=12
  - low-drift factual: mean is stable but `max_drift` grows monotonically
    with n (more chances to land a divergent variant). Track both, not just
    mean.
"""

from __future__ import annotations

from _client import active_model, poll_until_done, submit_batch

PROMPTS = [
    ("factual",    "What is the capital of France?"),
    ("open_ended", "What makes a good leader?"),
]
N_VALUES = (3, 5, 8, 12)


def main() -> None:
    model = active_model()
    print(f"active model: {model}", flush=True)
    jobs = [
        {"category": c, "prompt": p, "num_variants": n}
        for c, p in PROMPTS for n in N_VALUES
    ]
    submit_batch(jobs)
    for j in jobs:
        print(f"  {j['category']:11s} n={j['num_variants']:2d} -> {j['run_id']}")
    poll_until_done(jobs)

    by_prompt: dict[str, list[tuple[int, float | None, float | None]]] = {}
    for j in jobs:
        s = j["state"]
        md = s.get("mean_drift")
        mx = s.get("max_drift")
        md = md if isinstance(md, (int, float)) else None
        mx = mx if isinstance(mx, (int, float)) else None
        by_prompt.setdefault(j["prompt"], []).append((j["num_variants"], md, mx))

    for p, rows in by_prompt.items():
        rows.sort()
        print(f"\n  {p}")
        print(f"  {'n':>4s} {'mean':>9s} {'max':>9s}")
        for n, md, mx in rows:
            ms = f"{md:.4f}" if md is not None else "—"
            xs = f"{mx:.4f}" if mx is not None else "—"
            print(f"  {n:4d} {ms:>9s} {xs:>9s}")


if __name__ == "__main__":
    main()
