"""Offline aggregate-only final analysis of the hash-pinned t3 snapshot.

No student identifiers, text, per-person scores, or record-level human judgments
are exported. All label-based results are conditional on model candidates.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiv.full_turn_reporting import load_frozen, sha256, student_indicators, kappa
from aiv.models import candidate_metric_intervals, score_outer_bounds
from aiv.metrics import NAMES, WEIGHTS, IDEAL, dhi
from aiv.association import lagged_process_association

PIN = "7e885ceecf12a180349369ed9ddfc962ad71f1ea270f68b037a86a396430631d"
PLAN = Path("runtime/research/full-turns-v1-20260926T143225Z/t3/plan.json")
STATES = ("agreed", "abstained", "disagreement", "uncertain", "technical_failure")
DIMENSIONS = {"task": "level", "contribution": "contribution_level"}
TERM_NAMES = {"25f": "秋季", "26s": "春季", "all": "全部"}
SCHEME_NAMES = {"balanced": "均衡", "higher_order": "高阶优先", "process": "过程优先"}
STATE_NAMES = {"agreed": "候选", "abstained": "弃权", "disagreement": "分歧", "uncertain": "不确定", "technical_failure": "失败"}


def safe_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_merge_functions(root=ROOT):
    """Use the exact pure production functions without importing its API worker."""
    path = root / "aiv/fast_strong_workflow.py"
    source = ast.parse(path.read_text(encoding="utf-8"))
    selected = [node for node in source.body if isinstance(node, ast.FunctionDef)
                and node.name in {"consensus", "resolve_dimension"}]
    if len(selected) != 2:
        raise ValueError("Production merge contract missing")
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["consensus"], namespace["resolve_dimension"]


def final_label(row, dimension="task"):
    final = row["dimensions"][dimension]["final"]
    return final["label"] if final["status"] == "agreed" else None


def mean_or_none(values):
    return float(np.mean(values)) if values else None


def describe(values):
    values = np.asarray(values, dtype=float)
    if len(values) == 0:
        return {"n": 0, "minimum": None, "q25": None, "median": None, "mean": None, "q75": None, "maximum": None}
    q = np.quantile(values, [0, .25, .5, .75, 1])
    return {"n": len(values), "minimum": float(q[0]), "q25": float(q[1]),
            "median": float(q[2]), "mean": float(values.mean()), "q75": float(q[3]), "maximum": float(q[4])}


def worst_case(levels, total):
    """Exact missing-label bounds, holding the supplied candidate labels fixed."""
    if total < len(levels) or total <= 0 or any(type(v) is not int or not 1 <= v <= 6 for v in levels):
        raise ValueError("Invalid candidate/total counts")
    unknown = total - len(levels)
    hot = sum(x >= 4 for x in levels)
    return {"total_turns": total, "candidate_turns": len(levels), "unknown_turns": unknown,
            "candidate_abl_raw": mean_or_none(levels), "candidate_hot": hot / len(levels) if levels else None,
            "abl_raw_lower": (sum(levels) + unknown) / total,
            "abl_raw_upper": (sum(levels) + 6 * unknown) / total,
            "hot_lower": hot / total, "hot_upper": (hot + unknown) / total}


def distribution_analysis(real):
    distribution_rows, descriptions, joint_rows = [], [], []
    terms = sorted({r["record"]["term"] for r in real})
    for term in ["all", *terms]:
        cohort = real if term == "all" else [r for r in real if r["record"]["term"] == term]
        grouped = defaultdict(list)
        for row in cohort:
            grouped[(row["record"]["term"], row["record"]["student"])].append(row)
        for dimension in DIMENSIONS:
            levels = [final_label(r, dimension) for r in cohort if final_label(r, dimension) is not None]
            counts = Counter(levels)
            student_levels = [[final_label(r, dimension) for r in rows if final_label(r, dimension) is not None] for rows in grouped.values()]
            candidate_students = [ls for ls in student_levels if ls]
            states = Counter(r["dimensions"][dimension]["final"]["status"] for r in cohort)
            for level in range(1, 7):
                distribution_rows.append({"kind": "real", "term": term, "dimension": dimension, "level": level,
                    "count": counts[level], "candidate_denominator": len(levels), "turn_denominator": len(cohort),
                    "candidate_share": counts[level] / len(levels) if levels else None,
                    "equal_student_candidate_share": mean_or_none([ls.count(level) / len(ls) for ls in candidate_students]),
                    "student_candidate_denominator": len(candidate_students)})
            descriptions.append({"kind": "real", "term": term, "dimension": dimension, "turns": len(cohort),
                "students": len(grouped), "candidate_turns": len(levels), "candidate_students": len(candidate_students),
                "students_without_candidates": len(grouped) - len(candidate_students), "candidate_coverage": len(levels) / len(cohort),
                "equal_student_coverage": float(np.mean([sum(final_label(r, dimension) is not None for r in rows)/len(rows) for rows in grouped.values()])),
                "turn_weighted_abl_raw": mean_or_none(levels), "turn_weighted_hot": mean_or_none([x >= 4 for x in levels]),
                "equal_student_abl_raw": mean_or_none([np.mean(ls) for ls in candidate_students]),
                "equal_student_hot": mean_or_none([np.mean(np.asarray(ls) >= 4) for ls in candidate_students]),
                **{s: states[s] for s in STATES}})
        pairs = [(final_label(r), final_label(r, "contribution")) for r in cohort
                 if final_label(r) is not None and final_label(r, "contribution") is not None]
        matrix = Counter(pairs)
        for task in range(1, 7):
            for contribution in range(1, 7):
                joint_rows.append({"kind": "real", "term": term, "task_level": task, "contribution_level": contribution,
                    "count": matrix[task, contribution], "paired_denominator": len(pairs), "turn_denominator": len(cohort)})
    pairs = [(final_label(r), final_label(r, "contribution")) for r in real
             if final_label(r) is not None and final_label(r, "contribution") is not None]
    joint = {"kind": "real", "unit": "same_turn_dual_candidate", "turn_denominator": len(real),
             "paired_denominator": len(pairs), "task_above_contribution": sum(a > b for a,b in pairs),
             "equal": sum(a == b for a,b in pairs), "task_below_contribution": sum(a < b for a,b in pairs),
             "task_hot_contribution_not_hot": sum(a >= 4 and b < 4 for a,b in pairs),
             "mean_task_minus_contribution": mean_or_none([a-b for a,b in pairs]),
             "scope": "Only same-turn nonempty final candidates; marginals cannot substitute for this pairing."}
    return descriptions, distribution_rows, joint, joint_rows


def flow_analysis(data, real):
    consensus, resolve = load_merge_functions()
    fast = [m["id"] for m in data.plan["models"] if m["role"] == "fast"]
    for row in data.rows:
        for dimension, field in DIMENSIONS.items():
            basic = consensus(row["independent"], field, 2)
            review = consensus(row["review"], field, 2)
            final = resolve(basic, review, row["review_selected"])
            if final != row["dimensions"][dimension]["final"] or basic != row["dimensions"][dimension]["basic"]:
                raise ValueError("Production merge does not reproduce frozen result")
    output, transitions = [], []
    for name in ("random_audit", "high_risk"):
        rows = [r for r in real if r["review_selection"] == name]
        for dimension, field in DIMENSIONS.items():
            variants = {model: [consensus([next(e for e in r["independent"] if e["model"] == model)], field, 1) for r in rows] for model in fast}
            variants["fast_strict"] = [r["dimensions"][dimension]["basic"] for r in rows]
            variants["final_review_merge"] = [r["dimensions"][dimension]["final"] for r in rows]
            baseline = variants["fast_strict"]
            for method, values in variants.items():
                counts = Counter(x["status"] for x in values)
                common = [(a,b) for a,b in zip(baseline, values) if a["label"] is not None and b["label"] is not None]
                output.append({"kind": "real", "stratum": name, "dimension": dimension, "method": method,
                    "turn_denominator": len(rows), **{s: counts[s] for s in STATES},
                    "candidate_coverage": counts["agreed"]/len(rows),
                    "status_changed_from_strict": sum(a["status"] != b["status"] for a,b in zip(baseline,values)),
                    "label_or_null_changed_from_strict": sum(a["label"] != b["label"] for a,b in zip(baseline,values)),
                    "both_candidate_pairs": len(common), "candidate_label_changed": sum(a["label"] != b["label"] for a,b in common),
                    "gained_candidate": sum(a["label"] is None and b["label"] is not None for a,b in zip(baseline,values)),
                    "lost_candidate": sum(a["label"] is not None and b["label"] is None for a,b in zip(baseline,values)),
                    "final_fast_fallback": sum(v.get("source") == "fast_review_incomplete" for v in values)})
            counter = Counter((a["status"], b["status"]) for a,b in zip(baseline,variants["final_review_merge"]))
            for a in STATES:
                for b in STATES:
                    transitions.append({"kind": "real", "stratum": name, "dimension": dimension,
                        "before_status": a, "after_status": b, "count": counter[a,b], "turn_denominator": len(rows)})
    return output, transitions


def agreement_from_labels(pairs):
    nonmissing = [(a,b) for a,b in pairs if a is not None and b is not None]
    matrix = np.zeros((6,6), dtype=int)
    for a,b in nonmissing:
        matrix[a-1,b-1] += 1
    return {"paired_items": len(pairs), "both_labeled": len(nonmissing),
            "both_abstained":sum(a is None and b is None for a,b in pairs),
            "one_abstained":sum((a is None)!=(b is None) for a,b in pairs),
            "exact_including_abstention": sum(a == b for a,b in pairs),
            "agreement_including_abstention": sum(a == b for a,b in pairs)/len(pairs) if pairs else None,
            "kappa6": kappa(matrix), "kappa3": kappa(matrix.reshape(3,2,3,2).sum(axis=(1,3))),
            "matrix6": matrix.tolist()}


def human_analysis(path):
    before_hash = sha256(path)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    rows = [dict(r) for r in connection.execute("SELECT item,round_id,reviewer,label,elapsed FROM assignments WHERE submitted IS NOT NULL AND reviewer IN ('A','B') AND round_id IN ('pilot-v1','retest-v2','guided-v3')")]
    rounds, role_rows = {}, []
    for name in ("pilot-v1", "retest-v2", "guided-v3"):
        subset = [r for r in rows if r["round_id"] == name]
        by_item = defaultdict(dict)
        for row in subset:
            by_item[row["item"]][row["reviewer"]] = row["label"]
        pairs = [(v["A"],v["B"]) for v in by_item.values() if set(v) == {"A","B"}]
        times = [r["elapsed"] for r in subset if r["elapsed"] is not None]
        rounds[name] = {**agreement_from_labels(pairs), "submitted": len(subset), "timed": len(times),
                        "excluded_timing": len(subset)-len(times), "person_minutes": sum(times)/60,
                        "median_seconds": float(np.median(times)) if times else None,
                        "condition": "AI_recommendation_assisted_same_items" if name == "guided-v3" else "independent_blind_labels"}
        for role in ("A","B"):
            values = [r["elapsed"] for r in subset if r["reviewer"] == role and r["elapsed"] is not None]
            role_rows.append({"kind": "real", "round": name, "role": role,
                "submissions": sum(r["reviewer"] == role for r in subset), "timed": len(values),
                "person_minutes": sum(values)/60, "median_seconds": float(np.median(values)) if values else None})
    before = {(r["item"],r["reviewer"]):r for r in rows if r["round_id"] == "retest-v2"}
    after = {(r["item"],r["reviewer"]):r for r in rows if r["round_id"] == "guided-v3"}
    if before.keys() != after.keys() or len(before) != 16:
        raise ValueError("Human same-item/role pairing is not 16 complete pairs")
    pairs = [(before[k],after[k]) for k in before]
    deltas = [b["elapsed"]-a["elapsed"] for a,b in pairs if a["elapsed"] is not None and b["elapsed"] is not None]
    if len(deltas) != 16:
        raise ValueError("Human timing pair missing")
    total_before, total_after = (sum(r["elapsed"] for r in group.values())/60 for group in (before,after))
    summary = {"kind": "real", "unit": "same_item_reviewer_role", "source_sha256": before_hash,
        "rounds": rounds, "paired_submissions": len(pairs), "timed_pairs": len(deltas),
        "before_person_minutes": total_before, "after_person_minutes": total_after,
        "observed_time_reduction_fraction": 1-total_after/total_before,
        "faster_pairs": sum(v<0 for v in deltas), "slower_pairs": sum(v>0 for v in deltas),
        "unchanged_pairs": sum(v==0 for v in deltas), "label_unchanged_pairs": sum(a["label"] == b["label"] for a,b in pairs),
        "paired_delta_seconds": describe(deltas),
        "scope": "Label-review workflow; same items in fixed order with common AI recommendations. Recorded time includes pauses; no causal efficiency or student-learning effect."}
    connection.close()
    if sha256(path) != before_hash:
        raise ValueError("Human database changed during readonly analysis")
    return summary, role_rows


def missing_analysis(real):
    rows = []
    for term in ["all", *sorted({r["record"]["term"] for r in real})]:
        cohort = real if term == "all" else [r for r in real if r["record"]["term"] == term]
        for dimension in DIMENSIONS:
            labels = [final_label(r, dimension) for r in cohort if final_label(r,dimension) is not None]
            rows.append({"kind": "conditional", "term": term, "dimension": dimension, **worst_case(labels,len(cohort))})
    differences = []
    for dimension in DIMENSIONS:
        a = next(r for r in rows if r["term"] == "25f" and r["dimension"] == dimension)
        b = next(r for r in rows if r["term"] == "26s" and r["dimension"] == dimension)
        for metric in ("abl_raw", "hot"):
            differences.append({"kind":"conditional", "contrast":"spring_minus_autumn", "dimension":dimension,
                "metric":metric, "candidate_only_difference": b[f"candidate_{metric}"]-a[f"candidate_{metric}"],
                "all_turn_lower": b[f"{metric}_lower"]-a[f"{metric}_upper"],
                "all_turn_upper": b[f"{metric}_upper"]-a[f"{metric}_lower"]})
    return rows,differences


def distinguishable_pairs(bounds,tolerance=1e-10):
    """Conservative strict separation; shared endpoints are not distinguishable."""
    total=stable=0
    for i,a in enumerate(bounds):
        for b in bounds[i+1:]:
            total+=1
            stable+=a['lower']-b['upper']>tolerance or b['lower']-a['upper']>tolerance
    return total,int(stable)


def score_sensitivity(data, real):
    groups = defaultdict(list)
    for r in real:
        groups[(r["record"]["term"],r["record"]["student"])].append(r)
    internal = []
    for (term,_), rows in groups.items():
        candidates = [r for r in rows if final_label(r) is not None]
        if not candidates:
            continue
        labels = [final_label(r) for r in candidates]
        tools = [data.turns[r["record"]["id"]].get("agent") for r in candidates]
        tools = [str(t).strip() if t is not None else "" for t in tools]
        tool_count = len(set(tools)) if all(t and t.casefold() != "unknown" for t in tools) else None
        for epsilon in (0,.05,.10,.20):
            budget = math.ceil(epsilon*len(labels))
            intervals = candidate_metric_intervals(labels,tool_count=tool_count,max_changed_labels=budget)
            for scheme in WEIGHTS:
                for weight_change in (0,.10):
                    bounds = score_outer_bounds(intervals["outer_intervals"],scheme,relative_change=weight_change)
                    lower,upper=max(0.0,bounds['lower']),min(100.0,bounds['upper'])
                    internal.append({"term":term,"epsilon":epsilon,"scheme":scheme,"weight_relative_change":weight_change,
                        "lower":lower,"upper":upper,"width":upper-lower,
                        "label_budget":budget,"candidate_n":len(labels),"missing_tool":tool_count is None})
    aggregate=[]
    for term in ["all",*sorted({r["record"]["term"] for r in real})]:
        for epsilon in (0,.05,.10,.20):
            for scheme in WEIGHTS:
                for weight_change in (0,.10):
                    chosen = [v for v in internal if (term=="all" or v["term"]==term) and v["epsilon"]==epsilon and v["scheme"]==scheme and v["weight_relative_change"]==weight_change]
                    pair_count=distinguishable=0
                    for t in sorted({v["term"] for v in chosen}):
                        pairs=[v for v in chosen if v["term"]==t]
                        ntotal,nstable=distinguishable_pairs(pairs)
                        pair_count+=ntotal;distinguishable+=nstable
                    widths=describe([v["width"] for v in chosen])
                    aggregate.append({"kind":"conditional","term":term,"label_error_fraction":epsilon,"scheme":scheme,
                        "weight_relative_change":weight_change,"candidate_students":len(chosen),
                        "missing_ctq_students":len(chosen),"missing_tool_students":sum(v["missing_tool"] for v in chosen),
                        "label_budget_min":min(v["label_budget"] for v in chosen),"label_budget_max":max(v["label_budget"] for v in chosen),
                        "effective_changed_fraction_median":float(np.median([v['label_budget']/v['candidate_n'] for v in chosen])),
                        "effective_changed_fraction_max":max(v['label_budget']/v['candidate_n'] for v in chosen),
                        **{f"width_{k}":v for k,v in widths.items() if k!="n"},"within_term_pairs":pair_count,
                        "stably_distinguishable_pairs":int(distinguishable),
                        "stable_pair_fraction":distinguishable/pair_count if pair_count else None})
    return aggregate


def association_analysis(data,real):
    records=[]
    for row in real:
        turn=data.turns[row["record"]["id"]]
        records.append({"student":json.dumps([row["record"]["term"],row["record"]["student"]]),
            "term":row["record"]["term"],"timestamp":turn.get("turn_timestamp"),
            "label":final_label(row)})
    result=lagged_process_association(records,minimum_students=30)
    result.update(kind="real",unit="strictly_later_candidate_turn",input_real_turns=len(real),
        input_candidate_turns=sum(r["label"] is not None for r in records),minimum_records=100,minimum_students=30,
        observed_turn_timestamps=sum(r['timestamp'] is not None for r in records),
        design_rank_status="not_evaluated_sample_gate_failed" if result['status']=='unavailable' and result.get('eligible_records',0)<100 else "checked_by_existing_function",
        timestamp_policy="Only observed turn_timestamp; source CSV creation times are not inferred as turn times. Equal timestamps never order each other.",
        cluster_policy="student-by-term; no presumed cross-term identity continuity",
        interpretation="Prospective association of model-candidate HOT with earlier candidate counts, not independent learning or a causal effect")
    return result


def redteam_analysis(root):
    with (root/'results/synthetic/redteam.csv').open(encoding='utf-8-sig') as stream:
        numeric=[dict(r) for r in csv.DictReader(stream)]
    with (root/'results/synthetic/text-redteam.csv').open(encoding='utf-8-sig') as stream:
        text=[dict(r) for r in csv.DictReader(stream)]
    stacked=next(r for r in numeric if r['scenario']=='工具堆叠')
    hot_before=[2,3,4,5]
    hot_after=hot_before+[5]
    return {"kind":"synthetic_and_conditional","metric_attack":{"kind":"synthetic","tool_stacking_score_delta":float(stacked['delta']),
        "MAB_zero_weight_score_delta":float(stacked['guarded_delta']),"cost":"Removing MAB weight removes its reward for observed tool breadth."},
        "text_redteam":{"kind":"synthetic","conditions":len({r['case'] for r in text}),"outputs":len(text),
            "verb_injection_outputs":[{"model":r['model'],"task_level":float(r['level']) if r['level'] else None} for r in text if r['case']=='verbs'],
            "scope":"Old model/prompt conditions, one call per condition/model; not t3 robustness or an attack-success-rate estimate."},
        "hot_duplicate_demo":{"kind":"synthetic","before_labels":hot_before,"after_labels":hot_after,
            "before_n":len(hot_before),"after_n":len(hot_after),
            "before_HOT":sum(v>=4 for v in hot_before)/len(hot_before),
            "after_HOT":sum(v>=4 for v in hot_after)/len(hot_after),
            "scope":"Deterministic fixed-label arithmetic: copying one high-order label increases HOT without a stipulated new learning gain. No new model or human experiment."},
        "dhi_matching":{"kind":"conditional","matched_distribution":IDEAL.tolist(),"DHI":dhi(IDEAL),"asymmetric_DHI":dhi(IDEAL,asymmetric=True),
            "interpretation":"Matching the prescribed label distribution attains 1 by definition; it does not establish learning improvement."}}


def pilot_analysis(root):
    """Recompute the historical 12-item graph using its own original rules."""
    path=root/'runtime/research/pilot-v1-c49671a0bb96/annotations.json'
    rows=json.loads(path.read_text(encoding='utf-8'))
    sample_path=path.parent/'sample.json';summary_path=path.parent/'summary.json'
    sample=json.loads(sample_path.read_text(encoding='utf-8'))
    stored_summary=json.loads(summary_path.read_text(encoding='utf-8'))
    sample_lookup={r['id']:r for r in sample}
    input_digest=hashlib.sha256(json.dumps(sample,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if input_digest!=stored_summary['input_sha256'] or len(sample_lookup)!=len(sample) or any(r['record']!=sample_lookup.get(r['record']['id']) for r in rows):
        raise ValueError('Historical pilot source identity/hash mismatch')
    source=ast.parse((root/'aiv/annotation.py').read_text(encoding='utf-8'))
    node=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='majority')
    namespace={'Counter':Counter}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'aiv/annotation.py','exec'),namespace)
    majority=namespace['majority']
    for row in rows:
        single=row['independent'][0]['judgment']['level'] if row['independent'][0]['judgment'] else None
        vote=majority([e['judgment'] for e in row['independent']])
        peer=majority([e['judgment'] for e in row['peer']])
        arb=row['arbitration']
        final=arb['judgment']['level'] if arb and arb['judgment'] else peer
        if any(row[key]!=value for key,value in [('single_model',single),('vote',vote),('peer_vote',peer),('final',final)]):
            raise ValueError('Historical pilot aggregation does not match original majority rules')
    if len(rows)!=12 or sum(r['record']['term']=='synthetic' for r in rows)!=6:
        raise ValueError('Historical pilot membership changed')
    observed={'n':len(rows),'real':sum(r['record']['term']!='synthetic' for r in rows),
        'synthetic':sum(r['record']['term']=='synthetic' for r in rows),'final_available':sum(r['final'] is not None for r in rows),
        'independent_valid':sum(e['judgment'] is not None for r in rows for e in r['independent']),
        'peer_valid':sum(e['judgment'] is not None for r in rows for e in r['peer']),
        'arbitrations':sum(r['arbitration'] is not None for r in rows)}
    if any(stored_summary[k]!=v for k,v in observed.items()):raise ValueError('Historical pilot summary count mismatch')
    groups=[];table=[]
    for kind in ('real','synthetic'):
        cohort=[r for r in rows if (r['record']['term']=='synthetic')==(kind=='synthetic')]
        stages={}
        for stage in ('independent','peer','arbitration'):
            entries=[e for r in cohort for e in ([r[stage]] if stage=='arbitration' else r[stage]) if e]
            stages[stage]={'outputs':len(entries),'valid':sum(e['judgment'] is not None for e in entries),
                'invalid':sum(e['judgment'] is None for e in entries),
                'nonnull_task_labels':sum(e['judgment'] is not None and e['judgment']['level'] is not None for e in entries)}
        changed=both_valid=0
        for row in cohort:
            initial={e['model']:e['judgment'] for e in row['independent']}
            for e in row['peer']:
                if initial[e['model']] is not None and e['judgment'] is not None:
                    both_valid+=1;changed+=initial[e['model']]['level']!=e['judgment']['level']
        groups.append({'kind':kind,'record_denominator':len(cohort),'stages':stages,
            'same_model_independent_peer_valid_pairs':both_valid,'same_model_task_label_or_null_changes':changed,
            'final_candidates':sum(r['final'] is not None for r in cohort),
            'final_label_or_null_changes_from_independent_vote':sum(r['final']!=r['vote'] for r in cohort)})
        previous_key='single_model'
        for key in ('single_model','vote','peer_vote','final'):
            pairs=[(r[previous_key],r[key]) for r in cohort]
            table.append({'kind':kind,'method':key,'record_denominator':len(cohort),
                'candidate_count':sum(r[key] is not None for r in cohort),'no_candidate_count':sum(r[key] is None for r in cohort),
                'comparison_method':previous_key,'label_or_null_changes':sum(a!=b for a,b in pairs),
                'gained_candidate':sum(a is None and b is not None for a,b in pairs),
                'lost_candidate':sum(a is not None and b is None for a,b in pairs),
                'both_candidate_label_changes':sum(a is not None and b is not None and a!=b for a,b in pairs)})
            previous_key=key
    return {'kind':'real_and_synthetic_separately','unit':'historical_first-question_record',
        'source_sha256':sha256(path),'rule_source_sha256':sha256(root/'aiv/annotation.py'),
        'sample_file_sha256':sha256(sample_path),'summary_file_sha256':sha256(summary_path),
        'input_canonical_sha256':input_digest,'sample_records_match_annotations':True,'stored_summary_counts_reconciled':True,
        'by_kind':groups,'comparisons':table,'original_rules_recomputed':True,
        'baseline_model':rows[0]['independent'][0]['model'],
        'rule':'Three nonnull task votes required before majority; at least two equal; valid arbitration overrides peer vote.',
        'scope':'Exploratory pilot under older models/prompts/output limits, 6 real and 6 engineered records; not t3 ablation or independent accuracy.'},table


def build(root=ROOT,output=None,build_dir=None,plots=True):
    start=time.perf_counter()
    output=output or root/'results/final-analysis'
    build_dir=build_dir or root/'build/final_analysis_20260927'
    output.mkdir(parents=True,exist_ok=True);build_dir.mkdir(parents=True,exist_ok=True)
    data=load_frozen(root/PLAN,PIN,root=root)
    real=[r for r in data.rows if r['record']['term']!='synthetic']
    students=student_indicators(data)
    if len(real)!=3515 or len(students)!=401 or len(data.plan['audit_ids'])!=352:
        raise ValueError('Formal population denominators changed')
    descriptive,distribution,joint,joint_rows=distribution_analysis(real)
    flow,transitions=flow_analysis(data,real)
    human,roles=human_analysis(root/'runtime/research/review.sqlite3')
    missing,contrasts=missing_analysis(real)
    sensitivity=score_sensitivity(data,real)
    association=association_analysis(data,real)
    redteam=redteam_analysis(root)
    pilot,pilot_rows=pilot_analysis(root)
    tables={'descriptive.csv':descriptive,'level-distribution.csv':distribution,'joint-levels.csv':joint_rows,
            'flow-comparison.csv':flow,'flow-transitions.csv':transitions,'human-role-timing.csv':roles,
            'unknown-label-bounds.csv':missing,'term-contrast-bounds.csv':contrasts,'score-range-sensitivity.csv':sensitivity,
            'pilot-exploration.csv':pilot_rows}
    for name,rows in tables.items():write_csv(output/name,rows)
    sources={name:sha256(root/name) for name in ['runtime/research/review.sqlite3','results/synthetic/redteam.csv',
        'results/synthetic/text-redteam.csv','aiv/models.py','aiv/association.py','aiv/fast_strong_workflow.py','aiv/full_turn_reporting.py',
        'aiv/annotation.py','runtime/research/pilot-v1-c49671a0bb96/annotations.json',
        'runtime/research/pilot-v1-c49671a0bb96/sample.json','runtime/research/pilot-v1-c49671a0bb96/summary.json',
        'scripts/build_final_analysis.py']}
    summary={'schema_version':1,'created_utc':datetime.now(timezone.utc).isoformat(),'source':data.source,'source_hashes':sources,
        'analysis_metadata':{
            'label_distributions':{'kind':'real','unit':'candidate-labelled turn','denominator':'separate task/contribution candidates; joint requires both nonempty on the same turn'},
            'student_equal_descriptions':{'kind':'real','unit':'student-by-term','denominator':'coverage averages all 401 units; label means average only candidate-bearing units of the relevant dimension'},
            'flow':{'kind':'real','unit':'same input turn','denominator':'352 fixed-random or 827 separately selected high-risk turns; never pooled for a representative claim'},
            'human':{'kind':'real','unit':'human label-review submission','denominator':'16 same-item/role pairs; 8 paired items for A/B agreement; kappa excludes any abstention'},
            'missing_label_bounds':{'kind':'conditional','unit':'ABL raw L1-L6; HOT fraction 0-1','denominator':'all eligible turns; fixed candidate labels plus arbitrary L1-L6 replacements for every unknown'},
            'score_ranges':{'kind':'conditional','unit':'score points 0-100','denominator':'356 candidate-bearing student-by-term units; 37747 within-term unordered pairs; 45 units without candidates excluded'},
            'score_epsilon':'ceil(epsilon * candidate_n) labels may change; independent metric boxes conservatively relax their joint feasibility',
            'weight_change':'Each base weight independently changes by at most ±10% multiplicatively, then all five weights are renormalized; all 32 vertices are evaluated.',
            'uncertainty':'No new statistical CI is constructed; deterministic outer ranges and descriptive comparisons retain their source-specific scope.'},
        'denominators':{'real_turns':len(real),'student_terms':len(students),'task_candidates':sum(final_label(r) is not None for r in real),
            'contribution_candidates':sum(final_label(r,'contribution') is not None for r in real),'random_review':352,
            'high_risk_review':sum(r['review_selection']=='high_risk' for r in real),'human_same_item_role_pairs':16,
            'students_with_task_candidates':sum(s['candidate_turns']>0 for s in students),'full_AIV_available':0},
        'descriptive':descriptive,'joint':joint,'flow':flow,'human':human,'human_role_timing':roles,
        'flow_merge_rule':{
            'consensus_priority':['nonterminal_or_missing_expected_entries -> pending','missing_judgment -> technical_failure',
                'unequal_labels_including_null -> disagreement','all_null -> abstained',
                'task_label_nonnull_but_any_uncertain -> uncertain','equal_nonnull -> agreed'],
            'final_priority':['not_selected -> fast_consensus','review_agreed_or_abstained -> review',
                'review_technical_failure_and_fast_agreed_or_abstained -> fast_review_incomplete','otherwise -> review_status'],
            'source':'aiv/fast_strong_workflow.py:consensus/resolve_dimension','verified_on_all_frozen_rows':True},
        'missing_label_bounds':missing,'term_contrast_bounds':contrasts,'association':association,
        'score_range_sensitivity':sensitivity,'redteam':redteam,'pilot_exploration':pilot,
        'figures':{name:{'png':f'results/final-analysis/{name}.png','pdf':f'results/final-analysis/{name}.pdf'} for name in
            ('fig01_dimensions','fig02_random_flow','fig03_unknown_labels','fig04_human_pairs','fig05_score_sensitivity')},
        'interpretation':{'real':'Observed source-backed model candidates or human label-review submissions; not ground truth.',
            'conditional':'Deterministic sensitivity over stated unknown-label/label-error/weight assumptions; not 95% confidence or population ability.',
            'synthetic':'Engineered inputs; separate from real students.',
            'score_ranges':'356 candidate-bearing student-term units only; CTQ unknown for all and tools unknown for 99. Zero label error does not remove missing-metric width.',
            'stable_pairs':'Within-term pairs whose conservative outer score ranges do not overlap. Sufficient certificate only, not observed true ranking.',
            'label_error_budget':'max_changed=ceil(epsilon*n); for small n the effective change fraction exceeds nominal epsilon. Separation uses 1e-10 numerical tolerance.',
            'flow':'Same turns under each method within the fixed random or separately selected risk stratum. Production uncertain and failed-review fallback rules retained; no accuracy claim.'},
        'checks':{**data.checks,'production_final_merge_recomputed':True,'aggregate_outputs_only':True,'no_model_calls':True}}
    safe_json(output/'summary.json',summary)
    if plots:make_plots(summary,tables,output)
    after=load_frozen(root/PLAN,PIN,root=root)
    if after.source!=data.source:
        raise ValueError('Frozen source changed during build')
    hashes={p.name:sha256(p) for p in sorted(output.iterdir()) if p.is_file()}
    receipt={'status':'computed','source_manifest_sha256':PIN,'seconds':time.perf_counter()-start,'files':hashes,
        'script_sha256':sha256(Path(__file__)),'no_model_calls':True,'raw_or_individual_exports':False,
        'visual_review':'pending' if plots else 'not_generated'}
    safe_json(build_dir/'receipt.json',receipt)
    return summary,receipt


def make_plots(summary,tables,output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    available={f.name for f in font_manager.fontManager.ttflist}
    font=next((f for f in ['Microsoft YaHei','Noto Sans CJK SC','SimHei','SimSun'] if f in available),None)
    if font is None:raise RuntimeError('No installed Chinese plotting font')
    plt.rcParams.update({'font.family':font,'font.size':11,'axes.titlesize':14,'axes.labelsize':11,'axes.unicode_minus':False,
        'pdf.fonttype':42,'ps.fonttype':42,'axes.spines.top':False,'axes.spines.right':False,'savefig.facecolor':'white'})
    blue='#245A81';orange='#C47B29';gray='#697680'
    def save(fig,name,note):
        fig.text(.06,.025,note,fontsize=9,color='#4A5055',ha='left',va='bottom')
        fig.subplots_adjust(bottom=.19,top=.88,wspace=.32)
        fig.savefig(output/(name+'.png'),dpi=210,bbox_inches='tight')
        fig.savefig(output/(name+'.pdf'),bbox_inches='tight')
        plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(12,5.2))
    for dimension,color,offset,label in [('task',blue,-.18,'任务候选 n=1010'),('contribution',orange,.18,'贡献候选 n=776')]:
        rows=[r for r in tables['level-distribution.csv'] if r['term']=='all' and r['dimension']==dimension]
        vals=[100*r['candidate_share'] for r in rows]
        axes[0].bar(np.arange(1,7)+offset,vals,width=.34,color=color,label=label)
    axes[0].set(xticks=range(1,7),xticklabels=[f'L{x}' for x in range(1,7)],ylabel='各维候选中的比例（%）',title='任务要求与已展示贡献的六级分布')
    axes[0].legend(frameon=False,fontsize=9)
    rows=[r for r in tables['joint-levels.csv'] if r['term']=='all']
    matrix=np.array([r['count'] for r in rows]).reshape(6,6)
    im=axes[1].imshow(matrix,cmap='Blues',vmin=0)
    for i in range(6):
        for j in range(6):axes[1].text(j,i,str(matrix[i,j]),ha='center',va='center',fontsize=10,color='white' if matrix[i,j]>matrix.max()*.5 else '#203040')
    axes[1].set(xticks=range(6),yticks=range(6),xticklabels=[f'L{i}' for i in range(1,7)],yticklabels=[f'L{i}' for i in range(1,7)],
        xlabel='贡献候选层级',ylabel='任务候选层级',title=f"同回合共同非空 n={summary['joint']['paired_denominator']}")
    fig.colorbar(im,ax=axes[1],label='回合数',fraction=.047,pad=.03)
    save(fig,'fig01_dimensions','真实数据 · 3515 个回合；左图按各自候选分母，右图仅共同非空配对。候选标签不等于独立能力。')
    fig,axes=plt.subplots(1,2,figsize=(12,5.1),sharex=True)
    palette={'agreed':blue,'abstained':'#C3CCD2','disagreement':orange,'uncertain':'#D6BC8A','technical_failure':'#3D4247'}
    for ax,dimension,title in zip(axes,['task','contribution'],['任务维度','贡献维度']):
        rows=[r for r in tables['flow-comparison.csv'] if r['stratum']=='random_audit' and r['dimension']==dimension]
        left=np.zeros(len(rows))
        for state in STATES:
            vals=np.array([r[state] for r in rows]);ax.barh(np.arange(4),vals/352*100,left=left/352*100,color=palette[state],label=STATE_NAMES[state])
            for i,v in enumerate(vals):
                if v>=22:ax.text((left[i]+v/2)/352*100,i,str(v),ha='center',va='center',fontsize=10,color='white' if state=='agreed' else '#202629')
            left+=vals
        ax.set(yticks=range(4),yticklabels=['DeepSeek 单快','GLM 单快','双快严格一致','正式复核合并'],xlabel='固定 352 回合中的比例（%）',title=title,xlim=(0,100));ax.invert_yaxis()
    handles,labels=axes[0].get_legend_handles_labels();fig.legend(handles,labels,loc='upper center',bbox_to_anchor=(.5,.99),ncol=5,frameon=False)
    save(fig,'fig02_random_flow','真实数据 · 固定随机复核 n=352；条内数字为回合数。沿用不确定判定与复核失败回退；高风险 827 回合另表。')
    fig,axes=plt.subplots(1,2,figsize=(12,5.2))
    rows=[r for r in tables['unknown-label-bounds.csv'] if r['term']!='all' and r['dimension']=='task']
    for i,r in enumerate(rows):
        for ax,metric in zip(axes,['abl_raw','hot']):
            factor=100 if metric=='hot' else 1
            lo=r[f'{metric}_lower']*factor;hi=r[f'{metric}_upper']*factor;point=r[f'candidate_{metric}']*factor
            ax.plot([lo,hi],[i,i],color=blue,lw=9,alpha=.23,solid_capstyle='butt');ax.plot([lo,hi],[i,i],'|',color=blue,markersize=18)
            ax.plot(point,i,'o',color=orange,markersize=8);ax.text(lo,i+.13,f'{lo:.2f} — {hi:.2f}',fontsize=11,color=blue)
        axes[0].text(1.03,i-.23,f"候选 {r['candidate_turns']}/{r['total_turns']}，未知 {r['unknown_turns']}",fontsize=10)
    for ax in axes:ax.set(yticks=[0,1],yticklabels=['秋季','春季'],ylim=(-.5,1.5));ax.grid(axis='x',alpha=.18)
    axes[0].set(xlim=(1,6),xlabel='平均认知层级 ABL（L1–L6）',title='全回合平均层级的未知标签范围')
    axes[1].set(xlim=(0,100),xlabel='高阶比例 HOT（%）',title='全回合高阶比例的未知标签范围')
    save(fig,'fig03_unknown_labels','条件情景 · 蓝段将无候选回合取任意 L1–L6；橙点仅为候选子集。保持候选标签固定；范围不是置信区间。')
    fig,axes=plt.subplots(1,2,figsize=(12,5.2))
    role_rows=tables['human-role-timing.csv']
    for role,color in [('A',blue),('B',orange)]:
        vals=[next(r['person_minutes'] for r in role_rows if r['role']==role and r['round']==rnd) for rnd in ['retest-v2','guided-v3']]
        axes[0].plot([0,1],vals,'o-',color=color,lw=2,label=f'评审角色 {role}')
        for x,y in enumerate(vals):axes[0].text(x+.04,y,f'{y:.2f}',fontsize=11,color=color,va='center')
    axes[0].set(xticks=[0,1],xticklabels=['第二轮独立盲评','第三轮同题辅助'],ylabel='记录用时（人分钟）',ylim=(0,17),xlim=(-.18,1.45),title='相同 8 题 × 2 个评审角色')
    axes[0].legend(frameon=False,loc='lower left')
    before=summary['human']['rounds']['retest-v2'];after=summary['human']['rounds']['guided-v3']
    vals=[before['agreement_including_abstention']*100,after['agreement_including_abstention']*100]
    axes[1].bar([0,1],vals,color=[blue,orange],width=.5)
    for i,(v,n) in enumerate(zip(vals,[before['exact_including_abstention'],after['exact_including_abstention']])):axes[1].text(i,v+3,f'{n}/8 = {v:.0f}%',ha='center')
    axes[1].set(xticks=[0,1],xticklabels=['独立盲评','同题辅助'],ylabel='包括共同弃权的完全一致率（%）',ylim=(0,100),title='双人完全一致：4/8 → 6/8')
    save(fig,'fig04_human_pairs',f"真实人审 · 16 组成对提交；{summary['human']['before_person_minutes']:.2f} → {summary['human']['after_person_minutes']:.2f} 人分钟；辅助 {after['exact_including_abstention']}/8 含 {after['both_abstained']} 题共同弃权。\n同题重复、共同推荐和固定顺序；时间包括停顿，属于过程观察。")
    fig,axes=plt.subplots(1,2,figsize=(12,5.2))
    for scheme,color,marker in [('balanced',blue,'o'),('higher_order',orange,'s'),('process','#69744B','^')]:
        rows=[r for r in tables['score-range-sensitivity.csv'] if r['term']=='all' and r['scheme']==scheme and r['weight_relative_change']==.1]
        x=[r['label_error_fraction']*100 for r in rows]
        axes[0].plot(x,[r['width_median'] for r in rows],marker=marker,color=color,lw=1.8,label=SCHEME_NAMES[scheme])
        axes[1].plot(x,[r['stable_pair_fraction']*100 for r in rows],marker=marker,color=color,lw=1.8,label=SCHEME_NAMES[scheme])
    for ax in axes:ax.set(xticks=[0,5,10,20],xlabel='名义改标预算 ε（%）；条数 ceil(εn)');ax.grid(alpha=.18)
    axes[0].set(ylabel='条件分数外包范围宽度中位数（分）',ylim=(0,100),title='标签误差与缺失扩大范围')
    axes[1].set(ylabel='同学期可稳定区分的学生对（%）',ylim=(0,100),title='同学期的条件性稳定区分')
    for ax in axes:
        ax.set_yticks([0,20,40,60,80,100])
        ax.tick_params(axis='y',labelleft=True)
    axes[0].legend(frameon=False)
    save(fig,'fig05_score_sensitivity','条件情景 · 356 个有候选的学生×学期；权重 ±10%。小样本向上取整可超过名义比例；非总体能力或真实排名。')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-plots',action='store_true')
    parser.add_argument('--out',type=Path,default=ROOT/'results/final-analysis')
    parser.add_argument('--build-dir',type=Path,default=ROOT/'build/final_analysis_20260927')
    args=parser.parse_args()
    # Fail closed if any accidental dependency attempts an external connection.
    def block_network(event,args):
        if event in {'socket.connect','socket.getaddrinfo'}:
            raise RuntimeError('Final aggregate analysis is offline')
    sys.addaudithook(block_network)
    result,receipt=build(output=args.out,build_dir=args.build_dir,plots=not args.no_plots)
    print(json.dumps({'denominators':result['denominators'],'association':result['association'],
        'output':str(args.out),'seconds':receipt['seconds']},ensure_ascii=False))
