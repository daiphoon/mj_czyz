from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest

from sqmy import cli, materials
from sqmy.config import Settings
from sqmy.discovery import LiveDiscovery
from sqmy.models import Candidate, EventItem
from test_discovery import FakeRouter


ROOT = Path(__file__).parents[1]


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "config").mkdir()
    for name in ("settings.toml", "sources.toml", "policy_mechanisms.toml"):
        shutil.copy(ROOT / "config" / name, tmp_path / "config" / name)
    return Settings.load(tmp_path / "config/settings.toml")


@pytest.mark.parametrize("marker", ["可核验缺口是：", "待核假设：", "原因推测：", "缺口假设（待核）："])
def test_explicit_assumption_boundary_preserves_both_parts(marker):
    text = "报道称投诉后已结算，双方说法有异。" + marker + "事前告知机制不足。"
    parts = materials.split_discovery_summary(text)
    assert parts["reported_excerpt"] == "报道称投诉后已结算，双方说法有异。"
    assert parts["upstream_hypotheses"] == marker + "事前告知机制不足。"
    assert "".join(parts.values()) == text


@pytest.mark.parametrize("text", [
    "公开调查称存在政策执行问题，但未列出办理材料。",
    "报道将其称为‘待核假设：机制不足’，仍应核对原文。",
])
def test_unmarked_or_quoted_claims_are_not_promoted_to_verified_facts(text):
    assert materials.split_discovery_summary(text) == {
        "reported_excerpt": text, "upstream_hypotheses": "",
    }


@pytest.mark.parametrize("command", ["scan", "monday"])
def test_normal_cli_loads_fact_first_input_without_extra_calls(settings, monkeypatch, command):
    fixture = json.loads((ROOT / "tests/fixtures/monday_observability.json").read_text())
    fixture[0]["xml"] = fixture[0]["xml"].replace(
        "公开数据反映养老服务存在制度执行问题和群体负担。",
        "报道称申请曾被退回，投诉后办结。待核假设：跨部门衔接存在缺口。",
    )
    fixture_path = settings.root / "fixture.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False))
    router = FakeRouter({"selections": []})
    monkeypatch.setattr("sqmy.discovery.build_router", lambda *a, **k: router)
    assert cli.main(["--config", str(settings.root / "config/settings.toml"), command,
                     "--fixture", str(fixture_path)]) == 0
    assert router.calls == 1
    payload = json.loads(router.prompt.rsplit("\n", 1)[1])
    item = next(x for x in payload if x["upstream_hypotheses"])
    assert "投诉后办结" in item["reported_excerpt"]
    assert item["upstream_hypotheses"] == "待核假设：跨部门衔接存在缺口。"
    assert "summary" not in item
    for instruction in ("不等于已验证事实", "政策衔接", "前瞻性风险", "决定性未知", "suggested_title"):
        assert instruction in router.prompt
    audit_path = next((settings.root / "data/runs").glob("*/screening_materials.json"))
    audit = json.loads(audit_path.read_text())
    assert audit["input_contract"] == "fact_first_v1"
    assert audit["model_input"] == payload
    frozen = json.loads((audit_path.parent / "scan_input.json").read_text())
    original = next(x for x in frozen["model_pool"] if x["id"] == item["id"])
    assert original["summary"] == item["reported_excerpt"] + item["upstream_hypotheses"]
    with LiveDiscovery(settings).wf.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_reviews").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM stage_usage").fetchone()[0] == 0


def test_bounded_model_view_does_not_mutate_event_or_repeat_call(settings, monkeypatch):
    discovery = LiveDiscovery(settings)
    run_id = discovery.wf.init_run("test_fixture")
    event = EventItem(id="case", source_id="s", source_name="s", source_level=2,
                      title="结算争议", url="https://example.test/case", region="全国",
                      published_at="2026-09-07", summary="报道陈述。待核假设：" + "未知" * 300)
    original = deepcopy(event)
    router = FakeRouter({"selections": []})
    monkeypatch.setattr("sqmy.discovery.build_router", lambda *a, **k: router)
    discovery._model_rank(run_id, [event])
    item = json.loads(router.prompt.rsplit("\n", 1)[1])[0]
    assert item["reported_excerpt"] + item["upstream_hypotheses"] == original.summary[:350]
    assert event == original
    settings.raw["budget"]["screening_tokens"] = 0
    assert discovery._model_rank(run_id, [event])[1] is True
    assert router.calls == 1


def test_legacy_resume_keeps_original_prompt_and_cache(settings, monkeypatch):
    discovery = LiveDiscovery(settings)
    run_id = discovery.wf.init_run("test_fixture")
    path = settings.root / "data/runs" / run_id / "screening_materials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"model_event_ids": ["case"]}')
    event = EventItem(id="case", source_id="s", source_name="s", source_level=2,
                      title="结算争议", url="https://example.test/case", region="全国",
                      published_at="2026-09-07", summary="报道陈述。待核假设：原因未明。")
    router = FakeRouter({"selections": []})
    monkeypatch.setattr("sqmy.discovery.build_router", lambda *a, **k: router)
    discovery._model_rank(run_id, [event])
    first_prompt = router.prompt
    item = json.loads(first_prompt.rsplit("\n", 1)[1])[0]
    assert item["summary"] == event.summary
    assert "reported_excerpt" not in item
    assert json.loads(path.read_text())["input_contract"] == "legacy"
    settings.raw["budget"]["screening_tokens"] = 0
    assert discovery._model_rank(run_id, [event])[1] is True
    assert router.calls == 1
    assert router.prompt == first_prompt


def test_report_preserves_dispute_and_hypothesis_without_approving(settings):
    discovery = LiveDiscovery(settings)
    candidate = Candidate(
        id="C1", title="结算争议", summary="材料称投诉后办结。可核验缺口是：事前约束不足。",
        event_date="2026-09-07", region="全国", affected_group="短期工作人员",
        institutional_conflict="待核", pain_point="费用争议", policy_gap="待核",
        policy_entry="待核", authority="待核", data_sufficiency="需查处理结论",
        policy_window="待核", history_relation="待核", priority="中", risk="未回源",
        recommendation="值得核对事实", score=60, gap_hypothesis="事前约束可能不足",
    )
    original = deepcopy(candidate)
    path = discovery._report("fixture-report", [candidate], [], 1, 1, 1, 0, False, 0,
                             0, "fixture", 1, 4)
    report = path.read_text()
    assert "发现材料陈述（未回源，不等于已核事实）：材料称投诉后办结。" in report
    assert "上游缺口/原因设想（待核）：可核验缺口是：事前约束不足。" in report
    assert "缺口假设（待核）：事前约束可能不足" in report
    assert "核验投入与限制：待填写" in report
    assert "人工研究入口复核（待填写）" in report
    assert candidate == original
    assert discovery.wf.status("fixture-report") == []
