"""Attach reproducible aggregate analysis and saved-label demonstrations to notebooks."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from textwrap import dedent

import nbformat as nbf

ROOT = Path(__file__).resolve().parents[1]
TAG = "final-analysis-v1"
VISUAL_TAG = "reviewer-visual-v1"
NEXT_TAG = "reviewer-next-v1"
MANAGED_TAGS = {TAG, VISUAL_TAG, NEXT_TAG}
CONCEPT_IDS = ["process-overview", "system-architecture", "model-review", "evidence-chain", "reproducibility"]


def md(text):
    return nbf.v4.new_markdown_cell(dedent(text).strip())


def code(text):
    return nbf.v4.new_code_cell(dedent(text).strip())


LOAD = '''
from pathlib import Path
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from IPython.display import display, Markdown
from aiv.notebook_materials import configure_style, save_figure
configure_style()
FINAL_DIR = ROOT / "results" / "final-analysis"
_final_summary_path = FINAL_DIR / "summary.json"
final_summary = json.loads(_final_summary_path.read_text(encoding="utf-8")) if _final_summary_path.is_file() else None

def final_csv(name):
    return pd.read_csv(FINAL_DIR / name) if final_summary is not None else pd.DataFrame()

if final_summary is None:
    display(Markdown("未找到公开汇总 `results/final-analysis/summary.json`；合成计算可独立运行。"))
else:
    display(pd.DataFrame([{
        "汇总版本": final_summary["source"]["revision"],
        "真实回合": final_summary["denominators"]["real_turns"],
        "学生—学期": final_summary["denominators"]["student_terms"],
    }]))
'''


def sections(prefix):
    if prefix == "00":
        # The overview is a launch page; each paper figure belongs beside its analysis.
        return []
    if prefix == "02":
        return [
            md("""
            ## 真实观察与前置探索：标注流程
            固定分层随机复核的 352 个回合支持同题比较：分别读取两个快模型、两快严格合并及最终复核合并的状态。
            候选、共同弃权、分歧、不确定和技术失败保留各自数量。高风险复核另有选择机制，在表中分列。
            来源：`results/final-analysis/flow-comparison.csv`、`flow-transitions.csv` 与 `summary.json`。
            """), code(LOAD), md("""
            ### 前置探索：12 题独立判断、互评与仲裁
            早期实验包含 6 条真实首问和 6 条人工构造样本，按原三票规则重算；两类样本分开统计。
            三个数字票齐备且至少两票相同才形成独立或互评多数票，有效仲裁覆盖互评票。
            """), code('''
            if final_summary is not None and "pilot_exploration" in final_summary:
                pilot = final_summary["pilot_exploration"]
                pilot_comparison = final_csv("pilot-exploration.csv")
                display(pilot_comparison.rename(columns={"kind": "样本类型", "method": "方法", "record_denominator": "题目分母", "candidate_count": "候选数", "no_candidate_count": "无候选数", "comparison_method": "比较基线", "label_or_null_changes": "标签或弃权变化数", "gained_candidate": "新增候选", "lost_candidate": "退出候选", "both_candidate_label_changes": "共同候选标签变化"}))
                pilot_stages = []
                for group in pilot["by_kind"]:
                    for stage, values in group["stages"].items():
                        pilot_stages.append({"样本类型": group["kind"], "阶段": stage, "题目分母": group["record_denominator"], "输出数": values["outputs"], "有效输出": values["valid"], "无效输出": values["invalid"]})
                display(pd.DataFrame(pilot_stages))
                display(pd.DataFrame([{"样本类型": group["kind"], "同模型独立—互评双有效配对": group["same_model_independent_peer_valid_pairs"], "标签或弃权变化": group["same_model_task_label_or_null_changes"]} for group in pilot["by_kind"]]))
            '''), md("""
            真实 6 题在单 Claude、独立票、互评票、最终裁定下的候选数为 3 → 1 → 2 → 2；合成 6 题各阶段均为 6。
            真实同模型双有效配对 17 对中改变 3 对，合成 18 对中改变 2 对。合成构造标签用于方法探索；它们不充当独立人工正确性基准。
            这些旧模型、提示与输出限制下的结果与下面的 t3 随机子集分别阅读。

            ### t3：固定随机子集的同题状态
            """), code('''
            if final_summary is not None:
                flow = final_csv("flow-comparison.csv")
                methods = ["deepseek-v4.1-flash", "glm-5.3-flash", "fast_strict", "final_review_merge"]
                method_names = ["DeepSeek 单模型", "GLM 单模型", "两快严格合并", "最终复核合并"]
                status_labels = {"agreed": "候选", "abstained": "弃权", "disagreement": "分歧", "uncertain": "不确定", "technical_failure": "技术失败"}
                status_colors = ["#176B77", "#D2DCE0", "#BC632F", "#DDAE57", "#765D91"]
                fig, axes = plt.subplots(1, 2, figsize=(13, 4.9), sharex=True)
                for ax, dimension, title in zip(axes, ["task", "contribution"], ["任务要求", "学生贡献"]):
                    selected = flow.loc[flow.stratum.eq("random_audit") & flow.dimension.eq(dimension)].set_index("method").loc[methods]
                    assert selected.turn_denominator.nunique() == 1
                    assert selected[list(status_labels)].sum(axis=1).eq(selected.turn_denominator).all()
                    left = np.zeros(len(selected))
                    for (status, label), color in zip(status_labels.items(), status_colors):
                        values = selected[status].to_numpy()
                        ax.barh(range(4), values, left=left, color=color, label=label)
                        for y, value, start in zip(range(4), values, left):
                            if value >= 15:
                                ax.text(start + value / 2, y, str(int(value)), ha="center", va="center", fontsize=9,
                                        color="white" if status == "agreed" else "#202629")
                        left += values
                    ax.set_yticks(range(4), method_names)
                    ax.invert_yaxis()
                    ax.set(xlim=(0, int(selected.turn_denominator.iloc[0])), xlabel="同题回合数", title=f"{title} · 分母 {int(selected.turn_denominator.iloc[0])}")
                handles, labels = axes[0].get_legend_handles_labels()
                fig.legend(handles, labels, loc="lower center", ncol=5, frameon=False)
                fig.tight_layout(rect=(0, .09, 1, 1))
                save_figure(fig, "final-random-review-flow", "固定随机复核子集的同题状态比较；每种方法、每个维度均保留全部352回合。", kind="real", sources=["results/final-analysis/flow-comparison.csv", "results/final-analysis/summary.json"], notebook="02_标注与互评.ipynb")
                plt.show()
                display(flow[["stratum", "dimension", "method", "turn_denominator", "agreed", "candidate_coverage", "gained_candidate", "lost_candidate", "final_fast_fallback"]].round(3))
            '''), md("""
            ### 同题状态怎样变化
            随机子集的任务候选由两快严格合并的 110 条变为最终合并的 86 条：原候选保留 79 条，新增 7 条，退出候选 31 条。
            这是相同回合在给定合并规则下的输出变化；评价准确性仍需独立参考标签。
            """), code('''
            if final_summary is not None:
                transitions = final_csv("flow-transitions.csv")
                task_transitions = transitions.loc[transitions.stratum.eq("random_audit") & transitions.dimension.eq("task")]
                matrix = task_transitions.pivot(index="before_status", columns="after_status", values="count").reindex(index=status_labels, columns=status_labels).fillna(0).astype(int)
                assert matrix.to_numpy().sum() == 352
                assert matrix.loc["agreed"].sum() == 110 and matrix["agreed"].sum() == 86
                display(matrix.rename(index=status_labels, columns=status_labels))
            '''),
        ]
    if prefix == "03":
        return [
            md("""
            ## 真实观察：同题人审的耗时与判断
            重测与 AI 建议辅助阶段覆盖相同的 8 道题、A/B 两位评审，共 16 对“同题—同角色”提交。
            记录耗时包含停顿；题目顺序固定，辅助阶段共享 AI 推荐。以下描述工作流程中的观察变化。
            来源：`results/final-analysis/human-role-timing.csv` 与 `summary.json` 的 `human`。
            """), code(LOAD), code('''
            if final_summary is not None:
                human = final_summary["human"]
                timing = final_csv("human-role-timing.csv")
                stages = ["retest-v2", "guided-v3"]
                paired_timing = timing.loc[timing["round"].isin(stages)].pivot(index="role", columns="round", values="person_minutes").reindex(columns=stages)
                display(paired_timing.rename(columns={"retest-v2": "独立重测（人分钟）", "guided-v3": "AI辅助（人分钟）"}).round(2))
                display(pd.DataFrame([{"同题同角色配对": human["timed_pairs"], "前阶段人分钟": human["before_person_minutes"], "辅助阶段人分钟": human["after_person_minutes"], "耗时减少配对": human["faster_pairs"], "耗时增加配对": human["slower_pairs"]}]).round(2))
                fig, ax = plt.subplots(figsize=(8.6, 4.5))
                for role, color, marker in [("A", "#176B77", "o"), ("B", "#BC632F", "s")]:
                    values = paired_timing.loc[role].to_numpy()
                    ax.plot([0, 1], values, marker=marker, linewidth=2.2, color=color, label=f"评审 {role}（8 题）")
                    for x, value in enumerate(values):
                        ax.annotate(f"{value:.2f}", (x, value), xytext=(0, 9), textcoords="offset points", ha="center")
                ax.set_xticks([0, 1], ["独立重测", "AI 建议辅助"])
                ax.set(xlim=(-.2, 1.2), ylim=(0, paired_timing.to_numpy().max()*1.2), ylabel="同题累计记录耗时（人分钟）", title="同题人审耗时：两位评审的变化方向不同")
                ax.legend(frameon=False)
                fig.tight_layout()
                save_figure(fig, "final-human-role-timing", "同题8项、两角色共16对；时间包含停顿，固定顺序与共同推荐限制因果解释。", kind="real", sources=["results/final-analysis/human-role-timing.csv", "results/final-analysis/summary.json"], notebook="03_人审与校准.ipynb")
                plt.show()
            '''), md("""
            累计耗时由 24.55 降至 19.41 人分钟，A 减少而 B 增加。13 对耗时减少、3 对增加。
            同题重复、固定顺序、共同推荐和记录停顿均可能影响时间，因此这些观察不估计 AI 辅助的因果效率收益。
            """), code('''
            if final_summary is not None:
                human_agreement = []
                for stage in ["retest-v2", "guided-v3"]:
                    row = human["rounds"][stage]
                    numeric_equal = int(np.trace(np.asarray(row["matrix6"])))
                    human_agreement.append({"阶段": stage, "同题分母": row["paired_items"], "完全一致（含共同弃权）": row["exact_including_abstention"], "共同弃权": row["exact_including_abstention"] - numeric_equal, "双方数字标签": row["both_labeled"], "六级κ": row["kappa6"], "三阶κ": row["kappa3"]})
                display(pd.DataFrame(human_agreement).round(3))
            '''), md("""
            完全一致由 4/8 变为 6/8，后者包含 2 道共同弃权；六级 κ 分别只用 7 道与 6 道双方数字标签题计算。
            辅助阶段的采纳记录与独立盲审有不同测量条件，不能作为独立金标准来校准全部模型标签。
            """),
        ]
    if prefix == "04":
        return [
            md("""
            ## 真实资料：严格时间顺序检查
            B 的次级关联分析要求可观测的逐回合时间、严格晚于暴露的候选结果及足够学生簇。
            源 CSV 创建时间描述来源记录，不能回填为学生每次提问的发生时间；相同时间戳也不构成先后顺序。
            来源：`results/final-analysis/summary.json` 的 `association`。
            """), code(LOAD), code('''
            if final_summary is not None:
                association = final_summary["association"]
                checks_b = pd.DataFrame([
                    ["全部真实回合", association["input_real_turns"]],
                    ["任务候选回合", association["input_candidate_turns"]],
                    ["已观测逐回合时间戳", association["observed_turn_timestamps"]],
                    ["符合严格晚于条件的记录", association["eligible_records"]],
                    ["符合条件的学生簇", association["student_clusters"]],
                    ["事前最少记录门槛", association["minimum_records"]],
                    ["事前最少学生簇门槛", association["minimum_students"]],
                ], columns=["检查项目", "数量"])
                display(checks_b)
                display(Markdown(f"**当前状态：{association['status']}。** 逐回合时间戳为 {association['observed_turn_timestamps']}；可进入严格前瞻关联的记录和学生簇均为 0，因此未拟合模型、未评估设计矩阵秩。"))
                assert association["observed_turn_timestamps"] == association["eligible_records"] == association["student_clusters"] == 0
            '''), md("""
            下一步应在采集时记录逐回合时间和会话关系，再检查前瞻样本门槛、设计矩阵及学生簇。
            因果学习增量还需要明确干预、独立学习结果、处理前基础和可比条件。达到 100 条/30 簇工程门槛本身不保证统计功效。
            """),
        ]
    if prefix == "05":
        return [
            md("""
            ## 条件分析：把未知标签保留在学期分母中
            暂时接受已给出的候选标签正确，将其余回合的层级限制在 L1–L6，可得到 ABL 与 HOT 的可行范围。
            范围描述指定假设下的极值，未引入错标率或抽样误差模型。
            来源：`unknown-label-bounds.csv`、`term-contrast-bounds.csv` 与 `summary.json`，均位于 `results/final-analysis/`。
            """), code(LOAD), code('''
            if final_summary is not None:
                unknown = final_csv("unknown-label-bounds.csv")
                task_bounds = unknown.loc[unknown.dimension.eq("task") & unknown.term.isin(["25f", "26s"])].set_index("term").loc[["25f", "26s"]]
                display(task_bounds[["total_turns", "candidate_turns", "unknown_turns", "candidate_abl_raw", "abl_raw_lower", "abl_raw_upper", "candidate_hot", "hot_lower", "hot_upper"]].round(3))
                fig, axes = plt.subplots(1, 2, figsize=(11.8, 4.0))
                for ax, key, label, limits in [(axes[0], "abl_raw", "ABL 原始层级", (1, 6)), (axes[1], "hot", "高阶占比 HOT", (0, 1))]:
                    for y, (term, row) in enumerate(task_bounds.iterrows()):
                        ax.plot([row[key+"_lower"], row[key+"_upper"]], [y, y], color="#176B77", linewidth=5, solid_capstyle="round")
                        ax.scatter(row["candidate_"+key], y, color="#BC632F", marker="D", s=55, zorder=3)
                    ax.set_yticks([0, 1], [f"25f · {int(task_bounds.loc['25f', 'total_turns'])} 回合", f"26s · {int(task_bounds.loc['26s', 'total_turns'])} 回合"])
                    ax.set(xlim=limits, ylim=(-.55, 1.55), xlabel=label, title="全回合条件范围与候选子集点值")
                fig.tight_layout()
                save_figure(fig, "final-unknown-label-bounds", "线段为未知标签任取L1–L6的条件范围，菱形为候选子集点值；已判定标签暂视为正确。", kind="conditional", sources=["results/final-analysis/unknown-label-bounds.csv", "results/final-analysis/summary.json"], notebook="05_指标性质与不确定性.ipynb")
                plt.show()
                contrasts = final_csv("term-contrast-bounds.csv")
                display(contrasts.loc[contrasts.dimension.eq("task")].round(4))
            '''), md("""
            学期差取“春季减秋季”。仅使用任务候选时 ABL 与 HOT 的差为负；纳入全部未知标签后，两项差的条件范围均跨过 0。
            这些范围不支持确定学期差方向，也不代表学生能力差或 AI 处理效应。

            ## 条件分析：缺失指标、改标预算与权重
            范围计算覆盖 356 个有任务候选的学生—学期单位：全部缺少已验证 CTQ 边，其中 99 个还缺工具身份。
            每组重新使用同一标签向量计算相关指标，再对缺失指标和权重进行外包界分析。
            """), code('''
            if final_summary is not None:
                ranges = final_csv("score-range-sensitivity.csv")
                selected_ranges = ranges.loc[ranges.term.eq("all") & np.isclose(ranges.weight_relative_change, .1)]
                display(selected_ranges[["scheme", "label_error_fraction", "candidate_students", "label_budget_min", "label_budget_max", "effective_changed_fraction_median", "effective_changed_fraction_max", "width_median", "stably_distinguishable_pairs", "within_term_pairs"]].round(3))
                fig, axes = plt.subplots(1, 2, figsize=(11.8, 4.4))
                scheme_names = {"balanced": "均衡", "higher_order": "高阶优先", "process": "过程优先"}
                for (scheme, group), color, marker in zip(selected_ranges.groupby("scheme", sort=False), ["#176B77", "#BC632F", "#765D91"], ["o", "s", "^"]):
                    group = group.sort_values("label_error_fraction")
                    label = scheme_names.get(scheme, scheme)
                    axes[0].plot(group.label_error_fraction, group.width_median, marker=marker, color=color, label=label)
                    axes[1].plot(group.label_error_fraction, 100*group.stable_pair_fraction, marker=marker, color=color, label=label)
                axes[0].set(ylabel="条件分数范围宽度中位数（分）", ylim=(0, 100), title="范围宽度 · 权重各项 ±10% 后归一化")
                axes[1].set(ylabel="可稳定区分的同学期配对（%）", ylim=(0, max(1, selected_ranges.stable_pair_fraction.max()*115)), title="外包界不重叠的配对比例")
                for ax in axes:
                    ax.set_xlabel("名义改标预算 ε；条数为 ceil(ε × 候选数)")
                    ax.set_xticks(sorted(selected_ranges.label_error_fraction.unique()))
                    ax.legend(frameon=False)
                fig.tight_layout()
                save_figure(fig, "final-score-range-sensitivity", "356个有任务候选的学生—学期单位；按ceil(εn)改标，缺失CTQ及工具情景保留；稳定配对仅在同学期内比较。", kind="conditional", sources=["results/final-analysis/score-range-sensitivity.csv", "results/final-analysis/summary.json"], notebook="05_指标性质与不确定性.ipynb")
                plt.show()
                small_budget = selected_ranges.loc[np.isclose(selected_ranges.label_error_fraction, .05)].iloc[0]
                display(Markdown(f"ε=0.05 时向上取整后的实际可改标比例：中位数 **{small_budget.effective_changed_fraction_median:.0%}**，最大 **{small_budget.effective_changed_fraction_max:.0%}**。候选数较少时，名义5%可对应一条乃至全部候选；应连同实际预算阅读曲线。"))
            '''), md("""
            外包界不重叠是给定方案和假设下可区分的充分条件。重叠表示该检验不能稳定区分，不能推断学生能力相等。
            即使不改标签，缺失的 CTQ 和工具信息也使范围保持一定宽度；这些范围不是 95% 置信区间。
            """),
        ]
    if prefix == "06":
        return [
            md("""
            ## 合成反例与条件检验：保留证据不变时的评分变化
            下列结果来自固定标签工具堆叠、早期文本扰动缓存与目标分布匹配。三者分别检验评分激励、指定模型提示条件下的输出和指标定义。
            来源：`results/final-analysis/summary.json` 的 `redteam`，并保留原合成 CSV 来源。
            """), code(LOAD), code('''
            if final_summary is not None:
                redteam_summary = final_summary["redteam"]
                attack = redteam_summary["metric_attack"]
                attack_comparison = pd.DataFrame({"方案": ["均衡五指标", "MAB权重置零"], "相对各自未操纵基线的增分": [attack["tool_stacking_score_delta"], attack["MAB_zero_weight_score_delta"]]})
                display(attack_comparison.round(2))
                fig, ax = plt.subplots(figsize=(7.8, 4.1))
                bars = ax.bar(attack_comparison["方案"], attack_comparison.iloc[:, 1], color=["#BC632F", "#176B77"])
                ax.bar_label(bars, labels=[f"{x:+.2f}" for x in attack_comparison.iloc[:, 1]], padding=5)
                ax.set(ylim=(0, max(attack_comparison.iloc[:, 1])*1.2), ylabel="AIV 增量（分）", title="固定标签合成案例：只增加工具种类")
                fig.tight_layout()
                save_figure(fig, "final-tool-stacking-comparison", "同一固定标签案例的工具堆叠增量；MAB权重置零也移除了工具广度奖励。", kind="synthetic", sources=["results/final-analysis/summary.json", "results/synthetic/redteam.csv"], notebook="06_红队与失败边界.ipynb")
                plt.show()
                text_check = redteam_summary["text_redteam"]
                display(pd.DataFrame(text_check["verb_injection_outputs"]).rename(columns={"model": "模型", "task_level": "动词注入后的任务层级"}))
                display(pd.DataFrame([{"文本条件数": text_check["conditions"], "模型输出数": text_check["outputs"], "每条件每模型调用": 1, "目标分布匹配DHI": redteam_summary["dhi_matching"]["DHI"], "非对称DHI": redteam_summary["dhi_matching"]["asymmetric_DHI"]}]))
            '''), md("""
            工具堆叠增分为 11.39，MAB 权重置零后该案例的增量为 0，同时综合分停止奖励工具广度。
            早期文本实验共 7 条件、21 次输出，每条件每模型一次；动词注入后的三个任务输出均为 L2。这些旧模型/提示条件不用于估计 t3 的攻击成功率。
            预设目标分布与标签分布相同时 DHI 取 1，反映定义中的匹配程度；独立学习效果需要另测。
            """),
        ]
    if prefix == "07":
        return [
            md("""
            ## 比较完整证据与缺失证据
            两例共用保存的**合成任务标签** `[2, 3, 4, 5]`，调用 `scripts.demo_evidence_chain.run_case` 与共享指标函数。
            `complete` 具有已知合成会话边和两种工具；`missing_evidence` 将会话及工具身份设为未知。两例均不发起实时模型调用。
            """), code('''
            from scripts.demo_evidence_chain import run_case
            from aiv.notebook_materials import configure_style, save_figure
            from IPython.display import Markdown, display
            configure_style()
            demo_cases = {name: run_case(name) for name in ["complete", "missing_evidence"]}
            demo_view = pd.DataFrame([
                {"案例": name, **case["metrics"], "AIV_balanced": case["AIV_balanced"], "条件下界": case["conditional_range"]["lower"], "条件上界": case["conditional_range"]["upper"]}
                for name, case in demo_cases.items()
            ])
            # 保留底层 None；显示时明确写作 null，避免与零分混淆。
            display(demo_view.round(4).astype(object).where(demo_view.notna(), "null"))
            for name, case in demo_cases.items():
                display(Markdown(f"**{name}** · {case['report']['scope']}\\n\\n观察：{case['report']['observed']}\\n\\n行动：{case['report']['action']}"))
            assert demo_cases["complete"]["source"] == demo_cases["missing_evidence"]["source"] == "synthetic_saved_labels"
            assert not any(case["live_model_call"] for case in demo_cases.values())
            assert demo_cases["missing_evidence"]["metrics"]["CTQ"] is None
            assert demo_cases["missing_evidence"]["metrics"]["MAB"] is None
            assert demo_cases["missing_evidence"]["AIV_balanced"] is None
            '''), code('''
            fig, ax = plt.subplots(figsize=(8.5, 3.8))
            for y, (name, case) in enumerate(demo_cases.items()):
                bounds = case["conditional_range"]
                ax.plot([bounds["lower"], bounds["upper"]], [y, y], color="#176B77", linewidth=5, solid_capstyle="round")
                if case["AIV_balanced"] is not None:
                    ax.scatter(case["AIV_balanced"], y, color="#BC632F", s=65, zorder=3)
                ax.annotate(f"[{bounds['lower']:.2f}, {bounds['upper']:.2f}]", (bounds["upper"], y), xytext=(7, 0), textcoords="offset points", va="center")
            ax.set_yticks([0, 1], ["complete：已知边与工具", "missing_evidence：缺少边与工具"])
            ax.set(xlim=(0, 100), ylim=(-.55, 1.55), xlabel="均衡 AIV 的条件范围（分）", title="相同合成标签，证据完整度决定可计算结果")
            fig.tight_layout()
            save_figure(fig, "synthetic-live-evidence-chain", "已保存合成标签的现场计算；缺少会话边和工具时保留null指标及条件范围。", kind="synthetic", sources=["scripts/demo_evidence_chain.py", "aiv/metrics.py", "aiv/models.py"], notebook="07_教育报告与复用.ipynb")
            plt.show()
            '''), md("""
            完整例的均衡分约为 61.65；缺证例的 CTQ、MAB 与点分均为 `null`，条件范围为 `[36, 76]`。
            下一步行动对应具体缺口：完整例要求补充独立推理与核验；缺证例先补同会话关系和工具来源。
            范围使用固定权重和已保存标签；合成案例展示计算与报告流程，不是对真实学习增益的估计。
            """),
        ]
    if prefix == "09":
        return [
            md("""
            ## 真实观察：六级分布与同回合双维比较
            任务与贡献分别有 1,010 和 776 条候选，占全部 3,515 回合的不同子集。
            边际分布用于描述各自候选；两维比较使用同一回合均有候选的 48 条记录。
            来源：`descriptive.csv`、`level-distribution.csv`、`joint-levels.csv` 与 `summary.json`，均位于 `results/final-analysis/`。
            """), code(LOAD), code('''
            if final_summary is not None:
                descriptive = final_csv("descriptive.csv")
                level_distribution = final_csv("level-distribution.csv")
                display(descriptive[["term", "dimension", "turns", "students", "candidate_turns", "candidate_students", "students_without_candidates", "candidate_coverage", "turn_weighted_abl_raw", "equal_student_abl_raw", "turn_weighted_hot", "equal_student_hot"]].round(4))
                levels_all = level_distribution.loc[level_distribution.term.eq("all")]
                fig, ax = plt.subplots(figsize=(9.3, 4.4))
                for shift, dimension, label, color in [(-.18, "task", "任务候选", "#176B77"), (.18, "contribution", "贡献候选", "#BC632F")]:
                    group = levels_all.loc[levels_all.dimension.eq(dimension)].sort_values("level")
                    assert group["count"].sum() == group.candidate_denominator.iloc[0]
                    bars = ax.bar(group.level+shift, 100*group.candidate_share, width=.36, color=color, label=f"{label} n={int(group.candidate_denominator.iloc[0])}")
                    ax.bar_label(bars, labels=[str(int(n)) for n in group["count"]], padding=3, fontsize=9)
                ax.set_xticks(range(1, 7), [f"L{x}" for x in range(1, 7)])
                ax.set(ylim=(0, levels_all.candidate_share.max()*118), ylabel="各维度候选内部占比（%）", title="双维六级边际分布；柱顶为回合数量")
                ax.legend(frameon=False)
                fig.tight_layout()
                save_figure(fig, "final-dimension-level-distribution", "各维度分别以自身候选为分母；两个候选集合并非同一批回合。", kind="real", sources=["results/final-analysis/level-distribution.csv", "results/final-analysis/descriptive.csv", "results/final-analysis/summary.json"], notebook="09_Agent批量结果与互评.ipynb")
                plt.show()
            '''), md("""
            按回合加权回答“候选回合的平均标签”；按有候选的学生—学期等权回答“这些学生—学期的平均标签”。
            无候选的学生—学期另列，两个加权结果都不能外推为全部学生能力。
            """), code('''
            if final_summary is not None:
                joint_levels = final_csv("joint-levels.csv")
                joint_all = joint_levels.loc[joint_levels.term.eq("all")]
                joint_matrix = joint_all.pivot(index="task_level", columns="contribution_level", values="count").reindex(index=range(1, 7), columns=range(1, 7)).fillna(0).astype(int)
                assert joint_matrix.to_numpy().sum() == final_summary["joint"]["paired_denominator"] == 48
                fig, ax = plt.subplots(figsize=(6.6, 5.2))
                im = ax.imshow(joint_matrix.to_numpy(), cmap="Blues", vmin=0, aspect="equal")
                for y in range(6):
                    for x in range(6):
                        value = joint_matrix.iloc[y, x]
                        ax.text(x, y, str(value), ha="center", va="center", color="white" if value > joint_matrix.to_numpy().max()*.55 else "#273949")
                ax.set_xticks(range(6), [f"L{x}" for x in range(1, 7)])
                ax.set_yticks(range(6), [f"L{x}" for x in range(1, 7)])
                ax.set(xlabel="同回合贡献候选层级", ylabel="同回合任务候选层级", title="同回合双维共同候选 · n=48")
                fig.colorbar(im, ax=ax, label="回合数")
                fig.tight_layout()
                save_figure(fig, "final-joint-candidate-levels", "48条同回合双维非空候选；任务较高13条、相同29条、贡献较高6条。", kind="real", sources=["results/final-analysis/joint-levels.csv", "results/final-analysis/summary.json"], notebook="09_Agent批量结果与互评.ipynb")
                plt.show()
                joint = final_summary["joint"]
                display(pd.DataFrame([{"共同候选": joint["paired_denominator"], "任务较高": joint["task_above_contribution"], "相同": joint["equal"], "贡献较高": joint["task_below_contribution"], "任务高阶且贡献非高阶": joint["task_hot_contribution_not_hot"]}]))
            '''), md("""
            同回合比较得到任务较高 13 条、相同 29 条、贡献较高 6 条，共 48 条。
            共同候选仅占全部回合的一小部分；双维边际均值之差不等于这 48 条同回合差值，也不能用来判断全部学生的任务与贡献差距。
            """),
        ]
    return []


def _repair_legacy(nb, prefix):
    """Keep existing experiments while updating their reader-facing interpretation."""
    replacements = {
        "批量 agent 调用与互评由另一对话执行。本页重放已有合成缓存；完成批次通过 09 本的文件接口接入。": "本页重放合成缓存，并读取固定随机子集的真实流程比较；09 本继续展示全回合分布与同回合双维候选。",
        "当前公式对工具堆叠有明显增分；逐轮拆分后 CTQ 无法识别而弃权。展示失败比宣称全面鲁棒更有用。": "固定标签时增加工具种类会提高综合分；逐轮拆分使 CTQ 缺少有效相邻边，点分保持空值。",
        "现阶段应报告使用方式与后续可观察过程的关联。对 AI 的真实学习增量、成熟效应与课程差异，缺少独立信息时结论是不可识别。": "过程关联也须具有可靠的时间顺序。当前逐回合时间戳缺失，严格前瞻关联尚无合格样本；独立学习增量另需结果、对照与处理前信息。",
        "这里的标准差与成本是教学参数。实际分配必须用试标耗时和残差估计；70 分钟对应辅助复核阶段，不冒充独立准确性评估预算。": "这里的标准差、成本与70分钟预算均为教学设定。实际分配应使用目标复核任务的试标耗时和残差估计。",
        "交卷后通过白名单导出建立独立公开版本。公开包只含教学源码、合成样本、可发布缓存与环境说明。": "公开复现材料通过白名单整理为独立仓库，包含获准发布的源码、合成样本、图文和环境说明。",
        "从干净内核依次执行七个单元；使用共享计算函数扩展到你自己的合规数据，并重新验证标签和识别假设。": "运行本页两例并检查指标、条件范围与建议；用于其他授权数据时，重新核验标签、会话边、工具来源和识别条件。",
        "本页只读取显式选定、哈希固定的 `frozen` 快照，不扫描运行目录，不发起模型调用。": "原始输入部分读取显式选定、哈希固定的 `frozen` 快照；后半部读取 `results/final-analysis/` 的聚合表。所有单元离线计算，不发起模型调用。",
        "最终人工核验作为已封存的方法记录读取；本页不开展新一轮测评。": "最终人工核验作为已封存的方法记录读取，展示其状态与判定规则。",
        "人工记录已封存；本页保留校准方法的合成示例。最终封存状态在 08 本中只读展示，真实总体校准结果在 09 本中留空，等待适用的参考标签与抽样设计。": "本页将校准方法的合成实验与同题人工复核记录分列。总体误差校准仍需独立参考标签及相应抽样设计。",
    }
    for cell in nb.cells:
        if TAG in cell.metadata.get("tags", []):
            continue
        if cell.cell_type == "markdown":
            for old, new in replacements.items():
                cell.source = cell.source.replace(old, new)
            cell.source = cell.source.replace("## Goal", "## 学习目标").replace("## Setup", "## 运行准备").replace("## Checks", "## 计算检查").replace("## Next Steps", "## 继续研究")
            # Repair a former fixed-index setup insertion into a concept Markdown cell.
            cell.source = cell.source.replace("\nfrom aiv.notebook_materials import configure_style, save_figure\nconfigure_style()", "")
        elif prefix == "04" and '"学生聚类与时间顺序"' in cell.source:
            cell.source = cell.source.replace('"学生聚类与时间顺序"', '"学生簇与逐回合时间顺序"').replace('"部分可用"', '"学生簇可用；逐回合时间戳缺失"')
    if prefix in {"02", "03", "04", "05"}:
        nb.cells[0].source = nb.cells[0].source.replace(
            "**数据来源：** `aiv/data.py` 的确定性合成样本。不是竞赛原始数据或真实教育效果证据。",
            "**数据来源：** 原有方法实验使用 `aiv/data.py` 的确定性合成样本；本页后半部读取 `results/final-analysis/` 的真实聚合观察或条件分析。两类结果分别标明范围。")
    if prefix == "00":
        nb.cells[0].source = nb.cells[0].source.replace("Notebook 复现包", "Notebook 复现材料")
        nb.cells[0].source = nb.cells[0].source.replace("人工核验已经封存；新一轮模型输出只从显式指定的冻结快照读取。", "模型与人审观察固定至 t3 及同题复核记录；`results/final-analysis/summary.json` 与十张聚合 CSV 连接各章。")
        nb.cells[0].source = nb.cells[0].source.replace("九张聚合 CSV", "十张聚合 CSV")
    if prefix == "07":
        for cell in nb.cells:
            if cell.cell_type == "code" and "excluded" in cell.source:
                cell.source = cell.source.replace("'论文全文'", "'未获再分发许可的文献全文'").replace('"论文全文"', '"未获再分发许可的文献全文"')
        nb.cells[0].source = nb.cells[0].source.replace(
            "**数据来源：** `aiv/data.py` 的确定性合成样本。不是竞赛原始数据或真实教育效果证据。",
            "**数据来源：** `aiv/data.py` 的固定种子合成样本，以及 `scripts/demo_evidence_chain.py` 的已保存合成任务标签。用于演示指标计算与报告流程。")
    if prefix == "08":
        for cell in nb.cells:
            if cell.cell_type == "markdown" and "上图显示原始索引字段" in cell.source and "3,515 个真实回合均无已观测逐回合时间戳" not in cell.source:
                cell.source += " 原始索引的时间字段描述来源记录；3,515 个真实回合均无已观测逐回合时间戳，不能据此建立严格前瞻次序。"



# Reader guidance is separate from computation and its source-specific caveats.
INTRODUCTIONS = {
    "01": ("认识输入、观测单位与缺失", "核对记录粒度、字段和六级分布。", "固定种子合成样本，24名合成学生、192条提问。", "修改 synthetic_records(seed=26) 的种子；改变样本规模时同步调整数量检查。"),
    "02": ("标签证据与复核流程", "校验连续引文，重放合成缓存，再比较同题复核状态。", "合成问句和缓存；t3流程表及早期真实、合成各6题的汇总。", "修改 question、example 的证据或投票组合，观察结构校验与弃权。"),
    "03": ("人工复核的预算与观察结果", "分配人工预算，检查残差校正，再读取同题复评的时间与一致性。", "合成有限总体与覆盖率试验；16对同题同角色的真实聚合记录。", "调整 allocate_review 的分层比例、标准差、成本和 budget；残差示例可调整抽样量。"),
    "04": ("比较资格与因果识别边界", "用已知真值检查混杂，再核对日志的时间和结果条件。", "真效应0.4的合成机制；公开汇总中的真实时间资格检查。", "为 causal_simulation(seed=26, n=1000) 指定种子和样本量，比较估计变化。"),
    "05": ("指标、权重与条件范围", "计算五项指标，观察未知标签、改标预算与权重如何改变范围。", "合成记录与标签误差模拟；公开未知标签范围和评分敏感性表。", "调整 weight_sensitivity(scores, seed=26, draws=200)；真实汇总按既定权重与预算分组读取。"),
    "06": ("用反例检查评分激励", "比较固定标签攻击、保存的文本测试与MAB消融。", "合成反例、旧模型与提示条件的文本缓存、公开反例汇总。", "比较各场景相对基线的 delta，再对照MAB权重置零后的变化。"),
    "07": ("从指标生成可行动的报告", "比较三类角色报告，运行完整与缺失证据两例，并核对补证建议。", "合成记录与保存的任务标签[2,3,4,5]；工作台配图另标明构造资料。", "切换 educational_report 的 role；比较 run_case('complete') 与 run_case('missing_evidence')。"),
}
CHECKS = {
    "01": "检查记录数、学生数、唯一ID及L1–L6取值。",
    "02": "检查票不齐时保留弃权，并确认本页只重放保存的输入。",
    "03": "检查分配不超预算，以及全量参考下校正值等于已知总体均值。",
    "04": "检查模拟来源与已知效应，再读取真实时间资格计数。",
    "05": "检查DHI目标分布值、缺少会话边时的CTQ与合成分数取值。",
    "06": "检查分段后点分缺失，以及工具堆叠对当前公式的增分。",
    "07": "检查合成来源；两例计算还检查缺证时CTQ、MAB与点分保持None。",
}
NEXT = {
    "00": ("01_数据与问题.ipynb", "01 · 检查输入与观测单位"),
    "01": ("08_真实数据与封存记录.ipynb", "08 · 核对真实范围与来源"),
    "08": ("02_标注与互评.ipynb", "02 · 检查证据与复核流程"),
    "02": ("09_Agent批量结果与互评.ipynb", "09 · 阅读全回合双维结果"),
    "09": ("03_人审与校准.ipynb", "03 · 人工复评与校准条件"),
    "03": ("04_识别边界与模拟.ipynb", "04 · 检查比较资格"),
    "04": ("05_指标性质与不确定性.ipynb", "05 · 计算指标与条件范围"),
    "05": ("06_红队与失败边界.ipynb", "06 · 检验评分反例"),
    "06": ("07_教育报告与复用.ipynb", "07 · 将结果写成行动建议"),
    "07": ("00_论文材料总览.ipynb", "返回总览，选择其他计算或论文入口"),
}

# Each tuple is (repository-relative path, evidence kind, caption, insertion anchor).
VISUALS = {
    "00": [("slides/roadshow/images/03.png", "concept", "路演03｜测量、比较、指标、反例与教学行动的反馈链。", "overview")],
    "01": [("slides/roadshow/images/02.png", "concept", "路演02｜先区分评价对象，再决定需要哪些输入。", "overview")],
    "02": [
        ("slides/roadshow/images/04.png", "concept", "路演04｜独立标注与独立复核；随机和风险子集分别解释。", "overview"),
        ("results/final-analysis/fig02_random_flow.png", "real", "论文图2｜固定随机352回合的同题状态；下方保留从汇总表重绘的代码。", "### t3：固定随机子集的同题状态")],
    "03": [
        ("slides/roadshow/images/15.png", "real", "路演15｜同题人工复评；固定顺序与共同推荐限制因果解释。", "overview"),
        ("results/final-analysis/fig04_human_pairs.png", "real", "论文图4｜配对时间与一致性，分别保留两位评审和共同弃权。", "## 真实观察：同题人审的耗时与判断")],
    "04": [("slides/roadshow/images/13.png", "concept", "路演13｜增量解释所需的可比条件、时间与独立结果。", "overview")],
    "05": [
        ("slides/roadshow/images/06.png", "conditional", "路演06｜已观测子指标与条件范围；完整AIV仍受缺失证据约束。", "overview"),
        ("results/final-analysis/fig03_unknown_labels.png", "conditional", "论文图3｜固定已有候选，未知标签取遍L1–L6的范围。", "## 条件分析：把未知标签保留在学期分母中"),
        ("results/final-analysis/fig05_score_sensitivity.png", "conditional", "论文图5｜缺失指标、向上取整的改标预算与权重扰动。", "## 条件分析：缺失指标、改标预算与权重")],
    "06": [("slides/roadshow/images/07.png", "synthetic", "路演07｜保持标签不变的合成工具堆叠反例与MAB消融。", "overview")],
    "07": [
        ("slides/roadshow/images/11.jpg", "engineering", "路演11｜两条构造输入经实际模型调用后的教师简报；与本页合成报告计算分别解释。", "after_reports"),
        ("slides/roadshow/images/09.png", "synthetic", "路演09｜保存的合成标签与完整证据；界面将计算值61.65显示为61.7。", "after_demo"),
        ("slides/roadshow/images/10.png", "synthetic", "路演10｜同一标签缺少会话和工具来源后，保留36–76的条件范围及补证建议。", "after_demo")],
    "08": [("slides/roadshow/images/14.png", "concept", "路演14｜任务要求与已展示贡献分别定位到原文证据。", "overview")],
    "09": [
        ("slides/roadshow/images/05.png", "real", "路演05｜3,515个真实回合的双维候选；配对比较使用同回合共同候选。", "overview"),
        ("results/final-analysis/fig01_dimensions.png", "real", "论文图1｜六级边际分布与同回合配对；下面保留汇总表和绘图计算。", "## 真实观察：六级分布与同回合双维比较")],
}


def revise_existing(nb, prefix):
    """Keep calculations and qualifications while replacing repetitive handoff prose."""
    _repair_legacy(nb, prefix)
    if prefix in INTRODUCTIONS:
        title, objective, inputs, parameters = INTRODUCTIONS[prefix]
        nb.cells[0].source = f"# {prefix} · {title}\n\n**运行目标：** {objective}\n\n**输入：** {inputs}\n\n**可调整：** {parameters}"
        nb.cells[0].metadata["tags"] = sorted(set(nb.cells[0].metadata.get("tags", [])) | {"reviewer-intro"})
    retained = []
    for cell in nb.cells:
        if cell.cell_type == "markdown":
            heading = cell.source.strip().splitlines()[0] if cell.source.strip() else ""
            if heading in {"## 继续研究", "## 材料衔接", "## Next Steps"}:
                continue
            if cell.source.strip() == "公开复现材料通过白名单整理为独立仓库，包含获准发布的源码、合成样本、图文和环境说明。":
                continue
            if heading == "## 计算检查" and prefix in CHECKS:
                cell.source = "## 计算检查\n" + CHECKS[prefix]
            if prefix == "07" and cell.source.strip() == "## 复现与导出":
                cell.source = "## 检查导出字段\n查看种子、记录数与来源范围，便于交接计算。"
        retained.append(cell)
    nb.cells = retained


def managed_cell(cell, prefix, key, tag, previous):
    """Stable IDs preserve saved outputs when source code has not changed."""
    cell.id = hashlib.sha256(f"{tag}:{prefix}:{key}".encode()).hexdigest()[:12]
    cell.metadata["tags"] = [tag]
    old = previous.get(cell.id)
    if old is not None and old.cell_type == cell.cell_type and old.source == cell.source:
        old.metadata.update(cell.metadata)
        return old
    return cell


def visual_cell(spec, prefix, previous):
    relative_path, kind, caption, _ = spec
    cell = code("\n".join([
        "from IPython.display import Image, Markdown, display",
        f"reference_path = ROOT / {relative_path!r}",
        "if reference_path.is_file():",
        "    display(Image(filename=str(reference_path), width=960))",
        f"    display(Markdown({caption!r}))",
        "else:",
        "    display(Markdown('配图未附于本地副本：`' + reference_path.name + '`。可继续执行下方计算。'))",
    ]))
    cell.metadata["reviewed_visual"] = {"relative_path": relative_path, "kind": kind, "caption": caption}
    return managed_cell(cell, prefix, relative_path, VISUAL_TAG, previous)


def visual_position(cells, anchor):
    """Return a boundary after setup or the calculation illustrated by a reference."""
    if anchor == "overview":
        public = next((i for i, c in enumerate(cells) if c.cell_type == "code" and "reviewer-public-load" in c.metadata.get("tags", [])), None)
        if public is not None:
            return public + 1
        setup = next((i for i, c in enumerate(cells) if c.cell_type == "code" and "ROOT =" in c.source), None)
        if setup is None:
            raise ValueError("A ROOT setup cell is required before an inline image")
        return setup + 1
    if anchor in {"after_reports", "after_demo"}:
        pattern = r"educational_report\(\s*records\s*,\s*role\s*\)" if anchor == "after_reports" else r"\bdemo_cases\s*="
        position = next((i for i, c in enumerate(cells) if c.cell_type == "code" and re.search(pattern, c.source)), None)
    else:
        position = next((i for i, c in enumerate(cells) if c.cell_type == "markdown" and anchor in c.source), None)
    if position is None:
        raise ValueError(f"Reference image anchor not found: {anchor}")
    return position + 1


def transform(nb, prefix):
    """Update a Notebook in memory; callers own serialization and execution."""
    previous = {c.id: c for c in nb.cells if MANAGED_TAGS.intersection(c.metadata.get("tags", []))}
    nb.cells = [c for c in nb.cells if not MANAGED_TAGS.intersection(c.metadata.get("tags", []))]
    if prefix in VISUALS:
        nb.metadata["concept_illustrations_excluded"] = CONCEPT_IDS[:]
        nb.cells = [c for c in nb.cells if "concept-illustration-v1" not in c.metadata.get("tags", [])
                    and not (c.cell_type == "markdown" and "<!-- concept-illustration-v1:" in c.source)]
    revise_existing(nb, prefix)
    extra = [managed_cell(cell, prefix, str(i), TAG, previous) for i, cell in enumerate(sections(prefix))]
    insertion = next((i for i, c in enumerate(nb.cells) if "authorized-source" in c.metadata.get("tags", [])), len(nb.cells)) if prefix == "09" else len(nb.cells)
    nb.cells[insertion:insertion] = extra
    boundaries = {}
    for spec in VISUALS.get(prefix, []):
        boundaries.setdefault(visual_position(nb.cells, spec[3]), []).append(visual_cell(spec, prefix, previous))
    rebuilt = []
    for i, cell in enumerate(nb.cells):
        rebuilt.extend(boundaries.get(i, []))
        rebuilt.append(cell)
    rebuilt.extend(boundaries.get(len(nb.cells), []))
    if prefix in NEXT:
        target, label = NEXT[prefix]
        rebuilt.append(managed_cell(md(f"**下一步：** [{label}]({target})"), prefix, "navigation", NEXT_TAG, previous))
    nb.cells = rebuilt
    if extra:
        nb.metadata["material_role"] = "research_companion_with_synthetic_experiments"
    nbf.validate(nb)
    return nb


def finalize(root=ROOT):
    root = Path(root).resolve()
    changes = []
    for path in sorted((root / "notebooks").glob("[0-9][0-9]_*.ipynb")):
        original = path.read_bytes()
        nb = nbf.reads(original.decode("utf-8"), as_version=4)
        transform(nb, path.name[:2])
        content = nbf.writes(nb).encode("utf-8")
        if content != original:
            path.write_bytes(content)
        changes.append({"notebook": path.name,
                        "managed_cells": sum(TAG in c.metadata.get("tags", []) for c in nb.cells),
                        "reference_images": sum(VISUAL_TAG in c.metadata.get("tags", []) for c in nb.cells),
                        "changed": content != original})
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(finalize(args.root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
