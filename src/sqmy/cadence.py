from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from .config import Settings
from .db import Database
from .models import TaskStatus


def refresh_due(settings: Settings, timestamp: str) -> bool:
    try:
        checked = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return True
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    max_age = timedelta(hours=int(settings.section("project")["refresh_after_hours"]))
    return datetime.now(timezone.utc) - checked.astimezone(timezone.utc) > max_age


def require_source_freshness(settings: Settings, db: Database, run_id: str) -> None:
    with db.connect() as conn:
        run = conn.execute(
            "SELECT created_at FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        task = conn.execute(
            """SELECT result_json FROM tasks
               WHERE run_id=? AND kind='incremental_review' AND status=?
               ORDER BY updated_at DESC LIMIT 1""",
            (run_id, TaskStatus.COMPLETED),
        ).fetchone()
    if run is None:
        raise ValueError(f"未找到运行：{run_id}")
    reference = run["created_at"]
    if task is not None:
        result = json.loads(task["result_json"] or "{}")
        if result.get("decision") != "keep":
            raise ValueError("选题增量复核尚未人工确认保留，不能继续研究或生成正式稿")
        reference = result.get("decided_at") or ""
    if refresh_due(settings, reference):
        raise ValueError(
            f"来源新鲜度已超过{settings.section('project')['refresh_after_hours']}小时；"
            f"请先运行 sqmy refresh {run_id} 并记录人工决定"
        )
