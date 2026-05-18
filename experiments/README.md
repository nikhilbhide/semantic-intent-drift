# SID Experiments

Driver scripts for running structured experiments against a deployed SID
benchmark service. Each script is self-contained, prints a summary to stdout,
and relies only on the stdlib + a tiny shared client in `_client.py`.

## Setup

```bash
export SID_ENDPOINT=http://<load-balancer-ip>
# experiment 03 also requires kubectl context pointed at the SID cluster
```

Optional knobs (defaults shown):

```bash
export SID_POLL_INTERVAL_S=10
export SID_PER_RUN_DEADLINE_S=900
```

## Experiments

| # | Script | What it measures |
|---|---|---|
| 1 | `01_baseline_categories.py` | Per-category drift profile across 6 prompts × 5 variants |
| 2 | `02_format_constraint.py` | Paired (raw vs "Answer in one concise sentence." prefix), 8 prompts × 8 variants |
| 3 | `03_pro_vs_flash.py` | Same prompt set across `gemini-2.5-pro` and `gemini-2.5-flash`; swaps the active model via `kubectl set env` |
| 4 | `04_variants_sweep.py` | Stability of `mean_drift` / `max_drift` as `num_variants` ∈ {3, 5, 8, 12} |

Run any of them with:

```bash
cd experiments
python3 01_baseline_categories.py
```

## Key findings from initial runs

- **The drift metric measures response-shape variance as much as semantic
  divergence.** A factual prompt ("What is the capital of France?") drifted
  more than open-ended ones on `gemini-2.5-pro` because the model answered
  with several different phrasings — all containing "Paris".
- **Templated models look more stable.** On the same prompt, `gemini-2.5-flash`
  produced near-identical wording across paraphrases (drift 0.01) where
  `gemini-2.5-pro` varied phrasing (drift 0.12).
- **Forcing a one-sentence format doesn't reliably reduce drift** — it helps
  for short factual answers but hurts for reasoning, where the verbose
  "show your working" template is more stable than ad-hoc one-liners.
- **`mean_drift` and `max_drift` carry different signals.** For low-drift
  prompts, `mean` converges fast with N but `max` grows monotonically — more
  paraphrases mean more chances for a tail variant.
