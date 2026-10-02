from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

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
        router = build_router(Path('.'), cfg, codex_model="offline-test-model")
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
            CodexCliClient(Path("."), "offline-test-model", runner=runner).analyze("p", {})
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


def test_failure_diagnostics_never_copy_stderr_prompt_or_command():
    import json
    import traceback
    import pytest
    from sqmy.providers import safe_diagnostic
    secret = "PRIVATE_PROMPT sk-sensitive Bearer secret /private/user"
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, stdout=secret,
            stderr="error: unexpected argument --secret=" + secret)
    with pytest.raises(ProviderError) as caught:
        CodexCliClient(Path('.'), 'offline-test-model', runner=runner).analyze(secret, {})
    diagnostic = caught.value.diagnostic
    assert diagnostic['exit_code'] == 2
    assert diagnostic['category'] == 'invalid_argument'
    assert len(diagnostic['summary']) < 160
    assert secret not in json.dumps(diagnostic)
    assert secret not in str(caught.value)
    # 调用方伪造摘要和额外字段也不能穿过安全投影。
    assert secret not in json.dumps(safe_diagnostic(dict(diagnostic, summary=secret, command=secret)))
    assert safe_diagnostic({'category': ['private']})['category'] == 'unknown'


def test_unknown_failure_does_not_guess_quota_from_task_text_or_fallback():
    import pytest
    fallback = FakeClient()
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1,
            stdout='task text contains quota exceeded and 429', stderr='opaque PRIVATE_SECRET')
    primary = CodexCliClient(Path('.'), 'offline-test-model', runner=runner)
    with pytest.raises(ProviderError) as caught:
        ProviderRouter(primary, fallback, ['quota_exceeded', 'rate_limited']).analyze('p', {})
    assert caught.value.diagnostic['category'] == 'unknown'
    assert caught.value.diagnostic['exit_code'] == 1
    assert fallback.calls == 0


def test_explicit_error_event_preserves_known_quota_classification():
    import pytest
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1,
            stdout='{"type":"error","message":"quota exceeded"}', stderr='')
    with pytest.raises(QuotaExceeded) as caught:
        CodexCliClient(Path('.'), 'offline-test-model', runner=runner).analyze('p', {})
    assert caught.value.diagnostic['category'] == 'quota_exceeded'


def test_process_start_error_does_not_expose_exception_chain():
    import pytest
    import traceback
    def runner(command, **kwargs):
        raise OSError('PRIVATE_PATH_AND_SECRET')
    private_prompt = 'PRIVATE_PROMPT'
    try:
        CodexCliClient(Path('.'), 'offline-test-model', runner=runner).analyze(private_prompt, {})
    except ProviderError as error:
        assert error.diagnostic['category'] == 'process_start'
        assert 'PRIVATE_PATH_AND_SECRET' not in traceback.format_exc()
        assert 'PRIVATE_PROMPT' not in traceback.format_exc()
    else:
        pytest.fail('must fail')


def test_argument_error_does_not_use_quoted_quota_or_rate_as_fallback_reason():
    import json
    import pytest
    for detail in ['quota exceeded', 'rate limit exceeded']:
        private = 'SYNTHETIC_PRIVATE_SENTINEL'
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 2, stdout='',
                stderr=f"error: unexpected argument '{detail}' {private}")
        fallback = FakeClient(result=ModelResult({'ok': True}, 1, 1, 'fake', 'test', 'test'))
        with pytest.raises(ProviderError) as caught:
            ProviderRouter(CodexCliClient(Path('.'), 'offline-test-model', runner=runner), fallback,
                           ['quota_exceeded', 'rate_limited']).analyze(private, {})
        assert caught.value.diagnostic['category'] == 'invalid_argument'
        assert caught.value.diagnostic['exit_code'] == 2
        assert fallback.calls == 0
        assert private not in str(caught.value) + json.dumps(caught.value.diagnostic)


def test_quota_wording_variants_remain_explicit_without_task_text_inference():
    import pytest
    from sqmy.providers import codex_failure_diagnostic
    for prefix in ["You have hit your usage limit", "You've hit your usage limit",
                   "You have reached your usage limit", "You have exceeded your usage limit"]:
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout='', stderr=prefix)
        with pytest.raises(QuotaExceeded) as caught:
            CodexCliClient(Path('.'), 'offline-test-model', runner=runner).analyze('p', {})
        assert caught.value.diagnostic['category'] == 'quota_exceeded'
    for line in ["opaque quota exceeded", "error: unsupported flag 'quota exceeded'",
                 "error: unsupported flag \"rate limit exceeded\""]:
        assert codex_failure_diagnostic(line, '', 2)['category'] == 'unknown'
    assert codex_failure_diagnostic('', 'You have reached your usage limit', 1)['category'] == 'unknown'


def test_deepseek_client_is_disabled_before_secret_lookup_or_dispatch(monkeypatch):
    import os
    import pytest
    from sqmy.providers import DeepSeekClient
    original = os.environ.get
    def no_secret(key, *args):
        assert key != 'DEEPSEEK_API_KEY', 'disabled client must not read credentials'
        return original(key, *args)
    monkeypatch.setattr(os.environ, 'get', no_secret)
    with patch('urllib.request.urlopen', side_effect=AssertionError('must not dispatch')) as dispatch:
        client = DeepSeekClient('historical-model', 100)
        with pytest.raises(ProviderError) as caught:
            client.analyze('SYNTHETIC_PRIVATE_SENTINEL', {})
    assert caught.value.code == 'provider_disabled'
    assert caught.value.diagnostic['category'] == 'provider_disabled'
    assert 'SYNTHETIC_PRIVATE_SENTINEL' not in str(caught.value)
    dispatch.assert_not_called()


def test_legacy_auto_config_and_keys_cannot_enable_deepseek(monkeypatch):
    import pytest
    from sqmy.config import Settings
    cfg = dict(Settings.load().section('model'), provider='auto', fallback_provider='deepseek',
               fallback_on=['quota_exceeded', 'rate_limited'])
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-not-a-real-key')
    router = build_router(Path('.'), cfg, codex_model="offline-test-model")
    assert router.fallback is None
    assert not router.fallback_on
    for error in [QuotaExceeded('quota'), ProviderError('ordinary failure')]:
        router.primary = FakeClient(error=error)
        with pytest.raises(type(error)):
            router.analyze('p', {})
    cfg['provider'] = 'deepseek'
    with pytest.raises(ProviderError) as caught:
        build_router(Path('.'), cfg, codex_model="offline-test-model")
    assert caught.value.code == 'provider_disabled'
