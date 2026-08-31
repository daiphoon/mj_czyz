from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

from .db import Database, now


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class BudgetGuard:
    action_limit: int
    used: int = 0
    max_calls: int | None = None
    calls_used: int = 0

    def reserve(self, estimated_tokens: int) -> None:
        if self.max_calls is not None and self.calls_used >= self.max_calls:
            raise BudgetExceeded(
                f"单一行为调用次数已达上限：已用 {self.calls_used}，上限 {self.max_calls}"
            )
        if self.used + estimated_tokens > self.action_limit:
            raise BudgetExceeded(
                f"单一行为预算不足：本行为已用 {self.used}，预计新增 "
                f"{estimated_tokens}，行为上限 {self.action_limit}"
            )

    def record(self, actual_tokens: int) -> None:
        self.used += actual_tokens
        self.calls_used += 1

    def actual_overrun_reasons(self, actual_tokens: int) -> list[str]:
        total = self.used + actual_tokens
        if total <= self.action_limit:
            return []
        return [
            f"本行为调用前已用 {self.used}，本次实际 {actual_tokens}，"
            f"累计 {total} 超过行为上限 {self.action_limit}"
        ]


def recent_usage(db: Database, days: int = 7, *, task_id: str | None = None) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db.initialize()
    task_filter = " AND task_id=?" if task_id is not None else ""
    params = (cutoff, task_id) if task_id is not None else (cutoff,)
    with db.connect() as conn:
        model_row = conn.execute(
            f"""SELECT COALESCE(SUM(input_tokens+output_tokens),0),
                      COALESCE(SUM(estimated_cost_cny),0),COUNT(*)
               FROM model_calls WHERE created_at>=?{task_filter}""",
            params,
        ).fetchone()
        stage_filter = " AND stage=?" if task_id is not None else ""
        stage_params = (cutoff, task_id) if task_id is not None else (cutoff,)
        stage_row = conn.execute(
            f"""SELECT COALESCE(SUM(token_used),0),COUNT(*)
                FROM stage_usage WHERE updated_at>=?{stage_filter}""",
            stage_params,
        ).fetchone()
        overruns = conn.execute(
            f"SELECT COUNT(*) FROM model_calls WHERE created_at>=? AND over_budget=1{task_filter}",
            params,
        ).fetchone()[0]
    measured_tokens = int(model_row[0])
    interactive_tokens = int(stage_row[0])
    return {
        "window_days": days,
        "task_id": task_id,
        "token_used": measured_tokens + interactive_tokens,
        "measured_model_tokens": measured_tokens,
        "estimated_interactive_tokens": interactive_tokens,
        "estimated_cost_cny": float(model_row[1]),
        "model_calls": int(model_row[2]),
        "interactive_records": int(stage_row[1]),
        "over_budget_calls": int(overruns),
        "accounting_note": (
            "程序模型调用按返回用量计；交互式Codex按阶段声明上限保守估算，"
            "不是ChatGPT Plus官方Token统计。该时间窗只用于观察和复盘，"
            "不作为Token硬闸门。"
        ),
    }


def record_stage_usage(
    db: Database,
    *,
    run_id: str,
    topic_id: str,
    stage: str,
    token_used: int,
    input_hash: str,
    provider: str,
    model: str,
    note: str,
    execution_mode: str = "interactive_codex",
    accounting_method: str = "declared_stage_cap",
) -> int:
    """幂等记录交互式阶段的保守用量，并返回本次新增到运行账本的Token。"""
    if stage not in {"pre_research", "deep_research", "writing"}:
        raise ValueError(f"不支持的交互式阶段：{stage}")
    if token_used < 0:
        raise ValueError("Token用量不得为负数")
    required = {
        "run_id": run_id,
        "topic_id": topic_id,
        "input_hash": input_hash,
        "provider": provider,
        "model": model,
        "note": note,
    }
    missing = [name for name, value in required.items() if not str(value).strip()]
    if missing:
        raise ValueError("交互式用量记录缺少：" + "、".join(missing))
    db.initialize()
    stamp = now()
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise ValueError(f"未找到运行：{run_id}")
        exact_call = conn.execute(
            "SELECT 1 FROM model_calls WHERE run_id=? AND task_id=? LIMIT 1",
            (run_id, stage),
        ).fetchone()
        if exact_call is not None:
            return 0
        existing = conn.execute(
            """SELECT id,token_used FROM stage_usage
               WHERE run_id=? AND topic_id=? AND stage=? AND execution_mode=?""",
            (run_id, topic_id, stage, execution_mode),
        ).fetchone()
        previous = int(existing["token_used"]) if existing else 0
        recorded = max(previous, token_used)
        delta = recorded - previous
        conn.execute(
            """INSERT INTO stage_usage(
                 run_id,topic_id,stage,execution_mode,accounting_method,provider,
                 model,token_used,input_hash,note,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id,topic_id,stage,execution_mode) DO UPDATE SET
                 accounting_method=excluded.accounting_method,
                 provider=excluded.provider,model=excluded.model,
                 token_used=MAX(stage_usage.token_used,excluded.token_used),
                 input_hash=excluded.input_hash,note=excluded.note,
                 updated_at=excluded.updated_at""",
            (
                run_id,
                topic_id,
                stage,
                execution_mode,
                accounting_method,
                provider,
                model,
                token_used,
                input_hash,
                note.strip(),
                stamp,
                stamp,
            ),
        )
        if delta:
            conn.execute(
                "UPDATE runs SET token_used=token_used+?,updated_at=? WHERE id=?",
                (delta, stamp, run_id),
            )
    return delta


def estimate_model_call_tokens(prompt: str, model_cfg: dict, budget_cfg: dict) -> int:
    provider = model_cfg.get("primary_provider") if model_cfg.get("provider") == "auto" else model_cfg.get("provider")
    overhead_key = "codex_cli_fixed_overhead_tokens" if provider == "codex_cli" else "api_fixed_overhead_tokens"
    multiplier_key = "codex_cli_estimate_multiplier" if provider == "codex_cli" else "api_estimate_multiplier"
    estimated_input = max(1, len(prompt) // 2) + int(budget_cfg[overhead_key])
    base = estimated_input + int(model_cfg["screening_max_output_tokens"])
    return math.ceil(base * float(budget_cfg[multiplier_key]))


def record_budget_adjustment(
    db: Database,
    *,
    stage: str,
    old_limit: int,
    new_limit: int,
    reason: str,
    expected_benefit: str,
    run_id: str | None = None,
) -> int:
    if old_limit < 0 or new_limit < 0 or old_limit == new_limit:
        raise ValueError("预算调整必须记录两个不同的非负额度")
    if not reason.strip() or not expected_benefit.strip():
        raise ValueError("必须记录提高或降低预算的原因和预期收益")
    db.initialize()
    with db.connect() as conn:
        if run_id and conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise ValueError(f"未找到运行：{run_id}")
        cursor = conn.execute(
            """INSERT INTO budget_adjustments(
                 run_id,stage,old_limit,new_limit,reason,expected_benefit,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                run_id,
                stage.strip(),
                old_limit,
                new_limit,
                reason.strip(),
                expected_benefit.strip(),
                now(),
            ),
        )
    return int(cursor.lastrowid)


def review_budget_adjustment(
    db: Database,
    adjustment_id: int,
    *,
    actual_tokens: int,
    actual_benefit: str,
    decision: str,
) -> None:
    if decision not in {"retain", "revert", "reassess"}:
        raise ValueError("预算复盘结论只能是 retain、revert 或 reassess")
    if actual_tokens < 0 or not actual_benefit.strip():
        raise ValueError("必须记录非负实际Token和实际收益")
    db.initialize()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM budget_adjustments WHERE id=?", (adjustment_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"未找到预算调整记录：{adjustment_id}")
        conn.execute(
            """UPDATE budget_adjustments SET actual_tokens=?,actual_benefit=?,
                 decision=?,completed_at=? WHERE id=?""",
            (actual_tokens, actual_benefit.strip(), decision, now(), adjustment_id),
        )


def budget_adjustments(db: Database, limit: int = 20) -> list[dict]:
    db.initialize()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM budget_adjustments ORDER BY created_at DESC,id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
