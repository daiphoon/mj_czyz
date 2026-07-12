from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class BudgetGuard:
    weekly_limit: int
    used: int = 0
    stage_limit: int | None = None

    def reserve(self, estimated_tokens: int) -> None:
        if self.stage_limit is not None and estimated_tokens > self.stage_limit:
            raise BudgetExceeded(f"阶段预算不足：预计 {estimated_tokens}，阶段上限 {self.stage_limit}")
        if self.used + estimated_tokens > self.weekly_limit:
            raise BudgetExceeded(f"预算不足：已用 {self.used}，申请 {estimated_tokens}，上限 {self.weekly_limit}")

    def record(self, actual_tokens: int) -> None:
        self.used += actual_tokens


def weekly_usage(db: Database, days: int = 7) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db.initialize()
    with db.connect() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(input_tokens+output_tokens),0),
                      COALESCE(SUM(estimated_cost_cny),0),COUNT(*)
               FROM model_calls WHERE created_at>=?""",
            (cutoff,),
        ).fetchone()
    return {"window_days": days, "token_used": int(row[0]), "estimated_cost_cny": float(row[1]), "model_calls": int(row[2])}


def estimate_model_call_tokens(prompt: str, model_cfg: dict, budget_cfg: dict) -> int:
    provider = model_cfg.get("primary_provider") if model_cfg.get("provider") == "auto" else model_cfg.get("provider")
    overhead_key = "codex_cli_fixed_overhead_tokens" if provider == "codex_cli" else "api_fixed_overhead_tokens"
    estimated_input = max(1, len(prompt) // 2) + int(budget_cfg[overhead_key])
    return estimated_input + int(model_cfg["screening_max_output_tokens"])
