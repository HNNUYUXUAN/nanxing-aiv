"""Build reviewer notebooks from public aggregates, with optional authorized detail."""
import argparse
import hashlib
import json
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
    for position, cell in enumerate(cells):
        cell.id = hashlib.sha256(f"{name}:{position}:{cell.cell_type}:{cell.source}".encode("utf-8")).hexdigest()[:12]
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


PUBLIC_LOAD = """
PUBLIC_DIR = ROOT / "results" / "final-analysis"
public_summary = json.loads((PUBLIC_DIR / "summary.json").read_text(encoding="utf-8"))
public_counts = public_summary["denominators"]
public_source = public_summary["source"]
public_descriptive = pd.read_csv(PUBLIC_DIR / "descriptive.csv")
"""


def intro(source):
    cell = md(source)
    cell.metadata["tags"] = ["reviewer-intro"]
    return cell


def public_load():
    cell = code(PUBLIC_LOAD)
    cell.metadata["tags"] = ["reviewer-public-load"]
    return cell


def public_overview_cells():
    return [
        intro("""
        # 南行 · 评审阅读与复算

        从公开聚合结果核对论文结论，再运行合成实验检查指标与报告的行为。
        本组材料包含真实观察、条件范围和固定种子实验；每类结果保留自己的分母与解释范围。
        """),
        code(SETUP), public_load(),
        md(r"""
        ## 开始复算

        1. 在项目 Python 环境中打开本页，选择 **Restart Kernel and Run All Cells**。
        2. 下方检查通过后，按阅读路线打开关注的章节；各本可独立从头运行。
        3. 一次执行十本并导出离线 HTML，在项目根目录运行：

        ```powershell
        .venv\Scripts\python.exe -X utf8 -B scripts/execute_notebooks.py --workers 1
        ```

        输出入口为 [离线图文目录](../build/notebooks/index.html)，执行结果见 `build/notebooks/execution.json`。
        环境安装与论文图重绘步骤见 [复现说明](../docs/复现说明.md)。本组单元使用仓库内文件离线计算。
        """),
        md("## 核对公开结果"),
        code("""
        from scripts.reproduce import verify_aggregates

        checks = verify_aggregates(public_summary)
        overview = pd.DataFrame([
            ["真实学生 Q 回合", public_counts["real_turns"], "回合"],
            ["学生—学期", public_counts["student_terms"], "学生 × 学期"],
            ["任务层级候选", public_counts["task_candidates"], "回合；按任务维度统计"],
            ["贡献层级候选", public_counts["contribution_candidates"], "回合；按贡献维度统计"],
            ["同回合双维共同候选", public_summary["joint"]["paired_denominator"], "回合；两维均有候选"],
            ["完整真实 AIV 可计算量", public_counts["full_AIV_available"], "学生—学期"],
        ], columns=["核对项目", "数量", "单位 / 范围"])
        display(overview)
        assert all(checks.values())
        display(Markdown("**检查通过**：状态分母守恒；未知标签范围合法；扰动预算增加时范围与稳定配对按预期变化。"))
        export_table(overview, "material-status")
        """),
        md("""
        候选层级描述可观测任务与贡献，不能直接解释为学生能力或学习增量。
        完整 AIV 当前缺少所需字段；[05](05_指标性质与不确定性.ipynb) 展示给定假设下的条件范围，
        [07](07_教育报告与复用.ipynb) 展示完整与缺证两例的实际计算及行动建议。

        ## 选择阅读路线

        **快速核验：00 → 09 → 05 → 07。** 完整阅读按下表从上至下。

        | 打开章节 | 要核验的问题 | 输入与输出 |
        |---|---|---|
        | [01 数据与问题](01_数据与问题.ipynb) | 一行、回合和学生分别代表什么？ | 固定种子样例、数据字典 |
        | [08 真实数据与封存记录](08_真实数据与封存记录.ipynb) | 两学期覆盖多少记录，哪些字段缺失？ | 公开聚合表、来源版本 |
        | [02 标注与互评](02_标注与互评.ipynb) | 合并和复核怎样改变候选状态？ | 保存的合成缓存、352 回合固定随机子集 |
        | [09 批量结果与独立复核](09_Agent批量结果与互评.ipynb) | 任务与贡献候选怎样分布？ | 全回合聚合、48 条双维共同候选 |
        | [03 人审与校准](03_人审与校准.ipynb) | 人审耗时和判断发生了什么变化？ | 16 对同题同角色观察、校准模拟 |
        | [04 识别边界与模拟](04_识别边界与模拟.ipynb) | 现有日志能识别哪种增量？ | 已知真值模拟、时间条件检查 |
        | [05 指标性质与不确定性](05_指标性质与不确定性.ipynb) | 未知标签和缺失指标怎样影响分数？ | 条件范围、权重与改标敏感性 |
        | [06 红队与失败边界](06_红队与失败边界.ipynb) | 工具堆叠和文本扰动会怎样改变结果？ | 合成反例、保存的扰动结果 |
        | [07 教育报告与复用](07_教育报告与复用.ipynb) | 如何从可用证据生成下一步行动？ | 完整与缺证两例、三类报告 |
        """),
        md("## 确认来源与运行环境"),
        code("""
        import hashlib
        import platform
        from importlib.metadata import version

        source_receipt = pd.DataFrame([
            ["聚合文件", "results/final-analysis/summary.json"],
            ["冻结修订 / 状态", f"{public_source['revision']} / {public_source['snapshot_status']}"],
            ["聚合文件 SHA-256", hashlib.sha256((PUBLIC_DIR / "summary.json").read_bytes()).hexdigest()],
            ["冻结清单 SHA-256", public_source["manifest_sha256"]],
            ["Python / 随机种子", f"{platform.python_version()} / 26"],
            ["核心库", ", ".join(f"{name} {version(name)}" for name in ["numpy", "pandas", "matplotlib"])],
        ], columns=["项目", "本次运行"])
        display(source_receipt)
        assert LIVE_API is False
        """),
    ]


def public_source_cells():
    return [
        intro("""
        # 08 · 真实数据的范围与可计算条件

        先核对学生 Q 回合、学生—学期和候选标签的分母，再检查时间与字段能支持哪些计算。
        本页直接读取已发布的 `summary.json` 和 `descriptive.csv`；来源对应 t3 冻结结果。
        """),
        code(SETUP), public_load(),
        md("""
        ## 先区分观察单位

        | 单位 | 在本项目中的含义 | 使用位置 |
        |---|---|---|
        | 源 CSV 行 | 一条来源记录；行内可包含多个 Q/A | 来源回查、候选会话恢复 |
        | 学生 Q 回合 | 行内恢复且满足测评条件的学生提问 | 双维标注、状态分布 |
        | 学生—学期 | 同一学期内同一学生的回合集合 | 学生等权描述、条件评分 |
        | 双维共同候选 | 同一回合在任务与贡献两维均有候选 | 任务与贡献的配对比较 |

        回合内 Q/A 角色由结构标记恢复，候选会话尚不等于已验证的连续互动。
        学期人数按各自范围统计，跨学期连续身份不能由人数相加推定。

        ## 核对学期规模
        """),
        code("""
        term_scope = public_descriptive.loc[
            public_descriptive.dimension.eq("task") & public_descriptive.term.ne("all"),
            ["term", "turns", "students", "candidate_turns", "candidate_students"],
        ].sort_values("term").reset_index(drop=True)
        contribution_scope = public_descriptive.loc[
            public_descriptive.dimension.eq("contribution") & public_descriptive.term.ne("all"),
            ["term", "turns", "students", "candidate_turns"],
        ].set_index("term")
        assert term_scope.turns.sum() == public_counts["real_turns"]
        assert term_scope.students.sum() == public_counts["student_terms"]
        assert all(contribution_scope.loc[row.term, "turns"] == row.turns for row in term_scope.itertuples())
        assert all(contribution_scope.loc[row.term, "students"] == row.students for row in term_scope.itertuples())
        scope_view = term_scope.rename(columns={
            "term": "学期", "turns": "学生Q回合", "students": "学生—学期",
            "candidate_turns": "任务候选回合", "candidate_students": "有任务候选的学生—学期",
        })
        scope_view["贡献候选回合"] = term_scope.term.map(contribution_scope.candidate_turns)
        display(scope_view)
        export_table(scope_view, "public-term-scope")

        fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
        for ax, field, title, unit in zip(
            axes, ["turns", "students"], ["符合测评条件的学生 Q 回合", "纳入范围的学生—学期"],
            ["回合数", "学生—学期数"],
        ):
            values = term_scope[field]
            bars = ax.bar(term_scope.term, values, color=[COLORS[t] for t in term_scope.term], width=.55)
            ax.bar_label(bars, labels=[f"{value:,}" for value in values], padding=4)
            ax.set(ylabel=unit, xlabel="学期", title=title, ylim=(0, values.max() * 1.2))
        fig.tight_layout()
        save_figure(fig, "final-public-term-scope", "各学期的学生Q回合与学生—学期规模；两图分别保留观察单位。",
                    kind="real", sources=["results/final-analysis/descriptive.csv", "results/final-analysis/summary.json"],
                    notebook="08_真实数据与封存记录.ipynb")
        plt.show()
        """),
        md("""
        25f 选取 `[2025-09-01, 2026-03-01)`，26s 选取 `[2026-03-01, 2026-09-01)`；
        26s 同时按提供的学生名单筛选。选取流程排除重复行、无文本或仅链接的提问。
        两学期的纳入范围与字段条件不同，规模差异不能直接解释为教学效果差异。

        ## 检查时间与指标条件
        """),
        code("""
        association = public_summary["association"]
        baseline = next(row for row in public_summary["score_range_sensitivity"]
                        if row["term"] == "all" and row["scheme"] == "balanced"
                        and row["label_error_fraction"] == 0 and row["weight_relative_change"] == 0)
        availability = pd.DataFrame([
            ["已观测逐回合时间戳", association["observed_turn_timestamps"], public_counts["real_turns"], "回合", "检查前后顺序"],
            ["严格前瞻合格记录", association["eligible_records"], public_counts["real_turns"], "回合", "当前未拟合前瞻关联模型"],
            ["有任务候选的学生—学期", baseline["candidate_students"], public_counts["student_terms"], "学生—学期", "条件评分范围的对象"],
            ["缺少已验证 CTQ", baseline["missing_ctq_students"], baseline["candidate_students"], "学生—学期", "缺失项进入条件范围"],
            ["缺少工具身份", baseline["missing_tool_students"], baseline["candidate_students"], "学生—学期", "工具广度保持缺失"],
            ["完整 AIV 可计算量", public_counts["full_AIV_available"], public_counts["student_terms"], "学生—学期", "保留不可计算状态"],
        ], columns=["字段 / 条件", "数量", "分母", "单位", "计算含义"])
        display(availability)
        export_table(availability, "public-computability")
        assert association["observed_turn_timestamps"] == association["eligible_records"] == association["student_clusters"] == 0
        """),
        md("""
        源 CSV 的创建时间描述来源记录，不能代替每次提问的发生时间，也不能建立严格先后顺序。
        缺失指标继续保留为空；条件范围由明确假设下的极值组成。

        ## 追溯聚合结果
        """),
        code("""
        source_view = pd.DataFrame([
            ["聚合输入", "results/final-analysis/summary.json；descriptive.csv"],
            ["统计修订", public_source["revision"]],
            ["冻结状态", public_source["snapshot_status"]],
            ["冻结清单 SHA-256", public_source["manifest_sha256"]],
            ["标注文件 SHA-256", public_source["annotations_sha256"]],
        ], columns=["来源项目", "记录"])
        display(source_view)
        assert LIVE_API is False
        """),
    ]


def public_batch_cells():
    return [
        intro("""
        # 09 · 批量结果与独立复核

        核对全部回合的状态分母，比较任务与贡献的六级候选分布，再查看同一回合的双维配对。
        本页使用 t3 公开聚合表；[02](02_标注与互评.ipynb) 展示固定随机子集的同题流程变化。
        """),
        code(SETUP), public_load(),
        md("""
        ## 阅读口径

        - **边际分布**：任务与贡献分别使用自己的候选回合分母。
        - **同回合比较**：只使用两维均有候选的共同子集。
        - **复核选择**：固定随机子集与高风险子集分开报告。

        候选、弃权、分歧、不确定和技术失败各自保留数量。模型一致性描述输出关系；准确性仍需独立参考标签。
        """),
        code("""
        final_status = public_descriptive.loc[public_descriptive.term.eq("all"),
            ["dimension", "turns", "agreed", "abstained", "disagreement", "uncertain", "technical_failure"]].copy()
        status_columns = ["agreed", "abstained", "disagreement", "uncertain", "technical_failure"]
        assert final_status[status_columns].sum(axis=1).eq(final_status.turns).all()
        final_status["dimension"] = final_status.dimension.map({"task": "任务", "contribution": "贡献"})
        display(final_status.rename(columns={"dimension": "维度", "turns": "全部回合", "agreed": "候选",
            "abstained": "共同弃权", "disagreement": "分歧", "uncertain": "不确定", "technical_failure": "技术失败"}))
        export_table(final_status, "public-final-status")
        """),
    ]


def authorized_record_cells():
    """Retain the authorized analysis as an explicitly requested supplement."""
    cells = [
        md("## 数据与来源校验"),
        code('''
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
        md("## 从原始记录到候选回合"),
        code('''
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
        md("## 结果：学期记录规模"),
        code('''
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
        md("## 结果：观察时间分布"),
        code('''
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
        md("## 结果：字段支持哪些分析"),
        code('''
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
        md("## 已封存的人工记录"),
        code('''
                archive = final_archive_summary()
                if archive is None:
                    display(Markdown("本机没有最终封存导出，封存摘要暂留空。"))
                else:
                    display(pd.DataFrame(archive.items(), columns=["字段", "值"]))
                    export_table(pd.DataFrame([archive]), "human-archive-status")
                    assert archive["completed"] == archive["total"]
                '''),
    ]
    selected = []
    if all((ROOT / f"runtime/research/{name}").is_file() for name in ("records.json", "data-audit.json")):
        selected.extend(cells[:2])
    if (ROOT / "runtime/research/turns-v1/manifest.json").is_file():
        selected.extend(cells[2:5])
    if all((ROOT / f"runtime/research/{name}").is_file() for name in ("records.json", "data-audit.json")):
        selected.extend(cells[5:14])
    if (ROOT / "runtime/research/final-verification-export.json").is_file():
        selected.extend(cells[14:16])
    return authorized_group(selected)


def authorized_batch_cells():
    """Retain the authorized analysis as an explicitly requested supplement."""
    cells = [
        md("## 冻结证据入口"),
        code('''
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
                    display(Markdown("**解释口径**：下列表格只描述模型候选标签、弃权、执行与配对一致性；并非独立人工真值或学习效果。"))
                '''),
        md("## A：覆盖、未完成与候选标签"),
        code('''
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
        md("## A：独立模型与复核配对"),
        code('''
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
        md("## D：重复稳定性与调用账本"),
        code('''
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
        md("## 冻结派生分析：状态、复核分层与评分可用性"),
        code('''
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
        md("## 分层复核及复跑的固定请求分母"),
        code('''
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
        md("## B/C/E：下游结论边界"),
        code('''
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
    ]
    config = json.loads((ROOT / "notebooks/research-inputs.json").read_text(encoding="utf-8"))
    snapshot = config.get("turn_snapshot")
    plan = config.get("turn_plan")
    if not snapshot:
        return authorized_group(cells[:2]) if plan else []
    selected = cells[:11]
    if config.get("turn_materials"):
        selected.extend(cells[11:16])
    else:
        selected.append(code("materials = None"))
    selected.extend(cells[16:])
    return authorized_group(selected)


def authorized_group(cells):
    if not cells:
        return []
    heading = md("## 授权原始输入复算\n以下补充单元从显式配置的本地输入重算聚合结果，并核对来源哈希。")
    for cell in cells:
        if cell.cell_type == "markdown":
            cell.source = cell.source.replace("## ", "### ")
        cell.metadata["tags"] = ["authorized-source"]
    heading.metadata["tags"] = ["authorized-source"]
    return [heading, *cells]


def build(root=ROOT, *, include_authorized_sections=False):
    global ROOT, OUT
    ROOT = Path(root).resolve()
    OUT = ROOT / "notebooks"
    if not (ROOT / "results/final-analysis/summary.json").is_file():
        raise FileNotFoundError("The public aggregate results/final-analysis/summary.json is required")
    update_tutorials()
    write("00_论文材料总览.ipynb", public_overview_cells())
    records = public_source_cells()
    batch = public_batch_cells()
    if include_authorized_sections:
        records.extend(authorized_record_cells())
        batch.extend(authorized_batch_cells())
    write("08_真实数据与封存记录.ipynb", records)
    write("09_Agent批量结果与互评.ipynb", batch)
    from scripts.finalize_notebook_narrative import finalize
    finalize(ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository containing notebooks and public aggregate results")
    parser.add_argument("--include-authorized-sections", action="store_true",
                        help="Append source-level aggregate checks for explicitly configured local research inputs")
    args = parser.parse_args()
    build(args.root, include_authorized_sections=args.include_authorized_sections)
    print("Prepared 00, 08, 09 and updated 01–07. Run scripts/execute_notebooks.py to execute them.")


if __name__ == "__main__":
    main()
