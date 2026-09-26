from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse
import hashlib
from html import escape
import json
import os
import re
import sys
import time
import nbformat
from nbclient import NotebookClient
from nbconvert import HTMLExporter
from jupyter_client import KernelManager

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from aiv.notebook_materials import atomic_write_text
from scripts.package_research import (concept_illustration_inputs, concept_illustration_receipt,
                                      prepare_concept_illustrations, verify_concept_html_assets)
OUT = ROOT / "build/notebooks"
OUT.mkdir(parents=True, exist_ok=True)


def execute(path):
    start = time.monotonic()
    nb = nbformat.read(path, as_version=4)
    manager = KernelManager(kernel_name="python3")
    manager.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
    try:
        NotebookClient(
            nb, timeout=180, km=manager,
            resources={"metadata": {"path": str(ROOT)}},
        ).execute(env={**os.environ, "MPLBACKEND": "module://matplotlib_inline.backend_inline"})
    finally:
        if manager.has_kernel:
            manager.shutdown_kernel(now=True)
    nbformat.validate(nb)
    atomic_write_text(path, nbformat.writes(nb))
    html, _ = HTMLExporter(require_js_url="", mathjax_url="", exclude_input_prompt=True,
                           exclude_output_prompt=True).from_notebook_node(nb)
    html = re.sub(r'<script\b[^>]*\bsrc="[^"]*"[^>]*>\s*</script>', "", html)
    page_title = path.stem.replace("批量结果与互评", "批量结果与独立复核") if path.stem.startswith("09_") else path.stem
    html = html.replace("<title>Notebook</title>", f"<title>{escape(page_title)}</title>")
    html = html.replace("</head>", '''<link rel="icon" href="data:,">
<style>body{font-family:"Microsoft YaHei",system-ui,sans-serif!important;color:#273949!important;max-width:1180px;margin:auto!important;padding:28px!important}
.jp-RenderedHTMLCommon{font-size:15px!important;line-height:1.8!important}.jp-InputArea{margin-top:10px}.jp-OutputArea-output{max-width:100%;overflow-x:auto}
table{font-size:13px!important}td{overflow-wrap:anywhere;max-width:420px}details summary{cursor:pointer;color:#176b77;font-size:13px;padding:6px 0}nav{border-bottom:1px solid #dbe3ea;padding:10px 0 20px;margin-bottom:26px}nav a{color:#176b77}nav span{margin-left:24px;font-size:13px;color:#607385}
@media(max-width:700px){body{padding:12px!important}.jp-Cell{padding:4px!important}}</style></head>''')
    html = html.replace("</body>", '''<script>
document.querySelectorAll('.jp-CodeCell .jp-Cell-inputWrapper').forEach(input=>{const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='查看计算代码';input.before(details);details.append(summary,input)});
const nav=document.createElement('nav');nav.innerHTML='<a href="index.html">← 返回 Notebook 与图表目录</a><span>离线阅读 · 代码可展开</span>';document.body.prepend(nav);
</script></body>''')
    atomic_write_text(OUT / (path.stem + ".html"), html)
    return {
        "notebook": path.name,
        "seconds": round(time.monotonic() - start, 2),
        "status": "executed",
        "role": nb.metadata.get("material_role", "tutorial"),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "html": path.stem + ".html",
        "html_sha256": hashlib.sha256((OUT / (path.stem + ".html")).read_bytes()).hexdigest(),
        "figures_in_outputs": sum("image/png" in o.get("data", {}) for c in nb.cells if c.cell_type == "code" for o in c.outputs),
    }


def write_index(results, figures):
    reading_order = {part: index for index, part in enumerate(
        ("00", "01", "08", "02", "09", "03", "04", "05", "06", "07"))}
    ordered = sorted(results, key=lambda r: reading_order.get(r["notebook"][:2], 99))
    def label(result):
        name = result["notebook"].removesuffix(".ipynb")
        return name.replace("批量结果与互评", "批量结果与独立复核") if name.startswith("09_") else name
    links = "".join(f'<li><a href="{escape(r["html"])}">{escape(label(r))}</a><small>{r["figures_in_outputs"]} 张内嵌图 · 已执行</small></li>' for r in ordered)
    connected = json.loads((ROOT / "notebooks/research-inputs.json").read_text("utf-8")).get("turn_snapshot") is not None
    batch_status = "09 本已接入显式冻结快照" if connected else "09 本保留冻结结果入口"
    kind_labels = {"real": "真实数据", "synthetic": "合成实验", "conditional": "条件范围分析"}
    cards = "".join(
        f'<article data-kind="{f["kind"]}"><span class="tag">{kind_labels[f["kind"]]}</span>'
        f'<h3>{escape(f["caption"])}</h3><a href="{f["files"]["png"]}"><img loading="lazy" src="{f["files"]["png"]}" alt="{escape(f["caption"])}"></a>'
        f'<p class="file-id">{f["id"]} · {escape(f["notebook"][:2])}</p><p>'
        + " · ".join(f'<a href="{url}">{fmt.upper()}</a>' for fmt, url in f["files"].items())
        + f' · <a href="figures/{f["id"]}.json">来源</a></p></article>' for f in figures
    )
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>论文 Notebook 与图表</title><link rel="icon" href="data:,"><style>
*{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#213547;font:16px/1.7 "Microsoft YaHei",system-ui,sans-serif}
main{max-width:1240px;margin:auto;padding:48px 32px}header{border-bottom:1px solid #cfdae2;padding-bottom:28px;margin-bottom:28px}
h1{font-size:34px;letter-spacing:-1px;margin:8px 0}h2{font-size:23px;margin-top:38px}h3{font-size:17px;font-weight:600;margin:10px 0}
p{margin:10px 0}a{color:#086878;text-underline-offset:4px}small{display:block;color:#607385}.eyebrow{color:#176b77;font-weight:700;letter-spacing:2px}
.note{background:#e8eff6;border-left:4px solid #176b77;padding:14px 20px;margin:20px 0}.notebooks{columns:2;padding-left:22px}.notebooks li{break-inside:avoid;margin-bottom:14px}
.toolbar{display:flex;gap:12px;align-items:center;margin:18px 0;flex-wrap:wrap}select{font:inherit;padding:8px 18px;border:1px solid #afbfcb;border-radius:8px;background:white}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}article{background:white;border:1px solid #dbe3ea;border-radius:12px;padding:22px;min-width:0}article[hidden]{display:none}
img{width:100%;height:auto;display:block}.tag{font-size:12px;color:#416172;background:#e9f2f4;padding:4px 9px;border-radius:20px}.file-id{font-size:13px;color:#607385}footer{border-top:1px solid #cfdae2;margin-top:40px;padding-top:20px;font-size:14px}
@media(max-width:760px){main{padding:24px 16px}.grid{grid-template-columns:1fr}.notebooks{columns:1}h1{font-size:28px}}
</style><main><header><div class="eyebrow">MATH HACKATHON · RESEARCH MATERIALS</div><h1>论文 Notebook 与图表</h1>
<p>沿“原始证据 → A 标注 → B 识别 → C 指标 → D 回验 → E 决策”阅读。下方按建议顺序排列。</p></header>
<div class="note">图表区分真实观察、合成实验和条件范围分析；''' + batch_status + '''。条件范围由给定缺失与扰动假设求得。</div>
<h2>Notebook 阅读入口</h2><ul class="notebooks">''' + links + '''</ul>
<p><a href="interactive/real-monthly.html">打开月度分布交互图</a> · <a href="figure-manifest.json">图表与来源清单</a> · <a href="execution.json">本次执行记录</a></p>
<h2>可用于论文的图表素材</h2><div class="toolbar"><label for="kind">筛选来源</label><select id="kind"><option value="all">全部图表</option><option value="real">真实数据</option><option value="synthetic">合成实验</option><option value="conditional">条件范围分析</option></select><span id="count" aria-live="polite"></span></div>
<div class="grid">''' + cards + '''</div><footer>PDF / SVG 适合论文排版，PNG 适合预览。每图来源摘要见“来源”；合成实验只支持给定生成机制下的方法描述。</footer></main>
<script>const select=document.querySelector('#kind');function filter(){let n=0;document.querySelectorAll('article').forEach(card=>{card.hidden=select.value!=='all'&&card.dataset.kind!==select.value;if(!card.hidden)n++});document.querySelector('#count').textContent=n+' 张图表'}select.addEventListener('change',filter);filter();</script></html>'''
    if not any(f["id"] == "real-monthly" for f in figures):
        html = html.replace('<a href="interactive/real-monthly.html">打开月度分布交互图</a> · ', '')
    atomic_write_text(OUT / "index.html", html)


def validate_exports(results, figures, wall_seconds, concepts=None):
    if len(results) != 10 or any(result["status"] != "executed" for result in results):
        raise ValueError("The ten-notebook package did not execute completely")
    if len({figure["id"] for figure in figures}) != len(figures):
        raise ValueError("Duplicate figure id")
    for result in results:
        if not (OUT / result["html"]).is_file():
            raise FileNotFoundError(result["html"])
        exported_count = sum(figure["notebook"] == result["notebook"] for figure in figures)
        if result.get("figures_in_outputs", 0) < exported_count:
            raise ValueError(f"Notebook figure outputs missing: {result['notebook']}")
    for figure in figures:
        if set(figure["files"]) != {"pdf", "svg", "png"}:
            raise ValueError(f"Incomplete figure exports: {figure['id']}")
        for relative in figure["files"].values():
            path = OUT / relative
            if not path.is_file() or path.stat().st_size == 0:
                raise FileNotFoundError(path)
        for source in figure["sources"]:
            path = ROOT / source["path"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError(f"Figure source changed: {path}")
    config = json.loads((ROOT / "notebooks/research-inputs.json").read_text("utf-8"))
    validation = {"notebooks_executed": len(results), "figures": len(figures),
                  "export_formats": ["pdf", "svg", "png"],
                  "source_hashes_rechecked": True, "all_export_files_present": True,
                  "inline_figure_outputs_verified": True,
                  "live_api": False, "wall_seconds": wall_seconds,
                  "turn_snapshot": "connected" if config.get("turn_snapshot") else "pending",
                  "snapshot_manifest_sha256": (config.get("turn_snapshot") or {}).get("manifest_sha256"),
                  "materials_manifest_sha256": (config.get("turn_materials") or {}).get("manifest_sha256")}
    validation["concept_illustrations"] = concepts or concept_illustration_receipt(concept_illustration_inputs(ROOT))
    verify_concept_html_assets(ROOT, validation["concept_illustrations"])
    atomic_write_text(OUT / "validation.json", json.dumps(validation, ensure_ascii=False, indent=2))
    return validation


def main():
    parser = argparse.ArgumentParser(description="Execute notebooks offline using this Python interpreter.")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    started = time.monotonic()
    # Builders overwrite Markdown. Attach immediately before any kernel starts.
    concepts = prepare_concept_illustrations(ROOT)
    started_wall = time.time()
    config_bytes = (ROOT / "notebooks/research-inputs.json").read_bytes()
    paths = sorted((ROOT / "notebooks").glob("*.ipynb"))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(execute, paths))
    if (ROOT / "notebooks/research-inputs.json").read_bytes() != config_bytes:
        raise ValueError("Notebook input configuration changed during execution")
    if concept_illustration_receipt(concept_illustration_inputs(ROOT)) != concepts:
        raise ValueError("Concept illustration inputs changed during Notebook execution")
    figures = [json.loads(p.read_text("utf-8")) for p in sorted((OUT / "figures").glob("*.json")) if p.stat().st_mtime >= started_wall]
    report = {"results": results, "wall_seconds": round(time.monotonic() - started, 2),
              "live_api": False, "python": sys.executable, "figures": len(figures),
              "concept_illustrations": concepts}
    atomic_write_text(OUT / "execution.json", json.dumps(report, ensure_ascii=False, indent=2))
    atomic_write_text(OUT / "figure-manifest.json", json.dumps(figures, ensure_ascii=False, indent=2))
    write_index(results, figures)
    validate_exports(results, figures, report["wall_seconds"], concepts)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
