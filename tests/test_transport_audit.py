from copy import deepcopy
from email.message import Message
import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from sqmy.config import Settings
from sqmy.evidence import EvidenceStore
from sqmy.fetch import DirectFetcher, _PublicRedirect
from sqmy.retrieval_ledger import RetrievalLedger
from sqmy.search_providers import TavilyKeylessProvider
from sqmy.search_router import SearchRouter
from sqmy.search_types import SearchRequest
from sqmy.tavily import retrieval_usage
from sqmy.transport_audit import TransportAudit, observing
from sqmy.workflow import Workflow


class JSONResponse:
    status = 200
    def __init__(self, value):
        self.body = json.dumps(value).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, limit):
        return self.body[:limit]


@pytest.fixture
def context(tmp_path):
    shutil.copytree(Path(__file__).parents[1]/'config',tmp_path/'config')
    settings = Settings(tmp_path, deepcopy(Settings.load(Path(__file__).parents[1] / 'config/settings.toml').raw))
    workflow = Workflow(settings)
    run = workflow.init_run('diagnostic')
    ledger = RetrievalLedger(settings, workflow.db, run, 'diagnostic')
    return settings, workflow, run, ledger


def rows(context):
    with context[1].db.connect() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM retrieval_calls ORDER BY id')]


def router_for(context):
    provider = TavilyKeylessProvider({'enabled': True})
    router = SearchRouter([provider], cfg=context[0].raw['search'], ledger=context[3])
    return provider, router


def test_local_before_dispatch_retains_reservation_without_claiming_provider_outage(context, monkeypatch):
    provider, router = router_for(context)
    def broken_observer(*args, **kwargs):
        raise AttributeError('private-observation-local-error-do-not-echo')
    monkeypatch.setattr('sqmy.search_providers.send_json', broken_observer)
    for q in ('规则一', '规则二'):
        response = router.search(SearchRequest(q))
        assert response.errors[-1]['code'] == 'local_before_dispatch'
        audit = response.attempts[0]['transport']
        assert audit['dispatch_attempts'] == audit['responses_received'] == 0
        assert audit['failure_stage'] == 'provider_setup'
    assert len(rows(context)) == 2
    assert router.health['tavily_keyless:keyless:web'].failures == 0
    assert router.health['tavily_keyless:keyless:web'].state(router.clock()) == 'CLOSED'
    usage = retrieval_usage(context[1].db, context[2])['actions'][0]
    assert usage['reservations'] == usage['requests'] == 2
    assert usage['http_dispatch_attempts_known'] == 0 and usage['unknown_transport_records'] == 0
    assert usage['failure_stages'] == {'provider_setup': 2}


def test_real_transport_entry_is_persisted_before_open_and_usage_is_preserved(context, monkeypatch):
    provider, router = router_for(context)
    def open_request(request, **kwargs):
        row = rows(context)[0]
        trace = json.loads(row['result_json'])['transport']
        assert row['status'] == 'running' and trace['dispatch_attempts'] == 1
        assert trace['responses_received'] == 0 and trace['phase'] == 'dispatching'
        assert request.get_header('X-tavily-access-mode') == 'keyless'
        assert request.get_header('Authorization') is None
        return JSONResponse({'usage': {'credits': 2}, 'results': [{'title': '规则', 'url': 'https://www.gov.cn/example', 'content': '待核摘要'}]})
    opener = Mock(open=open_request)
    monkeypatch.setattr('sqmy.search_providers.build_opener', lambda *args: opener)
    response = router.search(SearchRequest('规则'))
    assert response.status == 'completed' and response.results[0].verification_status == 'unverified'
    row = rows(context)[0]
    assert row['reported_credits'] == 2 and row['accounted_credits'] == row['cost_equivalent_usd'] == 0
    trace = json.loads(row['result_json'])['transport']
    assert trace['dispatch_attempts'] == trace['responses_received'] == 1 and trace['http_status'] == 200
    usage = retrieval_usage(context[1].db, context[2])['actions'][0]
    assert usage['requests'] == 1 and usage['http_dispatch_attempts_known'] == usage['responses_received_known'] == 1
    assert usage['reported_credits'] == 2


def test_timeout_after_dispatch_is_unknown_delivery_not_zero_request(context, monkeypatch):
    provider, router = router_for(context)
    opener = Mock()
    opener.open.side_effect = TimeoutError()
    monkeypatch.setattr('sqmy.search_providers.build_opener', lambda *args: opener)
    response = router.search(SearchRequest('规则'))
    assert response.errors[-1]['code'] == 'transport_failed'
    trace = json.loads(rows(context)[0]['result_json'])['transport']
    assert trace['dispatch_attempts'] == 1 and trace['responses_received'] == 0
    assert trace['failure_stage'] == 'dispatching'
    usage = retrieval_usage(context[1].db, context[2])['actions'][0]
    assert usage['dispatches_without_response'] == 1 and usage['reported_credits'] is None


def test_audit_checkpoint_failure_prevents_open(context, monkeypatch):
    provider, router = router_for(context)
    opener = Mock()
    monkeypatch.setattr('sqmy.search_providers.build_opener', lambda *args: opener)
    def cannot_checkpoint(*args):
        raise OSError('local checkpoint unavailable')
    monkeypatch.setattr(context[3], 'record_transport', cannot_checkpoint)
    result = router.search(SearchRequest('规则'))
    opener.open.assert_not_called()
    assert result.errors[-1]['code'] == 'local_before_dispatch'
    assert result.attempts[0]['transport']['failure_stage'] == 'audit_checkpoint'
    assert result.attempts[0]['transport']['dispatch_attempts'] == 0


def test_interruption_preserves_dispatch_unknown_without_refund(context, monkeypatch):
    provider, router = router_for(context)
    opener = Mock()
    opener.open.side_effect = KeyboardInterrupt()
    monkeypatch.setattr('sqmy.search_providers.build_opener', lambda *args: opener)
    with pytest.raises(KeyboardInterrupt):
        router.search(SearchRequest('规则'))
    row = rows(context)[0]
    assert row['status'] == 'running'
    trace = json.loads(row['result_json'])['transport']
    assert trace['dispatch_attempts'] == 1 and trace['responses_received'] == 0
    assert retrieval_usage(context[1].db, context[2])['actions'][0]['reservations'] == 1


def test_cross_run_cache_counts_scope_but_never_duplicates_dispatch_or_usage(context, monkeypatch):
    settings, workflow, run, ledger = context
    provider = TavilyKeylessProvider({'enabled': True})
    opener = Mock()
    opener.open.return_value = JSONResponse({'usage': {'credits': 2}, 'results': []})
    monkeypatch.setattr('sqmy.search_providers.build_opener', lambda *args: opener)
    cache = settings.root / 'search-cache'
    SearchRouter([provider], cfg=settings.raw['search'], ledger=ledger, cache_dir=cache).search(SearchRequest('规则'))
    other = workflow.init_run('diagnostic')
    other_ledger = RetrievalLedger(settings, workflow.db, other, 'diagnostic')
    response = SearchRouter([provider], cfg=settings.raw['search'], ledger=other_ledger, cache_dir=cache).search(SearchRequest('规则'))
    assert response.attempts[0]['cache_hit'] and opener.open.call_count == 1
    usage = retrieval_usage(workflow.db, other)['actions'][0]
    assert usage['reservations'] == usage['cache_hits'] == 1
    assert usage['http_dispatch_attempts_known'] == usage['responses_received_known'] == 0
    assert usage['reported_credits'] is None


@pytest.mark.parametrize('address', ['198.18.0.53', '2001:2::26', '127.0.0.1', '10.0.0.1', '::1'])
def test_non_global_dns_is_still_security_refusal_before_dispatch_without_extract(context, address):
    provider = TavilyKeylessProvider({'enabled': True})
    provider.extract = Mock(return_value='不允许使用的文本')
    opener = Mock()
    resolver = lambda *a, **k: [(2,1,6,'',(address,443))]
    fetcher = DirectFetcher(dict(context[0].raw['fetch'],allow_keyless_extract=True),opener=opener,resolver=resolver,
        extractor=provider,ledger=context[3])
    record = fetcher.fetch_record('https://official.example/policy',['条款'])
    assert record['status'] == 'refused' and record['error_code'] == 'unsafe_destination'
    assert record['destination_error'] == 'non_global_dns_address'
    assert record['transport_metadata']['dispatch_attempts'] == 0
    assert record['transport_metadata']['failure_stage'] == 'destination_validation'
    opener.open.assert_not_called()
    provider.extract.assert_not_called()
    assert rows(context) == []
    source = EvidenceStore.source_from_fetch(record,key='blocked',source_name='未读来源')
    assert not source['primary_source'] and source['fetch_status'] == 'source_unread'
    with pytest.raises(ValueError,match='失败、不完整'):
        EvidenceStore.link_detail(record,claim_part='条款',support_scope='无',limitation='安全拒绝',locator='无',
            checked_at='2026-01-01',reviewed_by='offline',verified=True)


def test_redirect_to_non_global_dns_stops_second_dispatch():
    def resolver(host,*args,**kwargs):
        return [(2,1,6,'',('198.18.0.53' if host=='unsafe.example' else '93.184.216.34',443))]
    from urllib.request import Request
    audit = TransportAudit(tracked=True)
    with observing(audit):
        audit.before_dispatch()
        with pytest.raises(ValueError):
            _PublicRedirect(resolver).redirect_request(Request('https://safe.example/policy'),None,302,'',{},'https://unsafe.example/policy')
    assert audit.dispatches == 1 and audit.responses == 1
    assert audit.phase == 'redirect_destination_validation'


def test_old_audit_rows_remain_unknown_and_unchanged(context):
    settings, workflow, run, ledger = context
    provider = TavilyKeylessProvider({'enabled': True})
    ledger.reserve(provider,SearchRequest('旧记录'),'legacy-reservation')
    before = rows(context)
    usage = retrieval_usage(workflow.db,run)['actions'][0]
    assert usage['reservations'] == usage['unknown_transport_records'] == 1
    assert rows(context) == before


def test_manual_transport_cannot_be_promoted_into_program_evidence():
    record={'provider':'manual_public_url_read','url':'https://official.example/policy','fetch_status':'source_unread'}
    with pytest.raises(ValueError,match='Fetch记录'):
        EvidenceStore.source_from_fetch(record,key='manual',source_name='人工补读')


def test_response_normalization_failure_preserves_real_response_and_credits(context,monkeypatch):
    provider,router=router_for(context)
    opener=Mock()
    opener.open.return_value=JSONResponse({'usage':{'credits':2},'results':'invalid-shape'})
    monkeypatch.setattr('sqmy.search_providers.build_opener',lambda *args:opener)
    result=router.search(SearchRequest('规则'))
    assert result.errors[-1]['code']=='missing_results'
    trace=json.loads(rows(context)[0]['result_json'])['transport']
    assert trace['dispatch_attempts']==trace['responses_received']==1
    assert trace['failure_stage']=='provider_normalization' and rows(context)[0]['reported_credits']==2


def test_search_local_failure_still_fetches_known_public_url_without_evidence_promotion(context,monkeypatch):
    from sqmy.retrieval import execute_retrieval
    from test_fetch_evidence import Page,public_dns
    settings,workflow,run,ledger=context
    provider,router=router_for(context)
    def local_error(*args,**kwargs):
        raise AttributeError('local-only')
    monkeypatch.setattr('sqmy.search_providers.send_json',local_error)
    monkeypatch.setattr('sqmy.retrieval_pipeline.build_search_router',lambda *args:router)
    opener=Mock()
    opener.open.return_value=Page('<title>规则</title><p>退款：须实名办理，现行执行效果待核。</p>'.encode())
    monkeypatch.setattr('sqmy.retrieval_pipeline.DirectFetcher',lambda cfg,**kwargs:DirectFetcher(cfg,opener=opener,resolver=public_dns,**kwargs))
    result=execute_retrieval(settings,workflow.db,run,{'stage':'diagnostic',
        'queries':[{'purpose':'policy','query':'规则'}],
        'pages':[{'url':'https://example.test/policy','terms':['退款']}]})
    assert result['status']=='needs_review' and result['research_status']=='RESEARCH_INCOMPLETE'
    state=json.loads(Path(result['report']).read_text())
    page=state['results'][1]['result']
    assert page['status']=='fetched' and page['transport_metadata']['dispatch_attempts']==1
    assert page['transport_metadata']['responses_received']==1
    assert EvidenceStore.source_from_fetch(page,key='policy',source_name='规则')['fetch_status']=='source_unread'
    opener.open.assert_called_once()
    with workflow.db.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM source_usages').fetchone()[0]==0


def test_allowed_public_redirect_records_second_dispatch_without_relaxing_validation():
    from test_fetch_evidence import public_dns
    from urllib.request import Request
    audit=TransportAudit(tracked=True)
    with observing(audit):
        audit.before_dispatch()
        request=_PublicRedirect(public_dns).redirect_request(Request('https://safe.example/one'),None,302,'',{},'https://safe.example/two')
    assert request.full_url=='https://safe.example/two'
    assert audit.dispatches==2 and audit.responses==1


def test_invalid_json_records_response_parse_failure(context,monkeypatch):
    provider,router=router_for(context)
    response=JSONResponse({})
    response.body=b'<html>not-json</html>'
    opener=Mock()
    opener.open.return_value=response
    monkeypatch.setattr('sqmy.search_providers.build_opener',lambda *args:opener)
    result=router.search(SearchRequest('规则'))
    trace=result.attempts[0]['transport']
    assert trace['dispatch_attempts']==trace['responses_received']==1
    assert trace['failure_stage']=='response_parse' and rows(context)[0]['reported_credits'] is None


def test_direct_http_error_records_received_response_without_claiming_service_retirement():
    from test_fetch_evidence import public_dns
    from urllib.error import HTTPError
    opener=Mock()
    opener.open.side_effect=HTTPError('https://example.test/policy',410,'ignored',{},None)
    result=DirectFetcher({},opener=opener,resolver=public_dns).fetch('https://example.test/policy')
    assert result.status=='failed' and result.http_status==410
    assert result.transport_metadata['dispatch_attempts']==result.transport_metadata['responses_received']==1
    assert result.transport_metadata['failure_stage']=='http_status'


@pytest.mark.parametrize('failure',['cache_read','request','tls','checkpoint'])
def test_production_bing_presend_failures_do_not_open_circuit(context,monkeypatch,failure):
    import hashlib
    from sqmy.collector import SourceCollector
    from sqmy.search_providers import BingNewsRssProvider
    settings,workflow,run,ledger=context
    collector=SourceCollector(settings.root,settings.raw)
    provider=BingNewsRssProvider(collector)
    router=SearchRouter([provider],cfg=dict(settings.raw['search'],providers=['bing_news_rss']),ledger=ledger)
    network=Mock()
    monkeypatch.setattr('sqmy.collector.urlopen',network)
    def local_failure(*args,**kwargs):
        raise OSError('offline local failure; not a provider response')
    query='有界测试'
    if failure=='cache_read':
        digest=hashlib.sha256(provider.endpoint(query).encode()).hexdigest()
        cache=settings.root/'data/cache/counterevidence'/f'{digest}.xml'
        cache.parent.mkdir(parents=True)
        cache.write_text('<rss><channel><title>缓存</title></channel></rss>')
        original_read=Path.read_text
        def read(path,*args,**kwargs):
            if path==cache:
                local_failure()
            return original_read(path,*args,**kwargs)
        monkeypatch.setattr(Path,'read_text',read)
    elif failure=='request':
        monkeypatch.setattr('sqmy.collector.Request',local_failure)
    elif failure=='tls':
        monkeypatch.setattr('sqmy.collector.ssl.create_default_context',local_failure)
    else:
        monkeypatch.setattr(ledger,'record_transport',local_failure)
    request=SearchRequest(query,search_type='news')
    result=router.search(request)
    assert result.errors[-1]['code']=='local_before_dispatch'
    assert result.errors[-1]['category']=='local'
    network.assert_not_called()
    assert result.attempts[0]['transport']['dispatch_attempts']==0
    assert router.health['bing_news_rss:none:news'].state(router.clock())=='CLOSED'
    assert router.health['bing_news_rss:none:news'].failures==0
    stored=rows(context)
    assert len(stored)==1 and stored[0]['status']=='failed'
    assert json.loads(stored[0]['result_json'])['transport']['dispatch_attempts']==0
    assert retrieval_usage(workflow.db,run)['actions'][0]['reservations']==1


def test_production_bing_bad_network_RSS_response_still_opens_circuit(context,monkeypatch):
    from sqmy.collector import SourceCollector
    from sqmy.search_providers import BingNewsRssProvider
    settings,workflow,run,ledger=context
    provider=BingNewsRssProvider(SourceCollector(settings.root,settings.raw))
    router=SearchRouter([provider],cfg=dict(settings.raw['search'],providers=['bing_news_rss']),ledger=ledger)
    response=JSONResponse({})
    response.body=b'<html>provider returned HTML instead of RSS</html>'
    network=Mock(return_value=response)
    monkeypatch.setattr('sqmy.collector.urlopen',network)
    result=router.search(SearchRequest('有界测试',search_type='news'))
    assert result.errors[-1]['code']=='rss_endpoint_unavailable'
    trace=result.attempts[0]['transport']
    assert trace['dispatch_attempts']==trace['responses_received']==1
    assert trace['failure_stage']=='response_parse'
    assert router.health['bing_news_rss:none:news'].state(router.clock())=='OPEN'
    again=router.search(SearchRequest('第二个问题',search_type='news'))
    assert again.errors[-1]['code']=='circuit_open' and network.call_count==1
    assert len(rows(context))==1
