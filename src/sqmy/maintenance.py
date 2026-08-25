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


def preflight(settings: Settings, *, stage: str = "scan") -> dict:
    stage = {"monday": "scan", "thursday": "refresh"}.get(stage, stage)
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

    discovery = settings.section("discovery")
    discovery_limits_ok = (
        isinstance(discovery.get("scan_all_source_tiers"), bool)
        and isinstance(discovery.get("max_parallel_fetches"), int)
        and not isinstance(discovery.get("max_parallel_fetches"), bool)
        and discovery["max_parallel_fetches"] > 0
        and isinstance(discovery.get("premodel_clue_reserve"), int)
        and not isinstance(discovery.get("premodel_clue_reserve"), bool)
        and 0 <= discovery["premodel_clue_reserve"] <= discovery["screened_max"]
        and isinstance(discovery.get("premodel_max_per_source"), int)
        and not isinstance(discovery.get("premodel_max_per_source"), bool)
        and discovery["premodel_max_per_source"] > 0
        and isinstance(discovery.get("cache_ttl_hours"), int)
        and not isinstance(discovery.get("cache_ttl_hours"), bool)
        and discovery["cache_ttl_hours"] > 0
        and isinstance(discovery.get("pending_batch_max_wait_hours"), int)
        and not isinstance(discovery.get("pending_batch_max_wait_hours"), bool)
        and discovery["pending_batch_max_wait_hours"] > 0
    )
    source_catalog_ok = False
    source_detail = "sources.toml无法校验"
    try:
        with (settings.root / "config/sources.toml").open("rb") as fh:
            sources = tomllib.load(fh)["sources"]
        source_ids = [item.get("id") for item in sources]
        source_catalog_ok = (
            bool(sources)
            and len(source_ids) == len(set(source_ids))
            and all(
                isinstance(item.get("id"), str) and item["id"]
                and isinstance(item.get("name"), str) and item["name"]
                and item.get("type") == "rss_search"
                and isinstance(item.get("query"), str) and item["query"]
                and item.get("level") in {1, 2, 3}
                and item.get("expansion_tier", 1) in {1, 2, 3, 4}
                for item in sources
            )
        )
        source_detail = f"sources={len(sources)}, ids={'unique' if len(source_ids) == len(set(source_ids)) else 'duplicate'}"
    except Exception as exc:
        source_detail = f"{type(exc).__name__}: {exc}"
    add(
        "discovery_source_config",
        discovery_limits_ok and source_catalog_ok,
        source_detail if discovery_limits_ok else "来源扫描并发、线索保留或单来源上限非法",
        blocking=stage == "scan",
    )

    scoring = settings.section("scoring")
    expected_score_keys = {
        "haidian_relevance", "beijing_relevance", "timeliness", "pain_authenticity",
        "policy_window", "data_verifiability", "operability", "mechanism_innovation",
    }
    score_keys_ok = set(scoring) == expected_score_keys
    score_values_ok = all(
        not isinstance(value, bool) and isinstance(value, int) and value >= 0
        for value in scoring.values()
    )
    score_total = sum(scoring.values()) if score_values_ok else -1
    add(
        "candidate_scoring_config",
        score_keys_ok and score_values_ok and score_total == 100,
        f"keys={'ok' if score_keys_ok else 'invalid'}, total={score_total}",
        blocking=stage == "scan",
    )
    penalties = settings.section("penalties")
    penalties_ok = bool(penalties) and all(
        not isinstance(value, bool) and isinstance(value, int) and value >= 0
        for value in penalties.values()
    )
    add(
        "candidate_penalties_config",
        penalties_ok,
        f"items={len(penalties)}" if penalties_ok else "扣分项必须是非负整数",
        blocking=stage == "scan",
    )

    observability = settings.section("observability")
    observability_ok = (
        isinstance(observability.get("enabled"), bool)
        and isinstance(observability.get("exclusion_sample_per_stage"), int)
        and not isinstance(observability.get("exclusion_sample_per_stage"), bool)
        and observability["exclusion_sample_per_stage"] >= 0
        and isinstance(observability.get("rolling_evaluation_days"), int)
        and observability["rolling_evaluation_days"] > 0
        and isinstance(observability.get("minimum_live_runs_for_comparison"), int)
        and observability["minimum_live_runs_for_comparison"] > 0
        and isinstance(observability.get("minimum_observation_span_days"), int)
        and observability["minimum_observation_span_days"] > 0
        and observability["minimum_observation_span_days"]
        < observability["rolling_evaluation_days"]
    )
    shadow = settings.section("shadow_verification")
    signal_keys = (
        "problem_signals", "policy_signals", "data_signals",
        "case_signals", "coverage_signals",
    )
    threshold = shadow.get("original_title_similarity_threshold")
    shadow_ok = (
        isinstance(shadow.get("enabled"), bool)
        and shadow.get("mode") == "shadow"
        and isinstance(shadow.get("max_events"), int)
        and not isinstance(shadow.get("max_events"), bool)
        and 0 <= shadow["max_events"] <= settings.section("discovery")["screened_max"]
        and isinstance(shadow.get("max_live_queries_per_run"), int)
        and not isinstance(shadow.get("max_live_queries_per_run"), bool)
        and shadow["max_live_queries_per_run"] >= 0
        and isinstance(shadow.get("max_hits_per_query"), int)
        and not isinstance(shadow.get("max_hits_per_query"), bool)
        and shadow["max_hits_per_query"] > 0
        and isinstance(shadow.get("search_lookback_days"), int)
        and not isinstance(shadow.get("search_lookback_days"), bool)
        and shadow["search_lookback_days"] >= 0
        and isinstance(threshold, (int, float))
        and not isinstance(threshold, bool)
        and 0 <= threshold <= 1
        and bool(shadow.get("official_domains"))
        and all(isinstance(value, str) and value for value in shadow["official_domains"])
        and all(
            isinstance(shadow.get(key), list)
            and bool(shadow[key])
            and all(isinstance(value, str) and value for value in shadow[key])
            for key in signal_keys
        )
    )
    add(
        "discovery_observability_config",
        observability_ok and shadow_ok,
        "ok" if observability_ok and shadow_ok else "漏斗或影子核验参数非法",
        blocking=stage == "scan",
    )

    quality = settings.section("quality")
    quality_values = [
        quality.get("production_evaluation_weeks"), quality.get("stable_production_weeks"),
        quality.get("minimum_drafts_per_week"), quality.get("stretch_drafts_per_week"),
    ]
    quality_ok = all(isinstance(value, int) and value > 0 for value in quality_values)
    if quality_ok:
        quality_ok = (
            quality["production_evaluation_weeks"] > quality["stable_production_weeks"]
            and quality["stretch_drafts_per_week"] >= quality["minimum_drafts_per_week"]
        )
    add(
        "weekly_quality_config",
        quality_ok,
        "ok" if quality_ok else "统计窗口需大于稳定周数，且冲刺目标不得低于最低目标",
        blocking=stage == "scan",
    )

    project = settings.section("project")
    cadence_ok = all(
        isinstance(project.get(key), int)
        and not isinstance(project.get(key), bool)
        and project[key] > 0
        for key in ("candidate_pool_days", "refresh_after_hours")
    )
    add(
        "cadence_config",
        cadence_ok,
        "ok" if cadence_ok else "候选池天数和新鲜度期限必须为正整数",
        blocking=stage in {"scan", "refresh", "research", "writing"},
    )

    writable = [settings.root / "data", settings.root / "outputs", settings.root / "logs"]
    not_writable = [str(path) for path in writable if not path.exists() or not os.access(path, os.W_OK)]
    add("working_directories", not not_writable, "writable" if not not_writable else ";".join(not_writable))

    model_cfg = settings.section("model")
    zero_model_stage = stage == "refresh"
    needs_codex = not zero_model_stage and model_cfg["provider"] in {"auto", "codex_cli"}
    codex_path = shutil.which("codex")
    codex_detail = "not required for zero-model stage" if zero_model_stage else (codex_path or "not found")
    add("codex_cli", not needs_codex or bool(codex_path), codex_detail)
    add("deepseek_fallback", bool(os.environ.get("DEEPSEEK_API_KEY")), "configured" if os.environ.get("DEEPSEEK_API_KEY") else "not configured", blocking=False)

    env_path = settings.root / ".env"
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        add("env_permissions", mode == 0o600, oct(mode))
    else:
        add("env_permissions", True, "no .env file", blocking=False)

    usage = weekly_usage(db)
    discovery_usage = weekly_usage(db, task_id="screening")
    budget_cfg = settings.section("budget")
    remaining = budget_cfg["weekly_token_limit"] - usage["token_used"]
    reserves = {
        "scan": budget_cfg["screening_tokens"],
        "refresh": 0,
        "research": budget_cfg["deep_research_tokens"],
        "writing": budget_cfg["writing_tokens"],
    }
    if stage not in reserves:
        raise ValueError(f"不支持的预检阶段：{stage}")
    reserve = reserves[stage]
    if stage == "scan":
        non_discovery_used = max(
            0, usage["token_used"] - discovery_usage["token_used"]
        )
        remaining_research_reserve = max(
            0,
            budget_cfg["research_writing_reserve_tokens"] - non_discovery_used,
        )
        protected_available = (
            budget_cfg["weekly_token_limit"]
            - remaining_research_reserve
            - usage["token_used"]
        )
        discovery_available = (
            budget_cfg["weekly_discovery_token_limit"]
            - discovery_usage["token_used"]
        )
        screening_ready = min(protected_available, discovery_available) >= reserve
        headroom_detail = (
            f"stage=scan, total_used={usage['token_used']}, protected_available={protected_available}, "
            f"research_reserve_remaining={remaining_research_reserve}, "
            f"discovery_used={discovery_usage['token_used']}, discovery_available={discovery_available}, "
            f"reserve={reserve}"
        )
    else:
        screening_ready = remaining >= reserve
        headroom_detail = (
            f"stage={stage}, used={usage['token_used']}, remaining={remaining}, reserve={reserve}"
        )
    add(
        "stage_token_headroom", screening_ready,
        headroom_detail,
        # 发现元数据与入队为零模型步骤；额度不足时仍允许扫描并在模型前安全暂停。
        blocking=stage != "scan",
    )
    add(
        "weekly_cost_headroom",
        zero_model_stage or usage["estimated_cost_cny"] < budget_cfg["weekly_cost_limit_cny"],
        f"used={usage['estimated_cost_cny']:.6f}, limit={budget_cfg['weekly_cost_limit_cny']}",
    )
    ready = all(item["ok"] for item in checks if item["blocking"])
    return {
        "ready": ready,
        "stage": stage,
        "screening_ready": screening_ready if stage == "scan" else None,
        "model_calls": 0,
        "checks": checks,
        "weekly_usage": usage,
        "discovery_usage": discovery_usage,
    }


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
                for table in (
                    "research_reviews",
                    "novelty_audits",
                    "run_efficiency",
                    "event_items",
                    "candidates",
                    "tasks",
                    "model_calls",
                    "run_context",
                ):
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
