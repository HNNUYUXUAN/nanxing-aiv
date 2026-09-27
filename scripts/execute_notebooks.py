from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse
import hashlib
from html import escape, unescape
import json
import os
import re
import sys
import time
from urllib.parse import quote, unquote, urlsplit
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
READING_ORDER = ("00", "01", "08", "02", "09", "03", "04", "05", "06", "07")


def reading_navigation(path):
    """Keep chapter links useful after downloading the HTML collection."""
    available = {p.name[:2]: p for p in (ROOT / "notebooks").glob("[0-9][0-9]_*.ipynb")}
    ordered = [available[key] for key in READING_ORDER if key in available]
    items = ['<a href="index.html">← 阅读目录</a>', '<a href="../../README.md">项目说明</a>']
    if path in ordered:
        index = ordered.index(path)
        for adjacent, label in ((index - 1, "上一章"), (index + 1, "下一章")):
            if 0 <= adjacent < len(ordered):
                target = ordered[adjacent]
                items.append(f'<a href="{quote(target.stem)}.html">{label} · {escape(target.name[:2])}</a>')
    return '<nav aria-label="章节导航">' + " · ".join(items) + '</nav>'


def html_reading_links(html, notebook_path):
    """Resolve notebook-relative document links from build/notebooks instead."""
    def rewrite(match):
        target = unescape(match[1])
        url = urlsplit(target)
        if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
            return match[0]
        local = (notebook_path.parent / unquote(url.path)).resolve()
        if not local.is_relative_to(ROOT) or not local.exists():
            return match[0]
        if local.suffix == ".ipynb" and local.parent == ROOT / "notebooks":
            destination = local.stem + ".html"
        else:
            destination = os.path.relpath(local, OUT).replace("\\", "/")
        if url.fragment:
            destination += "#" + url.fragment
        return 'href="' + escape(quote(destination, safe="/#%?=&"), quote=True) + '"'
    return re.sub(r'href="([^"]+)"', rewrite, html)


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
    html = html_reading_links(html, path)
    page_title = path.stem.replace("批量结果与互评", "批量结果与独立复核") if path.stem.startswith("09_") else path.stem
    html = re.sub(r"<title>.*?</title>", f"<title>{escape(page_title)}</title>", html)
    html = html.replace("</head>", '''<link rel="icon" href="data:,">
<style>body{font-family:"Microsoft YaHei",system-ui,sans-serif!important;color:#273949!important;max-width:1180px;margin:auto!important;padding:28px!important}
.jp-RenderedHTMLCommon{font-size:15px!important;line-height:1.8!important}.jp-InputArea{margin-top:10px}.jp-OutputArea-output{max-width:100%;overflow-x:auto}
.jp-RenderedImage img,.jp-RenderedHTMLCommon img{max-width:100%!important;height:auto!important}.jp-RenderedHTMLCommon h2{padding-top:16px;border-top:1px solid #e1e7eb}.jp-RenderedHTMLCommon blockquote{border-left:3px solid #176b77;padding:8px 16px;background:#f3f7f8;color:#34495b}.jp-OutputArea-prompt,.jp-InputPrompt{display:none!important}.jp-Cell{min-width:0}nav{display:flex;gap:12px;flex-wrap:wrap}nav a{white-space:nowrap}
table{font-size:13px!important}td{overflow-wrap:anywhere;max-width:420px}details summary{cursor:pointer;color:#176b77;font-size:13px;padding:6px 0}nav{border-bottom:1px solid #dbe3ea;padding:10px 0 20px;margin-bottom:26px}nav a{color:#176b77}nav span{margin-left:24px;font-size:13px;color:#607385}
@media(max-width:700px){body{padding:12px!important}.jp-Cell{padding:4px!important}}</style></head>''')
    nav = reading_navigation(path)
    html = re.sub(r"(<body[^>]*>)", lambda m: m[0] + nav, html, count=1)
    html = html.replace("</body>", '''<script>
document.querySelectorAll('.jp-CodeCell .jp-Cell-inputWrapper').forEach(input=>{const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='查看计算代码';input.before(details);details.append(summary,input)});
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
        "figures_in_outputs": sum(any(mime in o.get("data", {}) for mime in ("image/png", "image/jpeg"))
            for c in nb.cells if c.cell_type == "code" for o in c.outputs),
        "reference_images_in_outputs": sum(any(mime in o.get("data", {}) for mime in ("image/png", "image/jpeg"))
            for c in nb.cells if c.cell_type == "code" and "reviewer-visual-v1" in c.metadata.get("tags", []) for o in c.outputs),
    }


def write_index(results, figures):
    reading_order = {part: index for index, part in enumerate(READING_ORDER)}
    ordered = sorted(results, key=lambda r: reading_order.get(r["notebook"][:2], 99))
    def label(result):
        name = result["notebook"].removesuffix(".ipynb")
        return name.replace("批量结果与互评", "批量结果与独立复核") if name.startswith("09_") else name
    links = "".join(f'<li><a href="{escape(r["html"])}">{escape(label(r))}</a><small>{r["figures_in_outputs"]} 张内嵌图 · 已执行</small></li>' for r in ordered)
    connected = json.loads((ROOT / "notebooks/research-inputs.json").read_text("utf-8")).get("turn_snapshot") is not None
    has_aggregates = (ROOT / "results/final-analysis/summary.json").is_file()
    batch_status = ("公开汇总与合成案例可离线复算" if has_aggregates else "合成实验可离线复算")
    if connected:
        batch_status += "，本地已连接授权冻结输入"
    kind_labels = {"real": "真实数据", "synthetic": "合成实验", "conditional": "条件范围分析"}
    cards = "".join(
        f'<article data-kind="{f["kind"]}"><span class="tag">{kind_labels[f["kind"]]}</span>'
        f'<h3>{escape(f["caption"])}</h3><a href="{f["files"]["png"]}"><img loading="lazy" src="{f["files"]["png"]}" alt="{escape(f["caption"])}"></a>'
        f'<p class="file-id">{f["id"]} · {escape(f["notebook"][:2])}</p><p>'
        + " · ".join(f'<a href="{url}">{fmt.upper()}</a>' for fmt, url in f["files"].items())
        + f' · <a href="figures/{f["id"]}.json">来源</a></p></article>' for f in figures
    )
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>南行 · Notebook 阅读与复算</title><link rel="icon" href="data:,"><style>
*{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#213547;font:16px/1.7 "Microsoft YaHei",system-ui,sans-serif}
main{max-width:1240px;margin:auto;padding:48px 32px}header{border-bottom:1px solid #cfdae2;padding-bottom:28px;margin-bottom:28px}
h1{font-size:34px;letter-spacing:-1px;margin:8px 0}h2{font-size:23px;margin-top:38px}h3{font-size:17px;font-weight:600;margin:10px 0}
p{margin:10px 0}a{color:#086878;text-underline-offset:4px}small{display:block;color:#607385}.eyebrow{color:#176b77;font-weight:700;letter-spacing:2px}
.note{background:#e8eff6;border-left:4px solid #176b77;padding:14px 20px;margin:20px 0}.notebooks{columns:2;padding-left:22px}.notebooks li{break-inside:avoid;margin-bottom:14px}
.toolbar{display:flex;gap:12px;align-items:center;margin:18px 0;flex-wrap:wrap}select{font:inherit;padding:8px 18px;border:1px solid #afbfcb;border-radius:8px;background:white}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}article{background:white;border:1px solid #dbe3ea;border-radius:12px;padding:22px;min-width:0}article[hidden]{display:none}
img{width:100%;height:auto;display:block}.tag{font-size:12px;color:#416172;background:#e9f2f4;padding:4px 9px;border-radius:20px}.file-id{font-size:13px;color:#607385}footer{border-top:1px solid #cfdae2;margin-top:40px;padding-top:20px;font-size:14px}
@media(max-width:760px){main{padding:24px 16px}.grid{grid-template-columns:1fr}.notebooks{columns:1}h1{font-size:28px}}
</style><main><header><div class="eyebrow">南行 · 可执行研究</div><h1>Notebook 阅读与复算</h1>
<p>先读 00 总览，运行 07 的两个案例；需要深入核查时，再按下方章节检查数据、判断与条件范围。</p>
<p><a href="../../README.md">项目说明</a> · <a href="../../build/paper/main.pdf">论文 PDF</a> · <a href="../../slides/roadshow/player/index.html">20 页路演</a></p></header>
<div class="note">图表区分真实观察、合成实验和条件范围分析；''' + batch_status + '''。条件范围由给定缺失与扰动假设求得。</div>
<h2>Notebook 阅读入口</h2><ul class="notebooks">''' + links + '''</ul>
<p><a href="interactive/real-monthly.html">打开月度分布交互图</a> · <a href="figure-manifest.json">图表与来源清单</a> · <a href="execution.json">本次执行记录</a></p>
<h2>计算图表与输入依据</h2><div class="toolbar"><label for="kind">筛选来源</label><select id="kind"><option value="all">全部图表</option><option value="real">真实数据</option><option value="synthetic">合成实验</option><option value="conditional">条件范围分析</option></select><span id="count" aria-live="polite"></span></div>
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
                  "turn_snapshot": "connected" if config.get("turn_snapshot") else "not_included",
                  "snapshot_manifest_sha256": (config.get("turn_snapshot") or {}).get("manifest_sha256"),
                  "materials_manifest_sha256": (config.get("turn_materials") or {}).get("manifest_sha256")}
    validation["concept_illustrations"] = concepts or concept_illustration_receipt(concept_illustration_inputs(ROOT))
    verify_concept_html_assets(ROOT, validation["concept_illustrations"])
    atomic_write_text(OUT / "validation.json", json.dumps(validation, ensure_ascii=False, indent=2))
    return validation


def main():
    parser = argparse.ArgumentParser(description="Execute notebooks offline using this Python interpreter.")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1)
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
              "live_api": False, "python": "Python " + sys.version.split()[0], "figures": len(figures),
              "input_scope": "authorized_inputs_and_public_aggregates" if json.loads(config_bytes).get("turn_snapshot") else "public_aggregate_and_synthetic",
              "concept_illustrations": concepts}
    atomic_write_text(OUT / "execution.json", json.dumps(report, ensure_ascii=False, indent=2))
    atomic_write_text(OUT / "figure-manifest.json", json.dumps(figures, ensure_ascii=False, indent=2))
    write_index(results, figures)
    validate_exports(results, figures, report["wall_seconds"], concepts)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
