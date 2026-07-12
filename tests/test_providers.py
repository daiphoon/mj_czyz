import unittest

from sqmy.providers import ModelResult, ProviderError, ProviderRouter, QuotaExceeded, extract_codex_usage


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
