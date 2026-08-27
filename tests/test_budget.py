from copy import deepcopy
from pathlib import Path
import tempfile

from sqmy.budget import (
    BudgetExceeded,
    BudgetGuard,
    budget_adjustments,
    estimate_model_call_tokens,
    record_budget_adjustment,
    review_budget_adjustment,
    weekly_usage,
)
from sqmy.config import Settings
from sqmy.db import Database, now


def test_stage_budget_is_enforced():
    guard = BudgetGuard(120_000, used=10_000, stage_limit=26_000)
    guard.reserve(25_000)
    try:
        guard.reserve(26_001)
        assert False, "expected BudgetExceeded"
    except BudgetExceeded:
        pass


def test_codex_estimate_includes_fixed_overhead():
    project = Path(__file__).parents[1]
    settings = Settings.load(project / "config/settings.toml")
    estimate = estimate_model_call_tokens("x" * 2000, settings.section("model"), settings.section("budget"))
    assert estimate >= 22_000


def test_actual_overrun_detects_stage_and_weekly_limits():
    guard = BudgetGuard(100_000, used=70_000, stage_limit=25_000)
    reasons = guard.actual_overrun_reasons(35_000)
    assert len(reasons) == 2
    assert "阶段上限" in reasons[0]
    assert "周上限" in reasons[1]


def test_screening_budget_protects_research_reserve_and_discovery_scope():
    guard = BudgetGuard(
        240_000,
        used=100_000,
        stage_limit=55_000,
        protected_reserve=90_000,
        scope_limit=100_000,
        scope_used=60_000,
    )
    guard.reserve(40_000)
    try:
        guard.reserve(40_001)
        assert False, "expected BudgetExceeded"
    except BudgetExceeded as exc:
        assert "发现阶段周预算不足" in str(exc)


def test_started_task_can_finish_across_weekly_but_not_stage_limit():
    guard = BudgetGuard(
        240_000,
        used=124_000,
        stage_limit=55_000,
        protected_reserve=90_000,
        scope_limit=100_000,
        scope_used=124_000,
    )
    reasons = guard.reserve(36_000, allow_started_task_overrun=True)
    assert len(reasons) == 2
    assert "周上限" in reasons[0]
    assert "发现阶段上限" in reasons[1]
    try:
        guard.reserve(55_001, allow_started_task_overrun=True)
        assert False, "expected stage safety limit to remain enforced"
    except BudgetExceeded as exc:
        assert "阶段上限" in str(exc)


def test_weekly_usage_reads_all_calls_not_last_ten_runs():
    project = Path(__file__).parents[1]
    base = Settings.load(project / "config/settings.toml")
    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(Path(temp), deepcopy(base.raw))
        db = Database(settings.database_path)
        db.initialize()
        stamp = now()
        with db.connect() as conn:
            for index in range(12):
                run_id = f"r{index}"
                conn.execute("INSERT INTO runs(id,phase,status,config_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)", (run_id, "discovery", "completed", "x", stamp, stamp))
                task_id = "screening" if index < 5 else "research"
                conn.execute("INSERT INTO model_calls(run_id,task_id,provider,model,prompt_hash,input_tokens,output_tokens,estimated_cost_cny,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, task_id, "test", "test", str(index), 10, 2, 0.0, "completed", stamp))
        assert weekly_usage(db)["token_used"] == 144
        assert weekly_usage(db, task_id="screening")["token_used"] == 60


def test_budget_adjustment_records_expected_and_actual_benefit():
    with tempfile.TemporaryDirectory() as temp:
        db = Database(Path(temp) / "workflow.db")
        adjustment_id = record_budget_adjustment(
            db,
            stage="screening",
            old_limit=26_000,
            new_limit=45_000,
            reason="扩大候选池前需要一次受控初筛",
            expected_benefit="验证新增来源能否发现可用候选并排除伪缺口",
        )
        review_budget_adjustment(
            db,
            adjustment_id,
            actual_tokens=70_912,
            actual_benefit="新增两个候选，并排除了两个已被政策覆盖的原始假设",
            decision="reassess",
        )
        record = budget_adjustments(db)[0]
        assert record["actual_tokens"] == 70_912
        assert record["decision"] == "reassess"
        assert "排除" in record["actual_benefit"]
