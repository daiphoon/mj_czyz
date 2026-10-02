from dataclasses import asdict
import json
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from sqmy.search_types import RetrievalIntent, SearchError, SearchRequest, SearchResult
from sqmy.search_providers import BraveSearchProvider, TavilyKeylessProvider, send_json
from sqmy.search_router import Circuit, SearchRouter


class Provider:
    auth_mode, paid, cost_usd = "none", False, 0.0
    search_types, parameters, available = frozenset({"web", "news"}), {}, True
    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error, self.calls = name, result or [], error, 0
    def search(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.mark.parametrize("error", [SearchError("timeout", "transient"), SearchError("429", "rate_limit", retry_after=30), SearchError("500", "transient")])
def test_failure_falls_back_without_global_unavailable(error):
    a = Provider("a", error=error)
    b = Provider("b", [SearchResult("政策", "https://www.gov.cn/policy", provider="b")])
    router = SearchRouter([a, b])
    result = router.search(SearchRequest("政策"))
    assert result.status == "completed" and len(result.results) == 1
    assert (a.calls, b.calls) == (1, 1)
    assert result.attempts[-1]["fallback_from"] == "a"


def test_open_half_open_and_single_probe_recover():
    clock = [100.0]
    a = Provider("a", error=SearchError("429", "rate_limit", retry_after=30))
    router = SearchRouter([a], clock=lambda: clock[0])
    request = SearchRequest("规则")
    assert router.search(request).status == "unavailable"
    assert router.search(request).errors[-1]["code"] == "circuit_open" and a.calls == 1
    clock[0] += 31
    circuit = router.health["a:none:web"]
    assert circuit.state(clock[0]) == "HALF_OPEN"
    assert circuit.allow(clock[0]) and not circuit.allow(clock[0])
    circuit.probe_running = False
    a.error = None
    assert router.search(request).status == "completed" and a.calls == 2
    assert circuit.state(clock[0]) == "CLOSED"


def test_auth_not_retried_and_invalid_query_does_not_open_circuit():
    auth = Provider("auth", error=SearchError("401", "authentication"))
    bad = Provider("bad", error=SearchError("400", "invalid_query"))
    router = SearchRouter([auth, bad], cfg={"max_retries": 2})
    router.search(SearchRequest("规则"))
    router.search(SearchRequest("规则"))
    assert auth.calls == 1 and bad.calls == 2
    assert router.health["bad:none:web"].failures == 0


def test_valid_empty_is_completed_and_does_not_expand():
    a, b = Provider("a"), Provider("b")
    result = SearchRouter([a, b]).search(SearchRequest("尚无匹配"))
    assert result.status == "completed" and result.results == [] and b.calls == 0


def test_parallel_dedup_and_local_domain_filter():
    a = Provider("a", [SearchResult("同文", "https://www.gov.cn/policy?utm_source=a")])
    b = Provider("b", [SearchResult("同文", "https://www.gov.cn/policy"), SearchResult("伪官方", "https://gov.cn.attacker.example/a")])
    result = SearchRouter([a, b], cfg={"allow_parallel": True}).search(SearchRequest("政策", domains=("gov.cn",)), strategy="parallel")
    assert [x.url for x in result.results] == ["https://www.gov.cn/policy"]
    assert a.calls == b.calls == 1


def test_cache_hit_expired_and_provider_isolation(tmp_path):
    clock = [100.0]
    a = Provider("a", [SearchResult("材料", "https://www.gov.cn/a")])
    router = SearchRouter([a], cache_dir=tmp_path, clock=lambda: clock[0])
    request = SearchRequest("规则")
    router.search(request)
    assert router.search(request).attempts[0]["cache_hit"] and a.calls == 1
    clock[0] += 6 * 3600
    router.search(request)
    assert a.calls == 2
    assert request.key("a", "none", {}) != request.key("b", "none", {})
    assert request.key("a", "none", {}) != request.key("a", "keyed", {})


def test_image_unsupported_and_paid_not_enabled():
    paid = Provider("paid")
    paid.paid = True
    result = SearchRouter([paid]).search(SearchRequest("图片", intent=RetrievalIntent.IMAGE_SEARCH, search_type="image"))
    assert result.status == "unavailable" and paid.calls == 0


def test_quota_blocks_until_manual_handling_and_health_survives_restart(tmp_path):
    clock = [100.0]
    p = Provider('quota', error=SearchError('433', 'quota'))
    first = SearchRouter([p], cache_dir=tmp_path, clock=lambda: clock[0])
    first.search(SearchRequest('政策'))
    clock[0] += 100000
    p.error = None
    result = SearchRouter([p], cache_dir=tmp_path, clock=lambda: clock[0]).search(SearchRequest('政策'))
    assert result.status == 'unavailable' and p.calls == 1


def test_invalid_limits_fail_before_network(tmp_path):
    from copy import deepcopy
    from sqmy.config import Settings
    from sqmy.search_router import validate_config
    cfg = deepcopy(Settings.load().raw)
    cfg['search']['max_searches_per_action'] = 5
    with pytest.raises(ValueError, match='范围'):
        validate_config(Settings(tmp_path, cfg))


def test_two_router_instances_preserve_other_provider_health(tmp_path):
    a, b = Provider('a', error=SearchError('401', 'authentication')), Provider('b')
    one, two = SearchRouter([a], cache_dir=tmp_path), SearchRouter([b], cache_dir=tmp_path)
    one.search(SearchRequest('A规则'))
    two.search(SearchRequest('B规则'))
    state = json.loads((tmp_path / 'health.json').read_text())
    assert state['a:none:web']['blocked'] and not state['b:none:web']['blocked']


def test_test_harness_blocks_default_codex_runner(tmp_path):
    from sqmy.providers import CodexCliClient
    with pytest.raises(AssertionError, match='禁止真实Codex'):
        CodexCliClient(tmp_path, 'offline-model', timeout_seconds=5).analyze('不得发送', {})


def test_keyless_never_uses_environment_key_and_has_explicit_quality_params(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "offline-secret-not-to-send")
    captured = []
    def send(req, **kwargs):
        captured.append(req)
        return {"results": [{"title": "规则", "url": "https://www.gov.cn/policy", "content": "摘要"}]}
    monkeypatch.setattr("sqmy.search_providers.send_json", send)
    client = TavilyKeylessProvider({"enabled": True})
    result = client.search(SearchRequest("现行规定", intent=RetrievalIntent.POLICY_SEARCH, domains=("gov.cn",)))
    req = captured[0]
    assert not any(k.lower() == "authorization" for k in req.headers)
    assert req.get_header("X-tavily-access-mode") == "keyless"
    assert b"offline-secret" not in req.data
    payload = json.loads(req.data)
    assert payload["include_domains_mode"] == "restrict" and payload["search_depth"] == "advanced"
    assert not payload["auto_parameters"] and not payload["include_raw_content"] and not payload["include_answer"]
    assert "start_date" not in payload and result[0].verification_status == "unverified"


def test_keyless_extract_checks_failed_results_and_url_not_order(monkeypatch):
    client = TavilyKeylessProvider({"enabled": True})
    url = "https://www.gov.cn/policy"
    captured = []
    def send(endpoint, payload):
        captured.append(payload)
        return {"results": [{"url": "https://other.example/", "raw_content": "错误页"}, {"url": url, "raw_content": "原文"}]}
    monkeypatch.setattr(client, "_send", send)
    assert client.extract(url) == "原文" and "query" not in captured[0]
    monkeypatch.setattr(client, "_send", lambda *a: {"results": [{"url": url, "raw_content": "原文"}], "failed_results": [{"url": url}]})
    with pytest.raises(SearchError, match="extract_unavailable"):
        client.extract(url)


def test_brave_missing_web_results_and_page_age_not_publication(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "offline-brave-key")
    client = BraveSearchProvider({"enabled": True}, allow_paid=True)
    monkeypatch.setattr("sqmy.search_providers.send_json", lambda *a, **k: {"news": {"results": []}})
    with pytest.raises(SearchError, match="missing_web_results"):
        client.search(SearchRequest("规则"))
    monkeypatch.setattr("sqmy.search_providers.send_json", lambda *a, **k: {"web": {"results": [{"title": "页面", "url": "https://www.gov.cn/a", "page_age": "2026-10-01"}]}})
    assert client.search(SearchRequest("规则"))[0].published_at is None


def test_shared_action_limit_counts_fallback_and_migration_preserves_rows(tmp_path):
    from copy import deepcopy
    from pathlib import Path
    from sqmy.config import Settings
    from sqmy.workflow import Workflow
    from sqmy.retrieval_ledger import RetrievalLedger
    settings = Settings(tmp_path, deepcopy(Settings.load(Path(__file__).parents[1] / "config/settings.toml").raw))
    settings.raw["search"] = {"max_calls_per_action": 1}
    wf = Workflow(settings)
    run = wf.init_run("diagnostic")
    a, b = Provider("a", error=SearchError("timeout", "transient")), Provider("b")
    router = SearchRouter([a, b], cfg=settings.raw["search"], ledger=RetrievalLedger(settings, wf.db, run, "diagnostic"))
    result = router.search(SearchRequest("规则"))
    assert result.status == "unavailable" and a.calls == 1 and b.calls == 0
    with wf.db.connect() as conn:
        old = [dict(r) for r in conn.execute("SELECT * FROM retrieval_calls")]
    wf.db.initialize()
    with wf.db.connect() as conn:
        assert old == [dict(r) for r in conn.execute("SELECT * FROM retrieval_calls")]


@pytest.mark.parametrize('status,category', [(400, 'invalid_query'), (401, 'authentication'), (403, 'authentication'),
    (429, 'rate_limit'), (432, 'quota'), (433, 'quota'), (500, 'transient'), (410, 'endpoint_unavailable')])
def test_http_error_classification_and_retry_after_do_not_echo_response(monkeypatch, status, category):
    from email.message import Message
    from urllib.request import Request
    headers = Message()
    headers['Retry-After'] = '25'
    opener = Mock()
    opener.open.side_effect = HTTPError('https://api.tavily.com/search', status, '不可回显的响应正文', headers, None)
    monkeypatch.setattr('sqmy.search_providers.build_opener', lambda *a: opener)
    with pytest.raises(SearchError) as error:
        send_json(Request('https://api.tavily.com/search'), timeout=1, max_bytes=100)
    assert error.value.category == category and error.value.retry_after == 25
    assert str(error.value) == f'http_{status}'


def test_legacy_sql_migration_preserves_unknown_consumption(tmp_path):
    import sqlite3
    from sqmy.db import Database, SCHEMA, now
    path = tmp_path / 'history.db'
    stamp = now()
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute('INSERT INTO runs(id,phase,status,config_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                     ('legacy', 'discovery', 'running', 'old', stamp, stamp))
        conn.execute('INSERT INTO retrieval_calls(run_id,action,endpoint,request_hash,status,reserved_credits,accounted_credits,cost_equivalent_usd,accounting_method,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     ('legacy', 'discovery', 'search', 'old-request', 'running', 2, 2, .016, 'unknown_reserved', stamp, stamp))
    db = Database(path)
    db.initialize()
    with db.connect() as conn:
        row = conn.execute('SELECT * FROM retrieval_calls').fetchone()
        assert row['status'] == 'running' and row['accounted_credits'] == 2 and row['cost_equivalent_usd'] == .016
        assert row['provider'] == 'legacy_unknown' and row['auth_mode'] == 'legacy_unknown'


def test_image_contract_cannot_silently_use_web():
    with pytest.raises(ValueError, match='不能降级'):
        SearchRequest('图片', intent=RetrievalIntent.IMAGE_SEARCH)


def test_endpoint_gone_opens_cooldown_without_claiming_retirement():
    c = Circuit()
    c.failure(SearchError('http_410', 'endpoint_unavailable'), 100, 2, 300)
    assert c.state(101) == 'OPEN' and not c.blocked
    assert c.state(401) == 'HALF_OPEN' and c.allow(401)
    c.success()
    assert c.state(401) == 'CLOSED'
    c.failure(SearchError('explicit_verified_retirement', 'retired'), 402, 2, 300)
    assert c.blocked and not c.allow(100000)


def test_extract_retry_after_survives_restart_and_recovers_once(tmp_path):
    clock = [100.0]
    provider = Provider('extractor')
    first = SearchRouter([provider], cache_dir=tmp_path, clock=lambda: clock[0])
    operation = Mock(side_effect=SearchError('http_429', 'rate_limit', retry_after=60))
    with pytest.raises(SearchError, match='http_429'):
        first.invoke_extract(provider, operation)
    second = SearchRouter([provider], cache_dir=tmp_path, clock=lambda: clock[0])
    with pytest.raises(SearchError, match='circuit_open'):
        second.invoke_extract(provider, operation)
    assert operation.call_count == 1
    clock[0] += 61
    operation.side_effect = None
    operation.return_value = '原文'
    assert second.invoke_extract(provider, operation) == '原文'
    assert operation.call_count == 2 and provider.calls == 0
    assert second.health['extractor:none:extract'].state(clock[0]) == 'CLOSED'
