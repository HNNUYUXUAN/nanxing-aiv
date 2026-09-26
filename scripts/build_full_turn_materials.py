"""Build private full-turn reports from an explicitly hash-pinned frozen t3 snapshot."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiv.full_turn_reporting import SCHEMA, build_reports, load_frozen, sha256


def build(plan: Path, expected_manifest_sha256: str, output: Path, *, root=ROOT,
          expected_student_terms=401, draws=2000) -> dict:
    root, output = root.resolve(), output.resolve()
    if not output.is_relative_to(root / "runtime/research"):
        raise ValueError("Student-level exports must stay in private runtime/research")
    if output.exists():
        raise FileExistsError("Use a new material directory; existing results are preserved")
    data = load_frozen(plan, expected_manifest_sha256, root=root)
    reports, students = build_reports(data, draws=draws)
    if len(students) != expected_student_terms:
        raise ValueError("Unexpected student-term denominator")
    output.mkdir(parents=True)
    for filename, report in reports.items():
        (output / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                       encoding="utf-8", newline="\n")
    with (output / "student-indicators.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(students[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(students)
    manifest = {"schema_version": SCHEMA, "source": data.source,
                "files": {p.name: sha256(p) for p in sorted(output.iterdir()) if p.is_file()},
                "code_hashes": {name: sha256(ROOT / name) for name in (
                    "aiv/full_turn_reporting.py", "scripts/build_full_turn_materials.py", "aiv/metrics.py")},
                "seed": 26, "bootstrap_draws": draws, "no_model_calls": True,
                "privacy": "Private student-indicators.csv contains pseudonymous student keys; JSON reports are aggregates only."}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8", newline="\n")
    return {"output": str(output), "manifest_sha256": sha256(output / "manifest.json"),
            "source": data.source, "denominators": reports["summary.json"]["denominators"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "runtime/research/full-turn-materials-t3-v1")
    args = parser.parse_args()
    print(json.dumps(build(args.plan, args.manifest_sha256, args.out), ensure_ascii=False))
