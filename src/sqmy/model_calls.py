"""模型边界：先持久化占额，再调用；精确用量不可得时保留明确标记的估算。"""
from datetime import datetime, timedelta, timezone
import json
import math
import os
import tempfile

from .budget import BudgetExceeded, BudgetGuard, estimate_model_call_tokens
from .db import now
from .providers import ProviderError


class CallLedger:
    def __init__(self, db, settings, run_id, stage, prompt_hash, *, validate=None, task_kind=None, topic_id=None):
        self.db, self.s, self.run_id = db, settings, run_id
        self.stage, self.prompt_hash = stage, prompt_hash
        self.validate, self.task_kind = validate, task_kind
        self.topic_id = topic_id
        self.action_id = f"{stage}:{topic_id}" if topic_id else stage
        if stage not in {"screening", "diagnostic", "deep_research"} or (topic_id and stage != "deep_research"):
            raise ValueError("未支持的模型行为阶段")
        self.overrun = None
        self.last_call_id = None

    def invoke(self, client, prompt, schema):
        cfg, budget = self.s.section("model"), self.s.section("budget")
        provider = client.provider
        if provider not in {"codex_cli", "deepseek"}:
            raise ProviderError("不支持记账的提供商")
        model = client.model or "subscription-default"
        estimate_cfg = dict(cfg, provider=provider)
        estimated = estimate_model_call_tokens(
            prompt + json.dumps(schema, ensure_ascii=False), estimate_cfg, budget
        )
        prices = (cfg["deepseek_input_price_cny_per_million"], cfg["deepseek_output_price_cny_per_million"]) if provider == "deepseek" else (0.0, 0.0)
        if any(not math.isfinite(value) or value < 0 for value in (*prices, budget["weekly_cost_limit_cny"])):
            raise ValueError("价格和付费上限必须为有限非负数")
        # 保守预留整个预计Token数按较高单价计费，未知用量时不擅自释放。
        reserved_cost = estimated * max(prices) / 1_000_000
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        limit = budget[f"{self.stage}_tokens"]
        stamp = now()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            usage = conn.execute(
                "SELECT COALESCE(SUM(input_tokens+output_tokens),0),COUNT(*) FROM model_calls WHERE run_id=? AND task_id=?",
                (self.run_id, self.action_id),
            ).fetchone()
            interactive = 0
            if self.topic_id:
                interactive = conn.execute("SELECT COALESCE(SUM(token_used),0) FROM stage_usage WHERE run_id=? AND topic_id=? AND stage=?",
                                           (self.run_id, self.topic_id, self.stage)).fetchone()[0]
            guard = BudgetGuard(limit, int(usage[0]) + int(interactive), cfg["max_calls_per_action"], int(usage[1]))
            guard.reserve(estimated)
            failures = conn.execute(
                """SELECT COUNT(*) FROM model_calls WHERE run_id=? AND task_id=?
                   AND prompt_hash=? AND provider=? AND status NOT IN ('completed','diagnostic')""",
                (self.run_id, self.action_id, self.prompt_hash, provider),
            ).fetchone()[0]
            if failures > cfg["max_retries"]:
                raise BudgetExceeded("同一输入的失败重试次数已达配置上限")
            spent = conn.execute(
                """SELECT COALESCE(SUM(estimated_cost_cny),0) FROM model_calls
                   WHERE created_at>=? OR accounting_method='reserved_estimate'""", (cutoff,),
            ).fetchone()[0]
            if provider == "deepseek" and spent + reserved_cost > budget["weekly_cost_limit_cny"]:
                raise BudgetExceeded(
                    f"付费预算不足：已用及在途预留 {spent:.6f} 元，预计新增 {reserved_cost:.6f} 元，"
                    f"上限 {budget['weekly_cost_limit_cny']:.6f} 元"
                )
            cursor = conn.execute(
                """INSERT INTO model_calls(run_id,task_id,provider,model,prompt_hash,
                   input_tokens,output_tokens,estimated_cost_cny,status,estimated_tokens,
                   stage_limit,accounting_method,created_at) VALUES(?,?,?,?,?,?,0,?,'running',?,?,'reserved_estimate',?)""",
                (self.run_id, self.action_id, provider, model, self.prompt_hash, estimated,
                 reserved_cost, estimated, limit, stamp),
            )
            call_id = cursor.lastrowid
            conn.execute("UPDATE runs SET token_used=token_used+?,estimated_cost_cny=estimated_cost_cny+?,updated_at=? WHERE id=?",
                         (estimated, reserved_cost, stamp, self.run_id))
        self.last_call_id = call_id
        self.export_audit()
        result, failure = None, None
        try:
            result = client.analyze(prompt, schema)
            if self.validate:
                self.validate(result.data)
        except (Exception, KeyboardInterrupt) as exc:
            failure = exc
        known = (result.input_tokens, result.output_tokens) if result and result.usage_known else getattr(failure, "usage", None)
        if known is not None and not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in known):
            known = None
        input_tokens, output_tokens = known if known is not None else (estimated, 0)
        method = "provider_reported" if known is not None else "conservative_estimate"
        cost = (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000 if known is not None else reserved_cost
        actual = input_tokens + output_tokens
        reasons = guard.actual_overrun_reasons(actual)
        if provider == "deepseek" and spent + cost > budget["weekly_cost_limit_cny"]:
            reasons.append("本次实际费用超过调用前预留，累计费用超过付费上限；已保存结果")
        code = getattr(failure, "code", type(failure).__name__) if failure else None
        status = "completed" if failure is None else "failed_invalid_result" if result else "failed:" + code
        if reasons:
            self.overrun = {
                "stage": self.stage, "estimated_tokens": estimated, "actual_tokens": actual,
                "action_limit": limit, "action_tokens_used_before_call": guard.used,
                "action_calls_used_before_call": guard.calls_used,
                "action_call_limit": cfg["max_calls_per_action"], "reasons": reasons,
                "policy": "保存当前有界行为；不自动扩题或进入新阶段",
            }
        response_id = result.response_id if result else getattr(failure, "response_id", "")
        with self.db.connect() as conn:
            conn.execute("""UPDATE model_calls SET input_tokens=?,output_tokens=?,estimated_cost_cny=?,
                         status=?,accounting_method=?,response_id=?,error_code=?,over_budget=? WHERE id=?""",
                         (input_tokens, output_tokens, cost, status, method, response_id, code, int(bool(reasons)), call_id))
            conn.execute("UPDATE runs SET token_used=token_used+?,estimated_cost_cny=estimated_cost_cny+?,updated_at=? WHERE id=?",
                         (actual - estimated, cost - reserved_cost, now(), self.run_id))
            if self.task_kind:
                conn.execute(
                    """INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,token_used,attempts,error,updated_at)
                       VALUES(?,?,?,?,?,?,?,1,?,?) ON CONFLICT(run_id,kind,input_hash) DO UPDATE SET
                       status=excluded.status,result_json=excluded.result_json,
                       token_used=tasks.token_used+excluded.token_used,attempts=tasks.attempts+1,
                       error=excluded.error,updated_at=excluded.updated_at""",
                    (f"{self.run_id}:{self.action_id}:{self.prompt_hash[:12]}", self.run_id,
                     self.task_kind, self.prompt_hash, "failed" if failure else "completed",
                     json.dumps(result.data, ensure_ascii=False) if result else None, actual,
                     str(failure) if result and isinstance(failure, ProviderError) else code, now()),
                )
        self.export_audit()
        if failure:
            if isinstance(failure, (ProviderError, KeyboardInterrupt)):
                raise failure
            raise ProviderError(f"模型调用未完成：{type(failure).__name__}") from failure
        return result

    def export_audit(self):
        """SQLite是权威账本；JSONL是可重建的单运行审计副本，不记录提示或错误原文。"""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM model_calls WHERE run_id=? ORDER BY id", (self.run_id,)).fetchall()
        directory = self.s.root / "data/runs" / self.run_id
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False) as stream:
            for row in rows:
                stream.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
            path = stream.name
        os.replace(path, directory / "model_calls.jsonl")
