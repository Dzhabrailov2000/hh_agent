"""
Управление состоянием агента (применённые отклики, журнал, уведомления).
"""
from __future__ import annotations

import json
from pathlib import Path

STATE_FILE = Path("state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {}
    state.setdefault("applied", [])
    state.setdefault("notified_messages", [])
    state.setdefault("journal", {})
    state.setdefault("failed_apply", {})
    state.setdefault("quick_reply_log", [])
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
