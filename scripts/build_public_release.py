"""Build a clean local public-release candidate without Git history or private data.

This command never changes GitHub visibility, creates a Git repository, or uploads.
Optional real aggregates/illustrations require a separate hash-bound review list.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BLOCKED_PARTS = {'.git', '.venv', '__pycache__', 'node_modules', '.pytest_cache',
                 '.ruff_cache', '.ipynb_checkpoints', '.playwright-cli'}
BLOCKED_ROOTS = {'data', 'archive', 'runtime', 'handoff', 'output', 'build',
                 '.tools', 'deploy', 'literature', 'templates'}
BLOCKED_SUFFIXES = {'.key', '.pem', '.pfx', '.p12', '.sqlite', '.sqlite3', '.db',
                    '.pyc', '.zip', '.tar', '.gz', '.docx', '.xlsx', '.env', '.log'}
TEXT_SUFFIXES = {'.py', '.md', '.txt', '.json', '.csv', '.ipynb', '.ps1', '.sh',
                 '.ini', '.svg', '.html', '.css', '.js', '.mjs'}
BASE_FILES = (
    'requirements-lock.txt', 'requirements.txt', 'pytest.ini', 'run_all.ps1', 'run_all.sh',
    'scripts/run_all.py', 'scripts/execute_notebooks.py', 'scripts/verify_models.py',
    'scripts/package_research.py', 'scripts/attach_concept_illustrations.py',
    'scripts/verify_full_turn_materials.py', 'scripts/build_full_turn_materials.py',
    'scripts/build_solution_materials.py', 'scripts/build_research_notebooks.py',
    'scripts/connect_notebook_snapshot.py',
    'tests/test_metrics.py', 'tests/test_models.py', 'tests/test_uncertainty.py',
    'tests/test_solution_materials.py', 'tests/test_analysis.py',
    'scripts/build_public_release.py',
    'docs/指标定义与性质.md', 'docs/第三方资源来源.md',
)
AIV_FILES = tuple('aiv/' + name for name in (
    '__init__.py', 'analysis.py', 'annotation.py', 'annotation_workflow.py', 'association.py',
    'calibration.py', 'data.py', 'evidence.py', 'fast_strong_workflow.py', 'final_verification.py',
    'full_turn_reporting.py', 'full_turn_workflow.py', 'guidance.py', 'media_workflow.py',
    'metrics.py', 'models.py', 'notebook_materials.py', 'figure_style.py', 'pool.py', 'pool_config.py',
    'review.py', 'server.py', 'solution_materials.py', 'turns.py', 'workbench.py', 'workbench_imports.py'))
NOTEBOOK_FILES = tuple('notebooks/' + name for name in (
    '00_论文材料总览.ipynb', '01_数据与问题.ipynb', '02_标注与互评.ipynb',
    '03_人审与校准.ipynb', '04_识别边界与模拟.ipynb', '05_指标性质与不确定性.ipynb',
    '06_红队与失败边界.ipynb', '07_教育报告与复用.ipynb', '08_真实数据与封存记录.ipynb',
    '09_Agent批量结果与互评.ipynb'))
SYNTHETIC_FILES = (
    'examples/synthetic-judge-cache.json',
    'results/synthetic/causal-simulation.csv', 'results/synthetic/label-dependence.csv',
    'results/synthetic/provenance.json', 'results/synthetic/redteam.csv',
    'results/synthetic/redteam.json', 'results/synthetic/residual-coverage-summary.json',
    'results/synthetic/residual-coverage-trials.csv', 'results/synthetic/student-scores.csv',
    'results/synthetic/text-redteam.csv', 'results/synthetic/text-redteam.json',
    'results/synthetic/weight-sensitivity.csv',
)
CONCEPT_INITIAL_PROMPTS = {
    'process-overview': 'assets/illustrations/prompts/process-overview.txt',
    'system-architecture': 'assets/illustrations/prompts/system-architecture.txt',
    'model-review': 'assets/illustrations/prompts/model-review.txt',
    'evidence-chain': 'assets/illustrations/prompts/evidence-chain.txt',
    'reproducibility': 'assets/illustrations/prompts/reproducibility.txt',
}
CONCEPT_REVISION_PROMPTS = {
    'process-overview': 'assets/illustrations/prompts/process-overview-revision-01.txt',
    'system-architecture': 'assets/illustrations/prompts/system-architecture-revision-01.txt',
    'model-review': 'assets/illustrations/prompts/model-review-revision-01.txt',
}
CONCEPT_PROMPT_FILES = tuple(CONCEPT_INITIAL_PROMPTS.values()) + tuple(CONCEPT_REVISION_PROMPTS.values())
CONCEPT_DESCRIPTIONS = {
    'process-overview': ('研究流程', '从证据、标注和识别条件到指标与反例回验，约束结论的适用范围。'),
    'system-architecture': ('系统模块', '工作台、调度账本、冻结快照和统计交付的职责关系。'),
    'model-review': ('独立判断与复核', '基础模型、复核模型与随机、风险、复跑任务分别保留独立判断和分母。'),
    'evidence-chain': ('证据追溯', '通过来源位置与版本连接原始证据、判断、计算和建议。'),
    'reproducibility': ('离线复现', '由同一快照和统计核心约束论文、Notebook 与复算结果的一致性。'),
}
PERSONAL_FIELDS = {'student', 'student_id', 'student_name', 'name', 'email', 'phone',
                   'question', 'student_text', 'raw_text', 'record_id', 'turn_id',
                   'session_id', 'source_file', 'source_row', 'evidence'}


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def relative_path(name: str) -> str:
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('Unsafe release path')
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('Unsafe release path')
    if any(p.lower() in BLOCKED_PARTS for p in path.parts):
        raise ValueError('Excluded release path')
    if path.parts[0].lower() in BLOCKED_ROOTS:
        raise ValueError('Private or third-party source is outside the public allowlist')
    if path.name.lower().startswith('.env') or path.suffix.lower() in BLOCKED_SUFFIXES:
        raise ValueError('Excluded release file type')
    return path.as_posix()


def read_source(root: Path, name: str) -> bytes:
    name = relative_path(name)
    path = root / name
    if any(part.is_symlink() for part in (path, *path.parents) if part != root.parent):
        raise ValueError('Symlinks are not public-release inputs')
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError('Missing or escaped public-release input: ' + name)
    if path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError('Oversized public-release input: ' + name)
    return path.read_bytes()


def clean_notebook(content: bytes, *, keep_concepts=False) -> bytes:
    source = json.loads(content)
    cells = []
    for cell in source['cells']:
        kind = cell['cell_type']
        text = ''.join(cell['source']) if isinstance(cell['source'], list) else cell['source']
        if not keep_concepts and kind == 'markdown' and '<!-- concept-illustration-v1:' in text:
            continue
        if kind not in ('markdown', 'code', 'raw'):
            raise ValueError('Unknown Notebook cell type')
        # Reconstruct rather than copying outputs, attachments, arbitrary metadata.
        clean = {'cell_type': kind, 'id': f'public-cell-{len(cells):04d}',
                 'metadata': {}, 'source': cell['source']}
        if kind == 'code':
            clean.update(execution_count=None, outputs=[])
        cells.append(clean)
    return json_bytes({'cells': cells, 'metadata': {
        'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
        'language_info': {'name': 'python'},
    }, 'nbformat': 4, 'nbformat_minor': 5})


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def local_secrets(root: Path) -> list[bytes]:
    from dotenv import dotenv_values
    values = set()
    for name in ('.env', 'deploy/workbench.env'):
        path = root / name
        if path.is_file():
            for key, value in dotenv_values(path, interpolate=False).items():
                if value and len(value) >= 8 and any(k in key.upper() for k in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
                    values.add(value.encode('utf-8'))
    return list(values)


def secret_findings(content: bytes, secrets: list[bytes]) -> list[str]:
    categories = []
    if any(value in content for value in secrets):
        categories.append('matches_local_credential')
    private_marker = b'-----BEGIN ' + b'(?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
    if re.search(private_marker, content):
        categories.append('private_key_material')
    if re.search(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9_-]{30,})\b', content):
        categories.append('token_shaped_literal')
    return categories


def scan_entries(entries: dict[str, bytes], secrets: list[bytes]) -> None:
    for name, content in entries.items():
        relative_path(name)
        findings = secret_findings(content, secrets)
        if findings:
            # Report the file and category only. Never interpolate matched text.
            raise ValueError('Secret scan failed: ' + name + ' [' + ','.join(findings) + ']')



def relative(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("Unsafe relative path")
    p = PurePosixPath(name)
    if p.is_absolute() or any(part in {".", "..", ""} for part in name.split("/")):
        raise ValueError("Unsafe relative path")
    return p.as_posix()


def source(root: Path, name: str) -> Path:
    p = root / relative(name)
    if not p.resolve().is_relative_to(root.resolve()) or any(x.is_symlink() for x in [p, *p.parents] if x != root.parent):
        raise ValueError("Source symlink or path escape")
    if not p.is_file():
        raise FileNotFoundError("Required allowlisted input missing")
    return p


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def sensitive_inventory(root: Path) -> tuple[dict, dict]:
    """Inspect known local sources; return values only in memory, counts in receipts."""
    from dotenv import dotenv_values
    values = {"credential": set(), "student_identifier": set(), "student_text": set()}
    counts = {}
    for name in (".env", "deploy/workbench.env"):
        p = root / name
        if p.exists():
            values["credential"].update(v for k, v in dotenv_values(p, interpolate=False).items()
                                        if v and any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD")))
    turn_path = root / "runtime/research/turns-v1/student_turns.jsonl"
    if turn_path.exists():
        for line in turn_path.read_text("utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                values["student_identifier"].update(str(item[k]) for k in ("student_id", "turn_id", "session_candidate_id") if item.get(k))
                if len(normalize(item.get("text", ""))) >= 16:
                    values["student_text"].add(normalize(item["text"]))
        counts["turn_source_files"] = 1
    csv_count = 0
    for term in ("25f", "26s"):
        # Discovery is solely for scanning sensitive values, never an inclusion rule.
        for p in sorted((root / f"data/{term}/csv").glob("*.csv")):
            csv_count += 1
            with p.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    for key in ("学号", "student_id"):
                        if row.get(key):
                            values["student_identifier"].add(row[key].strip())
                    text = row.get("问答记录", "")
                    if len(normalize(text)) >= 16:
                        values["student_text"].add(normalize(text))
    counts.update(csv_source_files=csv_count, **{k + "_values": len(v) for k, v in values.items()})
    return values, counts


def numeric_svg_transform(value: str) -> bool:
    """Accept only finite numeric SVG transforms with the required arity."""
    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
    transform = re.compile(
        r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(\s*(" + number
        + r"(?:(?:\s*,\s*|\s+)" + number + r")*)\s*\)")
    arities = {"matrix": {6}, "translate": {1, 2}, "scale": {1, 2},
               "rotate": {1, 3}, "skewX": {1}, "skewY": {1}}
    position, found = 0, False
    for match in transform.finditer(value):
        gap = value[position:match.start()]
        if (not found and gap.strip()) or (found and not re.fullmatch(r"\s*(?:,\s*)?", gap)):
            return False
        numbers = re.findall(number, match[2])
        if len(numbers) not in arities[match[1]] or not all(math.isfinite(float(n)) for n in numbers):
            return False
        position, found = match.end(), True
    return found and not value[position:].strip()


def inspect_svg_text(data: bytes) -> str:
    """Scan SVG content while excluding validated group geometry numbers.

    Matplotlib positions glyphs with decimal transform coordinates; their
    fractional digits are not identifiers. Text, metadata, comments, namespace
    URIs and every other attribute remain part of the privacy scan. The caller
    separately scans the original bytes for credentials and private keys.
    """
    text = data.decode("utf-8-sig", errors="strict")
    root = ET.fromstring(text, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True)))
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        raise ValueError("Expected an SVG namespace document")
    pieces = re.findall(r"<[!?][\s\S]*?>", text)
    pieces.extend(uri for _, (_, uri) in ET.iterparse(io.BytesIO(data), events=("start-ns",)))
    for element in root.iter():
        pieces.append(str(element.tag))
        for key, value in element.attrib.items():
            pieces.append(key)
            if element.tag == "{http://www.w3.org/2000/svg}g" and key == "transform" and numeric_svg_transform(value):
                pieces.append("numeric SVG transform")
            else:
                pieces.append(value)
        pieces.extend(value for value in (element.text, element.tail) if value)
    return "\n".join(pieces)


def inspect_text(name: str, data: bytes) -> str:
    if name.endswith(".pdf"):
        import fitz
        with fitz.open(stream=data, filetype="pdf") as pdf:
            if pdf.embfile_count():
                raise ValueError("PDF embedded files are not allowed")
            return "\n".join(page.get_text() for page in pdf) + json.dumps(pdf.metadata, ensure_ascii=False)
    if PurePosixPath(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return json.dumps(im.info, ensure_ascii=False, default=str) + str(im.getexif())
    if PurePosixPath(name).suffix.lower() == ".svg":
        return inspect_svg_text(data)
    text = data.decode("utf-8-sig", errors="strict")
    if name.endswith((".json", ".ipynb")):
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for k, v in value.items():
                    yield k
                    yield from strings(v)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
        text = text + "\n" + "\n".join(strings(json.loads(text)))
    return text


def scan_sensitive(entries: dict[str, bytes], values: dict) -> dict:
    text = "\n".join(inspect_text(n, b) for n, b in entries.items())
    normalized = normalize(text)
    raw = b"\n".join(entries.values())
    hits = {"credential": 0, "student_identifier": 0, "student_text": 0, "private_key": 0}
    for value in values.get("credential", ()):
        if value and (value.encode() in raw or normalize(value) in normalized):
            hits["credential"] += 1
    for value in values.get("student_identifier", ()):
        # Token boundaries avoid treating digits inside legitimate floats/hashes as IDs.
        # Fast exact-string rejection avoids a full regex scan per private ID.
        if value and value in text and re.search(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", text):
            hits["student_identifier"] += 1
    hits["student_text"] = sum(bool(v) and v in normalized for v in values.get("student_text", ()))
    hits["private_key"] = len(re.findall(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", raw))
    if any(hits.values()):
        # Never print matching values or snippets, including in exceptions.
        raise ValueError("Sensitive value scan failed: " + json.dumps(hits, sort_keys=True))
    return {"passed": True, "matches": hits,
            "scope": "Known local credentials, source identifiers and exact normalized text of at least 16 characters; no image OCR."}


def formal_gate(root: Path) -> tuple[dict, Path]:
    from scripts.package_research import validate_delivery
    read = lambda p: json.loads(p.read_text("utf-8"))
    config = read(root / "notebooks/research-inputs.json")
    frozen_entry, material_entry = config.get("turn_snapshot"), config.get("turn_materials")
    if not frozen_entry or not material_entry:
        raise ValueError("Formal results require pinned frozen inputs")
    snapshot = source(root, frozen_entry["manifest_path"])
    manifest_path = source(root, material_entry["manifest_path"])
    snapshot_hash, material_hash = sha(snapshot.read_bytes()), sha(manifest_path.read_bytes())
    if snapshot_hash != frozen_entry["manifest_sha256"] or material_hash != material_entry["manifest_sha256"]:
        raise ValueError("Formal input pin mismatch")
    for manifest in (snapshot, manifest_path):
        for name, expected in read(manifest)["files"].items():
            p = source(root, (manifest.parent / relative(name)).relative_to(root).as_posix())
            if sha(p.read_bytes()) != expected:
                raise ValueError("Formal material file hash mismatch")
    if read(manifest_path)["source"]["manifest_sha256"] != snapshot_hash:
        raise ValueError("Formal source mismatch")
    validate_delivery(root, snapshot_hash, material_hash)
    stats = read(root / "results/model-verification/full-turn-reporting-t3.json")
    cache = snapshot.parent / "cache-lineage-audit.json"
    if (stats.get("passed") is not True or not stats.get("checks") or not all(stats["checks"].values())
            or stats.get("source_manifest_sha256") != snapshot_hash
            or stats.get("materials_manifest_sha256") != material_hash
            or stats.get("verifier_sha256") != sha((root / "scripts/verify_full_turn_materials.py").read_bytes())
            or not cache.exists() or stats.get("cache_receipt_sha256") != sha(cache.read_bytes())):
        raise ValueError("Formal independent statistical acceptance is missing or stale")
    visual_bytes = (root / "build/paper/validation.json").read_bytes()
    visual = json.loads(visual_bytes)
    pdf_hash = sha((root / "build/paper/main.pdf").read_bytes())
    if visual.get("paper_pdf_sha256") != pdf_hash:
        raise ValueError("Formal PDF changed after visual acceptance")
    return {"snapshot_manifest_sha256": snapshot_hash, "materials_manifest_sha256": material_hash,
            "statistics_receipt_sha256": sha((root / "results/model-verification/full-turn-reporting-t3.json").read_bytes()),
            "visual_receipt_sha256": sha(visual_bytes), "paper_pdf_sha256": pdf_hash}, manifest_path.parent


def demo() -> bytes:
    # Handwritten synthetic vectors, unrelated to any source student or API output.
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'">
<title>南行 · 合成教学演示</title><style>body{font:17px/1.65 system-ui;margin:40px auto;padding:0 24px;max-width:850px;color:#143742;background:#f5f8f7}h1{font-size:38px}select{padding:9px;font:inherit}table{border-collapse:collapse;width:100%;margin-top:24px;background:white}th,td{padding:14px;text-align:left;border-bottom:1px solid #dae3e1}small{color:#45656b}</style>
<h1>南行</h1><p>合成教学演示 · 从五项指标到可解释的权重比较</p>
<label>权重方案 <select id="scheme"><option value="balanced">均衡</option><option value="higher_order">高阶认知</option><option value="process">学习过程</option></select></label>
<table><thead><tr><th>合成样例</th><th>ABL / HOT / CTQ / DHI / MAB</th><th>示例得分</th></tr></thead><tbody id="rows"></tbody></table>
<p><small>三组数值均为手工构造的教学向量。分数仅说明权重计算，不用于证明学习效果，也不是学生排名。计算方法与边界见随包Notebook。</small></p>
<script>const w={balanced:[.2,.2,.2,.2,.2],higher_order:[.1,.5,.2,.15,.05],process:[.1,.2,.4,.25,.05]};const examples=[[.55,.3,.4,.65,.5],[.7,.65,.35,.45,.25],[.5,.4,.8,.75,.75]];function draw(){document.getElementById('rows').innerHTML=examples.map((v,i)=>'<tr><td>样例 '+(i+1)+'</td><td>'+v.join(' / ')+'</td><td>'+v.reduce((s,x,k)=>s+100*x*w[document.getElementById('scheme').value][k],0).toFixed(1)+'</td></tr>').join('')}document.getElementById('scheme').addEventListener('change',draw);draw();</script></html>
'''.encode("utf-8")



def validate_aggregate(content: bytes, suffix: str) -> None:
    if suffix == '.json':
        value = json.loads(content)

        def visit(node):
            if isinstance(node, dict):
                if any(str(key).lower() in PERSONAL_FIELDS for key in node):
                    raise ValueError('Aggregate contains record-level or personal fields')
                for item in node.values():
                    visit(item)
            elif isinstance(node, list):
                for item in node:
                    visit(item)
        visit(value)
    elif suffix == '.csv':
        import csv
        import io
        header = next(csv.reader(io.StringIO(content.decode('utf-8-sig'))), [])
        if any(key.lower() in PERSONAL_FIELDS for key in header):
            raise ValueError('Aggregate contains record-level or personal fields')
    else:
        raise ValueError('Aggregates must be reviewed JSON or CSV')


def reviewed_extras(root: Path, review_path: Path | None) -> tuple[dict[str, bytes], list[dict]]:
    if review_path is None:
        return {}, []
    review = json.loads(review_path.read_text('utf-8'))
    if review.get('schema_version') != 1 or not isinstance(review.get('files'), list):
        raise ValueError('Invalid public-content review list')
    entries, receipts = {}, []
    accepted_formal = None
    for item in review['files']:
        source, destination = relative_path(item['source']), relative_path(item['destination'])
        kind = item.get('kind')
        approved = item.get('review', {})
        if not all(approved.get(k) is True for k in ('no_personal_data', 'no_student_text', 'redistribution_rights_confirmed')):
            raise ValueError('Content review and redistribution approval are required')
        if not approved.get('reviewer') or not approved.get('rights_basis'):
            raise ValueError('Reviewer and rights basis are required')
        if kind == 'deidentified_aggregate':
            if not (source == 'paper/aggregate_results.json' or source.startswith('results/public/')):
                raise ValueError('Aggregate source is outside the explicit allowlist')
            if not destination.startswith('results/aggregate/'):
                raise ValueError('Aggregate destination must be results/aggregate/')
            if accepted_formal is None:
                accepted_formal, _ = formal_gate(root)
        elif kind == 'illustration':
            if not source.startswith(('assets/', 'slides/images/', 'paper/figures/')):
                raise ValueError('Illustration source is outside the explicit allowlist')
            if not destination.startswith('public-assets/') or PurePosixPath(source).suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.svg'):
                raise ValueError('Illustrations require public-assets/ and a supported image type')
            if approved.get('visual_reviewed') is not True or approved.get('external_resources') != []:
                raise ValueError('Illustration needs visual acceptance and no external resources')
        else:
            raise ValueError('Unsupported reviewed-content kind')
        content = read_source(root, source)
        if sha(content) != item.get('sha256'):
            raise ValueError('Reviewed content changed: ' + source)
        if kind == 'deidentified_aggregate':
            validate_aggregate(content, PurePosixPath(source).suffix.lower())
        elif PurePosixPath(source).suffix.lower() == '.svg':
            from xml.etree import ElementTree as ET
            tree = ET.fromstring(content)
            if any(node.tag.split('}')[-1] in {'script', 'foreignObject'} or
                   any(k.split('}')[-1].lower().startswith('on') or
                       k.split('}')[-1] in {'href', 'src'} and not v.startswith('#')
                       for k, v in node.attrib.items()) for node in tree.iter()) or b'url(' in content.lower() or b'@import' in content.lower():
                raise ValueError('Illustration has active or external resources')
        if destination in entries:
            raise ValueError('Duplicate reviewed-content destination')
        entries[destination] = content
        receipts.append({'destination': destination, 'sha256': sha(content), 'kind': kind,
                         'review': approved,
                         **({'formal_acceptance': accepted_formal} if kind == 'deidentified_aggregate' else {})})
    return entries, receipts


def candidate_readme(has_license: bool, *, include_concepts=False) -> bytes:
    license_text = '项目许可见随附 LICENSE 文件；第三方内容保留各自权利说明。' if has_license else '项目级开源许可证尚待权利人选择；当前目录是本地待审发布候选。'
    gallery = ''
    if include_concepts:
        gallery = '\n## 方法概念图\n\n五张图说明方法与模块关系，实际实验状态以对应验收收据为准。\n\n'
        gallery += '\n\n'.join(
            f'### {title}\n\n![{title}](assets/illustrations/{figure}.png)\n\n{description}'
            for figure, (title, description) in CONCEPT_DESCRIPTIONS.items())
        gallery += ('\n\n五份初始提示词和三份修订提示词保存在 `assets/illustrations/prompts/`，'
                    '逐文件来源哈希记入公开清单。修订记录的范围以概念图清单说明为准。\n\n')
    return f'''# 南行｜公开复现候选

本目录包含数学建模代码、合成教学示例及清理后的 Notebook。合成数据用于检查计算与敏感性，不代表真实学生效果。经过独立审核的聚合结果或插图如有纳入，记录于 PUBLIC-RELEASE-MANIFEST.json。

打开 `demo/index.html` 可运行自包含的合成权重演示，无外部资源请求。Notebook 中未提供的真实输入保持缺失；正式PDF如有纳入，仅用于阅读，未捆绑第三方论文模板或声明可完整重编译。

Python 3.13，在独立目录创建虚拟环境后安装 requirements-lock.txt：

```powershell
python -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install -r requirements-lock.txt
.\\.venv\\Scripts\\python.exe scripts/run_all.py --core-only
```

省略 `--core-only` 可执行 Notebook。核心复算无需 API 密钥，入口禁止外网请求。安装依赖的网络准备与复算分别计时。

PUBLIC-RELEASE-MANIFEST.json 和 REPRODUCTION-MANIFEST.json 记录文件哈希。源码与 Notebook 中保留了研究数据接入接口；这些接口在公开候选中使用合成模式。实际研究数据应由有权使用者在自己的私有环境配置。

`.gitattributes`使用 `* -text` 保持文件字节；候选尚未经过许可选择，也未改变私有仓库的历史或可见性。

{gallery}
{license_text}

发布条件由项目负责人确认：作品已经提交、竞赛公开时间允许、许可证已定、附加聚合和图像审查完成。本构建程序只创建本地文件，不设置仓库可见性或上传内容。
'''.encode('utf-8')


def verify_markdown_images(name: str, text: str, entries: dict[str, bytes]) -> None:
    for image in re.findall(r'!\[[^]]*\]\(([^)]+)\)', text):
        if ':' in image or '\\' in image or image.startswith('/'):
            raise ValueError('Markdown image uses external or hidden content')
        parts = list(PurePosixPath(name).parent.parts)
        for part in image.split('/'):
            if part == '..':
                if not parts:
                    raise ValueError('Markdown image escaped release')
                parts.pop()
            elif part not in ('', '.'):
                parts.append(part)
        if '/'.join(parts) not in entries:
            raise ValueError('Markdown image is not included')


def verify_release(entries: dict[str, bytes], expected_names=None) -> dict:
    if expected_names is not None and set(entries) != set(expected_names):
        raise ValueError('Release exact membership mismatch')
    if len(entries) != len({name.casefold() for name in entries}):
        raise ValueError('Case-insensitive release path collision')
    for name in entries:
        relative_path(name)
    manifest = json.loads(entries['PUBLIC-RELEASE-MANIFEST.json'])
    if set(manifest['files']) != set(entries) - {'PUBLIC-RELEASE-MANIFEST.json'}:
        raise ValueError('Release manifest membership mismatch')
    for name, item in manifest['files'].items():
        if sha(entries[name]) != item['sha256'] or len(entries[name]) != item['bytes']:
            raise ValueError('Release file hash mismatch')
    reproduction = json.loads(entries['REPRODUCTION-MANIFEST.json'])
    if set(reproduction['files']) != set(entries) - {'PUBLIC-RELEASE-MANIFEST.json', 'REPRODUCTION-MANIFEST.json'}:
        raise ValueError('Reproduction manifest membership mismatch')
    if any(sha(entries[n]) != digest for n, digest in reproduction['files'].items()):
        raise ValueError('Reproduction file hash mismatch')
    config = json.loads(entries['notebooks/research-inputs.json'])
    if config != {'schema_version': 1, 'agent_batch': None, 'turn_snapshot': None,
                  'turn_materials': None, 'turn_plan': None}:
        raise ValueError('Private research configuration present')
    if entries['.gitattributes'] != b'* -text\n':
        raise ValueError('Git byte-preservation policy missing')
    verify_markdown_images('README.md', entries['README.md'].decode('utf-8'), entries)
    for name, content in entries.items():
        if not name.endswith('.ipynb'):
            continue
        for cell in json.loads(content)['cells']:
            if cell.get('attachments') or cell.get('metadata') or cell.get('outputs') or cell.get('execution_count') is not None:
                raise ValueError('Notebook output or hidden metadata present')
            if cell['cell_type'] == 'markdown':
                text = ''.join(cell['source']) if isinstance(cell['source'], list) else cell['source']
                verify_markdown_images(name, text, entries)
    return {'passed': True, 'files': len(entries), 'exact_membership': True,
            'sha256': True, 'notebook_images_resolve': True, 'readme_images_resolve': True}


def build_release(root: Path, output: Path, review_path: Path | None = None, *,
                  include_paper=False, include_concepts=False) -> dict:
    root, output = root.resolve(), output.resolve()
    if not output.is_relative_to(root / 'runtime/public-release') or output == root / 'runtime/public-release':
        raise ValueError('Stage public candidates under private runtime/public-release/')
    archive_path = output.with_name(output.name + '.zip')
    receipt_path = output.with_name(output.name + '.acceptance.json')
    if output.exists() or archive_path.exists() or receipt_path.exists():
        raise ValueError('Release destination already exists; use a new version directory')
    entries, source_hashes = {}, {}
    for name in BASE_FILES + AIV_FILES + NOTEBOOK_FILES + SYNTHETIC_FILES:
        content = read_source(root, name)
        source_hashes[name] = sha(content)
        if name in NOTEBOOK_FILES:
            content = clean_notebook(content, keep_concepts=include_concepts)
        if name == 'scripts/execute_notebooks.py':
            content = content.replace('人工核验已封存。图表区分真实观察与合成实验；'.encode(),
                                      '本包使用合成教学数据；'.encode())
        entries[name] = content
    provenance = json.loads(entries['results/synthetic/provenance.json'])
    if provenance.get('source') != 'synthetic' or provenance.get('real_effect_claim') is not False:
        raise ValueError('Synthetic provenance is missing or changed')
    concepts = {'status': 'not_included'}
    prompt_hashes = {}
    if include_concepts:
        from scripts.package_research import concept_illustration_inputs, concept_illustration_receipt
        inputs = concept_illustration_inputs(root)
        if inputs['status'] != 'accepted' or inputs.get('images') != 5:
            raise ValueError('All five concept images must pass acceptance before inclusion')
        expected_concept_files = {'assets/illustrations/manifest.json'} | {
            f'assets/illustrations/{figure}.png' for figure in CONCEPT_INITIAL_PROMPTS}
        if set(inputs['source_hashes']) != expected_concept_files:
            raise ValueError('Concept sources differ from the fixed five-image allowlist')
        concepts = concept_illustration_receipt(inputs)
        for name, digest in inputs['source_hashes'].items():
            content = read_source(root, name)
            if sha(content) != digest:
                raise ValueError('Concept input changed during collection')
            entries[name] = content
            source_hashes[name] = digest
        figures = json.loads(entries['assets/illustrations/manifest.json'])['figures']
        for row in figures:
            if (row.get('prompt') != CONCEPT_INITIAL_PROMPTS.get(row['id']) or
                    row.get('generation', {}).get('revision_prompt') != CONCEPT_REVISION_PROMPTS.get(row['id'])):
                raise ValueError('Concept prompt differs from the fixed reviewed allowlist')
        for name in CONCEPT_PROMPT_FILES:
            content = read_source(root, name)
            entries[name] = content
            prompt_hashes[name] = source_hashes[name] = sha(content)
    licenses = [name for name in ('LICENSE', 'LICENSE.txt', 'LICENSE.md', 'COPYING') if (root / name).is_file()]
    for name in licenses:
        entries[name] = read_source(root, name)
        source_hashes[name] = sha(entries[name])
    extras, reviewed = reviewed_extras(root, review_path)
    if entries.keys() & extras.keys():
        raise ValueError('Reviewed content cannot replace core files')
    entries.update(extras)
    formal = None
    if include_paper:
        formal, _ = formal_gate(root)
        entries['public-results/paper.pdf'] = source(root, 'build/paper/main.pdf').read_bytes()
        if sha(entries['public-results/paper.pdf']) != formal['paper_pdf_sha256']:
            raise ValueError('Formal PDF changed during release collection')
    entries['notebooks/research-inputs.json'] = json_bytes({
        'schema_version': 1, 'agent_batch': None, 'turn_snapshot': None, 'turn_materials': None, 'turn_plan': None})
    entries['README.md'] = candidate_readme(bool(licenses), include_concepts=include_concepts)
    entries['demo/index.html'] = demo()
    entries['.gitignore'] = b'.env\n.env.*\n.venv/\nruntime/\nbuild/\n__pycache__/\n.pytest_cache/\n.ipynb_checkpoints/\nnode_modules/\n'
    entries['.gitattributes'] = b'* -text\n'
    secrets = local_secrets(root)
    values, inventory = sensitive_inventory(root)
    scan_entries(entries, secrets)
    sensitive = scan_sensitive(entries, values)
    reproduction = {'schema_version': 'nanxing-reproduction-package-v1',
                    'mode': 'synthetic_teaching', 'snapshot_manifest_sha256': None,
                    'live_api': False,
                    'files': {name: sha(content) for name, content in sorted(entries.items())}}
    entries['REPRODUCTION-MANIFEST.json'] = json_bytes(reproduction)
    manifest = {'schema_version': 'nanxing-public-release-v1', 'published': False,
                'publication_ready': False, 'git_history_included': False,
                'license_status': 'existing_license_included' if licenses else 'owner_selection_pending',
                'release_checks_pending': ['submission_and_publication_window_confirmation',
                                           'owner_final_content_review'] + ([] if licenses else ['project_license_selection']),
                'data_mode': 'synthetic_default_with_explicitly_reviewed_extras',
                'notebook_outputs_and_attachments_removed': True, 'reviewed_content': reviewed,
                'concept_illustrations': concepts, 'formal_acceptance': formal,
                'concept_prompt_source_hashes': prompt_hashes,
                'files': {name: {'sha256': sha(content), 'bytes': len(content)}
                          for name, content in sorted(entries.items())}}
    entries['PUBLIC-RELEASE-MANIFEST.json'] = json_bytes(manifest)
    scan_entries(entries, secrets)
    checks = verify_release(entries, set(entries))
    if any(sha(read_source(root, name)) != digest for name, digest in source_hashes.items()):
        raise ValueError('Source changed during release collection')
    if include_paper and sha(source(root, 'build/paper/main.pdf').read_bytes()) != formal['paper_pdf_sha256']:
        raise ValueError('Formal PDF changed during release collection')
    receipt = {'schema_version': 'nanxing-public-preparation-v2', 'status': 'building',
               'published': False, 'publication_ready': False, 'source_hashes': source_hashes,
               'scan_inventory_counts': inventory, 'sensitive_scan': sensitive,
               'network_requests': 0, 'git_operations': 0}
    output.mkdir(parents=True)
    try:
        for name, content in sorted(entries.items()):
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        written = {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob('*') if p.is_file()}
        verify_release(written, set(entries))
        with zipfile.ZipFile(archive_path, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, content in sorted(entries.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None or len(archive.namelist()) != len(set(archive.namelist())):
                raise ValueError('Release archive integrity mismatch')
            zipped = {name: archive.read(name) for name in archive.namelist()}
        verify_release(zipped, set(entries))
        scan_entries(zipped, secrets)
        scan_sensitive(zipped, values)
        digest = sha(archive_path.read_bytes())
        archive_path.with_suffix('.sha256').write_bytes(f'{digest}  {archive_path.name}\n'.encode('utf-8'))
        receipt.update(status='prepared_local_only', directory=str(output), archive=str(archive_path), sha256=digest,
                       files=len(entries), reviewed_extras=len(reviewed), license_status=manifest['license_status'],
                       verification=checks, public_manifest_sha256=sha(entries['PUBLIC-RELEASE-MANIFEST.json']),
                       formal_acceptance=formal, concept_illustrations=concepts,
                       concept_prompt_source_hashes=prompt_hashes)
    except BaseException as error:
        receipt.update(status='failed', error_category=type(error).__name__)
        receipt_path.write_bytes(json_bytes(receipt))
        raise
    receipt_path.write_bytes(json_bytes(receipt))
    return receipt


def path_statistics(paths: set[str]) -> dict:
    groups = {
        'raw_material': lambda p: p.startswith(('data/', 'archive/')),
        'runtime': lambda p: p.startswith('runtime/'),
        'virtual_environment': lambda p: p.startswith('.venv/'),
        'dependencies': lambda p: 'node_modules' in p.split('/'),
        'cache': lambda p: any(x in BLOCKED_PARTS - {'.git', '.venv', 'node_modules'} for x in p.split('/')),
        'literature_pdf': lambda p: p.startswith('literature/') and p.lower().endswith('.pdf'),
        'generated_illustration_candidates': lambda p: p.startswith(('slides/images/', 'paper/figures/', 'assets/')) and PurePosixPath(p).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.svg', '.pdf'),
        'credential_shaped_filename': lambda p: PurePosixPath(p).name == '.env' or PurePosixPath(p).suffix.lower() in ('.key', '.pem', '.pfx', '.p12', '.env'),
    }
    return {'files': len(paths),
            'top_level_counts': dict(sorted(Counter(p.split('/')[0] for p in paths).items())),
            'categories': {kind: sum(bool(check(p)) for p in paths) for kind, check in groups.items()}}


def audit_git(root: Path) -> dict:
    def git(*args):
        return subprocess.check_output(['git', '-c', 'core.quotepath=false', *args], cwd=root)
    tracked = {p for p in git('ls-files', '-z').decode('utf-8', 'replace').split('\0') if p}
    historical = {p.strip('\n') for p in git('log', '--all', '--format=', '--name-only', '-z', '--no-renames').decode('utf-8', 'replace').split('\0') if p.strip('\n')}
    return {'schema_version': 1, 'scope': 'all_local_refs_path_inventory',
            'head': git('rev-parse', 'HEAD').decode().strip(),
            'commits': int(git('rev-list', '--all', '--count')),
            'tracked': path_statistics(tracked), 'historical_paths': path_statistics(historical),
            'root_license_files': [p for p in tracked if '/' not in p and p.lower().startswith(('license', 'copying'))],
            'secret_scan_scope': 'release_bytes_and_known_local_credentials; Git history path audit is not a complete historical secret-content scan',
            'visibility_change_performed': False, 'git_history_changed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--reviewed-content', type=Path)
    parser.add_argument('--include-paper', action='store_true', help='Require frozen statistical and PDF visual acceptance')
    parser.add_argument('--include-concepts', action='store_true', help='Require all five hash-bound conceptual images')
    parser.add_argument('--audit-git', type=Path, help='Write a private Git path-count audit')
    args = parser.parse_args()
    if args.audit_git:
        destination = args.audit_git.resolve()
        if not destination.is_relative_to(ROOT / 'runtime/public-release'):
            parser.error('Git audit must stay in runtime/public-release/')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(json_bytes(audit_git(ROOT)))
        print(json.dumps({'audit': str(destination), 'published': False}))
    if args.output:
        result = build_release(ROOT, args.output, args.reviewed_content,
                               include_paper=args.include_paper, include_concepts=args.include_concepts)
        print(json.dumps({k: result[k] for k in ('status', 'directory', 'archive', 'sha256', 'files', 'published')}, ensure_ascii=False))
    elif not args.audit_git:
        parser.error('Provide --output and/or --audit-git')


if __name__ == '__main__':
    main()
