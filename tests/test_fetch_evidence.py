from copy import deepcopy
from datetime import datetime, timezone
from email.message import Message
import hashlib
import json
from pathlib import Path
import shutil
from unittest.mock import Mock

import pytest

from sqmy.config import Settings
from sqmy.evidence import EvidenceStore
from sqmy.fetch import DirectFetcher, _PublicRedirect, validate_destination
from sqmy.retrieval import execute_retrieval
from sqmy.search_router import SearchRouter
from sqmy.search_types import SearchError, SearchResult
from sqmy.source_registry import SourceRegistry
from sqmy.workflow import Workflow


def public_dns(*args, **kwargs):
    return [(2, 1, 6, '', ('93.184.216.34', 443))]


class Page:
    status = 200
    def __init__(self, body, media='text/html; charset=utf-8', url='https://example.test/policy'):
        self.body, self.url = body, url
        self.headers = Message()
        self.headers['Content-Type'] = media
    def read(self, limit):
        return self.body[:limit]
    def geturl(self):
        return self.url
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


def page_fetcher(body, **kwargs):
    opener = Mock()
    opener.open.return_value = Page(body, **kwargs)
    return DirectFetcher({'excerpt_chars': 5000, 'context_chars': 350}, opener=opener, resolver=public_dns), opener


def test_html_metadata_excerpt_and_hashes_remain_unverified(tmp_path):
    body = '<title>现行办法</title><meta name="PublishDate" content="2024-01-01"><script>秘密噪声</script><p>退款条件：仅限实名申请；2025年修订。</p>'.encode()
    fetcher, opener = page_fetcher(body)
    result = fetcher.fetch('https://example.test/policy')
    record = result.record(['退款'], fetcher.cfg)
    assert result.status == 'fetched' and result.title == '现行办法'
    assert result.published_at == '2024-01-01'
    assert result.content_hash == hashlib.sha256(body).hexdigest()
    assert '秘密噪声' not in record['excerpt'] and '仅限实名申请' in record['excerpt']
    assert record['verification_status'] == 'unverified' and record['fetch_status'] == 'source_unread'
    assert record['content_hash_kind'] == 'response_bytes'
    assert 'clean_text' not in record
    assert 'Authorization' not in opener.open.call_args.args[0].headers
    source = EvidenceStore.source_from_fetch(record, key='policy', source_name='办法')
    assert source['verification_status'] == 'unverified' and source['primary_source'] is False
    detail = EvidenceStore.link_detail(record, claim_part='退款范围', support_scope='仅支持适用条件', limitation='实施效果待核',
        locator='退款条件段', checked_at=datetime.now(timezone.utc).isoformat(), reviewed_by='offline-reviewer')
    assert detail['fetch_status'] == 'source_unread'
    verified = EvidenceStore.link_detail(record, claim_part='退款范围', support_scope='仅支持适用条件', limitation='实施效果待核',
        locator='退款条件段', checked_at=datetime.now(timezone.utc).isoformat(), reviewed_by='offline-reviewer', verified=True)
    assert verified['fetch_status'] == 'excerpt_verified'
    with pytest.raises(ValueError, match='哈希'):
        EvidenceStore.source_from_fetch(dict(record, excerpt='篡改'), key='policy', source_name='办法')
    with pytest.raises(ValueError, match='Fetch'):
        EvidenceStore.source_from_fetch({'url': 'https://example.test/a', 'provider': 'brave'}, key='search', source_name='命中')


def test_pdf_and_truncated_text_cannot_be_verified():
    fetcher, _ = page_fetcher(b'%PDF-1.7', media='application/pdf')
    assert fetcher.fetch('https://example.test/policy').status == 'unsupported'
    fetcher, _ = page_fetcher('退款条件：须提供凭证。'.encode())
    fetcher.cfg['max_response_bytes'] = 12
    record = fetcher.fetch_record('https://example.test/policy', ['退款'])
    assert record['status'] == 'incomplete' and record['truncated']
    with pytest.raises(ValueError, match='不完整'):
        EvidenceStore.link_detail(record, claim_part='范围', support_scope='片段', limitation='未读完整', locator='首段',
            checked_at=datetime.now(timezone.utc).isoformat(), reviewed_by='offline-reviewer', verified=True)


def test_private_dns_and_redirect_are_refused_before_transport():
    private = lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 80))]
    with pytest.raises(ValueError):
        validate_destination('https://example.test/policy', private)
    from urllib.request import Request
    with pytest.raises(ValueError):
        _PublicRedirect(public_dns).redirect_request(Request('https://example.test/policy'), None, 302, '', {}, 'http://127.0.0.1/admin')
    opener = Mock()
    fetcher = DirectFetcher({}, opener=opener, resolver=private)
    assert fetcher.fetch('https://example.test/policy').status == 'refused'
    opener.open.assert_not_called()


def test_registry_subdomain_specificity_does_not_certify_a_claim():
    registry = SourceRegistry(Path(__file__).parents[1] / 'config/source_registry.toml')
    assert registry.lookup('https://tjj.beijing.gov.cn/data')['category'] == 'statistics'
    assert registry.lookup('https://beijing.gov.cn.evil.test/data')['authority_level'] == 3
    assert registry.lookup('https://beijing.gov.cn.evil.test/data')['primary_source'] is False


@pytest.fixture
def context(tmp_path):
    project = Path(__file__).parents[1]
    shutil.copytree(project / 'config', tmp_path / 'config')
    settings = Settings(tmp_path, deepcopy(Settings.load(project / 'config/settings.toml').raw))
    workflow = Workflow(settings)
    return settings, workflow, workflow.init_run('diagnostic')


class Provider:
    name, auth_mode, paid, cost_usd, available = 'offline', 'none', False, 0.0, True
    search_types, parameters = frozenset({'web', 'news'}), {}
    def __init__(self, results=None, error=None):
        self.results, self.error, self.calls = results or [], error, 0
    def search(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return self.results


def wire(context, monkeypatch, *, results=None, error=None):
    from sqmy.retrieval_ledger import RetrievalLedger
    settings, workflow, run = context
    provider = Provider(results, error)
    router = SearchRouter([provider], cfg={'max_retries': 0}, ledger=RetrievalLedger(settings, workflow.db, run, 'diagnostic'))
    opener = Mock()
    monkeypatch.setattr('sqmy.retrieval_pipeline.build_search_router', lambda *a, **k: router)
    return provider, opener


def test_all_searches_failed_known_url_stays_isolated_and_resume_frozen(context, monkeypatch):
    settings, workflow, run = context
    provider, opener = wire(context, monkeypatch, error=SearchError('timeout', 'transient'))
    plan = dict(stage='diagnostic', queries=[{'purpose': 'policy', 'query': '现行退款办法'}],
                pages=[{'url': 'https://example.test/policy', 'terms': ['退款']}])
    result = execute_retrieval(settings, workflow.db, run, plan)
    state = json.loads(Path(result['report']).read_text())
    assert result['status'] == 'needs_review' and result['research_status'] == 'RESEARCH_INCOMPLETE'
    assert state['results'][0]['result']['status'] == 'unavailable'
    assert state['results'][1]['result']['error_code'] == 'unsupported_transport'
    assert execute_retrieval(settings, workflow.db, run, plan)['reused']
    assert provider.calls == 1 and opener.open.call_count == 0
    with workflow.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM source_usages').fetchone()[0] == 0
        row = conn.execute('SELECT provider,status,cost_equivalent_usd FROM retrieval_calls').fetchone()
        assert tuple(row) == ('offline', 'failed', 0.0)
    settings.raw['fetch']['context_chars'] += 1
    with pytest.raises(ValueError, match='固定'):
        execute_retrieval(settings, workflow.db, run, plan)


def test_empty_policy_results_are_incomplete_without_expanding_queries(context, monkeypatch):
    settings, workflow, run = context
    provider, _ = wire(context, monkeypatch)
    result = execute_retrieval(settings, workflow.db, run, dict(stage='diagnostic', queries=[{'purpose': 'policy', 'query': '无结果'}]))
    assert result['research_status'] == 'RESEARCH_INCOMPLETE' and provider.calls == 1


def test_page_only_plan_never_requires_search(context, monkeypatch):
    settings, workflow, run = context
    provider, opener = wire(context, monkeypatch)
    result = execute_retrieval(settings, workflow.db, run, dict(stage='diagnostic', pages=[{'url': 'https://example.test/policy', 'terms': ['退款']}]))
    assert result['research_status'] == 'RESEARCH_INCOMPLETE'
    assert provider.calls == 0 and opener.open.call_count == 0


def test_failed_direct_http_extracts_only_known_url_and_accounts_before_send(context):
    from sqmy.retrieval_ledger import RetrievalLedger
    from sqmy.search_providers import TavilyKeylessProvider
    settings, workflow, run = context
    opener, extractor = Mock(), TavilyKeylessProvider({'enabled': True})
    opener.open.side_effect = TimeoutError()
    urls = []
    def extract(url):
        with workflow.db.connect() as conn:
            row = conn.execute('SELECT endpoint,status,auth_mode FROM retrieval_calls').fetchone()
            assert tuple(row) == ('extract', 'running', 'keyless')
        urls.append(url)
        return '退款适用条件：须提供有效凭证。'
    extractor.extract = extract
    fetcher = DirectFetcher(settings.raw['fetch'], opener=opener, resolver=public_dns, extractor=extractor,
        ledger=RetrievalLedger(settings, workflow.db, run, 'diagnostic'), cache_dir=settings.root / 'data/cache/fetch')
    record = fetcher.fetch_record('https://example.test/policy', ['退款'])
    assert record['status'] == 'fetched' and record['content_hash_kind'] == 'extract_returned_text'
    assert urls == ['https://example.test/policy']
    assert fetcher.fetch_record('https://example.test/policy', ['退款'])['cache_hit']
    assert opener.open.call_count == 1 and len(urls) == 1
    saved = list((settings.root / 'data/cache/fetch').glob('*.json'))
    assert len(saved) == 1 and 'clean_text' not in saved[0].read_text()


def test_routed_repair_shares_lower_legacy_limit_and_preserves_manual_stop(tmp_path, monkeypatch):
    from test_retrieval_repair import setup, repair
    from sqmy.research_gate import review_pre_research
    settings, workflow, run, base, review, legacy_calls = setup(tmp_path, monkeypatch)
    settings.raw['search'].update(enabled=True, allow_paid=False)
    settings.raw['tavily']['max_searches_per_action'] = 2
    provider = Provider([SearchResult('办法', 'https://www.gov.cn/policy')])
    from sqmy.retrieval_ledger import RetrievalLedger
    router = SearchRouter([provider], ledger=RetrievalLedger(settings, workflow.db, run, 'pre_research:C1'))
    monkeypatch.setattr('sqmy.retrieval_pipeline.build_search_router', lambda *a, **k: router)
    assert execute_retrieval(settings, workflow.db, run, base)['status'] == 'completed'
    assert execute_retrieval(settings, workflow.db, run, repair(base, review))['status'] == 'completed'
    result = execute_retrieval(settings, workflow.db, run, repair(base, review, 2))
    assert result['status'] == 'needs_review' and result['research_status'] == 'RESEARCH_INCOMPLETE'
    assert provider.calls == 2 and legacy_calls == []
    with workflow.db.connect() as conn:
        assert conn.execute('SELECT research_allowed FROM research_reviews').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM retrieval_calls').fetchone()[0] == 2
    review_pre_research(settings, run, 'C1', decision='stop', note='人工确认停止')
    with pytest.raises(ValueError, match='停止'):
        execute_retrieval(settings, workflow.db, run, repair(base, review))


def test_fetch_engine_pdf_record_stays_manual_and_unread(context, monkeypatch):
    from sqmy.search_providers import TavilyKeylessProvider
    settings, workflow, run = context
    assert settings.raw['search']['keyless_enabled'] and settings.raw['fetch']['allow_keyless_extract']
    extract = Mock(return_value='退款适用条件：须提供有效凭证。')
    monkeypatch.setattr(TavilyKeylessProvider, 'extract', extract)
    opener = Mock()
    opener.open.return_value = Page(b'%PDF-1.7', media='application/pdf')
    from sqmy.retrieval_ledger import RetrievalLedger
    record = DirectFetcher(settings.raw['fetch'], opener=opener, resolver=public_dns,
        extractor=TavilyKeylessProvider({'enabled': True}),
        ledger=RetrievalLedger(settings, workflow.db, run, 'diagnostic')).fetch_record('https://example.test/policy', ['退款'])
    extract.assert_not_called()
    assert record['status'] == 'unsupported' and record['content_type'] == 'application/pdf'
    assert record['provider'] == 'direct_http' and record['error_code'] == 'requires_manual_extraction'
    assert record['fetch_status'] == 'source_unread' and record['verification_status'] == 'unverified'
    with pytest.raises(ValueError, match='失败、不完整'):
        EvidenceStore.link_detail(record, claim_part='范围', support_scope='待核', limitation='人工提取尚未完成',
            locator='待人工读取', checked_at=datetime.now(timezone.utc).isoformat(), reviewed_by='offline-reviewer', verified=True)
    with workflow.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM retrieval_calls').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0


def test_production_image_intent_cannot_fall_back_to_web_search(context, monkeypatch):
    from sqmy.search_providers import TavilyKeylessProvider, BraveSearchProvider, BingNewsRssProvider
    settings, workflow, run = context
    providers = []
    for cls in (TavilyKeylessProvider, BraveSearchProvider, BingNewsRssProvider):
        call = Mock(return_value=[SearchResult('网页结果', 'https://www.gov.cn/policy')])
        monkeypatch.setattr(cls, 'search', call)
        providers.append(call)
    result = execute_retrieval(settings, workflow.db, run, dict(stage='diagnostic',
        queries=[{'purpose': 'policy', 'intent': 'IMAGE_SEARCH', 'query': '政策说明图片'}]))
    for provider in providers:
        provider.assert_not_called()
    assert result['status'] == 'needs_review' and result['research_status'] == 'RESEARCH_INCOMPLETE'
    state = json.loads(Path(result['report']).read_text())
    query = state['results'][0]['result']
    assert query['status'] == 'unavailable' and not query['results']
    assert any(error['code'] == 'unsupported_capability' for error in query['errors'])
    with workflow.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM retrieval_calls').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0


@pytest.mark.parametrize('code,category', [('http_429', 'rate_limit'), ('http_401', 'authentication'), ('http_432', 'quota'), ('http_433', 'quota')])
@pytest.mark.parametrize('second_direct_succeeds', [False, True])
def test_fetch_engine_extract_errors_cool_down_across_urls_but_direct_http_continues(context, monkeypatch, code, category, second_direct_succeeds):
    from sqmy.search_providers import TavilyKeylessProvider
    settings, workflow, run = context
    extract = Mock(side_effect=SearchError(code, category, retry_after=60))
    monkeypatch.setattr(TavilyKeylessProvider, 'extract', extract)
    opener = Mock()
    opener.open.side_effect = [TimeoutError(), Page('退款条件：仅限实名办理。'.encode(), url='https://example.test/two') if second_direct_succeeds else TimeoutError()]
    from sqmy.retrieval_ledger import RetrievalLedger
    fetcher = DirectFetcher(settings.raw['fetch'], opener=opener, resolver=public_dns,
        extractor=TavilyKeylessProvider({'enabled': True}), ledger=RetrievalLedger(settings, workflow.db, run, 'diagnostic'))
    records = [fetcher.fetch_record(url, ['退款']) for url in ('https://example.test/one', 'https://example.test/two')]
    assert opener.open.call_count == 2 and extract.call_count == 1
    assert records[0]['extract_error'] == code
    if second_direct_succeeds:
        assert records[1]['status'] == 'fetched' and records[1]['provider'] == 'direct_http'
    else:
        assert records[1]['status'] == 'failed' and records[1]['extract_error'] == 'circuit_open'
    with workflow.db.connect() as conn:
        row = conn.execute('SELECT COUNT(*),SUM(cost_equivalent_usd) FROM retrieval_calls').fetchone()
        assert tuple(row) == (1, 0.0)
    health = json.loads((settings.root / 'data/cache/search/health.json').read_text())['tavily_keyless:keyless:extract']
    assert health['blocked'] is (category in {'authentication', 'quota'})
    if category == 'rate_limit':
        assert health['open_until'] > datetime.now(timezone.utc).timestamp()


def test_incomplete_html_extract_preserves_original_type_and_method(context):
    from sqmy.search_providers import TavilyKeylessProvider
    from sqmy.retrieval_ledger import RetrievalLedger
    settings, workflow, run = context
    opener, extractor = Mock(), TavilyKeylessProvider({'enabled': True})
    opener.open.return_value = Page('退款条件：须提供凭证。'.encode())
    extractor.extract = Mock(return_value='退款条件：须提供有效凭证。')
    cfg = dict(settings.raw['fetch'], max_response_bytes=12)
    fetcher = DirectFetcher(cfg, opener=opener, resolver=public_dns, extractor=extractor,
        ledger=RetrievalLedger(settings, workflow.db, run, 'diagnostic'), cache_dir=settings.root / 'data/cache/fetch')
    record = fetcher.fetch_record('https://example.test/policy', ['退款'])
    assert record['content_type'] == 'text/html' and record['extracted_content_type'] == 'text/plain'
    assert record['extraction_method'] == 'tavily_keyless_extract'
    assert record['direct_fetch_metadata']['status'] == 'incomplete' and record['direct_fetch_metadata']['truncated']
    assert record['verification_status'] == 'unverified' and record['fetch_status'] == 'source_unread'


def test_search_rate_limit_does_not_block_fetch_engine_extract(context, monkeypatch):
    from sqmy.search_providers import TavilyKeylessProvider
    settings, workflow, run = context
    search = Mock(side_effect=SearchError('http_429', 'rate_limit', retry_after=60))
    extract = Mock(return_value='退款条件：须实名提供凭证。')
    monkeypatch.setattr(TavilyKeylessProvider, 'search', search)
    monkeypatch.setattr(TavilyKeylessProvider, 'extract', extract)
    opener = Mock()
    opener.open.side_effect = TimeoutError()
    from sqmy.retrieval_ledger import RetrievalLedger
    from sqmy.search_types import SearchRequest, RetrievalIntent
    from sqmy.search_router import build_search_router
    router = build_search_router(settings, workflow.db, run, 'diagnostic')
    result = router.search(SearchRequest('退款现行条件', intent=RetrievalIntent.POLICY_SEARCH))
    page = DirectFetcher(settings.raw['fetch'], opener=opener, resolver=public_dns,
        extractor=TavilyKeylessProvider({'enabled': True}), ledger=RetrievalLedger(settings, workflow.db, run, 'diagnostic')).fetch_record('https://example.test/policy', ['退款'])
    assert search.call_count == 1 and extract.call_count == 1
    assert result.status == 'unavailable'
    assert page['status'] == 'fetched' and page['provider'] == 'tavily_keyless'


def test_extract_rate_limit_does_not_block_production_search(context, monkeypatch):
    from sqmy.retrieval_ledger import RetrievalLedger
    from sqmy.search_providers import TavilyKeylessProvider
    from sqmy.search_types import SearchRequest, RetrievalIntent
    settings, workflow, run = context
    extractor = TavilyKeylessProvider({'enabled': True})
    extractor.extract = Mock(side_effect=SearchError('http_429', 'rate_limit', retry_after=60))
    opener = Mock()
    opener.open.side_effect = TimeoutError()
    ledger = RetrievalLedger(settings, workflow.db, run, 'diagnostic')
    fetcher = DirectFetcher(settings.raw['fetch'], opener=opener, resolver=public_dns, extractor=extractor, ledger=ledger)
    fetcher.fetch_record('https://example.test/policy', ['退款'])
    monkeypatch.setattr(TavilyKeylessProvider, 'search', Mock(return_value=[SearchResult('办法', 'https://www.gov.cn/policy')]))
    from sqmy.search_router import build_search_router
    router = build_search_router(settings, workflow.db, run, 'diagnostic')
    response = router.search(SearchRequest('退款现行办法', intent=RetrievalIntent.POLICY_SEARCH))
    assert response.status == 'completed' and len(response.results) == 1


def test_ordinary_page_410_does_not_mean_search_provider_retired():
    from urllib.error import HTTPError
    fetcher, opener = page_fetcher(b'')
    opener.open.side_effect = HTTPError('https://example.test/policy', 410, 'Gone', {}, None)
    result = fetcher.fetch('https://example.test/policy')
    assert result.status == 'failed' and result.error_code == 'http_or_parse_failed'
