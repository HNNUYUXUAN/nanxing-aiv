"""Run two deterministic evidence-to-report examples with saved synthetic labels."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiv.metrics import composite, metrics
from aiv.models import candidate_metric_intervals, score_outer_bounds


def run_case(case: str) -> dict:
    if case not in {"complete", "missing_evidence"}:
        raise ValueError("Unknown demonstration case")
    levels = [2, 3, 4, 5]
    complete = case == "complete"
    agents = ["工具甲", "工具甲", "工具乙", "工具乙"] if complete else [None] * 4
    sessions = ["已知合成会话"] * 4 if complete else [None] * 4
    values = metrics(levels, agents, sessions)
    intervals = candidate_metric_intervals(
        levels, tool_count=2 if complete else None,
        verified_edges=[(0, 1), (1, 2), (2, 3)] if complete else [],
    )
    result = {
        "case": case,
        "source": "synthetic_saved_labels",
        "live_model_call": False,
        "input": {"task_levels": levels, "tools": agents, "sessions": sessions},
        "metrics": values,
        "AIV_balanced": composite(values),
        "conditional_range": score_outer_bounds(intervals["outer_intervals"], relative_change=0),
        "report": {
            "observed": "四次任务要求依次为理解、应用、分析、评价，高阶任务占一半。",
            "action": (
                "围绕其中一次分析任务，要求学习者补充独立推理和核验依据。"
                if complete else "补充同会话相邻关系及实际工具来源后，再计算跃迁与综合分。"
            ),
            "scope": "已保存的合成任务标签；结果用于解释计算流程。",
            "missing": [] if complete else ["verified_session_edges", "tool_identity"],
        },
    }
    if complete:
        assert result["AIV_balanced"] is not None
        assert abs(values["CTQ"] - 0.6) < 1e-12
    else:
        assert values["CTQ"] is None and values["MAB"] is None
        assert result["AIV_balanced"] is None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["complete", "missing_evidence", "both"], default="both")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    names = ["complete", "missing_evidence"] if args.case == "both" else [args.case]
    result = {"schema": "evidence-demo-v1", "examples": [run_case(name) for name in names]}
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
