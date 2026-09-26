"""Run one registered product batch; credential values never enter CLI arguments."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiv.product_runner import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    settings = json.loads((args.directory / "settings.json").read_text("utf-8"))
    result = run(args.directory, settings, token=args.token)
    return 0 if result["state"] in {"finished", "paused", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
