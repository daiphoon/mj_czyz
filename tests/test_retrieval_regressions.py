from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from sqmy.models import EventItem
from sqmy.screener import deduplicate
from sqmy.retrieval import execute_retrieval, _parameters
from sqmy.retrieval_pipeline import parameters
from sqmy.retrieval_ledger import RetrievalLedger
from sqmy.search_router import SearchRouter
from sqmy.search_types import SearchResult
from test_fetch_evidence import context, Provider


def no_page_transport(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('isolated pages must not reach DNS, HTTP, Extract or the collector')
    for target in ('socket.getaddrinfo', 'sqmy.fetch.DirectFetcher.__init__',
                   'sqmy.search_providers.TavilyKeylessProvider.extract',
                   'sqmy.tavily.TavilyClient.__init__', 'sqmy.collector.SourceCollector._fetch_url'):
        monkeypatch.setattr(target, blocked)


def offline_search(context, monkeypatch):
    settings, workflow, run = context
    provider = Provider([SearchResult('规则', 'https://www.gov.cn/policy')])
    router = SearchRouter([provider], ledger=RetrievalLedger(settings, workflow.db, run, 'diagnostic'))
    monkeypatch.setattr('sqmy.retrieval_pipeline.build_search_router', lambda *a, **k: router)
    return provider


def page_plan():
    return dict(stage='diagnostic', pages=[{'url': 'https://example.test/policy', 'terms': ['退款']}])


def test_pages_isolated_queries_continue_and_unread_evidence_cannot_upgrade(context, monkeypatch):
    from sqmy.evidence import EvidenceStore
    settings, workflow, run = context
    provider = offline_search(context, monkeypatch)
    no_page_transport(monkeypatch)
    plan = dict(page_plan(), queries=[{'purpose': 'policy', 'query': '现行规则'}])
    result = execute_retrieval(settings, workflow.db, run, plan)
    state = json.loads(Path(result['report']).read_text())
    assert state['contract'] == 'retrieval_pipeline_v2'
    assert state['parameters']['contract'] == 'retrieval_pipeline_v1'
    assert state['page_fetch_policy'] == 'isolated_v1'
    assert provider.calls == 1 and state['results'][0]['result']['status'] == 'completed'
    record = state['results'][1]['result']
    assert record['status'] == 'unsupported' and record['error_code'] == 'unsupported_transport'
    assert record['fetch_status'] == 'source_unread' and record['body_read'] is False
    assert not record['excerpt'] and not record['content_hash'] and not record['target_found']
    assert record['transport_metadata']['dispatch_attempts'] == 0
    assert record['transport_metadata']['responses_received'] == 0
    assert result['status'] == 'needs_review' and result['research_status'] == 'RESEARCH_INCOMPLETE'
    source = EvidenceStore.source_from_fetch(record, key='unread', source_name='未读页面')
    assert source['verification_status'] == 'unverified' and source['fetch_status'] == 'source_unread'
    with pytest.raises(ValueError, match='失败、不完整'):
        EvidenceStore.link_detail(record, claim_part='退款', support_scope='待核', limitation='正文未读',
            locator='未取得', checked_at=datetime.now(timezone.utc).isoformat(), reviewed_by='offline-reviewer', verified=True)
    with workflow.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM retrieval_calls').fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM retrieval_calls WHERE endpoint='extract'").fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0
    before = Path(result['report']).read_bytes()
    retry = execute_retrieval(settings, workflow.db, run, plan, retry_failed=True)
    assert retry['reused'] and provider.calls == 1
    assert Path(result['report']).read_bytes() == before


@pytest.mark.parametrize('mode', ['diagnostic', 'run_id', 'retry_failed'])
def test_cli_pages_isolated_for_new_existing_and_retry(context, monkeypatch, capsys, mode):
    from sqmy.cli import main
    settings, workflow, run = context
    plan_file = settings.root/'plan.json'
    plan_file.write_text(json.dumps(page_plan()))
    monkeypatch.setattr('sqmy.cli.load_dotenv', lambda: None)
    no_page_transport(monkeypatch)
    args = ['--config', str(settings.root/'config/settings.toml'), 'retrieve', '--plan', str(plan_file)]
    args += ['--diagnostic'] if mode == 'diagnostic' else ['--run-id', run]
    if mode == 'retry_failed':
        execute_retrieval(settings, workflow.db, run, page_plan())
        args += ['--retry-failed']
    assert main(args) == 2
    output = capsys.readouterr().out
    result = json.loads(output[output.index('{'):])
    record = json.loads(Path(result['report']).read_text())['results'][0]['result']
    assert record['error_code'] == 'unsupported_transport' and record['body_read'] is False
    assert result['research_status'] == 'RESEARCH_INCOMPLETE'
    with workflow.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM retrieval_calls').fetchone()[0] == 0


def test_retry_search_keeps_page_isolated_and_shares_original_failure_budget(context, monkeypatch):
    from sqmy.search_types import SearchError
    settings, workflow, run = context
    provider = offline_search(context, monkeypatch)
    provider.error = SearchError('timeout', 'transient')
    no_page_transport(monkeypatch)
    plan = dict(page_plan(), queries=[{'purpose':'policy','query':'原问题'}])
    result = execute_retrieval(settings, workflow.db, run, plan)
    path = Path(result['report'])
    page_before = next(row for row in json.loads(path.read_text())['results'] if row['step'] == 'page:0')
    provider.error = None
    result = execute_retrieval(settings, workflow.db, run, plan, retry_failed=True)
    state = json.loads(path.read_text())
    records = {row['step']:row for row in state['results']}
    assert provider.calls == 2 and records['query:0']['result']['status'] == 'completed'
    assert records['page:0'] == page_before
    assert result['research_status'] == 'RESEARCH_INCOMPLETE'
    with workflow.db.connect() as conn:
        rows = list(conn.execute('SELECT action,status FROM retrieval_calls ORDER BY id'))
        assert sorted(tuple(row) for row in rows) == [('diagnostic','completed'),('diagnostic','failed')]
    before = path.read_bytes()
    assert execute_retrieval(settings, workflow.db, run, plan, retry_failed=True)['reused']
    assert provider.calls == 2 and path.read_bytes() == before


@pytest.mark.parametrize('contract,status', [('retrieval_pipeline_v1','needs_review'),
                                           ('retrieval_pipeline_v1','running'),
                                           ('retrieval_pipeline_v1','completed'), (None,'failed')])
def test_old_checkpoints_reuse_without_rewriting_or_paid_fallback(context, monkeypatch, contract, status):
    from sqmy.cli import main
    settings, workflow, run = context
    plan = page_plan()
    path = settings.root/'data/runs'/run/'retrieval-diagnostic.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    frozen = parameters(settings) if contract else _parameters(settings.raw['tavily'])
    state = dict(input_hash=hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
                 plan=plan, parameters=frozen, status=status, results=[{'step':'page:0', 'result':{
                     'status':'refused', 'error_code':'unsafe_destination', 'destination_error':'non_global_dns_address'}}],
                 research_status='RESEARCH_INCOMPLETE', usage={'reservations':4})
    if contract:
        state['contract'] = contract
    path.write_text(json.dumps(state))
    original = path.read_bytes()
    from sqmy.search_types import SearchRequest, SearchError
    ledger = RetrievalLedger(settings, workflow.db, run, 'diagnostic')
    for i in range(4):
        call_id = ledger.reserve(Provider(), SearchRequest('旧查询'+str(i)), 'old:'+str(i))
        ledger.finish(call_id, error=SearchError('timeout','transient'))
    def rows():
        with workflow.db.connect() as conn:
            return {table:[tuple(row) for row in conn.execute('SELECT * FROM '+table)]
                    for table in ('runs','tasks','retrieval_calls','source_usages','model_calls')}
    before = rows()
    no_page_transport(monkeypatch)
    monkeypatch.setattr('sqmy.retrieval_pipeline.build_search_router', Mock(side_effect=AssertionError('old run must not search')))
    monkeypatch.setattr('sqmy.cli.load_dotenv', lambda: None)
    for retry in (False, True):
        result = execute_retrieval(settings, workflow.db, run, plan, retry_failed=retry)
        assert result['reused'] and result['historical']
        assert result['checkpoint_status'] == status
        assert result['research_status'] == 'RESEARCH_INCOMPLETE'
        assert Path(result['report']).read_bytes() == original and rows() == before
    plan_file = settings.root/'plan.json'
    plan_file.write_text(json.dumps(plan))
    assert main(['--config', str(settings.root/'config/settings.toml'), 'retrieve', '--run-id', run,
                 '--plan', str(plan_file), '--retry-failed']) == 2
    assert path.read_bytes() == original and rows() == before
    changed_plan = dict(plan, pages=[{'url':'https://example.test/changed','terms':['退款']}])
    plan_file.write_text(json.dumps(changed_plan))
    with pytest.raises(ValueError, match='固定'):
        main(['--config',str(settings.root/'config/settings.toml'),'retrieve','--run-id',run,'--plan',str(plan_file)])
    assert path.read_bytes() == original and rows() == before
    settings.raw['fetch']['context_chars'] += 1
    if contract:
        with pytest.raises(ValueError, match='固定'):
            execute_retrieval(settings, workflow.db, run, plan, retry_failed=True)
    assert path.read_bytes() == original and rows() == before


def test_discovery_fixed_collection_and_frozen_contract_remain_unchanged(context, monkeypatch):
    from sqmy.retrieval_pipeline import collect_routed
    settings, workflow, run = context
    no_page_transport(monkeypatch)
    provider = offline_search(context, monkeypatch)
    collector = Mock(collection_stats=[])
    collector.collect.return_value = []
    collect_routed(collector, settings, workflow.db, run)
    path = settings.root/'data/runs'/run/'collection_checkpoint.json'
    state = json.loads(path.read_text())
    assert state['contract'] == 'retrieval_pipeline_v1'
    assert state['parameters'] == parameters(settings)
    before = path.read_bytes()
    calls = provider.calls
    collect_routed(collector, settings, workflow.db, run)
    collector.collect.assert_called_once()
    assert provider.calls == calls and path.read_bytes() == before


def test_unknown_contract_and_modified_isolation_policy_never_downgrade(context, monkeypatch):
    settings, workflow, run = context
    no_page_transport(monkeypatch)
    result = execute_retrieval(settings, workflow.db, run, page_plan())
    path = Path(result['report'])
    for field, value in [('contract','unknown_v99'), ('page_fetch_policy','direct_http')]:
        state = json.loads(path.read_text())
        state[field] = value
        original = json.dumps(state).encode()
        path.write_bytes(original)
        with pytest.raises(ValueError, match='契约|策略'):
            execute_retrieval(settings, workflow.db, run, page_plan(), retry_failed=True)
        assert path.read_bytes() == original
        state['contract'], state['page_fetch_policy'] = 'retrieval_pipeline_v2', 'isolated_v1'
        path.write_text(json.dumps(state))


def test_distinct_numbered_official_reports_are_not_title_duplicates():
    first = EventItem('environment', 'index', '审计署', 1,
        '审计署审计结果公告2026年第8号：生态环境专项审计结果',
        'https://audit.gov.cn/report/8', datetime.now(timezone.utc).isoformat(), '', '全国',
        material={'discovery_provenance': {'material_kind': 'audit_report'}})
    other = deepcopy(first)
    other.id, other.url = 'water', 'https://audit.gov.cn/report/9'
    other.title = '审计署审计结果公告2026年第9号：水资源专项审计结果'
    assert len(deduplicate([first, other], .80)) == 2
    reprint = deepcopy(first)
    reprint.id, reprint.url = 'reprint', 'https://audit.gov.cn/reprint/8'
    assert len(deduplicate([first, reprint], .80)) == 1
