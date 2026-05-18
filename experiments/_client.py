"""Shared HTTP helpers for SID experiments.

Reads the service endpoint from $SID_ENDPOINT (no default — fail loudly so
nobody accidentally hits a stale address). Submits runs, polls until terminal,
tolerates transient errors.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ENDPOINT = os.environ.get("SID_ENDPOINT")
if not ENDPOINT:
    sys.exit("SID_ENDPOINT not set; export SID_ENDPOINT=http://<load-balancer-ip>")

POLL_INTERVAL_S = float(os.environ.get("SID_POLL_INTERVAL_S", "10"))
PER_RUN_DEADLINE_S = float(os.environ.get("SID_PER_RUN_DEADLINE_S", "900"))


def submit(prompt: str, num_variants: int) -> str:
    body = json.dumps({"prompt": prompt, "num_variants": num_variants}).encode()
    req = urllib.request.Request(
        f"{ENDPOINT}/run",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["run_id"]


def get_state(run_id: str) -> dict:
    req = urllib.request.Request(f"{ENDPOINT}/results/{run_id}")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 202:
                try:
                    return json.loads(e.read())
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return {"status": "pending"}


def active_model() -> str:
    with urllib.request.urlopen(f"{ENDPOINT}/health", timeout=15) as r:
        return json.loads(r.read())["model"]


def submit_batch(jobs: list[dict], max_workers: int = 8) -> None:
    """Each job dict must have 'prompt' and 'num_variants'. Mutates with 'run_id' and 'started_mono'."""
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(submit, j["prompt"], j["num_variants"]): j for j in jobs}
        for fut, j in futs.items():
            j["run_id"] = fut.result()
            j["started_mono"] = time.monotonic()


def poll_until_done(jobs: list[dict]) -> None:
    pending = {j["run_id"]: j for j in jobs}
    while pending:
        time.sleep(POLL_INTERVAL_S)
        now = time.monotonic()
        for rid, j in list(pending.items()):
            s = get_state(rid)
            status = s.get("status")
            if status in {"succeeded", "failed"}:
                j["state"] = s
                del pending[rid]
                print(f"  [{int(now - j['started_mono']):4d}s] {status:9s} {rid}", flush=True)
            elif now - j["started_mono"] > PER_RUN_DEADLINE_S:
                j["state"] = {"status": "timeout"}
                del pending[rid]
                print(f"  [{int(now - j['started_mono']):4d}s] TIMEOUT   {rid}", flush=True)
