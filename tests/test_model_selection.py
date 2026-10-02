"""显式模型选择的零服务回归；所有数据库和输出均在 tmp_path。"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import subprocess

import pytest

from sqmy import cli
from sqmy.config import Settings
from sqmy.diagnostics import provider_check
from sqmy.discovery import LiveDiscovery
from sqmy.model_calls import CallLedger
from sqmy.models import EventItem
from sqmy.providers import CodexCliClient, ModelResult, ProviderRouter, build_router
from sqmy.semantic_review import semantic_review


@pytest.fixture
def unselected(tmp_path):
    source = Path(__file__).parents[1]
    shutil.copytree(source / 'config', tmp_path / 'config')
    raw = deepcopy(Settings.load(source / 'config/settings.toml').raw)
    raw['model'].update(provider='codex_cli', codex_model='legacy-full', screening_model='legacy-screening')
    raw['budget']['screening_tokens'] = 300_000
    raw['shadow_verification']['enabled'] = False
    return Settings(tmp_path, raw)


@pytest.mark.parametrize('command', [['scan'], ['monday'], ['provider-check'],
                                    ['semantic-review', 'run', 'topic', '--run']])
def test_cli_missing_model_stops_before_credentials_database_and_sources(unselected, monkeypatch, capsys, command):
    monkeypatch.setattr(cli.Settings, 'load', lambda *args: unselected)
    def forbidden(*args, **kwargs):
        pytest.fail('缺选择时不得初始化或读取凭据')
    monkeypatch.setattr(cli, 'Workflow', forbidden)
    monkeypatch.setattr(cli, 'load_dotenv', forbidden)
    with pytest.raises(SystemExit) as stopped:
        cli.main(command)
    assert stopped.value.code == 2
    assert '不能自动读取Codex界面选择' in capsys.readouterr().err
    assert not (unselected.root / 'data').exists()


def test_cli_forwards_exact_choice_without_persisting_config(unselected, monkeypatch):
    seen = []
    monkeypatch.setattr(cli.Settings, 'load', lambda *args: unselected)
    monkeypatch.setattr(cli, 'load_dotenv', lambda: None)
    class Workflow:
        def __init__(self, settings):
            seen.append(settings.selected_model)
        def status(self, *args, **kwargs):
            return [{'status': 'needs_review', 'checkpoint_json': '{}'}]
        def candidate_pool(self):
            return []
    class Discovery:
        def __init__(self, settings):
            seen.append(settings.require_model())
        def run(self, *args, **kwargs):
            return 'offline-run', []
    monkeypatch.setattr(cli, 'Workflow', Workflow)
    monkeypatch.setattr(cli, 'LiveDiscovery', Discovery)
    before = deepcopy(unselected.raw)
    assert cli.main(['--model', 'user-selected-model', 'scan']) == 0
    assert seen == ['user-selected-model', 'user-selected-model']
    assert unselected.raw == before and unselected.selected_model is None


@pytest.mark.parametrize('provider', ['codex_cli', 'auto'])
def test_legacy_config_cannot_supply_a_model(unselected, provider):
    cfg = dict(unselected.section('model'), provider=provider)
    with pytest.raises(ValueError, match='未指定本次模型'):
        build_router(unselected.root, cfg)
    router = build_router(unselected.root, cfg, codex_model='user-selected-model')
    assert router.primary.model == 'user-selected-model'
    assert router.fallback is None and not router.fallback_on


def test_codex_command_contains_exact_model_and_no_forced_effort(unselected):
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        Path(command[command.index('--output-last-message') + 1]).write_text('{"ok": true}')
        return subprocess.CompletedProcess(command, 0, '{"usage":{"input_tokens":2,"output_tokens":1}}', '')
    client = CodexCliClient(unselected.root, 'user-selected-model', runner=runner, timeout_seconds=5)
    result = client.analyze('offline metadata', {'type': 'object'})
    command = commands[0]
    assert command[command.index('--model') + 1] == result.model == 'user-selected-model'
    assert '--ignore-user-config' in command
    assert '--config' not in command
    assert not any('model_reasoning_effort' in value for value in command)
    with pytest.raises(ValueError, match='未指定本次模型'):
        CodexCliClient(unselected.root, runner=runner, timeout_seconds=5)
    assert len(commands) == 1


def test_other_dispatch_entries_stop_before_database(unselected, monkeypatch):
    monkeypatch.setattr('sqmy.diagnostics.Workflow', lambda *args: pytest.fail('不得初始化诊断'))
    monkeypatch.setattr('sqmy.semantic_review.Database', lambda *args: pytest.fail('不得初始化语义复核'))
    for operation in (lambda: provider_check(unselected),
                      lambda: semantic_review(unselected, 'run', 'topic', execute=True)):
        with pytest.raises(ValueError, match='未指定本次模型'):
            operation()
    discovery = object.__new__(LiveDiscovery)
    discovery.s = unselected
    with pytest.raises(ValueError, match='未指定本次模型'):
        discovery.run()
    assert not (unselected.root / 'data').exists()


def test_call_ledger_cannot_reserve_or_dispatch_with_missing_model(unselected):
    ledger = CallLedger(None, unselected, 'run', 'screening', 'hash')
    with pytest.raises(ValueError, match='未指定本次模型'):
        ledger.invoke(SimpleNamespace(provider='codex_cli', model=''), 'prompt', {})


def test_diagnostic_forwards_and_records_explicit_model(unselected, monkeypatch):
    class Client:
        provider = 'codex_cli'
        model = 'user-selected-model'
        def analyze(self, prompt, schema):
            return ModelResult({'ok': True}, 2, 1, 'fake', self.provider, self.model)
    def router(root, config, **kwargs):
        assert kwargs['codex_model'] == 'user-selected-model'
        return ProviderRouter(Client(), None, [])
    monkeypatch.setattr('sqmy.diagnostics.build_router', router)
    report = provider_check(unselected.with_model('user-selected-model'))
    assert report['selected_model'] == report['model'] == 'user-selected-model'
    from sqmy.db import Database
    with Database(unselected.database_path).connect() as conn:
        assert conn.execute('SELECT model FROM model_calls WHERE run_id=?', (report['run_id'],)).fetchone()[0] == 'user-selected-model'


def test_interactive_estimate_does_not_mislabel_unknown_model(unselected):
    assert unselected.interactive_model == 'unknown'
    assert unselected.with_model('user-selected-model').interactive_model == 'user-selected-model'


def test_settings_file_cannot_smuggle_a_runtime_choice(unselected):
    path = unselected.root / 'config/legacy.toml'
    path.write_text('[model]\nprovider="codex_cli"\ncodex_model="old-full"\nscreening_model="old-screen"\nselected_model="pretend-runtime"\n')
    settings = Settings.load(path)
    assert settings.selected_model is None
    with pytest.raises(ValueError, match='未指定本次模型'):
        settings.require_model()


def test_preflight_reports_missing_choice_without_guessing(unselected):
    from sqmy.maintenance import preflight
    def check(settings, stage):
        return next(c for c in preflight(settings, stage=stage)['checks'] if c['name'] == 'selected_model')
    missing = check(unselected, 'scan')
    assert not missing['ok'] and missing['blocking']
    assert check(unselected.with_model('user-selected-model'), 'scan')['ok']
    assert not check(unselected, 'refresh')['blocking']


def test_screening_ignores_legacy_frozen_models_and_records_current_argument(unselected, monkeypatch):
    selected = unselected.with_model('user-selected-model')
    discovery = LiveDiscovery(selected)
    run = discovery.wf.init_run('test_fixture')
    event = EventItem(id='event', source_id='s', source_name='调查', source_level=2,
                      title='办理材料退回调查', url='https://example.test/event', published_at='2026-10-01', summary='材料已退回', region='北京')
    observed = []
    class Client:
        provider = 'codex_cli'
        model = 'user-selected-model'
        def analyze(self, prompt, schema):
            return ModelResult({'selections': []}, 2, 1, 'fake', self.provider, self.model)
    def router(root, config, **kwargs):
        observed.append(kwargs['codex_model'])
        return ProviderRouter(Client(), None, [])
    monkeypatch.setattr('sqmy.discovery.build_router', router)
    discovery._model_rank(run, [event])
    path = selected.root / 'data/runs' / run / 'screening_materials.json'
    material = json.loads(path.read_text())
    assert material['selected_model'] == 'user-selected-model'
    # 模拟升级前冻结记录：只有旧固定字段，不能覆盖本次显式传入值。
    material.pop('selected_model')
    material['model_config'].update(screening_model='legacy-screening', codex_model='legacy-full')
    path.write_text(json.dumps(material, ensure_ascii=False, indent=2))
    with discovery.wf.db.connect() as conn:
        conn.execute("DELETE FROM tasks WHERE run_id=? AND kind='model_screening'", (run,))
    discovery._model_rank(run, [event])
    assert observed == ['user-selected-model', 'user-selected-model']
    with discovery.wf.db.connect() as conn:
        assert [row[0] for row in conn.execute('SELECT model FROM model_calls WHERE run_id=?', (run,))] == observed


def test_new_frozen_selection_allows_cache_but_refuses_changed_model_dispatch(unselected, monkeypatch):
    from test_discovery import FakeRouter
    discovery = LiveDiscovery(unselected.with_model('first-model'))
    run = discovery.wf.init_run('test_fixture')
    event = EventItem(id='event', source_id='s', source_name='调查', source_level=2,
                      title='办理退回调查', url='https://example.test/event', published_at='2026-10-01', summary='已退回', region='北京')
    router = FakeRouter({'selections': []})
    monkeypatch.setattr('sqmy.discovery.build_router', lambda *a, **k: router)
    discovery._model_rank(run, [event])
    discovery.s = discovery.s.with_model('second-model')
    _, cached, _ = discovery._model_rank(run, [event])
    assert cached and router.calls == 1
    with discovery.wf.db.connect() as conn:
        conn.execute("DELETE FROM tasks WHERE run_id=? AND kind='model_screening'", (run,))
    with pytest.raises(ValueError, match='模型与冻结初筛选择不同'):
        discovery._model_rank(run, [event])
    assert router.calls == 1
