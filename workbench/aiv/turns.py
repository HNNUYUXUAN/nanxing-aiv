"""Deterministic, provenance-preserving parsing of CSV Q/A fields.

The resulting sessions are candidates confined to one exported CSV row.  No
inter-row conversation link or per-turn timestamp is inferred.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from pathlib import Path
import json
import re

import pandas as pd

from aiv.data import load_competition


MARKER = re.compile(r"(?m)^[ \t]*([QA])[:：][ \t]*")
URL_ONLY = re.compile(r"https?://\S+\Z")
SCHEMA_VERSION = "csv-qa-turns-v1"


def _sha(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def parse_qa_record(record: dict, raw_text: str) -> tuple[list[dict], dict]:
    """Parse one selected CSV row; offsets index the decoded 问答记录 string."""

    markers = list(MARKER.finditer(raw_text))
    sequence = "".join(match.group(1) for match in markers)
    alternating = bool(markers) and sequence[0] == "Q" and all(
        sequence[j] != sequence[j + 1] for j in range(len(sequence) - 1)
    )
    issues: list[str] = []
    if not markers:
        issues.append("no_line_start_markers")
    elif not alternating:
        issues.append("non_alternating_qa")
    if alternating and sequence.endswith("Q"):
        issues.append("last_question_has_no_answer")
    if len(markers) > 2 and raw_text.count("\nQ:") + raw_text.count("\nQ：") == 0:
        # Marker indentation or carriage returns require inspection, not rejection.
        issues.append("unusual_question_marker_format")

    result = []
    q_index = 0
    empty_count = 0
    for message_index, match in enumerate(markers, start=1):
        marker_role = match.group(1)
        if marker_role == "Q":
            q_index += 1
        next_start = markers[message_index].start() if message_index < len(markers) else len(raw_text)
        untrimmed = raw_text[match.end() : next_start]
        left = len(untrimmed) - len(untrimmed.lstrip())
        right = len(untrimmed.rstrip())
        start, end = match.end() + left, match.end() + right
        body = raw_text[start:end]
        if not body:
            empty_count += 1
        role = "student" if marker_role == "Q" else "assistant"
        model_eligible = role == "student" and alternating and bool(body) and not URL_ONLY.fullmatch(body)
        parse_status = (
            "structural_alternating_unverified" if alternating else "needs_record_review"
        )
        source = {
            "file": record["source_file"].replace("\\", "/"),
            "row": record["source_row"],
            "sha256": record["source_sha256"],
            "record_id": record["id"],
            "field": "问答记录",
            "char_start": start,
            "char_end": end,
            "marker_start": match.start(),
            "marker_end": match.end(),
            "field_sha256": _sha(raw_text),
        }
        turn_id = _sha(
            f"{SCHEMA_VERSION}|{record['id']}|{message_index}|{role}|{body}"
        )[:24]
        result.append(
            {
                "turn_id": turn_id,
                "student_id": record["student"],
                "term": record["term"],
                "text": body,
                "role": role,
                "agent": record.get("agent"),
                "session_candidate_id": f"csvrow-{record['id']}",
                "session_status": "within_export_row_only_unverified",
                "turn_index": q_index,
                "message_index": message_index,
                "record_timestamp": record["timestamp"],
                "turn_timestamp": None,
                "parse_status": parse_status,
                "model_eligible": bool(model_eligible),
                "source": source,
            }
        )
    if empty_count:
        issues.append("empty_qa_segment")
    return result, {
        "record_id": record["id"],
        "source_file": record["source_file"].replace("\\", "/"),
        "source_row": record["source_row"],
        "session_candidate_id": f"csvrow-{record['id']}",
        "marker_sequence": sequence,
        "question_count": sum(item["role"] == "student" for item in result),
        "answer_count": sum(item["role"] == "assistant" for item in result),
        "structural_alternating": alternating,
        "issues": issues,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build_turn_manifest(root: Path, output: Path) -> dict:
    """Build an ignored private package for the established 1225-row study cohort."""

    root = Path(root)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    selected, selection_audit = load_competition(root)
    tables = {
        term: pd.read_csv(root / f"data/{term}/csv/qa.csv", dtype=str).fillna("")
        for term in ("25f", "26s")
    }
    turns: list[dict] = []
    audits: list[dict] = []
    first_question_matches = 0
    for record in selected:
        text = tables[record["term"]].iloc[record["source_row"] - 2]["问答记录"]
        parsed, audit = parse_qa_record(record, text)
        first_question = next((item for item in parsed if item["role"] == "student"), None)
        if first_question and first_question["text"] == record["question"]:
            first_question_matches += 1
        for item in parsed:
            span = item["source"]
            if text[span["char_start"] : span["char_end"]] != item["text"]:
                raise ValueError(f"Broken source span for {item['turn_id']}")
        turns.extend(parsed)
        audits.append(audit)
    ids = [item["turn_id"] for item in turns]
    if len(set(ids)) != len(ids):
        raise ValueError("Turn IDs are not unique")
    if first_question_matches != len(selected):
        raise ValueError("Recovered first questions differ from the frozen selection")
    students = [row for row in turns if row["role"] == "student"]
    _write_jsonl(output / "turns.jsonl", turns)
    _write_jsonl(output / "student_turns.jsonl", students)
    _write_jsonl(output / "record_audit.jsonl", audits)

    issue_counts = Counter(issue for audit in audits for issue in audit["issues"])
    repeated_questions = sum(
        count - 1
        for count in Counter((item["term"], item["text"]) for item in students).values()
    )
    source_inventory = []
    for map_path in sorted((root / "data").glob("*/*/map.json")):
        doc = json.loads(map_path.read_text(encoding="utf-8"))
        relative_parent = map_path.parent.relative_to(root / "data")
        original = root / "archive" / "raw" / relative_parent.with_suffix(".docx")
        original_sha = sha256(original.read_bytes()).hexdigest() if original.exists() else None
        source_inventory.append(
            {
                "map_file": str(map_path.relative_to(root)).replace("\\", "/"),
                "docx_file": str(original.relative_to(root)).replace("\\", "/"),
                "docx_sha256": doc["source_sha256"],
                "source_hash_verified": original_sha == doc["source_sha256"],
                "paragraphs": len(doc.get("paragraphs", [])),
                "figures": len(doc.get("figures", [])),
                "media": len(doc.get("media", [])),
                "csv_join_status": "no_verified_join_key",
            }
        )
    (output / "docx_source_inventory.json").write_text(
        json.dumps(source_inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    term_counts = {
        term: {
            "selected_records": sum(a["source_file"].startswith(f"data/{term}/") for a in audits),
            "student_turns": sum(t["term"] == term for t in students),
            "model_eligible_student_turns": sum(
                t["term"] == term and t["model_eligible"] for t in students
            ),
        }
        for term in ("25f", "26s")
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "study_cohort": "aiv.data.load_competition selected CSV rows",
        "source_scope": "25f/26s QA CSV only; DOCX records are not automatically joined",
        "counts": {
            "selected_records": len(selected),
            "session_candidates": len(audits),
            "all_messages": len(turns),
            "student_turns": len(students),
            "assistant_turns": len(turns) - len(students),
            "model_eligible_student_turns": sum(t["model_eligible"] for t in students),
            "records_with_multiple_questions": sum(a["question_count"] > 1 for a in audits),
            "structural_alternating_records": sum(a["structural_alternating"] for a in audits),
            "records_needing_review": sum(not a["structural_alternating"] for a in audits),
            "repeated_question_instances": repeated_questions,
            "issues": dict(sorted(issue_counts.items())),
            "by_term": term_counts,
        },
        "checks": {
            "turn_id_unique": True,
            "source_text_spans_valid": True,
            "first_question_exact_matches": first_question_matches,
            "docx_maps": len(source_inventory),
            "docx_source_hashes_verified": sum(
                item["source_hash_verified"] for item in source_inventory
            ),
        },
        "selection_audit": {term: selection_audit[term] for term in ("25f", "26s")},
        "files": {
            filename: sha256((output / filename).read_bytes()).hexdigest()
            for filename in (
                "turns.jsonl",
                "student_turns.jsonl",
                "record_audit.jsonl",
                "docx_source_inventory.json",
            )
        },
        "boundaries": [
            "Q/A roles are inferred from line-start markers and require semantic validation.",
            "A CSV row is one session candidate, not a confirmed contiguous session.",
            "Record creation time is not the timestamp of any individual turn.",
            "No existing first-question label is assigned to a later turn.",
            "DOCX images/transcripts cannot be joined to CSV rows without a verified key.",
            "Output is private runtime material and must not be published with student text.",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
