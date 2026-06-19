from __future__ import annotations

import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(find_dotenv(), override=True)


def _envText(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default).strip()


def _envFlag(name: str, default: bool = False) -> bool:
    rawValue = os.getenv(name)
    if rawValue is None:
        return bool(default)
    return rawValue.strip().lower() in {"1", "true", "yes", "on"}


def _envInt(name: str, default: int = 0) -> int:
    rawValue = os.getenv(name)
    if rawValue is None:
        return int(default)
    try:
        return int(str(rawValue).strip())
    except (TypeError, ValueError):
        return int(default)
