#!/usr/bin/env python3
"""Session persistence and project-local folded-memory storage."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from .storage import append_jsonl, write_json

SESSION_DIR = Path.home() / ".growagent" / "sessions"


def _ensure_dir() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def get_project_session_dir() -> Path:
    d = Path.cwd() / ".growagent" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session_id: str, data: dict[str, Any]) -> None:
    _ensure_dir()
    data = {
        **data,
        "metadata": {
            **data.get("metadata", {}),
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updatedAtNs": time.time_ns(),
        },
    }
    write_json(SESSION_DIR / f"{session_id}.json", data)


def save_folded_session_memory(session_id: str, record: dict[str, Any]) -> None:
    d = get_project_session_dir()
    append_jsonl(d / f"{session_id}.folded-memory.jsonl", record)
    write_json(d / f"{session_id}.folded-memory.latest.json", record)


def load_session(session_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        logging.exception("Unable to load session %s; original file preserved", path)
        return None


def list_sessions() -> list[dict[str, Any]]:
    _ensure_dir()
    results = []
    for f in SESSION_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            metadata = data.get("metadata")
            if isinstance(metadata, dict) and isinstance(metadata.get("cwd"), str):
                results.append(metadata)
        except Exception:
            logging.warning("Skipping unreadable session %s; original file preserved", f, exc_info=True)
    return results


def get_latest_session_id(project_dir: Path | None = None) -> str | None:
    project = (project_dir or Path.cwd()).resolve()
    sessions = [
        session
        for session in list_sessions()
        if session.get("cwd") and Path(session["cwd"]).resolve() == project
    ]
    if not sessions:
        return None
    sessions.sort(
        key=lambda session: (
            session.get("updatedAt") or session.get("startTime", ""),
            session.get("updatedAtNs", 0),
            session.get("startTime", ""),
        ),
        reverse=True,
    )
    return sessions[0].get("id")
