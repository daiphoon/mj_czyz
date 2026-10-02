from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
import subprocess
import tempfile
from typing import Callable


DIAGNOSTIC_SUMMARIES = {
    "unknown": "提供商失败，未取得可确认的错误类别；不推断根因",
    "timeout": "提供商请求超过配置等待时间",
    "process_start": "本地提供商进程未能启动",
    "quota_exceeded": "提供商明确报告额度限制",
    "rate_limited": "提供商明确报告请求速率限制",
    "invalid_argument": "提供商明确报告命令参数错误",
    "authentication": "提供商明确报告认证失败",
    "missing_output": "进程结束但未生成结构化结果文件",
    "invalid_json": "提供商结果无法解析为JSON",
    "http_error": "提供商返回HTTP错误，具体根因未确认",
    "network_error": "提供商连接未完成，具体根因未确认",
    "invalid_result": "提供商结果未通过结构校验",
    "provider_disabled": "提供商已停用；未发送请求",
}


def safe_diagnostic(value=None):
    """只投影白名单及固定摘要；从不复制提示、命令、路径或错误原文。"""
    value = value if isinstance(value, dict) else {}
    category = value.get("category")
    if not isinstance(category, str) or category not in DIAGNOSTIC_SUMMARIES:
        category = "unknown"
    result = {"category": category, "summary": DIAGNOSTIC_SUMMARIES[category]}
    for key in ("exit_code", "http_status"):
        number = value.get(key)
        result[key] = number if type(number) is int and -255 <= number <= 599 else None
    return result


def codex_failure_diagnostic(stderr, stdout, exit_code):
    # 仅接受明确错误行或JSON error事件；普通输出/任务正文不用于错误分类。
    messages = []
    for line in (stderr or "").splitlines():
        if re.match(r"^\s*(?:error:|you(?:'ve| have) (?:hit|reached|exceeded) your usage limit\b)", line, re.I):
            messages.append(line)
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "error" and isinstance(event.get("message"), str):
            messages.append(event["message"])
    # 引号内常是参数值或任务文本，不能把其中的额度/速率词当成服务错误。
    messages = [re.sub(r'''(?<!\w)(?:"[^"\n]*"|'[^'\n]*'|`[^`\n]*`)''', '', message)
                for message in messages]
    category = "unknown"
    patterns = (
        ("invalid_argument", r"^\s*(?:error:\s*)?(?:unexpected argument|unrecognized (?:argument|option)|invalid (?:argument|option))\b"),
        ("quota_exceeded", r"quota[_ ]exceeded|(?:hit|reached|exceeded).{0,20}usage limit|usage limit.{0,20}(?:hit|reached|exceeded)"),
        ("rate_limited", r"rate[_ ]limit(?:ed| exceeded)?|too many requests"),
        ("authentication", r"authentication failed|not authenticated|unauthorized|invalid api key"),
    )
    for label, pattern in patterns:
        if any(re.search(pattern, message, re.I) for message in messages):
            category = label
            break
    return safe_diagnostic({"category": category, "exit_code": exit_code})


class ProviderError(RuntimeError):
    code = "provider_error"

    def __init__(self, message: str, *, usage: tuple[int, int] | None = None, response_id: str = "", diagnostic: dict | None = None):
        super().__init__(message)
        self.diagnostic = safe_diagnostic(diagnostic)
        self.usage = usage
        self.response_id = response_id


class QuotaExceeded(ProviderError):
    code = "quota_exceeded"


class RateLimited(ProviderError):
    code = "rate_limited"


class ProviderDisabled(ProviderError):
    code = "provider_disabled"

    def __init__(self):
        super().__init__("DeepSeek已停用，请使用Codex订阅通道",
                         diagnostic={"category": "provider_disabled"})


@dataclass
class ModelResult:
    data: dict
    input_tokens: int
    output_tokens: int
    response_id: str
    provider: str
    model: str
    usage_known: bool = True


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
    provider = "codex_cli"
    def __init__(self, root: Path, model: str = "", runner: Callable[..., subprocess.CompletedProcess] = subprocess.run, *, timeout_seconds: float | None = None):
        from .config import validate_selected_model
        self.root = root
        self.model = validate_selected_model(model)
        self.runner = runner
        if timeout_seconds is None:
            from .config import Settings
            timeout_seconds = Settings.load().section('model')['codex_timeout_seconds']
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds < float('inf'):
            raise ValueError('codex_timeout_seconds必须为有限正数')
        self.timeout_seconds = timeout_seconds

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
            try:
                completed = self.runner(command, capture_output=True, text=True, timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout or ""
                if isinstance(output, bytes):
                    output = output.decode(errors="replace")
                usage = extract_codex_usage(output)
                # TimeoutExpired携带完整命令与提示；只向上保留安全错误和实际用量。
                raise ProviderError("Codex CLI调用超时", usage=usage if any(usage) else None,
                                    diagnostic={"category": "timeout"}) from None
            except OSError as exc:
                raise ProviderError("Codex CLI调用未完成", diagnostic={"category": "process_start"}) from None
            usage = extract_codex_usage(completed.stdout)
            known_usage = usage if any(usage) else None
            if completed.returncode != 0:
                diagnostic = codex_failure_diagnostic(completed.stderr, completed.stdout, completed.returncode)
                error_type = {"quota_exceeded": QuotaExceeded, "rate_limited": RateLimited}.get(diagnostic["category"], ProviderError)
                raise error_type("Codex CLI调用失败", usage=known_usage, diagnostic=diagnostic)
            if not output_path.exists():
                raise ProviderError("Codex CLI未生成结构化结果文件", usage=known_usage,
                                    diagnostic={"category": "missing_output", "exit_code": completed.returncode})
            try:
                data = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ProviderError("Codex CLI结果不是有效JSON", usage=known_usage,
                                    diagnostic={"category": "invalid_json", "exit_code": completed.returncode}) from None
        input_tokens, output_tokens = extract_codex_usage(completed.stdout)
        return ModelResult(data, input_tokens, output_tokens, "codex-cli", "codex_cli", self.model, known_usage is not None)


class DeepSeekClient:
    """兼容旧调用方；按用户决定停用，不读取凭据或发送请求。"""
    provider = "deepseek"

    def __init__(self, model: str, max_tokens: int, *, thinking: bool = True, reasoning_effort: str = "max"):
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort

    def analyze(self, prompt: str, schema: dict) -> ModelResult:
        raise ProviderDisabled()


class ProviderRouter:
    def __init__(self, primary, fallback, fallback_on: list[str]):
        self.primary = primary
        self.fallback = fallback
        self.fallback_on = set(fallback_on)

    def analyze(self, prompt: str, schema: dict, *, attempt=None) -> tuple[ModelResult, str | None]:
        invoke = attempt or (lambda client, p, s: client.analyze(p, s))
        try:
            return invoke(self.primary, prompt, schema), None
        except ProviderError as exc:
            if exc.code not in self.fallback_on:
                raise
            if self.fallback is None:
                raise
            return invoke(self.fallback, prompt, schema), exc.code


def build_router(root: Path, config: dict, *, codex_model: str | None = None) -> ProviderRouter | None:
    provider = config["provider"]
    if provider == "mock":
        return None
    if provider == "deepseek":
        raise ProviderDisabled()
    from .config import validate_selected_model
    # 此参数只接受本次显式选择；旧配置和恢复快照不能充当默认模型。
    selected_codex_model = validate_selected_model(codex_model)
    if provider == "codex_cli":
        return ProviderRouter(CodexCliClient(root, selected_codex_model, timeout_seconds=config.get('codex_timeout_seconds')), None, [])
    if provider == "auto":
        # 兼容已冻结旧配置，但不允许它或残留环境变量重新开启付费备用。
        return ProviderRouter(CodexCliClient(root, selected_codex_model, timeout_seconds=config.get('codex_timeout_seconds')), None, [])
    raise ValueError(f"不支持的provider：{provider}")
