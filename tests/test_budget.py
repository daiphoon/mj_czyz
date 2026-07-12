from copy import deepcopy
from pathlib import Path
import tempfile

from sqmy.budget import BudgetExceeded, BudgetGuard, estimate_model_call_tokens, weekly_usage
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
                conn.execute("INSERT INTO model_calls(run_id,provider,model,prompt_hash,input_tokens,output_tokens,estimated_cost_cny,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, "test", "test", str(index), 10, 2, 0.0, "completed", stamp))
        assert weekly_usage(db)["token_used"] == 144
