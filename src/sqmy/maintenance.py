from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tomllib

from .budget import weekly_usage
from .config import Settings
from .db import Database


def preflight(settings: Settings) -> dict:
    db = Database(settings.database_path)
    db.initialize()
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "blocking": blocking, "detail": detail})

    with db.connect() as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        running = conn.execute("SELECT id,updated_at FROM runs WHERE status='running'").fetchall()
    add("sqlite_integrity", integrity == "ok", integrity)

    stale_hours = settings.section("budget")["preflight_stale_running_hours"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
    stale = []
    for row in running:
        try:
            updated = datetime.fromisoformat(row["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated < cutoff:
                stale.append(row["id"])
        except ValueError:
            stale.append(row["id"])
    add("stale_running_tasks", not stale, "none" if not stale else ",".join(stale))

    required = [
        settings.root / "config/settings.toml",
        settings.root / "config/sources.toml",
        settings.root / "config/policy_mechanisms.toml",
        settings.root / settings.section("document")["template_path"],
        Path(settings.section("document")["reference_path"]),
    ]
    missing = [str(path) for path in required if not path.exists()]
    add("required_files", not missing, "all present" if not missing else ";".join(missing))

    parse_errors = []
    for path in required[:3]:
        try:
            with path.open("rb") as fh:
                tomllib.load(fh)
        except Exception as exc:
            parse_errors.append(f"{path.name}:{type(exc).__name__}")
    add("toml_parse", not parse_errors, "ok" if not parse_errors else ";".join(parse_errors))

    writable = [settings.root / "data", settings.root / "outputs", settings.root / "logs"]
    not_writable = [str(path) for path in writable if not path.exists() or not os.access(path, os.W_OK)]
    add("working_directories", not not_writable, "writable" if not not_writable else ";".join(not_writable))

    model_cfg = settings.section("model")
    needs_codex = model_cfg["provider"] in {"auto", "codex_cli"}
    codex_path = shutil.which("codex")
    add("codex_cli", not needs_codex or bool(codex_path), codex_path or "not found")
    add("deepseek_fallback", bool(os.environ.get("DEEPSEEK_API_KEY")), "configured" if os.environ.get("DEEPSEEK_API_KEY") else "not configured", blocking=False)

    env_path = settings.root / ".env"
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        add("env_permissions", mode == 0o600, oct(mode))
    else:
        add("env_permissions", True, "no .env file", blocking=False)

    usage = weekly_usage(db)
    budget_cfg = settings.section("budget")
    remaining = budget_cfg["weekly_token_limit"] - usage["token_used"]
    add(
        "weekly_token_headroom", remaining >= budget_cfg["screening_tokens"],
        f"used={usage['token_used']}, remaining={remaining}, screening_reserve={budget_cfg['screening_tokens']}",
    )
    add(
        "weekly_cost_headroom", usage["estimated_cost_cny"] < budget_cfg["weekly_cost_limit_cny"],
        f"used={usage['estimated_cost_cny']:.6f}, limit={budget_cfg['weekly_cost_limit_cny']}",
    )
    ready = all(item["ok"] for item in checks if item["blocking"])
    return {"ready": ready, "model_calls": 0, "checks": checks, "weekly_usage": usage}


class CleanupManager:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = Database(settings.database_path)
        self.db.initialize()

    def plan(self) -> dict:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT r.id,r.created_at,r.checkpoint_json,rc.mode
                   FROM runs r JOIN run_context rc ON rc.run_id=r.id
                   ORDER BY r.created_at"""
            ).fetchall()
        live = {row["id"] for row in rows if row["mode"] == "live"}
        replay_rows = [row for row in rows if row["mode"] == "replay"]
        latest_replay = {replay_rows[-1]["id"]} if replay_rows else set()
        diagnostics = set()
        for row in rows:
            try:
                if json.loads(row["checkpoint_json"] or "{}").get("diagnostic"):
                    diagnostics.add(row["id"])
            except json.JSONDecodeError:
                continue
        keep = live | latest_replay | diagnostics
        all_runs = {row["id"] for row in rows}
        delete_runs = sorted(all_runs - keep)

        paths: set[Path] = set()
        candidate_dir = self.s.root / "outputs/candidates"
        for path in candidate_dir.glob("*"):
            if path.name == ".gitkeep":
                continue
            if path.stem not in keep:
                paths.add(path)
        for base in (self.s.root / "data/runs", self.s.root / "outputs/review"):
            if not base.exists():
                continue
            for path in base.iterdir():
                if not path.is_dir():
                    continue
                if path.name in {"pre_research", "deep_research", "metrics"}:
                    continue
                if path.name not in keep:
                    paths.add(path)
        for path in (self.s.root / ".pytest_cache", self.s.root / "src/sqmy_workflow.egg-info"):
            if path.exists():
                paths.add(path)
        for path in self.s.root.rglob("__pycache__"):
            if ".venv" not in path.parts:
                paths.add(path)

        return {
            "keep_run_ids": sorted(keep), "delete_run_ids": delete_runs,
            "delete_paths": sorted(str(path.relative_to(self.s.root)) for path in paths),
            "preserve": [".env", ".venv", "data/cache", "data/sources", "outputs/review/pre_research", "outputs/review/deep_research", "templates"],
        }

    def apply(self) -> dict:
        plan = self.plan()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.s.root / "data/history/backups" / f"workflow-{stamp}.db"
        backup.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.s.database_path)
        target = sqlite3.connect(backup)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        delete_runs = plan["delete_run_ids"]
        if delete_runs:
            placeholders = ",".join("?" for _ in delete_runs)
            with self.db.connect() as conn:
                for table in ("novelty_audits", "run_efficiency", "event_items", "candidates", "tasks", "model_calls", "run_context"):
                    column = "run_id"
                    conn.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", delete_runs)
                conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", delete_runs)

        with self.db.connect() as conn:
            conn.execute(
                """UPDATE runs SET status='completed',updated_at=?
                   WHERE id IN (SELECT run_id FROM run_context WHERE mode='replay')""",
                (datetime.now(timezone.utc).isoformat(),),
            )

        deleted_paths = []
        for relative in plan["delete_paths"]:
            path = self.s.root / relative
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            deleted_paths.append(relative)

        with self.db.connect() as conn:
            conn.execute("VACUUM")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        return {
            "backup": str(backup), "deleted_runs": len(delete_runs),
            "deleted_paths": len(deleted_paths), "integrity_check": integrity,
            "kept_runs": plan["keep_run_ids"],
        }
