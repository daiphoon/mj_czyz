from copy import deepcopy
from datetime import datetime, timezone
import json
import io
import fcntl
import multiprocessing
import os
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest

from sqmy.budget import BudgetExceeded, recent_usage
from sqmy.config import Settings
from sqmy.discovery import LiveDiscovery
from sqmy.diagnostics import provider_check
from sqmy.model_calls import CallLedger
from sqmy.models import EventItem
from sqmy.providers import DeepSeekClient, ModelResult, ProviderError, ProviderRouter, QuotaExceeded


class Client:
    def __init__(self, provider="codex_cli", error=None):
        self.provider = provider
        self.model = "test"
        self.error = error
        self.calls = 0

    def analyze(self, prompt, schema):
        self.calls += 1
        if self.error:
            raise self.error
        return ModelResult({"selections": [{"id": "event"}]}, 100, 20,
                           "test-response", self.provider, self.model)


@pytest.fixture
def discovery(tmp_path):
    root = Path(__file__).parents[1]
    shutil.copytree(root / "config", tmp_path / "config")
    settings = Settings(tmp_path, deepcopy(Settings.load(root / "config/settings.toml").raw))
    settings.raw["model"]["provider"] = "codex_cli"
    settings.raw["budget"]["screening_tokens"] = 300_000
    settings.raw["shadow_verification"]["enabled"] = False
    return LiveDiscovery(settings)


@pytest.fixture
def event():
    return EventItem(id="event", source_id="s", source_name="权威调查", source_level=1,
                     title="北京市劳动用工政策执行问题", url="https://example.gov.cn/event",
                     published_at=datetime.now(timezone.utc).isoformat(),
                     summary="劳动者申诉责任错配", region="北京",
                     topics=["平台经济与劳动权益"], rule_score=85)


def test_failed_calls_are_counted_and_cannot_retry_forever(discovery, event):
    run = discovery.wf.init_run("test_fixture")
    client = Client(error=ProviderError("bad JSON"))
    with patch("sqmy.discovery.build_router", return_value=ProviderRouter(client, None, [])):
        for _ in range(3):
            with pytest.raises(ProviderError):
                discovery._model_rank(run, [event])
        with pytest.raises(BudgetExceeded):
            discovery._model_rank(run, [event])
    with discovery.wf.db.connect() as conn:
        calls = conn.execute("SELECT * FROM model_calls WHERE run_id=?", (run,)).fetchall()
    assert client.calls == len(calls) == 3
    assert all(row["status"].startswith("failed") for row in calls)


@pytest.mark.parametrize("fallback", [False, True])
def test_paid_budget_checked_at_dispatch_without_preflight(discovery, event, fallback):
    run = discovery.wf.init_run("test_fixture")
    discovery.s.raw["budget"]["weekly_cost_limit_cny"] = 0
    paid = Client("deepseek")
    primary = Client(error=QuotaExceeded("quota")) if fallback else paid
    router = ProviderRouter(primary, paid if fallback else None, ["quota_exceeded"])
    with patch("sqmy.discovery.build_router", return_value=router):
        with pytest.raises(BudgetExceeded):
            discovery._model_rank(run, [event])
    assert paid.calls == 0
    assert discovery.wf.status(run)[0]["status"] == "paused_budget"


def test_downstream_crash_resumes_frozen_pool_without_recollecting(discovery, event):
    client = Client()
    router = ProviderRouter(client, None, [])
    with patch("sqmy.discovery.SourceCollector.collect", return_value=[event]), \
         patch.object(discovery, "_should_run_model", return_value=(True, "test")), \
         patch("sqmy.discovery.build_router", return_value=router), \
         patch("sqmy.discovery.NoveltyAuditor.audit", side_effect=RuntimeError("injected crash")):
        with pytest.raises(RuntimeError, match="injected crash"):
            discovery.run()
    run = discovery.wf.status()[0]["id"]
    assert discovery.wf.status(run)[0]["status"] == "failed"
    with patch("sqmy.discovery.SourceCollector.collect", side_effect=AssertionError("must not recollect")), \
         patch("sqmy.discovery.build_router", side_effect=AssertionError("must reuse result")), \
         patch("sqmy.discovery.NoveltyAuditor.audit", return_value=[]):
        actual_run, candidates = discovery.run(resume_run_id=run)
    assert actual_run == run
    assert len(candidates) == 1
    assert client.calls == 1
    with discovery.wf.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run,)).fetchone()[0] == 1
        assert conn.execute("SELECT status FROM discovery_queue").fetchone()[0] == "screened"
    checkpoint = json.loads(discovery.wf.status(run)[0]["checkpoint_json"])
    assert checkpoint["screening_cache_hit"] is True


def test_response_parse_failure_preserves_reported_paid_usage(discovery, event, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-placeholder")
    client = DeepSeekClient("test", 100)
    payload = {"id": "response-1", "choices": [{"message": {"content": "not JSON"}}],
               "usage": {"prompt_tokens": 200, "completion_tokens": 40}}
    run = discovery.wf.init_run("test_fixture")
    with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())), \
         patch("sqmy.discovery.build_router", return_value=ProviderRouter(client, None, [])):
        with pytest.raises(ProviderError):
            discovery._model_rank(run, [event])
    usage = recent_usage(discovery.wf.db)
    assert usage["measured_model_tokens"] == 240
    assert usage["estimated_unconfirmed_model_tokens"] == 0
    assert usage["estimated_cost_cny"] == pytest.approx((200 * 3 + 40 * 6) / 1_000_000)
    with discovery.wf.db.connect() as conn:
        row = conn.execute("SELECT * FROM model_calls").fetchone()
        assert row["response_id"] == "response-1"
        assert row["accounting_method"] == "provider_reported"
    audit = discovery.s.root / "data/runs" / run / "model_calls.jsonl"
    assert len(audit.read_text().splitlines()) == 1
    assert "test-placeholder" not in audit.read_text()


def test_unknown_usage_is_estimated_separately_and_survives_retry(discovery, event):
    run = discovery.wf.init_run("test_fixture")
    client = Client(error=TimeoutError("network"))
    with patch("sqmy.discovery.build_router", return_value=ProviderRouter(client, None, [])):
        with pytest.raises(ProviderError):
            discovery._model_rank(run, [event])
    usage = recent_usage(discovery.wf.db)
    assert usage["measured_model_tokens"] == 0
    assert usage["estimated_unconfirmed_model_tokens"] > 0
    assert discovery.wf.status(run)[0]["token_used"] == usage["token_used"]


def test_quota_fallback_records_both_physical_attempts(discovery, event):
    run = discovery.wf.init_run("test_fixture")
    primary, paid = Client(error=QuotaExceeded("quota")), Client("deepseek")
    with patch("sqmy.discovery.build_router", return_value=ProviderRouter(primary, paid, ["quota_exceeded"])):
        discovery._model_rank(run, [event])
    with discovery.wf.db.connect() as conn:
        rows = conn.execute("SELECT provider,status FROM model_calls ORDER BY id").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("codex_cli", "failed:quota_exceeded"), ("deepseek", "completed")]


def test_retry_cap_is_enforced_independently_of_call_cap(discovery, event):
    discovery.s.raw["model"]["max_retries"] = 0
    run = discovery.wf.init_run("test_fixture")
    client = Client(error=ProviderError("bad result"))
    with patch("sqmy.discovery.build_router", return_value=ProviderRouter(client, None, [])):
        with pytest.raises(ProviderError):
            discovery._model_rank(run, [event])
        with pytest.raises(BudgetExceeded, match="失败重试"):
            discovery._model_rank(run, [event])
    assert client.calls == 1


def test_paid_reservation_is_visible_to_another_run(discovery):
    first, second = [discovery.wf.init_run("test_fixture") for _ in range(2)]
    discovery.s.raw["budget"]["weekly_cost_limit_cny"] = 0.06
    other = Client("deepseek")
    second_ledger = CallLedger(discovery.wf.db, discovery.s, second, "screening", "p")

    class InFlight(Client):
        def analyze(self, prompt, schema):
            with pytest.raises(BudgetExceeded, match="付费预算不足"):
                second_ledger.invoke(other, prompt, schema)
            return super().analyze(prompt, schema)

    CallLedger(discovery.wf.db, discovery.s, first, "screening", "p").invoke(InFlight("deepseek"), "p", {})
    assert other.calls == 0


def test_diagnostic_uses_same_paid_guard_and_failure_ledger(discovery, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-placeholder")
    discovery.s.raw["budget"]["weekly_cost_limit_cny"] = 0
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
        with pytest.raises(BudgetExceeded):
            provider_check(discovery.s, simulate_codex_quota=True)
    assert discovery.wf.status(include_all=True)[0]["status"] == "paused_budget"
    discovery.s.raw["budget"]["weekly_cost_limit_cny"] = 100
    discovery.s.raw["model"]["provider"] = "deepseek"
    with patch("urllib.request.urlopen", side_effect=TimeoutError("network")):
        with pytest.raises(ProviderError):
            provider_check(discovery.s)
    assert recent_usage(discovery.wf.db)["model_calls"] == 1


def test_hard_exit_leaves_reservation_and_resumes_same_snapshot(discovery, event):
    class CrashingClient(Client):
        def analyze(self, prompt, schema):
            os._exit(23)

    def child():
        with patch("sqmy.discovery.SourceCollector.collect", return_value=[event]), \
             patch.object(discovery, "_should_run_model", return_value=(True, "test")), \
             patch("sqmy.discovery.build_router", return_value=ProviderRouter(CrashingClient(), None, [])):
            discovery.run()

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("child did not exit")
    assert process.exitcode == 23
    run = discovery.wf.status()[0]["id"]
    assert discovery.wf.status(run)[0]["status"] == "running"
    before = recent_usage(discovery.wf.db)
    assert before["estimated_unconfirmed_model_tokens"] > 0
    with patch("sqmy.discovery.SourceCollector.collect", side_effect=AssertionError("must not recollect")), \
         patch("sqmy.discovery.build_router", return_value=ProviderRouter(Client(), None, [])), \
         patch("sqmy.discovery.NoveltyAuditor.audit", return_value=[]):
        _, candidates = discovery.run(resume_run_id=run)
    assert len(candidates) == 1
    usage = recent_usage(discovery.wf.db)
    assert usage["model_calls"] == 2
    assert usage["estimated_unconfirmed_model_tokens"] == before["estimated_unconfirmed_model_tokens"]
    assert usage["measured_model_tokens"] == 120


def test_resume_after_candidate_save_reuses_audits_and_keeps_overrun(discovery, event):
    class LargeResult(Client):
        def analyze(self, prompt, schema):
            result = super().analyze(prompt, schema)
            result.input_tokens = 100_000
            return result

    discovery.s.raw["budget"]["screening_tokens"] = 55_000
    client = LargeResult()
    with patch("sqmy.discovery.SourceCollector.collect", return_value=[event]), \
         patch.object(discovery, "_should_run_model", return_value=(True, "test")), \
         patch("sqmy.discovery.build_router", return_value=ProviderRouter(client, None, [])), \
         patch("sqmy.discovery.NoveltyAuditor.audit", return_value=[]), \
         patch.object(discovery, "_report", side_effect=OSError("disk fault")):
        with pytest.raises(OSError):
            discovery.run(force=True)
    run = discovery.wf.status()[0]["id"]
    assert len(discovery.wf.candidates(run)) == 1
    discovery.s.raw["budget"]["screening_tokens"] = 0
    with patch("sqmy.discovery.SourceCollector.collect", side_effect=AssertionError("no recollect")), \
         patch("sqmy.discovery.NoveltyAuditor.audit", side_effect=AssertionError("no re-audit")), \
         patch("sqmy.discovery.build_router", side_effect=AssertionError("no new call")):
        _, candidates = discovery.run(resume_run_id=run)
    assert len(candidates) == len(discovery.wf.candidates(run)) == 1
    assert client.calls == 1
    checkpoint = json.loads(discovery.wf.status(run)[0]["checkpoint_json"])
    assert checkpoint["budget_overrun"]["actual_tokens"] == 100_020


def test_scan_lock_rejects_parallel_consumer_without_new_run(discovery):
    path = discovery.s.root / "data/runs/.discovery.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="已有扫描"):
            discovery.run()
    assert discovery.wf.status(include_all=True) == []
