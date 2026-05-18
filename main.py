"""SID (Semantic Intent Drift) Benchmark Service.

FastAPI app wrapping a 6-agent LangGraph pipeline that measures how
a Vertex Gemini model's responses drift semantically when a single
user intent is paraphrased multiple ways.

Pipeline:
    1. Anchor       - produce a baseline answer from the original prompt
    2. Paraphraser  - rewrite the prompt N semantically-equivalent ways
    3. Regenerator  - answer each paraphrase
    4. Embedder     - embed anchor + variant answers via gemini-embedding
    5. DriftScorer  - cosine distance between anchor and each variant
    6. Reporter     - persist run JSON to GCS, append row to BigQuery

Execution model:
    POST /run returns 202 + run_id immediately. The pipeline runs in a
    FastAPI BackgroundTask. State is persisted to GCS at each transition
    (pending -> running -> succeeded/failed) so GET /results/{run_id} on
    any replica can see the latest state.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import TypedDict

import numpy as np
from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from google.api_core import exceptions as gax_exc
from google.cloud import bigquery, storage
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field
from pythonjsonlogger import jsonlogger
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from vertexai import init as vertex_init
from vertexai.generative_models import GenerativeModel
from vertexai.language_models import TextEmbeddingModel

PROJECT_ID = os.environ["GCP_PROJECT_ID"]
REGION = os.environ.get("GCP_REGION", "us-central1")
GEN_MODEL_ID = os.environ.get("VERTEX_MODEL", "gemini-2.5-pro")
EMBED_MODEL_ID = os.environ.get("VERTEX_EMBED_MODEL", "gemini-embedding-001")
GCS_BUCKET = os.environ["GCS_BUCKET"]
BQ_DATASET = os.environ["BQ_DATASET"]
BQ_TABLE = os.environ.get("BQ_TABLE", "runs")


def _configure_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            rename_fields={"asctime": "time", "levelname": "severity"},
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return logging.getLogger("sid")


log = _configure_logging()

vertex_init(project=PROJECT_ID, location=REGION)
_gen_model = GenerativeModel(GEN_MODEL_ID)
_embed_model = TextEmbeddingModel.from_pretrained(EMBED_MODEL_ID)
_gcs = storage.Client(project=PROJECT_ID)
_bq = bigquery.Client(project=PROJECT_ID)

_RETRYABLE = (
    gax_exc.ResourceExhausted,
    gax_exc.ServiceUnavailable,
    gax_exc.DeadlineExceeded,
    gax_exc.InternalServerError,
    gax_exc.Aborted,
)
_retry_vertex = retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(_RETRYABLE),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)


class SIDState(TypedDict, total=False):
    run_id: str
    prompt: str
    num_variants: int
    anchor_answer: str
    paraphrases: list[str]
    variant_answers: list[str]
    anchor_embedding: list[float]
    variant_embeddings: list[list[float]]
    drift_scores: list[float]
    mean_drift: float
    max_drift: float
    artifact_uri: str


@_retry_vertex
def _gen(prompt: str) -> str:
    resp = _gen_model.generate_content(prompt)
    return resp.text.strip()


@_retry_vertex
def _embed(texts: list[str]) -> list[list[float]]:
    return [e.values for e in _embed_model.get_embeddings(texts)]


def anchor_node(state: SIDState) -> SIDState:
    return {"anchor_answer": _gen(state["prompt"])}


def paraphraser_node(state: SIDState) -> SIDState:
    n = state["num_variants"]
    instruction = (
        f"Rewrite the following question in {n} semantically equivalent ways. "
        "Return ONLY the rewrites as a JSON array of strings — no prose.\n\n"
        f"Question: {state['prompt']}"
    )
    raw = _gen(instruction)
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("["): raw.rfind("]") + 1]
    paraphrases = json.loads(raw)[:n]
    return {"paraphrases": paraphrases}


def regenerator_node(state: SIDState) -> SIDState:
    return {"variant_answers": [_gen(p) for p in state["paraphrases"]]}


def embedder_node(state: SIDState) -> SIDState:
    all_texts = [state["anchor_answer"], *state["variant_answers"]]
    embeddings = _embed(all_texts)
    return {
        "anchor_embedding": embeddings[0],
        "variant_embeddings": embeddings[1:],
    }


def drift_scorer_node(state: SIDState) -> SIDState:
    anchor = np.array(state["anchor_embedding"])
    a_norm = anchor / np.linalg.norm(anchor)
    scores: list[float] = []
    for v in state["variant_embeddings"]:
        vec = np.array(v)
        v_norm = vec / np.linalg.norm(vec)
        scores.append(float(1.0 - np.dot(a_norm, v_norm)))
    return {
        "drift_scores": scores,
        "mean_drift": float(np.mean(scores)) if scores else 0.0,
        "max_drift": float(np.max(scores)) if scores else 0.0,
    }


def reporter_node(state: SIDState) -> SIDState:
    run_id = state["run_id"]
    ts = datetime.now(timezone.utc).isoformat()
    artifact = {
        "run_id": run_id,
        "timestamp": ts,
        "model": GEN_MODEL_ID,
        "embed_model": EMBED_MODEL_ID,
        "prompt": state["prompt"],
        "num_variants": state["num_variants"],
        "anchor_answer": state["anchor_answer"],
        "paraphrases": state["paraphrases"],
        "variant_answers": state["variant_answers"],
        "drift_scores": state["drift_scores"],
        "mean_drift": state["mean_drift"],
        "max_drift": state["max_drift"],
    }
    artifact_uri = _put_state(run_id, {**artifact, "status": "succeeded"})

    row = {
        "run_id": run_id,
        "timestamp": ts,
        "model": GEN_MODEL_ID,
        "prompt": state["prompt"],
        "num_variants": state["num_variants"],
        "mean_drift": state["mean_drift"],
        "max_drift": state["max_drift"],
        "artifact_uri": artifact_uri,
    }
    errors = _bq.insert_rows_json(f"{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}", [row])
    if errors:
        raise RuntimeError(f"BigQuery insert failed: {errors}")
    return {"artifact_uri": artifact_uri}


def _build_graph():
    g = StateGraph(SIDState)
    g.add_node("anchor", anchor_node)
    g.add_node("paraphraser", paraphraser_node)
    g.add_node("regenerator", regenerator_node)
    g.add_node("embedder", embedder_node)
    g.add_node("drift_scorer", drift_scorer_node)
    g.add_node("reporter", reporter_node)
    g.set_entry_point("anchor")
    g.add_edge("anchor", "paraphraser")
    g.add_edge("paraphraser", "regenerator")
    g.add_edge("regenerator", "embedder")
    g.add_edge("embedder", "drift_scorer")
    g.add_edge("drift_scorer", "reporter")
    g.add_edge("reporter", END)
    return g.compile()


_graph = _build_graph()


def _blob(run_id: str):
    return _gcs.bucket(GCS_BUCKET).blob(f"runs/{run_id}.json")


def _put_state(run_id: str, state: dict) -> str:
    _blob(run_id).upload_from_string(
        json.dumps(state, indent=2), content_type="application/json"
    )
    return f"gs://{GCS_BUCKET}/runs/{run_id}.json"


def _get_state(run_id: str) -> dict | None:
    b = _blob(run_id)
    if not b.exists():
        return None
    return json.loads(b.download_as_text())


def _execute_pipeline(run_id: str, prompt: str, num_variants: int) -> None:
    started = time.monotonic()
    log.info("run_started", extra={"run_id": run_id, "num_variants": num_variants})
    _put_state(
        run_id,
        {
            "run_id": run_id,
            "status": "running",
            "prompt": prompt,
            "num_variants": num_variants,
            "model": GEN_MODEL_ID,
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    try:
        _graph.invoke(
            {"run_id": run_id, "prompt": prompt, "num_variants": num_variants}
        )
        log.info(
            "run_succeeded",
            extra={"run_id": run_id, "latency_s": round(time.monotonic() - started, 2)},
        )
    except Exception as exc:
        log.exception("run_failed", extra={"run_id": run_id})
        _put_state(
            run_id,
            {
                "run_id": run_id,
                "status": "failed",
                "prompt": prompt,
                "num_variants": num_variants,
                "model": GEN_MODEL_ID,
                "error": f"{type(exc).__name__}: {exc}",
                "failed_at": datetime.now(timezone.utc).isoformat(),
            },
        )


class RunRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    num_variants: int = Field(5, ge=1, le=20)


class RunAccepted(BaseModel):
    run_id: str
    status: str = "pending"
    poll_url: str


app = FastAPI(title="SID Benchmark Service", version="2.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness — always 200 as long as the process is running."""
    return {"status": "ok", "model": GEN_MODEL_ID}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    """Readiness — Vertex-independent so a Vertex blip doesn't trip rollouts."""
    return {"status": "ready"}


@app.post("/run", status_code=202, response_model=RunAccepted)
def submit_run(req: RunRequest, bg: BackgroundTasks) -> RunAccepted:
    run_id = uuid.uuid4().hex
    _put_state(
        run_id,
        {
            "run_id": run_id,
            "status": "pending",
            "prompt": req.prompt,
            "num_variants": req.num_variants,
            "model": GEN_MODEL_ID,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    bg.add_task(_execute_pipeline, run_id, req.prompt, req.num_variants)
    log.info("run_submitted", extra={"run_id": run_id})
    return RunAccepted(run_id=run_id, poll_url=f"/results/{run_id}")


@app.get("/results/{run_id}")
def get_results(run_id: str, response: Response) -> dict:
    state = _get_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id} not found")
    if state.get("status") in {"pending", "running"}:
        response.status_code = 202
    return state


@app.get("/results")
def list_results(limit: int = 20) -> dict:
    limit = max(1, min(limit, 200))
    query = f"""
        SELECT run_id, timestamp, prompt, num_variants, mean_drift, max_drift, artifact_uri
        FROM `{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}`
        ORDER BY timestamp DESC
        LIMIT {limit}
    """
    rows = [dict(r) for r in _bq.query(query).result()]
    for r in rows:
        if isinstance(r.get("timestamp"), datetime):
            r["timestamp"] = r["timestamp"].isoformat()
    return {"runs": rows}
