from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest

from sqmy.budget import BudgetExceeded, interactive_usage_estimate, recent_usage, record_stage_usage
from sqmy.config import Settings
from sqmy.discovery import LiveDiscovery
from sqmy.maintenance import preflight
from sqmy.model_calls import CallLedger
from sqmy.providers import ModelResult
from sqmy.research_gate import check_pre_research
from sqmy.semantic_review import semantic_review
from test_delivery_quality import draft_context
from test_research_gate import valid_brief, prepare_run
from test_semantic_review import v3_context, enable


class Client:
    provider = 'codex_cli'
    model = 'offline-observation-model'
    calls = 0
    def analyze(self, prompt, schema):
        self.calls += 1
        return ModelResult({'selections': []}, 2_000_000, 100, 'offline', self.provider, self.model)


def settings_for(tmp_path):
    project = Path(__file__).parents[1]
    shutil.copytree(project / 'config', tmp_path / 'config')
    raw = deepcopy(Settings.load(project / 'config/settings.toml').raw)
    assert raw['budget']['enforce_token_limits'] is False
    raw['budget'].update(screening_tokens=0, diagnostic_tokens=0, deep_research_tokens=0,
                         pre_research_tokens=0, writing_tokens=0)
    return Settings(tmp_path, raw).with_model('offline-observation-model')


@pytest.mark.parametrize('stage', ['screening', 'diagnostic', 'deep_research'])
def test_all_program_stages_ignore_token_references_but_keep_call_limit(tmp_path, stage):
    settings = settings_for(tmp_path)
    discovery = LiveDiscovery(settings)
    run = discovery.wf.init_run('test_fixture')
    client = Client()
    ledger = CallLedger(discovery.wf.db, settings, run, stage, 'p',
                        topic_id='topic' if stage == 'deep_research' else None)
    if stage == 'deep_research':
        record_stage_usage(discovery.wf.db, run_id=run, topic_id='topic', stage=stage, token_used=9_000_000,
                           input_hash='old', provider='codex_subscription', model='unknown', note='历史交互估算')
    for _ in range(3): ledger.invoke(client, 'input', {})
    assert client.calls == 3 and ledger.overrun['enforced'] is False
    with pytest.raises(BudgetExceeded, match='调用次数'): ledger.invoke(client, 'input', {})
    assert client.calls == 3
    assert recent_usage(discovery.wf.db)['measured_model_tokens'] == 6_000_300


def test_same_configuration_can_explicitly_restore_token_enforcement(tmp_path):
    settings = settings_for(tmp_path)
    settings.raw['budget']['enforce_token_limits'] = True
    discovery = LiveDiscovery(settings)
    run = discovery.wf.init_run('test_fixture')
    client = Client()
    with pytest.raises(BudgetExceeded, match='预算不足'):
        CallLedger(discovery.wf.db, settings, run, 'screening', 'p').invoke(client, 'input', {})
    assert client.calls == 0


def test_observe_tokens_does_not_disable_paid_fee_reservation(tmp_path):
    settings = settings_for(tmp_path)
    settings.raw['budget']['weekly_cost_limit_cny'] = 0
    discovery = LiveDiscovery(settings)
    run = discovery.wf.init_run('test_fixture')
    client = Client(); client.provider = 'deepseek'  # injected offline double only
    with pytest.raises(BudgetExceeded, match='付费预算不足'):
        CallLedger(discovery.wf.db, settings, run, 'screening', 'p').invoke(client, 'input', {})
    assert client.calls == 0


def test_interactive_estimates_follow_artifacts_without_using_old_token_caps(tmp_path):
    settings = settings_for(tmp_path)
    for stage in ('pre_research', 'deep_research', 'writing'):
        small = interactive_usage_estimate(settings, stage, '材料' * 2000)
        large = interactive_usage_estimate(settings, stage, '材料' * 4000)
        assert small['token_used'] == 2000 and large['token_used'] == 4000
        assert large['accounting_method'] == 'artifact_proxy_estimate'
        assert '可能低估' in large['note']


def test_pre_research_estimate_above_old_limit_does_not_approve_deep_research(tmp_path):
    settings = settings_for(tmp_path)
    wf, run = prepare_run(settings)
    payload = valid_brief(run)
    payload['budget'].pop('token_limit')
    payload['budget']['token_estimate'] = 800_000
    path = tmp_path / 'brief.json'; path.write_text(json.dumps(payload, ensure_ascii=False))
    result = check_pre_research(settings, run, 'C1', path)
    assert result['gate']['research_allowed'] is True
    with wf.db.connect() as conn:
        record = conn.execute('SELECT human_decision FROM research_reviews').fetchone()
        assert record[0] != 'proceed'
        assert conn.execute("SELECT token_used FROM stage_usage WHERE stage='pre_research'").fetchone()[0] == 800_000


def test_semantic_call_after_large_interactive_estimate_is_observation_only(v3_context, monkeypatch):
    settings, wf, run, _ = v3_context
    settings.raw['budget']['deep_research_tokens'] = 1
    record_stage_usage(wf.db, run_id=run, topic_id='delivery-topic', stage='deep_research', token_used=1_000_000,
                       input_hash='existing', provider='codex_subscription', model='unknown', note='已有估算')
    router, path = enable(v3_context, monkeypatch)
    first = semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True)
    data = json.loads(path.read_text()); data['claims'][0]['claim_text'] += '补充适用范围'
    path.write_text(json.dumps(data))
    second = semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True)
    assert first['status'] == second['status'] == 'completed' and router.calls == 2
    assert wf.status(run, include_all=True)[0]['status'] != 'paused_budget'
    assert recent_usage(wf.db)['estimated_interactive_tokens'] == 1_000_000


def test_conversation_preflight_needs_no_cli_or_model_id_and_keeps_scope_checks(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    project = Path(__file__).parents[1]
    shutil.copytree(project / 'templates', tmp_path / 'templates')
    for name in ('data', 'outputs', 'logs'): (tmp_path / name).mkdir()
    settings.raw['document']['reference_path'] = str(tmp_path / settings.raw['document']['template_path'])
    settings = Settings(tmp_path, settings.raw)
    monkeypatch.setattr('sqmy.maintenance.shutil.which', lambda *a: None)
    result = preflight(settings, stage='scan-prepare')
    assert result['ready'] is True and result['model_calls'] == 0
    assert next(c for c in result['checks'] if c['name'] == 'token_policy')['detail'] == 'observe_only'
    assert not preflight(settings, stage='pre_research')['ready']


def test_standalone_over_reference_saves_without_budget_approval_request(tmp_path, monkeypatch):
    from test_discovery import FakeRouter
    settings = settings_for(tmp_path)
    discovery = LiveDiscovery(settings)
    router = FakeRouter({'selections': []}, input_tokens=2_000_000, output_tokens=100)
    monkeypatch.setattr('sqmy.discovery.build_router', lambda *a, **k: router)
    run, _ = discovery.run(Path(__file__).parent / 'fixtures/monday_observability.json')
    checkpoint = json.loads(discovery.wf.status(run, include_all=True)[0]['checkpoint_json'])
    assert checkpoint['budget_overrun']['enforced'] is False
    assert 'budget_action_required' not in checkpoint and router.calls == 1
