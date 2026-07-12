from __future__ import annotations

import hashlib
import json

from .config import Settings
from .db import now
from .providers import DeepSeekClient, ModelResult, ProviderRouter, QuotaExceeded, build_router
from .workflow import Workflow


class SimulatedQuotaClient:
    def analyze(self, prompt: str, schema: dict):
        raise QuotaExceeded("simulated_codex_quota")


def provider_check(settings: Settings, *, simulate_codex_quota: bool = False) -> dict:
    config = settings.section("model")
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
    }
    prompt = '请只输出JSON对象 {"ok": true}。'
    if simulate_codex_quota:
        fallback = DeepSeekClient(
            config["deepseek_model"], 1024,
            thinking=config["deepseek_thinking"],
            reasoning_effort=config["deepseek_reasoning_effort"],
        )
        router = ProviderRouter(SimulatedQuotaClient(), fallback, config["fallback_on"])
    else:
        router = build_router(settings.root, config)
        if router is None:
            raise RuntimeError("provider=mock，无法执行真实提供商诊断")
    result, fallback_reason = router.analyze(prompt, schema)
    if result.data != {"ok": True}:
        raise RuntimeError(f"诊断结果不符合预期：{result.data}")
    run_id = Workflow(settings).init_run("diagnostic")
    if result.provider == "deepseek":
        input_price = config["deepseek_input_price_cny_per_million"]
        output_price = config["deepseek_output_price_cny_per_million"]
    else:
        input_price = output_price = 0.0
    cost = (result.input_tokens * input_price + result.output_tokens * output_price) / 1_000_000
    workflow = Workflow(settings)
    with workflow.db.connect() as conn:
        conn.execute(
            "INSERT INTO model_calls(run_id,provider,model,prompt_hash,input_tokens,output_tokens,estimated_cost_cny,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, result.provider, result.model, hashlib.sha256(prompt.encode()).hexdigest(), result.input_tokens,
             result.output_tokens, cost, "fallback:" + fallback_reason if fallback_reason else "diagnostic", now()),
        )
        conn.execute(
            "UPDATE runs SET phase='selection',status='completed',token_used=?,estimated_cost_cny=?,checkpoint_json=?,updated_at=? WHERE id=?",
            (result.input_tokens + result.output_tokens, cost,
             json.dumps({"diagnostic": True, "provider": result.provider, "fallback_reason": fallback_reason}, ensure_ascii=False), now(), run_id),
        )
    return {
        "run_id": run_id, "provider": result.provider, "model": result.model,
        "fallback_reason": fallback_reason, "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens, "estimated_cost_cny": cost,
    }
