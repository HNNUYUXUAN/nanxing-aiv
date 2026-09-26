"""Update paper and notebook inputs from one explicit, immutable t3 snapshot.

Without --manifest this writes a pending report using only the pinned plan.
The optional --build runs ten clean-kernel notebooks, the existing LaTeX build,
and a Markdown mirror. No model API is called.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiv.notebook_materials import load_turn_plan, load_turn_snapshot, load_full_turn_materials
from scripts.connect_notebook_snapshot import connect
from scripts.package_research import concept_illustration_inputs, concept_illustration_receipt

PAPER = ROOT / "paper"
CONFIG = ROOT / "notebooks/research-inputs.json"
MARKER = "% GENERATED T3 RESULTS: updated only by paper/update_results.py"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tex(value):
    return str(value).replace("\\", r"\textbackslash{}").replace("_", r"\_").replace("%", r"\%").replace("&", r"\&").replace("#", r"\#")


def pin_materials(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("Use a local derived manifest")
    config = json.loads(CONFIG.read_text("utf-8"))
    config["turn_materials"] = {"manifest_path": path.relative_to(ROOT).as_posix(),
                                "manifest_sha256": sha(path)}
    candidate = CONFIG.with_name(".research-inputs-check.json")
    try:
        candidate.write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")
        load_full_turn_materials(candidate)
        candidate.replace(CONFIG)
    finally:
        candidate.unlink(missing_ok=True)


def table(headers, rows, caption):
    cols = "l" + "r" * (len(headers) - 1)
    return "\n".join([r"\begin{table}[H]\centering\small", r"\caption{" + caption + "}",
                       r"\begin{tabular}{" + cols + "}", r"\toprule",
                       " & ".join(map(tex, headers)) + r" \\", r"\midrule",
                       *[" & ".join(map(tex, row)) + r" \\" for row in rows],
                       r"\bottomrule\end{tabular}\end{table}"])


def kappa_interval(agreement, name):
    value = agreement.get(name)
    interval = agreement.get(name + "_cluster_interval95")
    if value is None:
        return "不可计算"
    point = f"{value:.3f}"
    return point + (f" ({interval[0]:.3f}, {interval[1]:.3f})" if interval is not None else " (区间不可计算)")


def generate():
    plan = load_turn_plan()
    if plan is None:
        raise ValueError("A pinned t3 plan is required")
    batch = load_turn_snapshot()
    materials = load_full_turn_materials()
    if batch and batch["revision"] != "t3":
        raise ValueError("Only t3 may update the final paper")
    values = {"FullPlanned": plan["planned_real"], "FullControls": plan["planned_controls"],
              "FullCompleted": "待冻结", "FullUnfinished": "待冻结", "FullCandidate": "待冻结",
              "FullStudents": "待冻结", "FullAiv": "待冻结", "FullStatus": "待正式冻结"}
    section = [r"\subsection{全回合 t3：固定计划与冻结结果}",
               "恢复后的学生 Q 回合与旧首问抽样是不同研究单位。本批固定计划为 "
               r"\FullPlanned{} 个真实回合与 \FullControls{} 条工程控制；按学期预选随机复核 "
               + str(plan["random_review_preselected"]) + " 回合，另预选 "
               + str(plan["repeat_records"]) + " 回合由两个快模型各复跑一次。"
               "强模型分别处理预选随机复核与高风险路由，首次复核不接收快模型答案。"]
    if batch is None:
        overview = (f"全回合 t3 的固定计划为 {plan['planned_real']} 个真实学生 Q 回合；"
                    "正式冻结结果尚未接入，完成量与候选结果留空。")
        section += ["目前仅披露冻结前已固定的计划分母，不从运行目录提取完成量。未运行、执行失败、主动弃权与候选标签分别记录。",
                    table(["学期", "计划真实回合", "已完成"],
                          [[term, n, "待冻结"] for term, n in plan["term_counts"].items()],
                          "全回合实验的固定计划；结果待正式冻结")]
    else:
        status = "完整结算" if batch["completed_real"] == batch["planned_real"] else "部分结算"
        values.update(FullCompleted=batch["completed_real"], FullUnfinished=batch["planned_real"]-batch["completed_real"],
                      FullCandidate=batch["candidate_labeled_real"], FullStatus=status)
        overview = (f"全回合 t3 已冻结为{status}：计划 {batch['planned_real']} 个真实回合，"
                    f"完成 {batch['completed_real']} 个，形成 {batch['candidate_labeled_real']} 个任务候选。"
                    "所有候选均为模型流程输出。")
        section += [overview, table(["学期", "计划", "完成", "未完成", "候选", "完成但无候选"],
                  [[r["term"], r["planned"], r["completed"], r["unfinished"], r["candidate_labeled"], r["unknown_or_abstained"]]
                   for r in batch["by_term"]], "t3 冻结范围：完成与候选分别计数")]
        pairs = batch["agreement"]
        section.append(table(["模型阶段", "双有效", "含弃权一致", "双非空", "非空一致"],
                        [[("两快" if r["stage"] == "independent" else "两强")
                          + ("任务" if r["dimension"] == "task" else "贡献"),
                          r["paired_structured"], r["same_including_abstain"], r["paired_labeled"], r["same_labeled"]]
                         for r in pairs], "两维标签的配对分母；一致性不等于准确率"))
        section += ["上表的两强总体仅用于描述执行配对，不能用于推断全体准确率。复核集合经分层随机与风险路由选择，后续分层表保留两种来源。",
                    r"\begin{figure}[H]\centering\includegraphics[width=.83\linewidth]{figures/real-turn-batch-coverage.pdf}",
                    r"\caption{同一冻结快照的计划与完成范围。未完成回合没有并入弃权。}\end{figure}"]
        if materials:
            summary = materials["summary"]
            counts = summary["denominators"]
            scores = summary["score_availability"]
            values.update(FullStudents=counts["student_term_rows"], FullAiv=scores["full_AIV_available"])
            task = summary["final_task_status_counts"]
            contribution = summary["final_contribution_status_counts"]
            names = {"agreed":"候选", "abstained":"一致弃权", "disagreement":"分歧", "uncertain":"不确定", "technical_failure":"技术失败"}
            section.append(table(["完成回合的状态", "任务维度", "贡献维度"],
                                 [[names[k], task.get(k, 0), contribution.get(k, 0)] for k in names],
                                 "仅已完成真实回合的两维状态；各列分母相同"))
            strata = materials["review_strata"]["strata"]
            repeats = materials["repeatability"]
            control = strata["control"]
            control_tasks = {k: sum(control[stage][k] for stage in ("independent_tasks", "review_tasks"))
                             for k in ("expected", "valid", "failed", "pending", "not_created")}
            stage_rows = [("真实基础", summary["independent_tasks"]),
                          ("随机复核", strata["random_audit"]["review_tasks"]),
                          ("风险复核", strata["high_risk"]["review_tasks"]),
                          ("工程控制", control_tasks), ("配对复跑", repeats["tasks"])]
            section.append(table(["逻辑任务来源", "计划", "有效", "失败", "待运行", "未创建"],
                                 [[label, *[t[k] for k in ("expected", "valid", "failed", "pending", "not_created")]]
                                  for label, t in stage_rows], "逻辑任务结算；有效输出包含明确弃权"))
            section.append(f"工程控制 {control['planned_records']} 个回合中 {control['completed_records']} 个已结算；"
                           "上表控制请求包含两基础与两复核模型，单独校验流程，不进入真实学生指标。"
                           "逻辑任务与真实 API 尝试分别记账，重试和精确缓存复用不扩大学生样本量。")
            review_rows = []
            for key, label in (("random_audit", "分层随机复核"), ("high_risk", "高风险复核")):
                r = materials["review_strata"]["strata"][key]
                a = r["agreement"]["task"]
                t = r["review_tasks"]
                review_rows.append([label, r["planned_records"], t["expected"], t["valid"], a["both_nonnull"]])
            section.append(table(["复核来源", "回合", "应有请求", "有效输出", "双非空配对"], review_rows,
                                 "真实复核分层及不同分母；工程控制另列"))
            agreement_rows = []
            agreement_sources = []
            for dimension, dimension_label in (("task", "任务"), ("contribution", "贡献")):
                agreement_sources.append(("两快·" + dimension_label, summary["independent_agreement"][dimension]))
                agreement_sources.extend((label + "·" + dimension_label, strata[key]["agreement"][dimension])
                                         for key, label in (("random_audit", "随机两强"), ("high_risk", "风险两强")))
            for label, agreement in agreement_sources:
                agreement_rows.append([label, agreement["both_nonnull"], agreement["student_clusters_dual_nonnull"],
                                       kappa_interval(agreement, "kappa6"), kappa_interval(agreement, "kappa3")])
            section.append(table(["来源", "双非空", "学生簇", "六级κ（95%区间）", "三阶κ（95%区间）"],
                                 agreement_rows, "t3 两维标签一致性；学生整簇重采样 2,000 次"))
            section.append("两快统计包含所有真实回合中双方均有有效输出的配对，不要求该回合的全部后续任务已结算；"
                           "κ 仅使用双方非空标签。两强随机与高风险子集分别报告；表中数值不能作跨模型能力排名。"
                           "学生整簇重采样按学生聚类，但相同输入的精确缓存可能使不同学生共享同一次模型响应；"
                           "该区间未消除这类跨簇依赖，不能视为独立调用或准确率的置信区间。"
                           "跨学生缓存链接数量以独立缓存核验收据为准。")
            repeat_rows = []
            for model, item in repeats["by_model"].items():
                for dimension, label in (("task", "任务"), ("contribution", "贡献")):
                    pair = item[dimension]
                    repeat_rows.append([model, label, pair["planned_pairs"], pair["paired_valid"], pair["equal"]])
            section.append(table(["快模型", "维度", "计划配对", "双有效配对", "相同输出"], repeat_rows,
                                 "首次与独立复跑的稳定性；相同输出含双方弃权"))
            section.append("复跑比较仅在首次与再次判断均有效的配对内计算；上述计数不表示独立人工准确率。"
                           "复跑任务均保留真实调用，不能用精确缓存响应替代重复性测量。")
            section += [r"按冻结范围导出 \FullStudents{} 行学生×学期记录；完整五维 AIV 可用 \FullAiv{} 行。"
                        "每行同时保留计划回合、完成范围、候选覆盖与缺失原因。结构解析得到的候选会话不自动成为已验证相邻边，缺工具身份的记录不补成零。",
                        f"其中 {scores['labeled_student_term_rows']} 个学生学期单元有任务候选，"
                        f"{scores['MAB_available']} 个可计算工具广度，{scores['CTQ_available']} 个可计算 CTQ。"
                        "完整分数缺失时不生成真实排名。",
                        "上述分层与学生级汇总来源于同一哈希固定的派生分析包；学生标识和逐条文本保留在私有环境。"]
        else:
            section.append("学生级派生分析包尚未接入；本节不报告学生级分数或新增排名。")
        source_parts = batch["source"].split("/")
        wrapped_source = ["/".join(source_parts[:2]) + "/", "/".join(source_parts[2:-1]) + "/", source_parts[-1]]
        section += ["冻结清单（以下三段顺序连接为同一路径）：",
                    r"\begin{flushleft}\footnotesize "
                    + r"\\".join(r"\path{" + part + "}" for part in wrapped_source)
                    + r"\end{flushleft}",
                    r"清单 SHA-256：\par{\footnotesize\path{" + batch["source_sha256"] + r"}\par}"]
    values["FullOverview"] = overview
    original = (PAPER / "results_snapshot.tex").read_text("utf-8").split(MARKER)[0]
    original = re.sub(r"\\newcommand\{\\ResultNewStatus\}\{[^\n]*\}",
                      lambda _: r"\newcommand{\ResultNewStatus}{\FullOverview{}}", original)
    macros = "\n".join("\\newcommand{\\" + key + "}{" + tex(value) + "}" for key, value in values.items())
    (PAPER / "results_snapshot.tex").write_text(original.rstrip()+"\n\n"+MARKER+"\n"+macros+"\n", "utf-8")
    (PAPER / "full_turn_results.tex").write_text("\n\n".join(section)+"\n", "utf-8")
    receipt = {"schema_version":"paper-results-v1", "status":"frozen" if batch else "pending",
               "plan":plan, "snapshot":batch, "materials":materials,
               "privacy":"aggregates only; no student identifiers or text"}
    (PAPER / "aggregate_results.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n","utf-8")
    return {"status":receipt["status"], "planned_real":plan["planned_real"],
            "completed_real": batch["completed_real"] if batch else None, "materials_connected":materials is not None}


def markdown_mirror():
    source = (PAPER / "main.tex").read_text("utf-8")
    source = re.sub(r"\\begin\{center\}\\small 南行.*?\\end\{center\}",
                    "南行｜缪雨轩、郭雨辰、徐问天、张梓涵\n\n中国第一届数学建模黑客松｜2026 年 9 月\n", source, count=1, flags=re.S)
    for name in ("results_snapshot.tex", "full_turn_results.tex", "final_analysis_results.tex",
                 "appendix_reproducibility.tex", "appendix_ai_usage.tex"):
        directive = r"\input{" + name + "}"
        if directive in source:
            source = source.replace(directive, (PAPER / name).read_text("utf-8"))
    source = source.replace(r"\begin{abstract}", r"\section*{摘要}").replace(r"\end{abstract}", "")
    cited = list(dict.fromkeys(key for group in re.findall(r"\\cite\{([^}]+)\}", source) for key in group.split(",")))
    source = re.sub(r"\\cite\{([^}]+)\}", lambda m: r"\texttt{["+m[1].replace("_", r"\_")+"]}", source)
    source = re.sub(r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}", "评价链：原始证据 → A 标注 → B 识别 → C 指标 → D 回验 → E 决策。", source, flags=re.S)
    out = subprocess.run(["pandoc", "--from=latex", "--to=gfm", "--wrap=none"], input=source,
                         text=True, encoding="utf-8", capture_output=True, check=True, cwd=PAPER)
    title = "# 面向 AI 辅助学习的增量价值评价：认知证据、误差传播与稳健决策\n\n"
    note = "本文件由 `paper/main.tex` 与同一结果入口生成；正式排版、参考文献和页码以 [PDF](../build/paper/main.pdf) 为准。\n\n"
    references = bibliography_lines(cited)
    body = re.sub(r"(?m)^[ \t]+$", "", out.stdout)
    body = re.sub(r'<embed src="(\.\./results/final-analysis/[^"<>]+)\.pdf"\s*/>',
                  r'<img src="\1.png" alt="研究结果统计图" />', body)
    (PAPER / "研究论文.md").write_text(title+note+body+"\n\n## 参考文献（与正文引用键对应）\n\n"+"\n".join(references)+"\n", "utf-8")


def bibliography_lines(cited):
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
    parser = BibTexParser(ignore_nonstandard_types=False)
    entries = bibtexparser.loads((PAPER / "references.bib").read_text("utf-8"), parser=parser).entries_dict
    def plain(value):
        return " ".join(value.replace("{", "").replace("}", "").replace(r"\&", "&").split())
    lines = []
    for key in cited:
        entry = entries[key]
        author = plain(entry.get("author", entry.get("editor", "")))
        year = plain(entry.get("year", entry.get("date", "")))
        title = plain(entry["title"])
        if not author or not year or not title:
            raise ValueError(f"Incomplete cited bibliography entry: {key}")
        venue = plain(entry.get("journal", entry.get("booktitle", entry.get("publisher", ""))))
        suffix = f" DOI: {entry['doi']}" if entry.get("doi") else (f" {entry['url']}" if entry.get("url") else "")
        lines.append(f"- `{key}`: {author} ({year}). {title}. {venue}{suffix}".rstrip())
    return lines


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--materials", type=Path)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--mirror-only", action="store_true",
                        help="Convert the current manuscript to Markdown without changing results or notebooks")
    args=parser.parse_args()
    if args.mirror_only:
        if args.build or args.manifest or args.materials:
            parser.error("--mirror-only cannot be combined with snapshot or build options")
        markdown_mirror()
        print(json.dumps({"mode": "mirror-only", "notebooks_changed": False}, ensure_ascii=False))
        return
    if args.manifest:
        if not args.manifest_sha256:
            parser.error("--manifest requires --manifest-sha256")
        connect(args.manifest, "t3", args.manifest_sha256)
    if args.materials:
        pin_materials(args.materials)
    concepts = concept_illustration_receipt(concept_illustration_inputs(ROOT))
    result=generate()
    result["concept_illustrations"] = concepts
    if args.build:
        # execute_notebooks attaches accepted concepts after the builder, before kernels.
        for script in ("scripts/build_research_notebooks.py", "scripts/execute_notebooks.py", "paper/prepare_bibliography.py"):
            subprocess.run([sys.executable, str(ROOT/script)],cwd=ROOT,check=True)
        if result["status"] == "frozen":
            (PAPER / "figures").mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "build/notebooks/figures/real-turn-batch-coverage.pdf",
                         PAPER / "figures/real-turn-batch-coverage.pdf")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/build_latex.ps1",
                        "-Source", "paper/main.tex", "-OutputDirectory", "build/paper"],cwd=ROOT,check=True)
    markdown_mirror()
    print(json.dumps(result,ensure_ascii=False))


if __name__ == "__main__":
    main()
