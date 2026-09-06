from __future__ import annotations

import hashlib

from .budget import BudgetExceeded
from .config import Settings
from .model_calls import CallLedger
from .models import Phase, TaskStatus
from .providers import DeepSeekClient, ProviderError, ProviderRouter, QuotaExceeded, RateLimited, build_router
from .workflow import Workflow


class SimulatedQuotaClient:
    def analyze(self, prompt: str, schema: dict):
        raise QuotaExceeded("simulated_codex_quota")


def provider_check(settings: Settings, *, simulate_codex_quota: bool = False) -> dict:
    config = dict(settings.section("model"))
    config["screening_max_output_tokens"] = config["diagnostic_max_output_tokens"]
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
    }
    prompt = '请只输出JSON对象 {"ok": true}。'
    workflow = Workflow(settings)
    run_id = workflow.init_run("diagnostic")

    def validate(data):
        if data != {"ok": True}:
            raise ProviderError("诊断结果不符合预期")

    # 诊断使用自己的有界输出与Token预算，不借用候选初筛的行为额度。
    diagnostic_settings = Settings(settings.root, dict(settings.raw, model=config))
    ledger = CallLedger(workflow.db, diagnostic_settings, run_id, "diagnostic",
                        hashlib.sha256(prompt.encode()).hexdigest(),
                        validate=validate, task_kind="model_diagnostic")
    try:
        if simulate_codex_quota:
            # 模拟错误不是外部调用，不计作真实尝试；真正的备用请求仍经过同一账本。
            router = ProviderRouter(DeepSeekClient(
                config["deepseek_model"], config["diagnostic_max_output_tokens"],
                thinking=config["deepseek_thinking"],
                reasoning_effort=config["deepseek_reasoning_effort"],
            ), None, [])
        else:
            router = build_router(settings.root, config)
            if router is None:
                raise ProviderError("provider=mock，无法执行真实提供商诊断")
        result, reason = router.analyze(prompt, schema, attempt=ledger.invoke)
    except (Exception, KeyboardInterrupt) as exc:
        status = (TaskStatus.PAUSED_BUDGET if isinstance(exc, BudgetExceeded)
                  else TaskStatus.PAUSED_QUOTA if isinstance(exc, (QuotaExceeded, RateLimited))
                  else TaskStatus.FAILED)
        workflow.db.checkpoint(run_id, phase=Phase.DISCOVERY, status=status,
                               data={"diagnostic": True, "next": "需人工重新授权诊断"},
                               error=type(exc).__name__)
        raise
    fallback_reason = "quota_exceeded" if simulate_codex_quota else reason
    report = {
        "run_id": run_id, "provider": result.provider, "model": result.model,
        "fallback_reason": fallback_reason,
    }
    with workflow.db.connect() as conn:
        call = conn.execute("SELECT * FROM model_calls WHERE id=?", (ledger.last_call_id,)).fetchone()
    report.update({key: call[key] for key in (
        "input_tokens", "output_tokens", "estimated_cost_cny", "accounting_method",
    )})
    if ledger.overrun:
        report["budget_overrun"] = ledger.overrun
    workflow.db.checkpoint(run_id, phase=Phase.SELECTION, status=TaskStatus.COMPLETED,
                           data=dict(report, diagnostic=True, next="none"))
    return report
