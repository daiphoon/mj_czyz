from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Callable


class ProviderError(RuntimeError):
    code = "provider_error"


class QuotaExceeded(ProviderError):
    code = "quota_exceeded"


class RateLimited(ProviderError):
    code = "rate_limited"


@dataclass
class ModelResult:
    data: dict
    input_tokens: int
    output_tokens: int
    response_id: str
    provider: str
    model: str


def _classify_error(message: str) -> type[ProviderError]:
    lower = message.lower()
    if any(term in lower for term in ("usage limit", "quota exceeded", "quota_exceeded", "额度", "5-hour", "five hour")):
        return QuotaExceeded
    if any(term in lower for term in ("rate limit", "rate_limit", "too many requests", "429")):
        return RateLimited
    return ProviderError


def extract_codex_usage(jsonl: str) -> tuple[int, int]:
    input_values, output_values = [], []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("input_tokens"), int):
                input_values.append(value["input_tokens"])
            if isinstance(value.get("output_tokens"), int):
                output_values.append(value["output_tokens"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for line in jsonl.splitlines():
        try:
            walk(json.loads(line))
        except json.JSONDecodeError:
            continue
    return max(input_values, default=0), max(output_values, default=0)


class CodexCliClient:
    def __init__(self, root: Path, model: str = "", runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.root = root
        self.model = model
        self.runner = runner

    def analyze(self, prompt: str, schema: dict) -> ModelResult:
        with tempfile.TemporaryDirectory(prefix="sqmy-codex-") as directory:
            temp = Path(directory)
            schema_path = temp / "schema.json"
            output_path = temp / "result.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            isolated_prompt = (
                "这是纯结构化元数据分析任务。不得调用任何工具，不得读取工作区文件，"
                "不得访问网络；只依据下列输入直接完成判断。\n" + prompt
            )
            command = [
                "codex", "exec", "--ephemeral", "--sandbox", "read-only",
                "--ignore-user-config", "--skip-git-repo-check", "--json",
                "--output-schema", str(schema_path), "--output-last-message", str(output_path),
                # 初筛不依赖仓库文件。使用空临时目录，避免把项目 AGENTS.md 和仓库上下文
                # 重复带入订阅调用；提示和 Schema 已包含完成任务所需的全部信息。
                "--cd", str(temp),
            ]
            if self.model:
                command += ["--model", self.model]
            command += [isolated_prompt]
            completed = self.runner(command, capture_output=True, text=True, timeout=180)
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout or "Codex CLI failed")[-4000:]
                raise _classify_error(message)(message)
            if not output_path.exists():
                raise ProviderError("Codex CLI未生成结构化结果文件")
            try:
                data = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ProviderError(f"Codex CLI结果不是有效JSON：{exc}") from exc
        input_tokens, output_tokens = extract_codex_usage(completed.stdout)
        return ModelResult(data, input_tokens, output_tokens, "codex-cli", "codex_cli", self.model or "subscription-default")


class DeepSeekClient:
    endpoint = "https://api.deepseek.com/chat/completions"

    def __init__(self, model: str, max_tokens: int, *, thinking: bool = True, reasoning_effort: str = "max"):
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise ProviderError("需要DeepSeek备用，但未设置DEEPSEEK_API_KEY")
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort

    def analyze(self, prompt: str, schema: dict) -> ModelResult:
        instruction = (
            "只输出有效JSON，不得输出Markdown。输出必须符合以下JSON Schema："
            + json.dumps(schema, ensure_ascii=False) + "\n任务：" + prompt
        )
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": instruction}],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_tokens,
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
            "reasoning_effort": self.reasoning_effort,
            "stream": False,
        }
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[-2000:]
            error_type = _classify_error(f"HTTP {exc.code}: {detail}")
            raise error_type(detail) from exc
        try:
            content = payload["choices"][0]["message"]["content"]
            data = json.loads(content)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise ProviderError(f"DeepSeek结果解析失败：{exc}") from exc
        usage = payload.get("usage", {})
        return ModelResult(
            data, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
            payload.get("id", ""), "deepseek", self.model,
        )


class ProviderRouter:
    def __init__(self, primary, fallback, fallback_on: list[str]):
        self.primary = primary
        self.fallback = fallback
        self.fallback_on = set(fallback_on)

    def analyze(self, prompt: str, schema: dict) -> tuple[ModelResult, str | None]:
        try:
            return self.primary.analyze(prompt, schema), None
        except ProviderError as exc:
            if exc.code not in self.fallback_on:
                raise
            if self.fallback is None:
                raise
            return self.fallback.analyze(prompt, schema), exc.code


def build_router(root: Path, config: dict, *, codex_model: str | None = None) -> ProviderRouter | None:
    provider = config["provider"]
    selected_codex_model = config.get("codex_model", "") if codex_model is None else codex_model
    if provider == "mock":
        return None
    if provider == "codex_cli":
        return ProviderRouter(CodexCliClient(root, selected_codex_model), None, [])
    if provider == "deepseek":
        return ProviderRouter(DeepSeekClient(config["deepseek_model"], config["screening_max_output_tokens"], thinking=config["deepseek_thinking"], reasoning_effort=config["deepseek_reasoning_effort"]), None, [])
    if provider == "auto":
        fallback = DeepSeekClient(config["deepseek_model"], config["screening_max_output_tokens"], thinking=config["deepseek_thinking"], reasoning_effort=config["deepseek_reasoning_effort"]) if os.environ.get("DEEPSEEK_API_KEY") else None
        return ProviderRouter(CodexCliClient(root, selected_codex_model), fallback, config["fallback_on"])
    raise ValueError(f"不支持的provider：{provider}")
