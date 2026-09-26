"""Pin an already frozen, private full-turn snapshot for offline notebooks."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiv.notebook_materials import load_turn_snapshot


CONFIG = ROOT / "notebooks/research-inputs.json"


def connect(manifest: Path, expected_revision: str | None = None,
            expected_sha256: str | None = None) -> dict:
    manifest = manifest.resolve()
    if not manifest.is_relative_to(ROOT):
        raise ValueError("The frozen manifest must be inside this workspace")
    relative = manifest.relative_to(ROOT).as_posix()
    entry = {"manifest_path": relative,
             "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
    if expected_sha256 and entry["manifest_sha256"] != expected_sha256:
        raise ValueError("Frozen manifest does not match the requested SHA-256")
    current = json.loads(CONFIG.read_text("utf-8"))
    current["turn_snapshot"] = entry
    candidate = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", prefix="notebook-snapshot-",
                                         dir=CONFIG.parent, encoding="utf-8",
                                         delete=False) as stream:
            candidate = Path(stream.name)
            json.dump(current, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        result = load_turn_snapshot(candidate)
        if expected_revision and result["revision"] != expected_revision:
            raise ValueError("Frozen revision does not match the requested revision")
        os.replace(candidate, CONFIG)
    finally:
        if candidate and candidate.exists():
            candidate.unlink()
    return {"experiment": result["experiment"], "revision": result["revision"],
            "status": result["status"], "planned_real": result["planned_real"],
            "completed_real": result["completed_real"],
            "candidate_labeled_real": result["candidate_labeled_real"],
            "source": result["source"], "source_sha256": result["source_sha256"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-revision")
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    print(json.dumps(connect(args.manifest, args.expected_revision, args.expected_sha256), ensure_ascii=False))
