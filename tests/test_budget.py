from copy import deepcopy
from pathlib import Path
import tempfile

from sqmy.budget import (
    BudgetExceeded,
    BudgetGuard,
    budget_adjustments,
    estimate_model_call_tokens,
    recent_usage,
    record_budget_adjustment,
    record_stage_usage,
    review_budget_adjustment,
)
from sqmy.config import Settings
from sqmy.db import Database, now


def test_action_budget_is_enforced_cumulatively():
    guard = BudgetGuard(26_000, used=1_000, max_calls=3, calls_used=1)
    guard.reserve(25_000)
    try:
        guard.reserve(25_001)
        assert False, "expected BudgetExceeded"
    except BudgetExceeded as exc:
        assert "单一行为预算不足" in str(exc)


def test_codex_estimate_includes_fixed_overhead():
    project = Path(__file__).parents[1]
    settings = Settings.load(project / "config/settings.toml")
    estimate = estimate_model_call_tokens("x" * 2000, settings.section("model"), settings.section("budget"))
    assert estimate >= 22_000


def test_actual_overrun_detects_action_limit():
    guard = BudgetGuard(100_000, used=70_000)
    reasons = guard.actual_overrun_reasons(35_000)
    assert len(reasons) == 1
    assert "行为上限" in reasons[0]
    assert "105000" in reasons[0]


def test_action_call_limit_prevents_repeated_expansion():
    guard = BudgetGuard(240_000, used=100_000, max_calls=3, calls_used=3)
    try:
        guard.reserve(1)
        assert False, "expected BudgetExceeded"
    except BudgetExceeded as exc:
        assert "调用次数已达上限" in str(exc)


def test_recent_usage_reads_all_calls_in_reporting_window():
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
        usage = recent_usage(db)
        assert usage["token_used"] == 144
        assert recent_usage(db, task_id="screening")["token_used"] == 60
        assert "不作为Token硬闸门" in usage["accounting_note"]


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


def test_interactive_stage_usage_is_idempotent_and_separated_from_measured_calls():
    with tempfile.TemporaryDirectory() as temp:
        db = Database(Path(temp) / "workflow.db")
        db.initialize()
        stamp = now()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO runs(id,phase,status,config_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("run-1", "research", "pending", "x", stamp, stamp),
            )
        first = record_stage_usage(
            db,
            run_id="run-1",
            topic_id="topic-1",
            stage="pre_research",
            token_used=30_000,
            input_hash="hash-1",
            provider="codex_subscription",
            model="gpt-test",
            note="按阶段上限保守估算",
        )
        repeated = record_stage_usage(
            db,
            run_id="run-1",
            topic_id="topic-1",
            stage="pre_research",
            token_used=30_000,
            input_hash="hash-2",
            provider="codex_subscription",
            model="gpt-test",
            note="输入变化但同一阶段不重复累计",
        )
        usage = recent_usage(db)
        assert (first, repeated) == (30_000, 0)
        assert usage["measured_model_tokens"] == 0
        assert usage["estimated_interactive_tokens"] == 30_000
        assert usage["token_used"] == 30_000
        assert usage["interactive_records"] == 1
        assert "不是ChatGPT Plus官方Token统计" in usage["accounting_note"]
        with db.connect() as conn:
            assert conn.execute("SELECT token_used FROM runs WHERE id='run-1'").fetchone()[0] == 30_000
            assert conn.execute("SELECT COUNT(*) FROM stage_usage").fetchone()[0] == 1
