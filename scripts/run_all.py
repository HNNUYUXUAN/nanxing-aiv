"""Reproduce the delivered results offline, using the current Python interpreter."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_package(root: Path) -> dict:
    path = root / "REPRODUCTION-MANIFEST.json"
    if not path.exists():
        return {"mode": "workspace", "verified_files": 0}
    manifest = json.loads(path.read_text("utf-8"))
    for relative, expected in manifest["files"].items():
        target = (root / relative).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            raise ValueError(f"Missing or unsafe package file: {relative}")
        if sha(target) != expected:
            raise ValueError(f"Package hash mismatch: {relative}")
    return {"mode": manifest["mode"], "verified_files": len(manifest["files"])}


@contextmanager
def preserve_notebook_inputs(root: Path):
    """Notebook execution may overwrite packaged figures as well as notebooks."""
    paths = set((root / "notebooks").glob("*.ipynb"))
    manifest = root / "REPRODUCTION-MANIFEST.json"
    if manifest.exists():
        for name in json.loads(manifest.read_text("utf-8"))["files"]:
            if name.startswith("build/notebooks/"):
                path = (root / name).resolve()
                if not path.is_relative_to(root.resolve()):
                    raise ValueError("Unsafe packaged Notebook input")
                paths.add(path)
    originals = {p: p.read_bytes() for p in paths}
    try:
        yield originals
    finally:
        for path, content in originals.items():
            path.write_bytes(content)


def same_numbers(left, right):
    """Compare parsed results independently of line endings and float formatting."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_numbers(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same_numbers(a, b) for a, b in zip(left, right))
    if type(left) in (int, float) and type(right) in (int, float):
        return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
    if isinstance(left, str) and isinstance(right, str):
        try:
            return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
        except ValueError:
            pass
    return left == right


def compare_baseline(expected: Path, actual: Path) -> int:
    from scripts.package_research import BASELINE_TABLES
    for name in BASELINE_TABLES:
        def read(path):
            if path.suffix == ".json":
                return json.loads(path.read_text("utf-8"))
            with path.open(encoding="utf-8-sig", newline="") as stream:
                return list(csv.DictReader(stream))
        if not same_numbers(read(expected / name), read(actual / name)):
            raise ValueError(f"Historical baseline recomputation differs: {name}")
    return len(BASELINE_TABLES)


def offline_environment(root: Path) -> dict:
    """Deny external sockets in child processes, while allowing Jupyter localhost."""
    guard = root / "build/offline-guard"
    guard.mkdir(parents=True, exist_ok=True)
    (guard / "sitecustomize.py").write_text('''import ipaddress, socket
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo
def _local(host):
    if host in (None, '', 'localhost', 'localhost.localdomain'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
def _check(address):
    if isinstance(address, tuple) and not _local(address[0]):
        raise RuntimeError('Offline reproduction blocks external network access')
def connect(self, address):
    _check(address)
    return _connect(self, address)
def connect_ex(self, address):
    _check(address)
    return _connect_ex(self, address)
def getaddrinfo(host, *args, **kwargs):
    if not _local(host):
        raise RuntimeError('Offline reproduction blocks external DNS access')
    return _getaddrinfo(host, *args, **kwargs)
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = getaddrinfo
''', "utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(guard), str(root)])
    env["PYTHONUTF8"] = "1"
    env["MPLBACKEND"] = "Agg"
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    for key in list(env):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(key)
    return env


def reproduce(*, notebooks: bool = True, root: Path = ROOT) -> dict:
    started = time.monotonic()
    checked = verify_package(root)
    env = offline_environment(root)
    output = root / "build/reproduction"
    output.mkdir(parents=True, exist_ok=True)
    steps = []

    def run(label: str, *args: str) -> None:
        tick = time.monotonic()
        completed = subprocess.run([sys.executable, *args], cwd=root, env=env,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=1500)
        # Logs stay local; commands in this runner never read API credentials.
        (output / f"{label}.log").write_text(completed.stdout + completed.stderr, "utf-8")
        steps.append({"step": label, "returncode": completed.returncode,
                      "seconds": round(time.monotonic() - tick, 3)})
        if completed.returncode:
            raise RuntimeError(f"Reproduction step {label} failed; inspect build/reproduction/{label}.log")

    config_path = root / "notebooks/research-inputs.json"
    config = json.loads(config_path.read_text("utf-8"))
    entry = config.get("turn_snapshot")
    source_hash = None
    regenerated = None
    matched_results = 0
    baseline_matched = 0
    if checked["mode"] == "authorized_research" and not entry:
        raise ValueError("The authorized research package requires a pinned frozen snapshot")
    if entry:
        manifest = (root / entry["manifest_path"]).resolve()
        if not manifest.is_relative_to(root.resolve()) or sha(manifest) != entry["manifest_sha256"]:
            raise ValueError("Pinned snapshot hash mismatch")
        source_hash = sha(manifest)
        plan = manifest.parent.parent / "plan.json"
        regenerated = root / "runtime/research" / ("reproduction-" + uuid.uuid4().hex[:12])
        run("full-turn-materials", "scripts/build_full_turn_materials.py", "--plan", str(plan),
            "--manifest-sha256", source_hash, "--out", str(regenerated))
        run("model-verification", "scripts/verify_models.py", "--annotations",
            str(manifest.parent / "annotations.json"), "--output",
            str(output / "model-verification.json"))
        run("independent-statistics", "scripts/verify_full_turn_materials.py", "--plan",
            str(plan), "--materials", str(regenerated), "--output",
            str(output / "independent-statistics.json"))
        from scripts.package_research import BASELINE, BASELINE_RESULTS
        baseline_plan = root / BASELINE / "plan.json"
        if not baseline_plan.exists():
            raise ValueError("The research package requires the historical 384-record baseline")
        baseline_out = regenerated / "baseline384"
        run("baseline384", "scripts/build_solution_materials.py", "--plan", str(baseline_plan),
            "--out", str(baseline_out))
        baseline_matched = compare_baseline(root / BASELINE_RESULTS, baseline_out)
    run("analytic-checks", "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "tests/test_metrics.py", "tests/test_models.py", "tests/test_uncertainty.py",
        "tests/test_solution_materials.py", "tests/test_analysis.py")
    if notebooks:
        with preserve_notebook_inputs(root) as notebook_inputs:
            run("notebooks", "scripts/execute_notebooks.py")
            execution_path = root / "build/notebooks/execution.json"
            execution = json.loads(execution_path.read_text("utf-8"))
            executed_dir = output / "executed-notebooks"
            executed_dir.mkdir(exist_ok=True)
            for item in execution["results"]:
                source = root / "notebooks" / item["notebook"]
                target = executed_dir / item["notebook"]
                target.write_bytes(source.read_bytes())
                item["executed_notebook"] = target.relative_to(root).as_posix()
                item["source_notebook_sha256_after_restore"] = hashlib.sha256(notebook_inputs[source]).hexdigest()
                if sha(target) != item["sha256"]:
                    raise ValueError("Executed Notebook receipt mismatch")
            execution_path.write_text(json.dumps(execution, ensure_ascii=False, indent=2) + "\n", "utf-8")
    if entry and (root / "deliverables/student-indicators.csv").exists():
        expected = root / "deliverables/student-indicators.csv"
        actual = regenerated / "student-indicators.csv"
        if sha(expected) != sha(actual):
            raise ValueError("Recomputed student table differs from delivered CSV")
    if regenerated and config.get("turn_materials"):
        derived_path = (root / config["turn_materials"]["manifest_path"]).resolve()
        if not derived_path.is_relative_to(root.resolve()) or sha(derived_path) != config["turn_materials"]["manifest_sha256"]:
            raise ValueError("Pinned derived material hash mismatch")
        derived = json.loads(derived_path.read_text("utf-8"))
        for name, expected in derived["files"].items():
            if sha(regenerated / name) != expected:
                raise ValueError(f"Recomputed result differs: {name}")
            matched_results += 1
    receipt = {"schema_version": "nanxing-reproduction-v1", "status": "passed",
               "package": checked, "snapshot_manifest_sha256": source_hash,
               "network": "external_sockets_blocked_localhost_only", "live_api": False,
               "notebooks_executed": notebooks, "steps": steps,
               "recomputed_materials": str(regenerated.relative_to(root)) if regenerated else None,
               "result_files_matched": matched_results,
               "baseline_tables_matched": baseline_matched,
               "seconds": round(time.monotonic() - started, 3)}
    if receipt["seconds"] >= 1800:
        raise RuntimeError("Core reproduction exceeded 30 minutes")
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-only", action="store_true", help="Skip Notebook rendering")
    args = parser.parse_args()
    print(json.dumps(reproduce(notebooks=not args.core_only), ensure_ascii=False))
