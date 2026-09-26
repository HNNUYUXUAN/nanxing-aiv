"""Create a local allowlisted reproduction ZIP; never publish or upload it."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SOURCE_FILES = [
    "requirements-lock.txt", "requirements.txt", "pytest.ini", "run_all.ps1", "run_all.sh",
    "scripts/run_all.py", "scripts/execute_notebooks.py", "scripts/verify_models.py",
    "scripts/package_research.py", "scripts/attach_concept_illustrations.py",
    "scripts/verify_full_turn_materials.py",
    "scripts/build_full_turn_materials.py", "scripts/build_solution_materials.py",
    "scripts/build_research_notebooks.py", "scripts/connect_notebook_snapshot.py",
    "tests/test_metrics.py", "tests/test_models.py", "tests/test_uncertainty.py",
    "tests/test_solution_materials.py", "tests/test_analysis.py", "docs/指标定义与性质.md",
    "docs/演示与复现.md", "docs/三模型预实验.md", "docs/红队实测.md",
]
PRIVATE_FILES = [
    "runtime/research/records.json", "runtime/research/data-audit.json",
    "runtime/research/final-verification-export.json",
    "runtime/research/turns-v1/manifest.json",
]
FROZEN_FILES = ["snapshot-manifest.json", "annotations.json", "summary.json",
                "evaluation-summary.json", "integrity-audit.json", "call-ledger.json",
                "cache-lineage-audit.json"]
BASELINE = "runtime/research/fast-strong-384-v3-20260926T121958Z/r3"
BASELINE_RESULTS = "runtime/research/solution-materials-384-v1"
BASELINE_TABLES = ["agreement.json", "semester-descriptions.json", "process-association.json",
                   "score-availability.json", "cost-allocation-simulation.json",
                   "synthetic-weight-comparison.json", "student-indicators.csv"]


def concept_illustration_inputs(root: Path) -> dict:
    """Read-only concept asset gate, shared by execution, delivery and packaging.

    Only absence or an entirely empty generation placeholder is optional. Once
    any image/hash/acceptance is supplied, all five figures must pass review.
    Source paths document provenance; they need not all be in a teaching ZIP.
    """
    from scripts.attach_concept_illustrations import MANIFEST, load_manifest, package_asset_paths
    root = Path(root).resolve()
    path = root / MANIFEST
    if not path.exists():
        return {"status": "pending", "reason": "manifest_missing", "source_hashes": {}, "package_paths": {}}
    manifest = load_manifest(root)
    if (manifest.get("status") == "awaiting_generation_and_visual_review"
            and all(row.get("qa") == "pending" and row.get("sha256") is None
                    and row.get("native_dimensions") is None
                    and not (root / row["file"]).exists() for row in manifest["figures"])):
        return {"status": "pending", "reason": "generation_pending", "source_hashes": {}, "package_paths": {}}
    if (manifest.get("kind") != "conceptual"
            or not isinstance(manifest.get("generation_surface"), str)
            or not manifest["generation_surface"].strip()
            or not isinstance(manifest.get("acceptance"), list) or not manifest["acceptance"]):
        raise ValueError("Concept illustration provenance or acceptance criteria are missing")
    for row in manifest["figures"]:
        sources = row.get("sources")
        if not isinstance(sources, list) or not sources or any(
                not isinstance(name, str) or not name.startswith(("aiv/", "paper/", "scripts/", "docs/"))
                or ".." in Path(name.replace("\\", "/")).parts for name in sources):
            raise ValueError("Concept illustration source provenance is missing or unsafe")
    paths = package_asset_paths(root)
    source_hashes = {name: sha((root / name).read_bytes()) for name in sorted(set(paths.values()))}
    return {"status": "accepted", "manifest_sha256": source_hashes[MANIFEST],
            "source_hashes": source_hashes, "package_paths": paths,
            "integration_script_sha256": sha((root / "scripts/attach_concept_illustrations.py").read_bytes()),
            "images": len(manifest["figures"])}


def concept_illustration_receipt(inputs: dict) -> dict:
    """Keep source pins and state in receipts, without duplicating destination maps."""
    return {key: value for key, value in inputs.items() if key != "package_paths"}


def verify_concept_html_assets(root: Path, concepts: dict) -> None:
    """The HTML mirror must have the same bytes as its accepted source PNGs."""
    for source, expected in concepts.get("source_hashes", {}).items():
        if source.endswith(".png"):
            mirror = root / "build/assets/illustrations" / Path(source).name
            if (not mirror.resolve().is_relative_to(root.resolve()) or not mirror.is_file()
                    or sha(mirror.read_bytes()) != expected):
                raise ValueError("Concept illustration HTML asset is missing or stale")


def check_pending_concept_notebooks(root: Path) -> None:
    for notebook in (Path(root) / "notebooks").glob("*.ipynb"):
        nb = json.loads(notebook.read_text("utf-8"))
        for cell in nb.get("cells", []):
            source = cell.get("source", "")
            source = "".join(source) if isinstance(source, list) else source
            if cell.get("cell_type") == "markdown" and "../assets/illustrations/" in source:
                raise ValueError("Notebook references concept images that are not accepted")


def prepare_concept_illustrations(root: Path) -> dict:
    """Attach only accepted figures, after builders and before kernel execution."""
    from scripts.attach_concept_illustrations import attach
    inputs = concept_illustration_inputs(root)
    if inputs["status"] == "accepted":
        applied = attach(root, apply=True)
        if applied.get("status") != "attached" or not applied.get("code_cells_preserved"):
            raise ValueError("Concept illustration attachment did not complete")
        if concept_illustration_inputs(root) != inputs:
            raise ValueError("Concept illustration inputs changed during attachment")
    else:
        # Missing pictures cannot be silently paired with old attached cells.
        check_pending_concept_notebooks(root)
    return concept_illustration_receipt(inputs)


def paper_source_hashes(root: Path) -> dict:
    paths = {p for p in (root / "paper").glob("*")
             if p.is_file() and p.suffix in {".tex", ".bib", ".cls", ".sty", ".bst"}}
    paths |= {p for p in (root / "paper/figures").rglob("*") if p.is_file()}
    hashes = {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in sorted(paths)}
    # Images outside paper/ still affect the rendered PDF and its visual receipt.
    hashes.update(concept_illustration_inputs(root)["source_hashes"])
    return hashes


def validate_delivery(root: Path, snapshot_hash: str, derived_hash: str) -> None:
    """Reject a newly pinned config paired with stale PDF or Notebook exports."""
    read = lambda name: json.loads((root / name).read_text("utf-8"))
    paper = read("paper/aggregate_results.json")
    if (paper.get("status") != "frozen" or
            (paper.get("snapshot") or {}).get("source_sha256") != snapshot_hash or
            (paper.get("materials") or {}).get("source_sha256") != derived_hash):
        raise ValueError("Paper and selected research inputs do not match")
    validation = read("build/notebooks/validation.json")
    concepts = concept_illustration_inputs(root)
    if concepts["status"] == "accepted" and validation.get("concept_illustrations") != concept_illustration_receipt(concepts):
        raise ValueError("Notebook concept illustration receipt is missing or stale")
    verify_concept_html_assets(root, concepts)
    if (validation.get("turn_snapshot") != "connected" or validation.get("notebooks_executed") != 10
            or validation.get("live_api") is not False or not validation.get("source_hashes_rechecked")
            or validation.get("snapshot_manifest_sha256") != snapshot_hash
            or validation.get("materials_manifest_sha256") != derived_hash):
        raise ValueError("All ten connected offline Notebooks must pass")
    execution = read("build/notebooks/execution.json")
    if concepts["status"] == "accepted" and execution.get("concept_illustrations") != concept_illustration_receipt(concepts):
        raise ValueError("Notebook concept execution receipt is missing or stale")
    executed = execution["results"]
    expected_names = {p.name for p in (root / "notebooks").glob("*.ipynb")}
    if len(executed) != 10 or len(expected_names) != 10 or {item["notebook"] for item in executed} != expected_names:
        raise ValueError("Notebook execution membership mismatch")
    for item in executed:
        html = root / "build/notebooks" / validate_relative(item["html"])
        if (item["status"] != "executed" or
                sha((root / "notebooks" / validate_relative(item["notebook"])).read_bytes()) != item["sha256"] or
                not html.is_file() or sha(html.read_bytes()) != item.get("html_sha256")):
            raise ValueError("Notebook export is stale or missing")
    visual = read("build/paper/validation.json")
    pages = visual.get("pdf_pages", 0)
    if (visual.get("status") != "frozen_validated" or visual.get("snapshot_manifest_sha256") != snapshot_hash
            or visual.get("materials_manifest_sha256") != derived_hash
            or visual.get("paper_pdf_sha256") != sha((root / "build/paper/main.pdf").read_bytes())
            or visual.get("paper_source_hashes") != paper_source_hashes(root)
            or pages < 1 or visual.get("visual_review_pages") != list(range(1, pages + 1))
            or not 1 <= visual.get("body_including_references_pages", 0) <= 30):
        raise ValueError("Final PDF needs hash-bound visual acceptance of every page")


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_relative(relative: str) -> str:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Unsafe archive path")
    name = path.as_posix()
    if any(part in {".git", ".venv", "__pycache__", "node_modules"} for part in path.parts):
        raise ValueError("Excluded archive path")
    if path.name == ".env" or path.name.endswith((".key", ".sqlite3", ".env")):
        raise ValueError("Credentials and live databases cannot enter a research archive")
    return name


def readme(mode: str) -> bytes:
    private = mode == "authorized_research"
    scope = ("本包包含竞赛授权研究输入与冻结模型输出，仅向获授权的团队和评委交付。"
             if private else "本包只包含合成教学数据与计算代码。真实研究输入未包含。")
    return f'''# 南行｜离线复现包

{scope}

Python 3.13。先在独立目录解压，创建虚拟环境并安装 requirements-lock.txt；该准备步骤需要软件依赖来源，计时复算从依赖安装完成后开始。

```powershell
python -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install -r requirements-lock.txt
.\\run_all.ps1
```

本次完整验收平台是 Windows。另提供 Linux/macOS 的 `sh run_all.sh` 入口，需先用 `python3 -m venv .venv` 安装同一锁文件；其他平台尚未完成实际整链验收。也可直接运行 `python scripts/run_all.py`。`--core-only` 执行统计与性质检查，省略Notebook渲染。

入口首先核对 REPRODUCTION-MANIFEST.json 中全部文件哈希，再离线重算。外部网络连接被禁用，Jupyter 内核所需回环通信保留。无需 .env 或 API key。核心复算须少于30分钟；实际时长和步骤状态保存在 build/reproduction/receipt.json。

阅读顺序：00 → 01 → 08 → 02 → 09 → 03 → 04 → 05 → 06 → 07。执行后的HTML目录为 build/notebooks/index.html。已交付的PDF、HTML和学生表位于 deliverables/（研究包）；论文源位于 paper/。重新编译论文是独立可选步骤，需要既有XeLaTeX/Biber环境，PDF阅读与核心复算不依赖TeX或浏览器。

## 数据与解释

每条真实结果绑定冻结清单哈希；完整/部分完成范围、随机/风险复核、控制题和重复性各自保留分母。CSV一行是学生×学期，数据字典见 DATA-DICTIONARY.md。新旧批次分别展示。研究包另含384条首问历史基线的哈希固定输入，入口独立重算7份数值表；图像时间戳不作为数值一致性标准。

模型一致性与稳定性不提供真实准确率保证。缺独立结果与识别条件时无法估计真实学习因果效应；缺可靠相邻关系或工具字段时相应指标、完整AIV与排名保持缺失。合成实验只验证声明的生成机制。

文件完整性与数值复算不能代替数据使用授权。请在竞赛授权范围内保存研究包。
'''.encode("utf-8")


def build_package(root: Path, output: Path, mode: str) -> dict:
    root = root.resolve()
    private = mode == "authorized_research"
    if private and not output.resolve().is_relative_to(root / "runtime"):
        raise ValueError("Authorized research packages must stay under private runtime/")
    entries: dict[str, bytes] = {}
    concepts = concept_illustration_inputs(root)
    if concepts["status"] == "pending":
        check_pending_concept_notebooks(root)
        if private and (root / "assets/illustrations/manifest.json").exists():
            raise ValueError("Final research delivery requires all declared concept illustrations to be accepted")

    def add(relative: str, *, destination: str | None = None, required: bool = True) -> None:
        relative = validate_relative(relative)
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Source escaped the workspace")
        if not path.is_file():
            if required:
                raise FileNotFoundError(relative)
            return
        entries[validate_relative(destination or relative)] = path.read_bytes()

    for relative in SOURCE_FILES:
        add(relative)
    for destination, source in concepts["package_paths"].items():
        add(source, destination=destination)
        if sha(entries[destination]) != concepts["source_hashes"][source]:
            raise ValueError("Concept illustration changed while packaging")
    for folder, pattern in [("aiv", "*.py"), ("notebooks", "*.ipynb"),
                            ("results/synthetic", "*"), ("examples", "synthetic-*.json")]:
        for path in sorted((root / folder).glob(pattern)):
            if path.is_file():
                add(path.relative_to(root).as_posix())
    config = json.loads((root / "notebooks/research-inputs.json").read_text("utf-8"))
    snapshot_hash = None
    if private:
        entry = config.get("turn_snapshot")
        if not entry:
            raise ValueError("Freeze and connect t3 before creating the research package")
        relative = validate_relative(entry["manifest_path"])
        manifest_path = root / relative
        if sha(manifest_path.read_bytes()) != entry["manifest_sha256"]:
            raise ValueError("Pinned snapshot hash mismatch")
        snapshot_hash = entry["manifest_sha256"]
        frozen = manifest_path.parent
        if frozen.parent.name != "t3":
            raise ValueError("The final research package must use t3")
        manifest = json.loads(manifest_path.read_text("utf-8"))
        for name, expected in manifest["files"].items():
            if sha((frozen / validate_relative(name)).read_bytes()) != expected:
                raise ValueError("Frozen artifact hash mismatch")
        audit_path = frozen / "integrity-audit.json"
        if not json.loads(audit_path.read_text("utf-8")).get("passed"):
            raise ValueError("Frozen integrity audit must pass before packaging")
        for name in FROZEN_FILES:
            add((frozen / name).relative_to(root).as_posix())
        for name in ("plan.json", "sample.json", "implementation-revision.json"):
            add((frozen.parent / name).relative_to(root).as_posix(), required=name != "implementation-revision.json")
        for relative in PRIVATE_FILES:
            add(relative)
        baseline_manifest = json.loads((root / BASELINE / "handoff-manifest.json").read_text("utf-8"))
        add(BASELINE + "/handoff-manifest.json")
        for name, expected in baseline_manifest["files"].items():
            path = root / BASELINE / validate_relative(name)
            if sha(path.read_bytes()) != expected:
                raise ValueError("Historical baseline input hash mismatch")
            add(path.relative_to(root).as_posix())
        baseline_entry = baseline_manifest["agent_batch"]
        for kind in ("annotations", "summary"):
            name = validate_relative(baseline_entry[kind + "_path"])
            if sha((root / name).read_bytes()) != baseline_entry[kind + "_sha256"]:
                raise ValueError("Historical baseline result hash mismatch")
            add(name)
        for name in BASELINE_TABLES:
            add(BASELINE_RESULTS + "/" + name)
        derived_entry = config.get("turn_materials")
        if not derived_entry:
            raise ValueError("Connect the audited full-turn materials before packaging")
        derived_path = root / validate_relative(derived_entry["manifest_path"])
        if sha(derived_path.read_bytes()) != derived_entry["manifest_sha256"]:
            raise ValueError("Derived material manifest hash mismatch")
        derived = json.loads(derived_path.read_text("utf-8"))
        if derived["source"]["manifest_sha256"] != snapshot_hash:
            raise ValueError("Derived materials and frozen snapshot do not match")
        validate_delivery(root, snapshot_hash, derived_entry["manifest_sha256"])
        add(derived_path.relative_to(root).as_posix())
        for name, expected in derived["files"].items():
            path = derived_path.parent / validate_relative(name)
            if sha(path.read_bytes()) != expected:
                raise ValueError("Derived result hash mismatch")
            add(path.relative_to(root).as_posix())
        turn_dir = root / "runtime/research/turns-v1"
        turn_manifest = json.loads((turn_dir / "manifest.json").read_text("utf-8"))
        for relative in turn_manifest["files"]:
            add((turn_dir / validate_relative(relative)).relative_to(root).as_posix())
        records = json.loads(entries["runtime/research/records.json"])
        for relative in sorted({record["source_file"] for record in records}):
            # Authorized CSV inputs are needed for the existing source-hash checks.
            add(relative)
        add((derived_path.parent / "student-indicators.csv").relative_to(root).as_posix(),
            destination="deliverables/student-indicators.csv")
        add("build/paper/main.pdf", destination="deliverables/paper.pdf")
        add("paper/figures/real-turn-batch-coverage.pdf")
        add("build/paper/validation.json", destination="deliverables/paper-validation.json")
        for path in sorted((root / "build/notebooks").rglob("*")):
            if path.is_file():
                add(path.relative_to(root).as_posix(), destination="deliverables/notebooks/" + path.relative_to(root / "build/notebooks").as_posix())
        for path in sorted((root / "paper").glob("*")):
            if path.is_file() and path.suffix in {".tex", ".bib", ".cls", ".sty", ".bst", ".py", ".md", ".json"}:
                add(path.relative_to(root).as_posix())
        for name in ("scripts/build_latex.ps1", "literature/references.bib"):
            add(name)
    else:
        config = {"schema_version": 1, "agent_batch": None, "turn_snapshot": None}
        # Executed research cells may contain aggregate private-study outputs.
        # A public teaching ZIP starts with clean, unexecuted notebooks.
        for name in list(entries):
            if name.endswith(".ipynb"):
                nb = json.loads(entries[name])
                for cell in nb["cells"]:
                    if cell["cell_type"] == "code":
                        cell["outputs"] = []
                        cell["execution_count"] = None
                entries[name] = (json.dumps(nb, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    entries["notebooks/research-inputs.json"] = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    entries["README.md"] = readme(mode)
    entries["DATA-DICTIONARY.md"] = (root / "docs/交付数据字典.md").read_bytes()
    from dotenv import dotenv_values
    secrets = []
    for file in (root / ".env", root / "deploy/workbench.env"):
        if file.exists():
            secrets += [value.encode() for key, value in dotenv_values(file).items()
                        if value and len(value) >= 8 and any(x in key.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]
    key_markers = [("-----BEGIN " + kind + " PRIVATE KEY-----").encode() for kind in ("RSA", "EC", "OPENSSH")]
    key_markers.append(("-----BEGIN " + "PRIVATE KEY-----").encode())
    for content in entries.values():
        if any(marker in content for marker in key_markers) or any(secret in content for secret in secrets):
            raise ValueError("Secret scan failed; no package written")
    if concept_illustration_inputs(root) != concepts:
        raise ValueError("Concept illustration inputs changed while packaging")
    manifest = {"schema_version": "nanxing-reproduction-package-v1", "mode": mode,
                "snapshot_manifest_sha256": snapshot_hash, "live_api": False,
                "concept_illustrations": concept_illustration_receipt(concepts),
                "files": {name: sha(content) for name, content in sorted(entries.items())}}
    entries["REPRODUCTION-MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, content in sorted(entries.items()):
            archive.writestr(name, content)
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
        for name, expected in manifest["files"].items():
            if sha(archive.read(name)) != expected:
                raise ValueError("ZIP entry hash mismatch")
    temporary.replace(output)
    digest = sha(output.read_bytes())
    output.with_suffix(".sha256").write_text(f"{digest}  {output.name}\n", "utf-8")
    return {"archive": str(output), "mode": mode, "files": len(entries),
            "concept_illustrations": concepts["status"],
            "bytes": output.stat().st_size, "sha256": digest, "published": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("authorized_research", "synthetic_teaching"), default="authorized_research")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    target = args.output or ROOT / "runtime/delivery" / ("南行_研究复现包.zip" if args.mode == "authorized_research" else "南行_合成教学包.zip")
    print(json.dumps(build_package(ROOT, target, args.mode), ensure_ascii=False))
