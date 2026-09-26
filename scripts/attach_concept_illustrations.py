"""Attach reviewed concept figures without changing Notebook calculations.

Default: read-only preflight. --apply adds deterministic Markdown cells and
copies the same PNG bytes beside exported HTML. Run after Notebook generation
and before scripts/execute_notebooks.py. No network or model calls are made.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile

import nbformat

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "assets/illustrations/manifest.json"
TAG = "concept-illustration-v1"
FIGURE_IDS = (
    "process-overview", "system-architecture", "model-review",
    "evidence-chain", "reproducibility",
)


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe illustration path: {relative}")
    result = (root / path).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f"Illustration escaped workspace: {relative}")
    return result


def load_manifest(root: Path) -> dict:
    value = json.loads((root / MANIFEST).read_text("utf-8"))
    rows = value.get("figures", [])
    if value.get("schema_version") != "nanxing-concept-illustrations-v1":
        raise ValueError("Unsupported illustration manifest")
    if len(rows) != len(FIGURE_IDS) or {r.get("id") for r in rows} != set(FIGURE_IDS):
        raise ValueError("The five concept figure IDs must be unique and complete")
    for row in rows:
        if row["file"] != f"assets/illustrations/{row['id']}.png":
            raise ValueError("Unexpected concept figure filename")
        for notebook in row["notebooks"]:
            if not notebook.startswith("notebooks/") or not notebook.endswith(".ipynb"):
                raise ValueError("Unexpected Notebook target")
            safe_path(root, notebook)
    return value


def review_assets(root: Path, manifest: dict) -> tuple[list[dict], list[dict]]:
    """Mechanical checks supplement, and never replace, visual acceptance."""
    assets, pending = [], []
    for row in manifest["figures"]:
        path = safe_path(root, row["file"])
        issues = []
        if row.get("qa") != "passed":
            issues.append("visual_review_pending")
        if not path.is_file():
            issues.append("image_missing")
        else:
            content = path.read_bytes()
            if (len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n"
                    or content[12:16] != b"IHDR"):
                issues.append("invalid_png_header")
            else:
                dimensions = list(struct.unpack(">II", content[16:24]))
                if min(dimensions) < 400 or max(dimensions) < 640:
                    issues.append("image_too_small")
                if row.get("native_dimensions") != dimensions:
                    issues.append("dimensions_not_bound")
            if not re.fullmatch(r"[0-9a-f]{64}", row.get("sha256") or ""):
                issues.append("hash_not_bound")
            elif sha(content) != row["sha256"]:
                issues.append("hash_mismatch")
        if issues:
            pending.append({"id": row["id"], "issues": issues})
        else:
            assets.append(row)
    return assets, pending


def package_asset_paths(root: Path) -> dict[str, str]:
    """ZIP destination -> reviewed source; called by an allowlisted packager.

    Source Notebooks, new HTML exports, and delivered HTML have three different
    parent directories. Every destination intentionally contains identical PNGs.
    """
    manifest = load_manifest(root)
    assets, pending = review_assets(root, manifest)
    if pending:
        raise ValueError("Concept illustration acceptance is incomplete")
    paths = {MANIFEST: MANIFEST}
    for row in assets:
        name = Path(row["file"]).name
        for folder in ("assets/illustrations", "build/assets/illustrations",
                       "deliverables/assets/illustrations"):
            paths[f"{folder}/{name}"] = row["file"]
    return paths


def cell_for(row: dict):
    source = (
        f"<!-- {TAG}:{row['id']} -->\n"
        f"### {row['title']}\n\n"
        f"![{row['alt']}](../{row['file']})\n\n"
        f"{row['caption']}"
    )
    cell = nbformat.v4.new_markdown_cell(source)
    cell["id"] = "concept-" + row["id"]
    cell.metadata["tags"] = [TAG]
    cell.metadata["concept_illustration"] = {
        "id": row["id"], "sha256": row["sha256"],
        "manifest": "../" + MANIFEST, "kind": "conceptual",
    }
    return cell


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False,
                                         prefix="." + path.name + ".") as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def attach(root: Path, *, apply: bool = False) -> dict:
    root = root.resolve()
    manifest = load_manifest(root)
    assets, pending = review_assets(root, manifest)
    if pending:
        return {"status": "awaiting_reviewed_images", "applied": False,
                "ready_images": len(assets), "pending": pending}
    targets: dict[str, list[dict]] = {}
    for row in assets:
        for relative in row["notebooks"]:
            targets.setdefault(relative, []).append(row)

    # Plan and validate every mutation before writing any Notebook or asset.
    edits: dict[Path, bytes] = {}
    notebook_results = []
    for relative, rows in sorted(targets.items()):
        path = safe_path(root, relative)
        original_bytes = path.read_bytes()
        nb = nbformat.reads(original_bytes.decode("utf-8"), as_version=4)
        nbformat.validate(nb)
        excluded = set(nb.metadata.get("concept_illustrations_excluded", []))
        rows = [row for row in rows if row["id"] not in excluded]
        code_before = [json.dumps(c, sort_keys=True, ensure_ascii=False)
                       for c in nb.cells if c.cell_type == "code"]
        cells = [c for c in nb.cells
                 if not (c.cell_type == "markdown" and
                         (TAG in c.metadata.get("tags", []) or
                          f"<!-- {TAG}:" in c.source))]
        insertion = 1 if cells and cells[0].cell_type == "markdown" else 0
        nb.cells = cells[:insertion] + [cell_for(row) for row in rows] + cells[insertion:]
        nbformat.validate(nb)
        if code_before != [json.dumps(c, sort_keys=True, ensure_ascii=False)
                           for c in nb.cells if c.cell_type == "code"]:
            raise AssertionError("Concept attachment changed a code cell")
        content = nbformat.writes(nb).encode("utf-8")
        changed = content != original_bytes
        if changed:
            edits[path] = content
        notebook_results.append({"notebook": relative, "figures": [r["id"] for r in rows],
                                 "changed": changed})
    for row in assets:
        content = safe_path(root, row["file"]).read_bytes()
        destination = safe_path(root, "build/assets/illustrations/" + Path(row["file"]).name)
        if not destination.is_file() or destination.read_bytes() != content:
            edits[destination] = content
    if apply:
        for path, content in edits.items():
            atomic_write(path, content)
    return {"status": "attached" if apply else "ready", "applied": apply,
            "images": len(assets), "notebooks": notebook_results,
            "files_changed" if apply else "files_would_change": len(edits),
            "code_cells_preserved": True,
            "next_step": "execute_notebooks.py regenerates HTML and hash-bound receipts"
                         if edits else "No changes required"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true", help="Read-only preflight (default)")
    args = parser.parse_args()
    result = attach(args.root, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "awaiting_reviewed_images" else 0


if __name__ == "__main__":
    raise SystemExit(main())
