from pathlib import Path
import subprocess
import tempfile
import unittest

from sqmy.providers import (
    CodexCliClient,
    ModelResult,
    ProviderError,
    ProviderRouter,
    QuotaExceeded,
    build_router,
    extract_codex_usage,
)


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def analyze(self, prompt, schema):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class ProviderRouterTest(unittest.TestCase):
    def test_configured_timeout_preserves_usage_without_prompt_trace(self):
        import traceback
        from sqmy.config import Settings
        cfg = dict(Settings.load().section('model'), provider='codex_cli', codex_timeout_seconds=420)
        router = build_router(Path('.'), cfg)
        def runner(command, **kwargs):
            self.assertEqual(kwargs['timeout'], 420)
            raise subprocess.TimeoutExpired(command, 420, output=b'{"usage":{"input_tokens":120,"output_tokens":30}}')
        router.primary.runner = runner
        private_prompt = 'PRIVATE_PROMPT_SENTINEL'
        try:
            router.analyze(private_prompt, {})
        except ProviderError as exc:
            self.assertEqual(exc.usage, (120, 30))
            self.assertNotIn('PRIVATE_PROMPT_SENTINEL', traceback.format_exc())
        else:
            self.fail('timeout must fail, not produce a partial candidate')

    def test_codex_bad_json_preserves_usage_without_leaking_output(self):
        def runner(command, **kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text("not JSON", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout='{"usage":{"input_tokens":120,"output_tokens":30}}', stderr="")

        with self.assertRaises(ProviderError) as caught:
            CodexCliClient(Path("."), runner=runner).analyze("p", {})
        self.assertEqual(caught.exception.usage, (120, 30))
        self.assertNotIn("not JSON", str(caught.exception))

    def test_extracts_codex_jsonl_usage(self):
        stream = '{"type":"turn.completed","usage":{"input_tokens":120,"output_tokens":30}}\n'
        self.assertEqual(extract_codex_usage(stream), (120, 30))
    def test_primary_success_does_not_call_fallback(self):
        result = ModelResult({"ranked_ids": ["1"]}, 0, 0, "x", "codex_cli", "default")
        primary, fallback = FakeClient(result=result), FakeClient(result=result)
        actual, reason = ProviderRouter(primary, fallback, ["quota_exceeded"]).analyze("p", {})
        self.assertIs(actual, result)
        self.assertIsNone(reason)
        self.assertEqual(fallback.calls, 0)

    def test_quota_error_calls_fallback(self):
        result = ModelResult({"ranked_ids": ["1"]}, 12, 3, "d", "deepseek", "deepseek-chat")
        fallback = FakeClient(result=result)
        actual, reason = ProviderRouter(FakeClient(error=QuotaExceeded("limit")), fallback, ["quota_exceeded"]).analyze("p", {})
        self.assertIs(actual, result)
        self.assertEqual(reason, "quota_exceeded")
        self.assertEqual(fallback.calls, 1)

    def test_parse_error_never_falls_back(self):
        fallback = FakeClient()
        with self.assertRaises(ProviderError):
            ProviderRouter(FakeClient(error=ProviderError("bad json")), fallback, ["quota_exceeded"]).analyze("p", {})
        self.assertEqual(fallback.calls, 0)

    def test_stage_can_override_codex_model(self):
        router = build_router(
            Path("."),
            {"provider": "codex_cli", "codex_model": "full-model"},
            codex_model="screening-model",
        )
        self.assertEqual(router.primary.model, "screening-model")

    def test_codex_analysis_uses_isolated_workspace_and_forbids_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / "AGENTS.md").write_text("very large project context", encoding="utf-8")
            observed = {}

            def runner(command, **kwargs):
                workspace = Path(command[command.index("--cd") + 1])
                output = Path(command[command.index("--output-last-message") + 1])
                observed["workspace"] = workspace
                observed["command"] = command
                observed["prompt"] = command[-1]
                self.assertNotEqual(workspace, root)
                self.assertFalse((workspace / "AGENTS.md").exists())
                output.write_text('{"ok": true}', encoding="utf-8")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"type":"turn.completed","usage":{"input_tokens":20,"output_tokens":5}}\n',
                    stderr="",
                )

            result = CodexCliClient(root, "screening-model", runner=runner).analyze(
                "分析以下元数据", {"type": "object"}
            )

        self.assertEqual(result.data, {"ok": True})
        self.assertIn("--skip-git-repo-check", observed["command"])
        self.assertIn("不得调用任何工具", observed["prompt"])
        self.assertIn("不得读取工作区文件", observed["prompt"])
