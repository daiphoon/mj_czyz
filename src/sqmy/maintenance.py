from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tomllib

from .budget import recent_usage
from .config import Settings
from .db import Database
from .evidence import assess_topic


def _bounded_stage_context(
    settings: Settings,
    db: Database,
    run_id: str,
    stage: str,
) -> tuple[bool, str]:
    with db.connect() as conn:
        run = conn.execute(
            "SELECT phase,status FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if run is None:
            return False, f"未找到运行：{run_id}"
        if run["status"] in {"completed", "skipped", "failed", "paused_quota"}:
            return False, f"运行状态不允许启动新步骤：{run['status']}"
        if stage == "pre_research":
            selected = conn.execute(
                "SELECT COUNT(*) FROM candidates WHERE run_id=? AND selected=1",
                (run_id,),
            ).fetchone()[0]
            ok = run["phase"] == "research" and int(selected) > 0
            return ok, (
                f"phase={run['phase']}, selected={selected}"
                if ok else "有限预研要求运行已进入research且至少人工选择1题"
            )
        review = conn.execute(
            """SELECT topic_id FROM research_reviews
               WHERE run_id=? AND research_allowed=1 AND human_decision='proceed'
               ORDER BY reviewed_at DESC,created_at DESC,rowid DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
    if review is None:
        return False, "尚无人工proceed的有效预研决策"
    if stage == "research":
        return True, f"topic_id={review['topic_id']}, human_proceed=true"
    if stage == "writing":
        gate = assess_topic(settings, review["topic_id"])
        if not gate["draft_allowed"]:
            return False, "证据闸门尚未通过"
        return True, f"topic_id={review['topic_id']}, evidence_gate=passed"
    return False, f"不支持的有界步骤：{stage}"


def preflight(
    settings: Settings,
    *,
    stage: str = "scan",
    run_id: str | None = None,
) -> dict:
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
        and all(
            isinstance(discovery.get(key), int)
            and not isinstance(discovery.get(key), bool)
            and 0 <= discovery[key] <= discovery["screened_max"]
            for key in (
                "premodel_external_reserve",
                "premodel_fresh_reserve",
                "premodel_aged_reserve",
            )
        )
        and all(
            isinstance(discovery.get(key), int)
            and not isinstance(discovery.get(key), bool)
            and 1 <= discovery[key] <= discovery["screened_max"]
            for key in (
                "premodel_quality_min_sources",
                "premodel_quality_min_fresh",
                "premodel_quality_min_high_score",
                "premodel_quality_strong_aged_min",
            )
        )
        and isinstance(discovery.get("premodel_quality_score_threshold"), int)
        and not isinstance(discovery.get("premodel_quality_score_threshold"), bool)
        and 0 <= discovery["premodel_quality_score_threshold"] <= 100
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
        source_detail if discovery_limits_ok else "来源扫描、队列留位、质量闸门或单来源上限非法",
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
        and all(
            isinstance(observability.get(key), int)
            and not isinstance(observability.get(key), bool)
            and observability[key] > 0
            for key in (
                "source_health_zero_result_streak",
                "source_health_irrelevant_min_runs",
                "minimum_shadow_outcomes_for_assessment",
            )
        )
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
        blocking=stage in {"scan", "refresh", "pre_research", "research", "writing"},
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

    usage = recent_usage(db)
    screening_usage = recent_usage(db, task_id="screening")
    budget_cfg = settings.section("budget")
    action_limits = {
        "scan": budget_cfg["screening_tokens"],
        "refresh": 0,
        "pre_research": budget_cfg["pre_research_tokens"],
        "research": budget_cfg["deep_research_tokens"],
        "writing": budget_cfg["writing_tokens"],
    }
    if stage not in action_limits:
        raise ValueError(f"不支持的预检阶段：{stage}")
    action_limit = action_limits[stage]
    action_limit_ok = zero_model_stage or (
        isinstance(action_limit, int)
        and not isinstance(action_limit, bool)
        and action_limit > 0
    )
    model_call_limit = model_cfg.get("max_calls_per_action")
    call_limit_ok = (
        isinstance(model_call_limit, int)
        and not isinstance(model_call_limit, bool)
        and model_call_limit > 0
    )
    action_scope_ok = stage in {"scan", "refresh"}
    action_scope_detail = "扫描或零模型复核的行为边界由命令确定"
    if stage in {"pre_research", "research", "writing"}:
        if run_id:
            action_scope_ok, action_scope_detail = _bounded_stage_context(
                settings, db, run_id, stage
            )
        else:
            action_scope_ok = False
            action_scope_detail = "必须提供run_id，才能确认人工选题及当前有界行为"
        add(
            "action_scope",
            action_scope_ok,
            action_scope_detail,
        )
    screening_ready = action_limit_ok and call_limit_ok
    action_limit_detail = (
        f"stage={stage}, action_limit={action_limit}, max_calls={model_call_limit}; "
        f"recent_{usage['window_days']}d_usage={usage['token_used']} is report_only"
    )
    add(
        "action_token_limit", action_limit_ok and (zero_model_stage or call_limit_ok),
        action_limit_detail,
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
        "run_id": run_id,
        "screening_ready": screening_ready if stage == "scan" else None,
        "action_scope_confirmed": action_scope_ok,
        "action_scope_detail": action_scope_detail,
        "model_calls": 0,
        "checks": checks,
        "recent_usage": usage,
        "recent_screening_usage": screening_usage,
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
        disposable_modes = {"mock", "test_fixture", "replay"}
        durable_modes = {
            row["id"] for row in rows if row["mode"] not in disposable_modes
        }
        replay_rows = [row for row in rows if row["mode"] == "replay"]
        latest_replay = {replay_rows[-1]["id"]} if replay_rows else set()
        diagnostics = set()
        for row in rows:
            try:
                if json.loads(row["checkpoint_json"] or "{}").get("diagnostic"):
                    diagnostics.add(row["id"])
            except json.JSONDecodeError:
                continue
        keep = durable_modes | latest_replay | diagnostics
        all_runs = {row["id"] for row in rows}
        delete_runs = sorted(all_runs - keep)

        paths: set[Path] = set()
        path_reasons: dict[Path, str] = {}

        def mark(path: Path, reason: str) -> None:
            paths.add(path)
            path_reasons[path] = reason

        candidate_dir = self.s.root / "outputs/candidates"
        for path in candidate_dir.glob("*"):
            if path.name == ".gitkeep":
                continue
            # 只删除数据库已明确判定为可丢弃运行的同名报告；未知命名可能是人工终审。
            if path.stem in delete_runs:
                mark(path, "disposable_run_candidate_report")
        for base in (self.s.root / "data/runs", self.s.root / "outputs/review"):
            if not base.exists():
                continue
            for path in base.iterdir():
                if not path.is_dir():
                    continue
                if path.name in {"pre_research", "deep_research", "metrics"}:
                    continue
                if path.name not in keep:
                    mark(path, "disposable_or_untracked_run_output")

        retention_days = int(
            self.s.section("cleanup")["docx_qa_intermediate_retention_days"]
        )
        if retention_days < 0:
            raise ValueError("DOCX中间QA保留天数不得为负数")
        qa_cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        version_pattern = re.compile(r"^(render|fidelity)-v(\d+)$")
        for run_id in keep:
            qa_root = self.s.root / "data/runs" / run_id / "docx_qa"
            if not qa_root.is_dir():
                continue
            groups: dict[str, list[tuple[int, Path]]] = {}
            for path in qa_root.iterdir():
                match = version_pattern.fullmatch(path.name)
                if path.is_dir() and match:
                    groups.setdefault(match.group(1), []).append((int(match.group(2)), path))
            for versions in groups.values():
                final_path = max(versions, key=lambda item: item[0])[1]
                for _, path in versions:
                    if path == final_path:
                        continue
                    timestamps = [path.stat().st_mtime]
                    timestamps.extend(
                        child.stat().st_mtime for child in path.rglob("*") if child.exists()
                    )
                    latest_change = datetime.fromtimestamp(max(timestamps), timezone.utc)
                    if latest_change < qa_cutoff:
                        mark(path, "expired_intermediate_docx_qa")
        for path in (self.s.root / ".pytest_cache", self.s.root / "src/sqmy_workflow.egg-info"):
            if path.exists():
                mark(path, "reproducible_python_artifact")
        for path in self.s.root.rglob("__pycache__"):
            if ".venv" not in path.parts:
                mark(path, "reproducible_python_cache")

        sorted_paths = sorted(paths, key=lambda path: str(path.relative_to(self.s.root)))

        return {
            "keep_run_ids": sorted(keep), "delete_run_ids": delete_runs,
            "delete_paths": [str(path.relative_to(self.s.root)) for path in sorted_paths],
            "delete_path_details": [
                {
                    "path": str(path.relative_to(self.s.root)),
                    "reason": path_reasons[path],
                }
                for path in sorted_paths
            ],
            "preserve": [
                ".env", ".venv", "data/cache", "data/sources",
                "outputs/review/pre_research", "outputs/review/deep_research", "templates",
                "每个docx_qa目录中编号最高的render-vN与fidelity-vN",
            ],
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
                    "stage_usage",
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
            "deleted_path_details": plan["delete_path_details"],
        }
