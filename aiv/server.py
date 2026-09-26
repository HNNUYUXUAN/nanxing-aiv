"""Loopback-only offline teaching demo and authenticated private review endpoints."""

import hmac
import math
import os
from pathlib import Path

from dotenv import dotenv_values
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt

from .analysis import (
    causal_simulation,
    educational_report,
    redteam,
    student_scores,
    weight_sensitivity,
)
from .data import synthetic_records
from .evidence import EvidenceIndex
from .final_verification import FinalVerificationStore
from .metrics import WEIGHTS, composite, metrics
from .review import ReviewStore
from .workbench import Workbench

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="认知证据实验室", docs_url=None, redoc_url=None)
store = ReviewStore(ROOT / "runtime/research/review.sqlite3")
records = synthetic_records()
evidence_index = EvidenceIndex(ROOT)
final_store = FinalVerificationStore(ROOT / 'runtime/research/final-verification.sqlite3')
workbench = Workbench(ROOT)


@app.middleware('http')
async def private_no_cache(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(('/api/evidence', '/api/review', '/api/final-review', '/api/workbench')):
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


def human_review_sealed():
    settings = {**dotenv_values(ROOT / ".env"), **os.environ}
    if settings.get('REVIEW_SEALED') == '1':
        return True
    archive = ROOT / 'runtime/research/final-verification-export.json'
    return (archive.exists() and
            Path(store.path).resolve() == (ROOT / 'runtime/research/review.sqlite3').resolve() and
            Path(final_store.path).resolve() == (ROOT / 'runtime/research/final-verification.sqlite3').resolve())


def workbench_auth(token):
    settings = {**dotenv_values(ROOT / ".env"), **os.environ}
    expected = settings.get("WORKBENCH_TOKEN") or settings.get("REVIEW_TOKEN_C")
    if not expected or not token or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "工作台口令无效")


@app.get('/api/public/overview')
def public_overview():
    try:
        state = workbench.run_state()
    except (ValueError, OSError):
        raise HTTPException(503, '冻结批次待核验') from None
    if state.get('frozen'):
        return {'source': 'frozen_aggregate', 'experiment': state['experiment'],
                'planned_real': state['planned_real'],
                'completed_records': state['snapshot'].get('real_completed'),
                'candidate_labels': state['snapshot'].get('real_candidate_labeled'),
                'frozen_at': state['snapshot'].get('frozen_at'),
                'boundary': '模型候选标签；不是人工真值或学习增量因果效应'}
    return {'source': 'synthetic', 'sample_records': len(records),
            'boundary': '合成数据仅用于说明操作流程'}


@app.get('/api/workbench/status')
def workbench_status(plan_id: str | None = Query(None, max_length=120),
                     x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    view = workbench_view(plan_id)
    return {'run': view.run_state(), 'quality': view.source_quality(),
            'imports': workbench.list_imports(), 'plans': workbench.plans()}


def workbench_view(plan_id=None):
    try:
        return workbench.select(plan_id)
    except (ValueError, OSError):
        raise HTTPException(409, '批次未登记，或所选批次文件未通过校验') from None


class ImportText(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=5 * 1024 * 1024)


@app.post('/api/workbench/import')
def workbench_import(body: ImportText, x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    try:
        return workbench.import_text(body.filename, body.content)
    except (ValueError, TypeError) as error:
        raise HTTPException(422, str(error)) from None


@app.post('/api/workbench/resume')
def workbench_resume(plan_id: str | None = Query(None, max_length=120),
                     x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    try:
        return workbench_view(plan_id).resume()
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@app.post('/api/workbench/imports/{import_id}/plan')
def workbench_create_plan(import_id: str, x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    try:
        return workbench.create_plan(import_id)
    except FileNotFoundError:
        raise HTTPException(404, '导入资料不存在') from None
    except (ValueError, FileExistsError) as error:
        raise HTTPException(409, str(error)) from None


@app.get('/api/workbench/cases')
def workbench_cases(q: str = Query('', max_length=300),
                    issue: str = Query('all', pattern='^(all|source_parse|unfinished|technical_failure|disagreement|uncertain|abstained|reviewed)$'),
                    offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
                    plan_id: str | None = Query(None, max_length=120),
                    x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    return workbench_view(plan_id).cases(q, issue, offset, limit)


@app.get('/api/workbench/cases/{turn_id}')
def workbench_case(turn_id: str, plan_id: str | None = Query(None, max_length=120),
                   x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    try:
        return workbench_view(plan_id).detail(turn_id)
    except KeyError:
        raise HTTPException(404, '没有找到该回合') from None


@app.get('/api/workbench/models')
def workbench_models(plan_id: str | None = Query(None, max_length=120),
                     x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    return workbench_view(plan_id).model_comparison()


@app.get('/api/workbench/teacher-report')
def workbench_teacher_report(plan_id: str | None = Query(None, max_length=120),
                            x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    return workbench_view(plan_id).teacher_report()


@app.get('/api/workbench/export')
def workbench_export(plan_id: str | None = Query(None, max_length=120),
                     x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    return Response(workbench_view(plan_id).export_package(), media_type='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="nanxing-workbench-audit.zip"',
                             'Cache-Control': 'no-store'})


@app.get('/api/workbench/teacher-report.html')
def workbench_teacher_html(plan_id: str | None = Query(None, max_length=120),
                          x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    return Response(workbench_view(plan_id).teacher_html(), media_type='text/html',
                    headers={'Content-Disposition': 'attachment; filename="teacher-report.html"'})


@app.get('/api/workbench/results.csv')
def workbench_results_csv(plan_id: str | None = Query(None, max_length=120),
                         x_workbench_token: str | None = Header(default=None)):
    workbench_auth(x_workbench_token)
    return Response(workbench_view(plan_id).results_csv().encode('utf-8-sig'), media_type='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="results-aggregate.csv"'})


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
        # Preserve missing components while simulating the selected weights and DHI.
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
        if any(value is None for value in samples):
            uncertainty = {"scores": [], "interval": None, "kind": "insufficient_data"}
        else:
            uncertainty = {
                "scores": samples,
                "interval": np.quantile(samples, [0.025, 0.5, 0.975]).tolist(),
                "kind": "conditional_label_simulation_not_validated_confidence_interval",
                "dependence_mixture": 0,
            }
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
    if human_review_sealed():
        return {"task": None, "progress": store.progress(), "role": role,
                "timing": store.timing(role), "sealed": True}
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
    if human_review_sealed():
        raise HTTPException(409, '人工复核已封存，仅保留只读记录')
    try:
        store.submit(role, body.assignment, body.label, body.evidence, body.reason, body.fill_method)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"saved": True}


def final_auth(token):
    if auth(token) != 'C':
        raise HTTPException(403, '最终核验请使用 C 角色口令')


@app.get('/api/final-review/state')
def final_state(x_review_token: str | None = Header(default=None)):
    final_auth(x_review_token)
    try:
        return final_store.state()
    except ValueError as error:
        raise HTTPException(409,str(error)) from None


class FinalItem(BaseModel):
    item: str = Field(max_length=100)


class FinalDecision(FinalItem):
    decision: str = ''
    label: StrictInt | None = None
    candidate_labels: list[StrictInt | None] = Field(default_factory=list,max_length=7)
    evidence: str = Field(default='',max_length=20000)
    reason_parts: dict[str,str] = Field(default_factory=dict)
    rule_statement: str = Field(default='',max_length=1200)
    departure_note: str = Field(default='',max_length=1200)
    fill_method: str = 'manual'
    confirmed: bool = False


@app.post('/api/final-review/start')
def start_final(body: FinalItem,x_review_token: str | None = Header(default=None)):
    final_auth(x_review_token)
    if human_review_sealed():
        raise HTTPException(409, '最终人工核验已封存，仅保留只读记录')
    try:
        final_store.start(body.item)
        return final_store.state()
    except ValueError as error:
        raise HTTPException(422,str(error)) from None


@app.post('/api/final-review/check')
def check_final(body: FinalDecision,x_review_token: str | None = Header(default=None)):
    final_auth(x_review_token)
    if human_review_sealed():
        raise HTTPException(409, '最终人工核验已封存，仅保留只读记录')
    try:
        return final_store.check(body.item,body.model_dump())
    except ValueError as error:
        raise HTTPException(422,str(error)) from None


@app.post('/api/final-review/submit')
def submit_final(body: FinalDecision,x_review_token: str | None = Header(default=None)):
    final_auth(x_review_token)
    if human_review_sealed():
        raise HTTPException(409, '最终人工核验已封存，仅保留只读记录')
    try:
        return final_store.submit(body.item,body.model_dump(),'C')
    except ValueError as error:
        raise HTTPException(422,str(error)) from None


@app.get('/api/final-review/export')
def export_final(x_review_token: str | None = Header(default=None)):
    final_auth(x_review_token)
    try:
        return JSONResponse(final_store.export(),headers={
            'Content-Disposition':'attachment; filename="final-verification.json"'})
    except ValueError as error:
        raise HTTPException(409,str(error)) from None


dist = ROOT / "demo/dist"
if dist.exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(dist / "index.html")
