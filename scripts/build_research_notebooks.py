"""Build the research companion and add figure exports to existing tutorials."""
from pathlib import Path
from textwrap import dedent
import sys
import nbformat as nbf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUT = ROOT / "notebooks"
SETUP = '''
from pathlib import Path
import sys, json
ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "aiv").is_dir())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from IPython.display import display, Markdown
from aiv.notebook_materials import (
    configure_style, save_figure, export_table, research_inputs,
    final_archive_summary, load_frozen_batch, load_turn_snapshot, load_turn_plan, load_full_turn_materials,
    turn_manifest_summary, pending_results, offline_monthly, COLORS,
)
configure_style()
LIVE_API = False
'''


def md(source):
    return nbf.v4.new_markdown_cell(dedent(source).strip())


def code(source):
    return nbf.v4.new_code_cell(dedent(source).strip())


def write(name, cells):
    path = OUT / name
    if path.is_file():
        previous = nbf.read(path, as_version=4)
        concepts = [cell for cell in previous.cells if cell.cell_type == "markdown"
                    and "concept-illustration-v1" in cell.metadata.get("tags", [])]
        cells = cells[:1] + concepts + cells[1:]
    nb = nbf.v4.new_notebook(cells=cells, metadata={
        "kernelspec": {"display_name": "Python (project .venv)", "language": "python", "name": "python3"},
        "language_info": {"name": "python"}, "material_role": "research_companion", "live_api": False,
    })
    nbf.validate(nb)
    nbf.write(nb, path)


def update_tutorials():
    extra = {
        "03": ["aiv/calibration.py", "results/synthetic/residual-coverage-summary.json", "results/synthetic/residual-coverage-trials.csv"],
        "05": ["results/synthetic/label-dependence.csv"],
        "06": ["results/synthetic/text-redteam.csv"],
    }
    for path in sorted(OUT.glob("0[1-7]_*.ipynb")):
        nb = nbf.read(path, as_version=4)
        prefix = path.name[:2]
        setup = next(cell for cell in nb.cells if cell.cell_type == "code")
        if "configure_style()" not in setup.source:
            setup.source += "\nfrom aiv.notebook_materials import configure_style, save_figure\nconfigure_style()"
        figure = 0
        heading = path.stem
        for cell in nb.cells:
            if "final-analysis-v1" in cell.metadata.get("tags", []):
                continue
            if cell.cell_type == "markdown" and cell.source.startswith("## "):
                heading = cell.source.splitlines()[0].lstrip("# ")
            if cell.cell_type == "code" and "plt.show()" in cell.source:
                figure += 1
                if prefix == "03" and "'aiv/calibration.py'" not in cell.source:
                    cell.source = cell.source.replace("sources=['aiv/data.py'", "sources=['aiv/calibration.py', 'aiv/data.py'")
                if prefix == "06" and "plt.annotate(" not in cell.source:
                    cell.source = cell.source.replace("plt.tight_layout()", "plt.annotate('Unidentifiable', (4, 2), ha='center', fontsize=9)\nplt.tight_layout()")
                if "save_figure(" not in cell.source:
                    sources = ["aiv/data.py", "aiv/analysis.py", "aiv/metrics.py"] + extra.get(prefix, [])
                    export = f"save_figure(plt.gcf(), 'synthetic-{prefix}-{figure:02d}', {('合成实验：' + heading)!r}, kind='synthetic', sources={sources!r}, notebook={path.name!r})\nplt.show()"
                    cell.source = cell.source.replace("plt.show()", export)
            if cell.cell_type == "markdown" and cell.source.startswith(("## Next Steps", "## 材料衔接", "## 继续研究")):
                if prefix == "03":
                    cell.source = "## 材料衔接\n本页将校准方法的合成实验与同题人工复核记录分列。总体误差校准仍需独立参考标签及相应抽样设计。"
                elif prefix == "02":
                    cell.source = "## 材料衔接\n本页重放合成缓存，并读取固定随机子集的真实流程比较；09 本继续展示全回合分布与同回合双维候选。"
        nb.metadata["material_role"] = "synthetic_tutorial"
        nb.metadata["live_api"] = False
        nbf.validate(nb)
        nbf.write(nb, path)


def build():
    update_tutorials()
    write("00_论文材料总览.ipynb", [
        md("""
        # 面向 AI 辅助学习的增量价值评价：Notebook 复现材料
        ## 当前结论
        这组 Notebook 将原始证据、认知标注、识别边界、指标、反例和教育建议串成可离线复算的 A–E 链。
        模型与人审观察固定至 t3 及同题复核记录；`results/final-analysis/summary.json` 与十张聚合 CSV 连接各章。
        未完成结果保持空值，合成实验、真实观察与条件推演分别标记。

        ## 范围与方法
        阅读顺序：**00 → 01 → 08 → 02 → 09 → 03 → 04 → 05 → 06 → 07**。
        01/08 建立源记录与回合分母，02/09 建立 A 的候选与误差，03 将误差条件化，04 界定 B 的可估计量，05 建立 C，06 用 D 回验，07 生成 E 的条件性行动。
        合成实验与真实观察分开标注。Notebook 默认离线运行，每本使用独立内核。
        ### 关键假设
        日志的任务要求层级不能直接解释成学生能力或学习增量。各学期的观察窗口和可用字段不同。
        """), code(SETUP),
        md("## 数据与完成状态"), code('''
        evidence = research_inputs()
        archive = final_archive_summary()
        turns = turn_manifest_summary()
        batch = load_turn_snapshot()
        states = pd.DataFrame([
            {"材料": "真实首问记录", "状态": "已接入" if evidence else "本机无私有输入", "数量": int(evidence["terms"].text_records.sum()) if evidence else pd.NA, "用途": "原始记录范围"},
            {"材料": "恢复的可测评学生回合", "状态": "已校验来源" if turns else "待接入", "数量": turns["counts"]["model_eligible_student_turns"] if turns else pd.NA, "用途": "A 的测评单位"},
            {"材料": "人工最终核验", "状态": archive["status"] if archive else "本机无封存导出", "数量": archive["completed"] if archive else pd.NA, "用途": "规则与来源记录"},
            {"材料": "两快全量与两强独立复核", "状态": "冻结快照：" + batch["status"] if batch else "待冻结接入", "数量": batch["completed_real"] if batch else pd.NA, "用途": "A 的候选结果；未完成仍留空"},
            {"材料": "合成方法演示", "状态": "可复现", "数量": 7, "用途": "01–07 本的方法与性质实验"},
        ])
        display(states.fillna("—"))
        export_table(states, "material-status")
        '''),
        md("## 结果与论文位置"), code('''
        sections = pd.DataFrame([
            ["数据与观察范围", "01 / 08", "原始行→回合映射、时间和字段可用性", "真实聚合数据与来源哈希"],
            ["A：认知证据", "02 / 09", "两快候选、两强独立复核、覆盖与弃权", "批量结果以冻结范围为准"],
            ["人工记录与校准", "03 / 08", "封存状态、残差估计与覆盖率模拟", "真实校准值留空"],
            ["B：增量识别", "04", "已知真值模拟、真实识别条件", "学习增量当前不可识别"],
            ["C：指标与合成", "05", "权重、标签依赖和分数范围", "合成实验"],
            ["D：独立回验", "06", "固定标签操纵和文本对照缓存", "反例与失败范围"],
            ["E：教育决策", "07", "使用范围、复现和行动示例", "条件性建议"],
        ], columns=["论文部分", "Notebook", "图表内容", "证据状态"])
        display(sections)
        export_table(sections, "paper-section-map")
        '''),
        md("## 图表与复现入口\n执行脚本会生成带筛选器的离线图表目录 `build/notebooks/index.html`，以及每张图的 PDF、SVG、PNG、来源摘要和稳定图号。真实月度图另有可筛选与缩放的离线 HTML。"),
        code('''
        import platform
        from importlib.metadata import version
        environment = {"python": platform.python_version(), "seed": 26, "live_api": LIVE_API,
                       **{name: version(name) for name in ["pandas", "numpy", "matplotlib", "nbformat", "nbclient", "plotly"]}}
        display(pd.DataFrame(environment.items(), columns=["项目", "版本 / 参数"]))
        assert LIVE_API is False
        '''),
        md("""
        ## 使用说明
        在项目根目录执行 `.venv\\Scripts\\python.exe -X utf8 -B scripts/reproduce.py --notebooks`。
        修改生成内容后运行 `scripts/build_research_notebooks.py`，该入口会同步挂载真实聚合分析与现场合成两例，再执行全部 Notebook。
        离线执行不读取 `.env`；付费模型调用仅通过独立的 `scripts/run_full_turns.py` 入口。
        结果接口见 `notebooks/README.md`。本机缺少私有输入时，08 本展示缺失状态，01–07 的方法演示仍可运行。
        """),
    ])
    write("08_真实数据与封存记录.ipynb", [
        md("""
        # 真实数据与封存记录
        ## 当前结论
        本页从本机研究输入计算记录规模、回合恢复、观察时间和字段可用性，只展示聚合结果。
        最终人工核验作为已封存的方法记录读取，展示其状态与判定规则。

        ## 范围与方法
        原始子集为每行问答记录中的**首个文本问题**。另用行内 Q/A 标记恢复候选回合，每个 CSV 行是一个候选会话，不跨行拼接。
        25f 的范围为 `[2025-09-01, 2026-03-01)`，26s 为 `[2026-03-01, 2026-09-01)`；
        26s 同时按提供的学生名单筛选。去除重复行、无文本或仅链接的提问。
        ### 关键假设
        名单是范围筛选条件；学生数按本地哈希标识去重，各学期人数不应直接相加为跨期去重总人数。
        过滤诊断可能重叠，因此不绘制把所有排除原因相加的漏斗图。
        Q/A 角色由结构标记推断，逐回合时间未知；候选会话不能直接解释为完整连续互动。
        """), code(SETUP),
        md("## 数据与来源校验"), code('''
        evidence = research_inputs()
        SOURCES = ["runtime/research/records.json", "runtime/research/data-audit.json"]
        if evidence is None:
            display(Markdown("**待接入：本机没有私有文本索引及审计文件。以下真实数据图保持空白。**"))
        else:
            display(evidence["terms"])
            display(pd.DataFrame(evidence["sources"]))
            export_table(evidence["terms"], "real-term-summary")
            display(Markdown(f"共 **{evidence['terms'].text_records.sum():,} 条**文本记录，跨学期去重后 **{evidence['unique_students_all_terms']} 个**学生标识。输入的原始文件哈希与过滤记录数量已校验。"))
        '''),
        md("## 从原始记录到候选回合"), code('''
        turns = turn_manifest_summary()
        if turns is None:
            display(Markdown("**待接入：多轮解析清单不存在。**"))
        else:
            counts = turns["counts"]
            recovery = pd.DataFrame([
                {"单位": "选入的原始 CSV 行", "数量": counts["selected_records"], "解释": "记录，非回合"},
                {"单位": "学生 Q 回合", "数量": counts["student_turns"], "解释": "含结构待核的学生文本"},
                {"单位": "可测评学生回合", "数量": counts["model_eligible_student_turns"], "解释": "当前 API 清单分母"},
                {"单位": "助手 A 回合", "数量": counts["assistant_turns"], "解释": "只作上下文与来源核查"},
                {"单位": "含多个 Q 的记录", "数量": counts["records_with_multiple_questions"], "解释": "多轮恢复入口"},
                {"单位": "需人工核对结构的记录", "数量": counts["records_needing_review"], "解释": "不自动确认为连续会话"},
            ])
            display(recovery)
            display(pd.DataFrame([{"来源": turns["source"], "SHA-256": turns["sha256"]}]))
            export_table(recovery, "turn-recovery")
            by_term = pd.DataFrame([{"学期": term, **values}
                                    for term, values in counts["by_term"].items()])
            display(by_term)
            export_table(by_term, "turn-recovery-by-term")
            fig, ax = plt.subplots(figsize=(8.8, 4.4))
            positions = np.arange(len(by_term))
            source_bars = ax.bar(positions - .18, by_term["selected_records"], width=.36,
                                 color="#7892A6", label="原始 CSV 行")
            turn_bars = ax.bar(positions + .18, by_term["model_eligible_student_turns"], width=.36,
                               color=[COLORS[t] for t in by_term["学期"]], label="可测评学生回合")
            ax.bar_label(source_bars, padding=4)
            ax.bar_label(turn_bars, padding=4)
            ax.set_xticks(positions, by_term["学期"])
            ax.set(ylabel="数量（不同分析单位）", ylim=(0, by_term["model_eligible_student_turns"].max()*1.15),
                   title="原始记录与恢复回合的规模")
            ax.legend(frameon=False)
            fig.tight_layout()
            save_figure(fig, "real-turn-recovery",
                        "各学期原始记录与可测评学生回合；两组柱为不同分析单位。",
                        kind="real", sources=[turns["source"]],
                        notebook="08_真实数据与封存记录.ipynb")
            plt.show()
        '''),
        md("行内位置可追溯到源文件、原始行号和字符区间；重复问句可能是合法的不同回合，不据此自动去重。DOCX 暂无可信 CSV 连接键。"),
        md("## 结果：学期记录规模"), code('''
        if evidence is not None:
            term = evidence["terms"]
            fig, ax = plt.subplots(figsize=(8.8, 4.2))
            bars = ax.barh(term.term, term.text_records, color=[COLORS[t] for t in term.term], height=.5)
            ax.bar_label(bars, labels=[f"{n:,} 条" for n in term.text_records], padding=8)
            ax.set(xlim=(0, term.text_records.max()*1.22), xlabel="纳入的文本记录数（条）", ylabel="学期",
                   title=f"真实文本子集：{term.text_records.sum():,} 条记录")
            ax.invert_yaxis()
            fig.tight_layout()
            save_figure(fig, "real-term-counts", "真实文本子集按学期分布；记录数不是学习效果。", kind="real", sources=SOURCES, notebook="08_真实数据与封存记录.ipynb")
            plt.show()
        '''),
        md("规模差异描述当前纳入的日志，不能据此比较两学期教学效果。时间覆盖不同，下一图按自然月展开。"),
        md("## 结果：观察时间分布"), code('''
        if evidence is not None:
            monthly = evidence["monthly"]
            fig, axes = plt.subplots(1, 2, figsize=(10, 4.5), sharey=True)
            for ax, (term, group) in zip(axes, monthly.groupby("term")):
                bars = ax.bar(group.month, group.records, color=COLORS[term])
                ax.bar_label(bars, padding=3, fontsize=9)
                ax.set(title=f"{term} · {group.records.sum():,} 条", xlabel="自然月", ylim=(0, monthly.records.max()*1.18))
                ax.set_xticks(range(len(group)), [month.replace("-", "\\n") for month in group.month])
            axes[0].set_ylabel("文本记录数（条）")
            fig.suptitle("真实文本记录的月度分布", fontsize=14)
            fig.tight_layout()
            save_figure(fig, "real-monthly", "各学期实际观察月内的记录数；首末月为部分观察月，纵轴相同。", kind="real", sources=SOURCES, notebook="08_真实数据与封存记录.ipynb")
            plt.show()
            export_table(monthly, "real-monthly")
            offline_monthly(monthly)
            display(Markdown("离线交互图已生成：`build/notebooks/interactive/real-monthly.html`，可点击图例筛选、框选缩放。"))
        '''),
        md("月度数量保留首末观察月；不同月的记录量不作按天标准化后的使用率解释。图表没有跨越两学期空窗连线。"),
        md("## 结果：字段支持哪些分析"), code('''
        if evidence is not None:
            availability = evidence["availability"]
            fields = ["提问文本", "时间戳", "智能体类型", "会话边界"]
            matrix = availability.pivot(index="term", columns="field", values="fraction").reindex(columns=fields)
            fig, ax = plt.subplots(figsize=(8.8, 3.8))
            ax.imshow(matrix, cmap="Blues", vmin=0, vmax=1, aspect="auto")
            ax.set_xticks(range(len(fields)), fields)
            ax.set_yticks(range(len(matrix)), matrix.index)
            ax.set_title("字段可用性：有记录的条数 / 本学期文本记录数")
            for i, term in enumerate(matrix.index):
                for j, field in enumerate(fields):
                    row = availability[(availability.term == term) & (availability.field == field)].iloc[0]
                    ax.text(j, i, f"{row.available}/{row.denominator}\\n{row.fraction:.0%}", ha="center", va="center", color="white" if row.fraction > .5 else "#273949")
            fig.tight_layout()
            save_figure(fig, "real-field-availability", "原始索引字段可用性；行内候选会话另由结构解析，26s 缺少智能体类型。", kind="real", sources=SOURCES, notebook="08_真实数据与封存记录.ipynb")
            plt.show()
            export_table(availability, "real-field-availability")
        '''),
        md("上图显示原始索引字段，故会话边界栏仍为 0%；行内 Q/A 可建立候选会话，但逐回合标签与边界仍需验证后才能计算 CTQ。26s 的工具广度 MAB 与完整五维 AIV 保持空值。"),
        md("## 已封存的人工记录"), code('''
        archive = final_archive_summary()
        if archive is None:
            display(Markdown("本机没有最终封存导出，封存摘要暂留空。"))
        else:
            display(pd.DataFrame(archive.items(), columns=["字段", "值"]))
            export_table(pd.DataFrame([archive]), "human-archive-status")
            assert archive["completed"] == archive["total"]
        '''),
        md("## 可用于论文的内容\n本页支持数据来源、过滤范围、回合恢复质量、字段缺口和人工记录封存方式的描述。模型标签及独立复核结果由 09 本接入；本页不据封存数量推断标注准确率。"),
    ])
    write("09_Agent批量结果与互评.ipynb", [
        md("""
        # Agent 批量结果与独立复核
        ## 当前状态
        两个快速模型独立标注全部符合条件的学生回合；两个强模型在固定分层随机子集和高风险回合独立复核。
        原始输入部分读取显式选定、哈希固定的 `frozen` 快照；后半部读取 `results/final-analysis/` 的聚合表。所有单元离线计算，不发起模型调用。

        ## 范围与方法
        单位为 08 本定义的学生 Q 回合；12 条工程合成控制与真实回合分列。若冻结时仍有未完成任务，
        所有比例明确保留计划分母和已完成分母，未完成不会计入弃权。
        ### 关键假设
        一致性只在同题、双模型均产生结构化输出的配对子集计算；另列双方都有数字标签的分母。
        强模型的复核子集受到风险路由与分层抽样影响，不能把两组一致率差写成互评收益或准确率变化。
        """), code(SETUP),
        md("## 冻结证据入口"), code('''
        batch = load_turn_snapshot()
        if batch is None:
            display(Markdown("**待冻结接入：`notebooks/research-inputs.json` 的 `turn_snapshot` 尚为空。以下真实测评结论留空。**"))
            planned = load_turn_plan()
            if planned is not None:
                display(pd.DataFrame([{"学期": term, "计划真实回合": n,
                                       "已完成真实回合": pd.NA, "状态": "待正式冻结"}
                                      for term, n in planned["term_counts"].items()]))
                display(Markdown(f"固定计划：{planned['planned_real']:,} 个真实回合，"
                                 f"{planned['planned_controls']} 条控制；复跑计划 "
                                 f"{planned['repeat_expected_tasks']} 次请求。未完成数和弃权数待冻结核定。"))
        else:
            display(pd.DataFrame([{"批次": batch["experiment"], "修订": batch["revision"],
                                   "冻结状态": batch["status"], "已完成真实回合": batch["completed_real"],
                                   "计划真实回合": batch["planned_real"],
                                   "快照清单": batch["source"], "清单 SHA-256": batch["source_sha256"]}]))
            display(Markdown("**解释口径：**下列表格只描述模型候选标签、弃权、执行与配对一致性；并非独立人工真值或学习效果。"))
        '''),
        md("## A：覆盖、未完成与候选标签"), code('''
        coverage = pd.DataFrame(columns=["term", "planned", "completed", "candidate_labeled", "unknown_or_abstained", "unfinished", "completed_fraction", "labeled_among_completed"])
        labels = pd.DataFrame(columns=["label", "completed_records"])
        if batch is not None:
            coverage = pd.DataFrame(batch["by_term"])
            coverage["completed_fraction"] = coverage.completed / coverage.planned
            coverage["labeled_among_completed"] = coverage.candidate_labeled / coverage.completed.replace(0, np.nan)
            labels = pd.DataFrame([{"label": "候选 L" + label if label != "None" else "弃权/未知",
                                    "completed_records": n} for label, n in batch["label_counts"].items()])
            labels = labels.sort_values("label").reset_index(drop=True)
        display(coverage)
        display(labels)
        export_table(coverage, "agent-coverage")
        export_table(labels, "agent-labels")
        if batch is not None:
            fig, ax = plt.subplots(figsize=(8.8, 4.4))
            by_term = coverage.set_index("term")
            x = np.arange(len(by_term))
            ax.bar(x - .18, by_term.planned, width=.36, color="#D9E0E4", label="计划回合（含未完成）")
            ax.bar(x + .18, by_term.completed, width=.36, color="#176B77", label="已完成回合")
            ax.set_xticks(x, by_term.index)
            for i, row in enumerate(coverage.itertuples()):
                ax.text(i, row.planned + max(.1, coverage.planned.max()*.02),
                        f"{row.completed}/{row.planned}", ha="center")
            ax.set(ylim=(0, max(coverage.planned)*1.16), ylabel="学生 Q 回合数",
                   title="冻结快照的完成范围（分母为全部计划回合）")
            ax.legend(frameon=False)
            fig.tight_layout()
            save_figure(fig, "real-turn-batch-coverage", "两学期的计划与已完成回合；未完成不计入弃权。",
                        kind="real", sources=[batch["source"]], notebook="09_Agent批量结果与互评.ipynb")
            plt.show()
        '''),
        md("候选标签分布仅以已完成回合为范围；弃权/未知单列。任何未完成回合都保留在计划分母中，不能据已完成子集推断全体比例。"),
        md("## A：独立模型与复核配对"), code('''
        paired = pd.DataFrame(columns=["stage", "dimension", "paired_structured", "same_including_abstain", "paired_labeled", "same_labeled", "labeled_agreement"])
        stages = pd.DataFrame(columns=["stage", "requested", "valid", "failed", "unfinished"])
        if batch is not None:
            paired = pd.DataFrame(batch["agreement"])
            stages = pd.DataFrame([{"stage": stage, **values,
                                    "unfinished": values["requested"] - values["valid"] - values["failed"]}
                                   for stage, values in batch["stages"].items()])
        display(paired)
        display(stages)
        export_table(paired, "agent-independent-review")
        export_table(stages, "agent-stage-coverage")
        if batch is not None and paired.paired_labeled.max() > 0:
            fig, ax = plt.subplots(figsize=(9.2, 4.5))
            subset = paired[paired.dimension.eq("task")]
            xpos = np.arange(len(subset))
            ax.bar(xpos, subset.paired_labeled, color=["#176B77", "#BC632F"])
            for x, row in zip(xpos, subset.itertuples()):
                ax.text(x, row.paired_labeled + max(1, subset.paired_labeled.max()*.04),
                        f"一致 {row.same_labeled}/{row.paired_labeled}", ha="center")
            ax.set_xticks(xpos, ["两快独立判断", "两强独立复核"])
            ax.set(ylim=(0, subset.paired_labeled.max()*1.23),
                   ylabel="双方均给数字标签的配对回合数", title="同一阶段内的数字标签配对分母")
            fig.tight_layout()
            save_figure(fig, "real-paired-label-coverage", "同阶段双方均给数字任务标签的配对数；强模型子集按抽样和风险路由选择。",
                        kind="real", sources=[batch["source"]], notebook="09_Agent批量结果与互评.ipynb")
            plt.show()
        '''),
        md("两强模型处理的是选定子集，而非全部回合；上图比较分母与一致数量，不作阶段间因果比较。双方同弃权计入结构化一致列，但不计入数字标签一致率。"),
        md("## D：重复稳定性与调用账本"), code('''
        repeat = pd.DataFrame(columns=["model", "dimension", "comparable", "equal", "stability"])
        execution = pd.DataFrame(columns=["metric", "value"])
        if batch is not None:
            repeat = pd.DataFrame(batch["repeat"])
            ledger = batch["ledger"]
            execution = pd.DataFrame([
                ["API 尝试数（账本合计）", sum(x["attempts"] for x in ledger)],
                ["已报告输入 token", sum(x["input_tokens"] or 0 for x in ledger)],
                ["已报告输出 token", sum(x["output_tokens"] or 0 for x in ledger)],
                ["用量未知的尝试数", sum(x["unknown_usage_attempts"] for x in ledger)],
                ["工程合成控制完成数", batch["completed_controls"]],
                ["工程合成控制计划数", batch["planned_controls"]],
                ["预选随机复核回合", batch["review_random_preselected"]],
                ["最终纳入复核回合", batch["review_selected_real"]],
            ], columns=["metric", "value"])
        display(repeat)
        display(execution)
        export_table(repeat, "agent-repeat-stability")
        export_table(execution, "agent-execution")
        '''),
        md("重复稳定性仅对同模型同题两次都有结构化输出者计算。token 合计只覆盖服务方报告用量的尝试，未核价格不填写货币成本。合成控制用于工程合同检查，不当作人工金标准。"),
        md("## 冻结派生分析：状态、复核分层与评分可用性"), code('''
        materials = load_full_turn_materials()
        if materials is None:
            display(Markdown("派生分析包待接入。学生级指标和分层复核结果尚不填入。"))
        else:
            summary = materials["summary"]
            denominators = pd.DataFrame(summary["denominators"].items(), columns=["口径", "数量"])
            display(denominators)
            export_table(denominators, "full-turn-denominators")
            fast_agreement = pd.DataFrame([{"维度": dimension,
                "双有效": values["both_valid"], "双非空": values["both_nonnull"],
                "学生簇": values["student_clusters_dual_nonnull"],
                "六级κ": values["kappa6"], "六级κ区间": values["kappa6_cluster_interval95"],
                "三阶κ": values["kappa3"], "三阶κ区间": values["kappa3_cluster_interval95"]}
                for dimension, values in summary["independent_agreement"].items()])
            display(fast_agreement)
            export_table(fast_agreement, "full-turn-fast-agreement")
            display(Markdown("两快一致性使用全部真实回合的双有效输出；不要求后续复核和复跑均已结算。κ 的分母为双方非空标签，区间按学生整簇重采样。相同输入的精确缓存可能使不同学生共享同一次模型响应；该区间未消除这类跨簇依赖，不能视为独立调用或准确率的置信区间。跨学生缓存链接数量以独立缓存核验收据为准。"))
            status_names = {"agreed": "候选", "abstained": "一致弃权", "disagreement": "分歧",
                            "uncertain": "不确定", "technical_failure": "技术失败"}
            status = pd.DataFrame([{"状态": label,
                "任务": summary["final_task_status_counts"].get(key, 0),
                "贡献": summary["final_contribution_status_counts"].get(key, 0)}
                for key, label in status_names.items()])
            assert status.任务.sum() == status.贡献.sum() == summary["denominators"]["completed_real"]
            display(status)
            export_table(status, "full-turn-final-status")
            fig, ax = plt.subplots(figsize=(9.2, 4.7))
            x = np.arange(len(status))
            a = ax.bar(x-.18, status.任务, width=.36, color="#176B77", label="任务要求")
            b = ax.bar(x+.18, status.贡献, width=.36, color="#BC632F", label="学生贡献")
            ax.bar_label(a, padding=3, fontsize=9)
            ax.bar_label(b, padding=3, fontsize=9)
            ax.set_xticks(x, status.状态)
            ax.set(ylabel="已完成真实回合数", title="任务与贡献的完成状态；未完成不在本图分母中")
            ax.set_ylim(0, max(status.任务.max(), status.贡献.max(), 1)*1.18)
            ax.legend(frameon=False)
            fig.tight_layout()
            save_figure(fig, "real-full-turn-final-status", "两个维度各以已完成真实回合为分母；技术失败与弃权分列。",
                        kind="real", sources=[materials["source"], batch["source"]], notebook="09_Agent批量结果与互评.ipynb")
            plt.show()
        '''),
        md("## 分层复核及复跑的固定请求分母"), code('''
        if materials is not None:
            review_rows = []
            names = {"random_audit": "分层随机复核", "high_risk": "高风险复核", "control": "工程控制"}
            for key, name in names.items():
                item = materials["review_strata"]["strata"][key]
                task = item["review_tasks"]
                agreement = item["agreement"]["task"]
                review_rows.append({"来源": name, "计划回合": item["planned_records"],
                                    "应有请求": task["expected"], "有效输出": task["valid"],
                                    "失败": task["failed"], "排队或运行": task["pending"],
                                    "未创建": task["not_created"], "双非空": agreement["both_nonnull"],
                                    "六级κ": agreement["kappa6"], "三阶κ": agreement["kappa3"]})
            review_view = pd.DataFrame(review_rows)
            display(review_view)
            export_table(review_view, "full-turn-review-strata")
            repeats = materials["repeatability"]
            display(pd.DataFrame([{"计划回合": repeats["planned_records"], "应有请求": repeats["expected_tasks"],
                                   **repeats["tasks"]}]))
            repeat_rows = []
            for model, item in repeats["by_model"].items():
                for dimension in ("task", "contribution"):
                    repeat_rows.append({"模型": model, "维度": dimension, **item[dimension]})
            repeat_view = pd.DataFrame(repeat_rows)
            display(repeat_view)
            export_table(repeat_view, "full-turn-repeat-fixed-denominator")
            availability = pd.DataFrame(materials["summary"]["score_availability"].items(), columns=["字段", "可用行数"])
            display(availability)
            export_table(availability, "full-turn-score-availability")
        '''),
        md("随机复核用于既定抽样范围的检验；高风险子集包含选取机制，单独报告。复跑应有请求固定为预选回合×两个快模型，未创建请求不消失。候选会话尚不等于验证过的相邻边，完整 AIV 与排名仍由字段可用性决定。"),
        md("## B/C/E：下游结论边界"), code('''
        pending = pending_results(batch, materials)
        pending_view = pending[["item", "status", "estimate", "denominator", "dependency"]].rename(columns={"item": "结果", "status": "状态", "estimate": "数值", "denominator": "分母", "dependency": "依据或下一项条件"})
        display(pending_view.astype(object).where(pending_view.notna(), "—"))
        export_table(pending, "pending-results")
        if batch is None:
            assert coverage.empty and paired.empty and repeat.empty and execution.empty
            assert pending.estimate.isna().all() and pending.denominator.isna().all()
        assert LIVE_API is False
        '''),
        md("候选标签从 A 输入 B/C，但不能填补独立学习结果、可比对照或缺失工具字段。C 的完整真实 AIV 与排名仍需各指标可观测及误差界；E 只能生成与证据范围相称的建议。"),
    ])

    from scripts.finalize_notebook_narrative import finalize
    finalize(ROOT)


if __name__ == "__main__":
    build()
    print("Prepared 00, 08, 09 and traceable figure exports for 01–07.")
