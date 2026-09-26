"""Explicit product boundaries. Configuration values never enter API responses."""
from pathlib import Path
import hmac
import os
from dotenv import dotenv_values

CODE_ROOT = Path(__file__).resolve().parents[1]


def settings():
    research = Path(os.getenv('WORKBENCH_RESEARCH_ROOT', str(CODE_ROOT / 'research'))).resolve()
    return dict(code_root=str(CODE_ROOT), research_root=str(research),
                runtime_root=str(Path(os.getenv('WORKBENCH_RUNTIME_ROOT', str(CODE_ROOT / 'runtime/product'))).resolve()),
                credentials_file=str(Path(os.getenv('WORKBENCH_CREDENTIALS_FILE', str(research / '.env'))).resolve()))


def token_ok(token):
    paths=settings()
    cfg = dotenv_values(paths['credentials_file'])
    gate_path=Path(os.getenv('WORKBENCH_GATE_FILE',str(Path(paths['research_root'])/'deploy/workbench.env')))
    gate=dotenv_values(gate_path) if gate_path.exists() else {}
    expected = os.getenv('WORKBENCH_TOKEN') or cfg.get('WORKBENCH_TOKEN') or gate.get('WORKBENCH_TOKEN') or cfg.get('REVIEW_TOKEN_C')
    return bool(expected and token and hmac.compare_digest(str(expected), str(token)))
