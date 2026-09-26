"""Loopback-only offline teaching demo and authenticated private review endpoints."""

from pathlib import Path
import hmac
import json
import math
import os
from dotenv import dotenv_values
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .data import synthetic_records
from .analysis import (
    student_scores,
    weight_sensitivity,
    redteam,
    educational_report,
    causal_simulation,
)
from .metrics import metrics, composite, label_uncertainty, WEIGHTS
from .review import ReviewStore
from .evidence import EvidenceIndex
from .workbench import router as workbench_router

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="认知证据实验室", docs_url=None, redoc_url=None)
store = ReviewStore(ROOT / "runtime/research/review.sqlite3")
records = synthetic_records()
evidence_index = EvidenceIndex(ROOT)
app.include_router(workbench_router)


@app.middleware('http')
async def private_no_cache(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(('/api/evidence', '/api/review', '/api/wb')):
        response.headers['Cache-Control'] = 'no-store'
    return response


def evidence_auth(token):
    if auth(token) != 'C':
        raise HTTPException(403, '全库查阅使用 C 角色口令；A/B 请在人工复核页阅读当前题目')


@app.get('/api/evidence/search')
def search_evidence(q: str = Query('', max_length=300), term: str = Query('', pattern='^(|25f|26s)$'),
                    offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
                    x_review_token: str | None = Header(default=None)):
    evidence_auth(x_review_token)
    try:
        return evidence_index.search(q, term, offset, limit)
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, '原文索引不可用，请核验 runtime/research/records.json') from None


@app.get('/api/evidence/records/{record_id}')
def read_evidence(record_id: str, x_review_token: str | None = Header(default=None)):
    evidence_auth(x_review_token)
    try:
        return evidence_index.detail(record_id)
    except KeyError:
        raise HTTPException(404, '没有找到该记录') from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    except OSError:
        raise HTTPException(503, '索引或来源文件不可用') from None


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def auth(token):
    settings = {**dotenv_values(ROOT / ".env"), **os.environ}
    for role in ("A", "B", "C"):
        expected = settings.get("REVIEW_TOKEN_" + role)
        if expected and token and hmac.compare_digest(token, expected):
            return role
    raise HTTPException(401, "复核口令无效")


@app.get("/api/demo")
def demo():
    return clean(
        {
            "records": records,
            "scores": student_scores(records).to_dict("records"),
            "sensitivity": weight_sensitivity(student_scores(records)).to_dict(
                "records"
            ),
            "redteam": redteam().to_dict("records"),
            "simulation": causal_simulation().to_dict("records"),
            "weights": {k: v.tolist() for k, v in WEIGHTS.items()},
            "source": "synthetic_constructed_labels",
        }
    )


class MetricRequest(BaseModel):
    student: str
    weights: list[float]
    uncertainty: float = 0.15
    asymmetric: bool = False


@app.post("/api/metrics")
def compute(body: MetricRequest):
    rows = [r for r in records if r["student"] == body.student]
    if not rows or not 0 <= body.uncertainty <= 0.5:
        raise HTTPException(422, "参数无效")
    levels = [r["label"] for r in rows]
    agents = [r["agent"] for r in rows]
    sessions = [r["session"] for r in rows]
    try:
        values = metrics(levels, agents, sessions, asymmetric=body.asymmetric)
        score = composite(values, body.weights)
        import numpy as np

        probs = np.full((len(rows), 6), body.uncertainty / 5)
        for i, level in enumerate(levels):
            probs[i, level - 1] = 1 - body.uncertainty
        uncertainty = label_uncertainty(probs, agents, sessions, draws=200)
        # Recompute with selected weights and DHI; the common sampler above remains a teaching helper.
        rng = np.random.default_rng(26)
        samples = []
        for _ in range(200):
            sampled = [rng.choice(range(1, 7), p=p) for p in probs]
            samples.append(
                composite(
                    metrics(sampled, agents, sessions, asymmetric=body.asymmetric),
                    body.weights,
                )
            )
        uncertainty["scores"] = samples
        uncertainty["interval"] = np.quantile(samples, [0.025, 0.5, 0.975]).tolist()
        return {"metrics": values, "score": score, "uncertainty": uncertainty}
    except ValueError:
        raise HTTPException(422, "权重须非负且和为1") from None


@app.get("/api/report/{role}")
def report(role: str):
    if role not in ("student", "teacher", "administrator"):
        raise HTTPException(404)
    return educational_report(records, role)


@app.get("/api/review/next")
def next_review(x_review_token: str | None = Header(default=None)):
    role = auth(x_review_token)
    if role == "C":
        store.advance()
    return {"task": store.next(role), "progress": store.progress(), "role": role, "timing": store.timing(role)}


class Submission(BaseModel):
    assignment: str
    label: int | None = None
    evidence: str = ""
    reason: str
    fill_method: str = 'manual'


@app.post("/api/review/submit")
def submit(body: Submission, x_review_token: str | None = Header(default=None)):
    role = auth(x_review_token)
    try:
        store.submit(role, body.assignment, body.label, body.evidence, body.reason, body.fill_method)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"saved": True}


dist = ROOT / "demo/dist"
if dist.exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(dist / "index.html")
