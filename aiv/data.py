"""Local competition ingestion; exports private rows only to runtime/."""

from pathlib import Path
import csv
import hashlib
import json
import re
import pandas as pd


def question_only(text):
    match = re.match(r"^Q[:：]\s*(.*?)(?:\n\s*A[:：]|$)", text, flags=re.S)
    return match.group(1).strip() if match else None


def load_competition(root):
    root = Path(root)
    roster = set()
    for path in (root / "data/26s/csv").glob("*students.csv"):
        with path.open(encoding="utf-8-sig", newline="") as f:
            table = list(csv.reader(f))
        for row in table[2:]:
            if row and row[0].strip().isdigit():
                record = dict(zip(table[1], row))
                roster.add(record.get("学号", "").strip())
    records = []
    audit = {}
    for term, start, end in [
        ("25f", "2025-09-01", "2026-03-01"),
        ("26s", "2026-03-01", "2026-09-01"),
    ]:
        path = root / f"data/{term}/csv/qa.csv"
        raw = pd.read_csv(path, dtype=str).fillna("")
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        dates = pd.to_datetime(raw["问题建立时间"], errors="coerce")
        window = (dates >= start) & (dates < end)
        in_roster = (
            raw["学号"].str.strip().isin(roster)
            if term == "26s"
            else pd.Series(True, index=raw.index)
        )
        seen = set()
        stats = dict(
            raw_rows=len(raw),
            invalid_dates=int(dates.isna().sum()),
            outside_window=int((~window).sum()),
            roster_unmatched=int((~in_roster).sum()),
            duplicate_rows=0,
            no_text_question=0,
            selected_rows=0,
        )
        for i, row in raw[window & in_roster].iterrows():
            question = question_only(row["问答记录"])
            signature = hashlib.sha256(
                json.dumps(row.to_dict(), ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            if signature in seen:
                stats["duplicate_rows"] += 1
                continue
            seen.add(signature)
            if not question or re.fullmatch(r"https?://\S+", question):
                stats["no_text_question"] += 1
                continue
            stats["selected_rows"] += 1
            student = hashlib.sha256(
                ("aiv-private-v1|" + row["学号"].strip()).encode()
            ).hexdigest()[:16]
            records.append(
                dict(
                    id=signature[:20],
                    student=student,
                    term=term,
                    timestamp=str(dates[i]),
                    question=question,
                    agent=row.get("智能体类型") or None,
                    source_file=str(path.relative_to(root)),
                    source_row=int(i + 2),
                    source_sha256=source_hash,
                    session=None,
                    session_status="not_supplied_do_not_infer_conversation_edges",
                    label=None,
                    label_status="pending",
                    confidence=None,
                )
            )
        audit[term] = stats
    audit["boundaries"] = [
        "Spring roster is a scope filter, not identity verification.",
        "No independent pre/post learning test supplied.",
        "No session identifier: real CTQ cannot use arbitrary adjacent CSV rows.",
        "Spring QA lacks agent type: MAB and full five-component AIV are not identifiable from this table.",
        "Image-only questions require separate image review; excluded from text subset with counts.",
    ]
    return records, audit


def synthetic_records(seed=26, students=24):
    import numpy as np

    rng = np.random.default_rng(seed)
    examples = [
        "请列举线性规划的基本要素。",
        "请解释线性规划可行域的含义，并举一个例子。",
        "我把每件产品的成本代入约束，得到 2x+3y≤120，请检查代入过程。",
        "我比较了两种假设：固定需求忽略了季节性，随机需求会改变库存约束，请分析影响。",
        "模型假设观测独立，但同一个学生有多次记录。我认为标准误偏小，应按学生聚类，你怎么看？",
        "我提出一个新方案：先按成本分层抽样，再用残差校正标签，最后用模拟检验覆盖率。请质疑我的设计。",
    ]
    rows = []
    for s in range(students):
        for j in range(8):
            label = int(rng.integers(1, 7))
            text = examples[label - 1]
            rows.append(
                dict(
                    id=f"synthetic-{s}-{j}",
                    student=f"S{s + 1:03d}",
                    term="synthetic",
                    timestamp=f"2026-01-{j + 1:02d}",
                    question=text,
                    agent=["探知侠", "逆行侠", "数模全才", "数模匹配"][
                        int(rng.integers(0, 4))
                    ],
                    session=f"{s}-session",
                    label=label,
                    confidence=0.8,
                    source="synthetic teaching examples",
                    label_status="constructed_truth",
                    evidence=text[: min(36, len(text))],
                    student_contribution=label >= 3,
                )
            )
    return rows
