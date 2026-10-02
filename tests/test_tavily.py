from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import pytest

from sqmy.collector import SourceCollector
from sqmy.config import Settings
from sqmy.db import Database
from sqmy.discovery import LiveDiscovery
from sqmy.materials import material_notes, mark_reposts, select_excerpt
from sqmy.models import EventItem
from sqmy.retrieval import collect_discovery, discovery_queries, execute_retrieval
from sqmy.tavily import TavilyClient, TavilyError, retrieval_usage, validate_config
from sqmy.workflow import Workflow


@pytest.fixture
def context(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    (tmp_path / "config").mkdir()
    shutil.copy(root / "config/sources.toml", tmp_path / "config/sources.toml")
    settings = Settings(tmp_path, deepcopy(Settings.load(root / "config/settings.toml").raw))
    # 保留密钥模式旧契约测试；发送始终被fixture或各测试stub。
    settings.raw["search"].update(enabled=False, allow_paid=True)
    wf = Workflow(settings)
    run = wf.init_run("diagnostic")
    monkeypatch.setenv("TAVILY_API_KEY", "offline-dummy-key")
    return settings, wf, run


def response(credits=2, **extra):
    return dict(results=[dict(title="患者重复检查费用负担调查", url="https://www.cnr.cn/test", content="待核实线索", published_date="2026-09-05")],
                usage={"credits": credits}, **extra)


def search(client, query="重复检查", **kwargs):
    return client.search(query, start_date="2026-06-08", end_date="2026-09-06", **kwargs)


def test_reserve_before_send_and_resume_reuses_even_without_key(context, monkeypatch):
    settings, wf, run = context
    calls = []
    def send(self, endpoint, payload):
        with wf.db.connect() as conn:
            row = conn.execute("SELECT * FROM retrieval_calls").fetchone()
        assert row["status"] == "running"
        assert row["accounted_credits"] == 2
        assert row["cost_equivalent_usd"] == 0.016
        assert payload["include_answer"] is False and payload["include_raw_content"] is False
        assert payload["auto_parameters"] is False and payload["max_results"] == 5
        calls.append(payload)
        return response()
    monkeypatch.setattr(TavilyClient, "_post", send)
    client = TavilyClient(settings, wf.db, run, "diagnostic")
    first = search(client)
    monkeypatch.delenv("TAVILY_API_KEY")
    assert search(client)["reused"]
    assert first["results"][0]["published_at"] == "2026-09-05"
    assert len(calls) == 1
    assert retrieval_usage(wf.db, run)["actions"][0]["accounted_credits"] == 2


def test_cross_run_cache_is_zero_credit_and_model_usage_untouched(context, monkeypatch):
    settings, wf, run = context
    monkeypatch.setattr(TavilyClient, "_post", lambda *args: response())
    search(TavilyClient(settings, wf.db, run, "diagnostic"))
    second = wf.init_run("diagnostic")
    result = search(TavilyClient(settings, wf.db, second, "diagnostic"))
    assert result["reused"]
    assert retrieval_usage(wf.db, second)["actions"][0]["accounted_credits"] == 0
    with wf.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert conn.execute("SELECT SUM(token_used) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("field,limit", [("action_credit_limit", 1.0), ("action_cost_limit_usd", 0.01)])
def test_credit_and_money_stop_before_request(context, monkeypatch, field, limit):
    settings, wf, run = context
    settings.raw["tavily"][field] = limit
    invoked = []
    monkeypatch.setattr(TavilyClient, "_post", lambda *args: invoked.append(True))
    with pytest.raises(TavilyError, match="action_credit_or_cost_limit"):
        search(TavilyClient(settings, wf.db, run, "diagnostic"))
    assert not invoked
    assert retrieval_usage(wf.db, run)["actions"] == []


def test_four_queries_limit_includes_retries(context, monkeypatch):
    settings, wf, run = context
    monkeypatch.setattr(TavilyClient, "_post", lambda *args: response())
    client = TavilyClient(settings, wf.db, run, "diagnostic")
    for i in range(4):
        search(client, f"场景{i}")
    with pytest.raises(TavilyError, match="action_request_limit"):
        search(client, "第五种场景")


def test_failed_unknown_preserved_and_no_automatic_retry(context, monkeypatch):
    settings, wf, run = context
    def fail(*args):
        raise OSError("connection failed with offline-dummy-key")
    monkeypatch.setattr(TavilyClient, "_post", fail)
    client = TavilyClient(settings, wf.db, run, "diagnostic")
    with pytest.raises(TavilyError, match="OSError"):
        search(client)
    with pytest.raises(TavilyError):
        search(client)
    assert retrieval_usage(wf.db, run)["actions"][0]["requests"] == 1
    with pytest.raises(TavilyError):
        search(client, retry=True)
    with pytest.raises(TavilyError):
        search(client, retry=True)
    usage = retrieval_usage(wf.db, run)["actions"][0]
    assert usage["requests"] == 2 and usage["accounted_credits"] == 4
    audit = (settings.root / "data/runs" / run / "retrieval_calls.json").read_text()
    assert "offline-dummy-key" not in audit and "connection failed" not in audit


def test_policy_search_has_no_news_date_window(context, monkeypatch):
    settings, wf, run = context
    payloads = []
    def send(self, endpoint, payload):
        payloads.append(payload)
        return response()
    monkeypatch.setattr(TavilyClient, "_post", send)
    client = TavilyClient(settings, wf.db, run, "diagnostic")
    client.search("检查检验互认 现行办法 适用范围", purpose="policy", start_date="2026-01-01", end_date="2026-09-06")
    assert payloads[0]["topic"] == "general"
    assert "start_date" not in payloads[0] and "end_date" not in payloads[0]


def test_extract_preserves_target_context_and_fractional_charge(context, monkeypatch):
    settings, wf, run = context
    url = "https://example.gov.cn/policy"
    body = "无关导航" * 10000 + "\n第四十五条：转移个人信息应当符合规定条件，但是法律另有规定的除外。\n本规定2026年修订，适用于指定对象。"
    monkeypatch.setattr(TavilyClient, "_post", lambda *args: {"results": [{"url": url, "raw_content": body}], "usage": {"credits": 0}})
    result = TavilyClient(settings, wf.db, run, "diagnostic").extract(url, ["第四十五条"], reason="http_incomplete")
    evidence = result["results"][0]
    text = "".join(item["text"] for item in evidence["passages"])
    assert "第四十五条" in text and "另有规定的除外" in text
    assert evidence["target_found"] and evidence["needs_review"]
    assert len(text) <= settings.raw["tavily"]["excerpt_chars"]
    assert retrieval_usage(wf.db, run)["actions"][0]["accounted_credits"] == 0.4
    with wf.db.connect() as conn:
        stored = conn.execute("SELECT result_json FROM retrieval_calls").fetchone()[0]
    assert "无关导航" * 1000 not in stored


def test_extract_failed_and_missing_target_not_success_evidence(context, monkeypatch):
    settings, wf, run = context
    cfg = settings.raw["tavily"]
    assert not select_excerpt("仅有公开征求意见通知，不含附件", ["电费"], cfg)["target_found"]
    result = select_excerpt("版本2025年\n版本2026年\n目标条款", ["目标条款"], cfg)
    assert any("多个版本" in warning for warning in result["warnings"])
    monkeypatch.setattr(TavilyClient, "_post", lambda *args: {"results": [], "failed_results": [{"error": "refused"}], "usage": {"credits": 0}})
    with pytest.raises(TavilyError, match="extract_unavailable"):
        TavilyClient(settings, wf.db, run, "diagnostic").extract("https://example.gov.cn/policy", ["内容"], reason="http_failed")


def test_plan_interrupt_resume_does_not_repeat_finished_search(context, monkeypatch):
    settings, wf, run = context
    plan = {"stage": "diagnostic", "queries": [{"purpose": "policy", "query": "现行规则"}, {"purpose": "policy", "query": "另一规则"}]}
    seen = []
    def send(self, endpoint, payload):
        seen.append(payload["query"])
        if len(seen) == 2:
            raise KeyboardInterrupt()
        return response()
    monkeypatch.setattr(TavilyClient, "_post", send)
    with pytest.raises(KeyboardInterrupt):
        execute_retrieval(settings, wf.db, run, plan)
    state = json.loads((settings.root / "data/runs" / run / "retrieval-diagnostic.json").read_text())
    assert state["status"] == "needs_review" and len(state["results"]) == 1
    result = execute_retrieval(settings, wf.db, run, plan)
    assert result["status"] == "needs_review" and len(seen) == 2
    result = execute_retrieval(settings, wf.db, run, plan, retry_failed=True)
    assert result["status"] == "completed" and seen == ["现行规则", "另一规则", "另一规则"]
    assert retrieval_usage(wf.db, run)["actions"][0]["accounted_credits"] == 6
    assert execute_retrieval(settings, wf.db, run, plan)["reused"]
    changed = deepcopy(plan)
    changed["queries"][0]["query"] = "扩大新议题"
    with pytest.raises(ValueError, match="已固定"):
        execute_retrieval(settings, wf.db, run, changed)


def test_no_plan_secrets_or_private_urls_saved(context, monkeypatch):
    settings, wf, run = context
    plans = [
        {"stage": "diagnostic", "queries": [{"purpose": "policy", "query": "offline-dummy-key"}]},
        {"stage": "diagnostic", "pages": [{"url": "http://127.0.0.1/", "terms": ["目标"], "reason": "http_failed"}]},
        {"stage": "diagnostic", "pages": [{"url": "https://example.gov.cn/?token=secret", "terms": ["目标"], "reason": "http_failed"}]},
    ]
    for plan in plans:
        with pytest.raises(ValueError):
            execute_retrieval(settings, wf.db, run, plan)
    assert not list((settings.root / "data/runs").glob("*/retrieval-*.json"))


def test_selected_and_pre_research_gates_are_not_bypassed(context):
    settings, wf, run = context
    live_run = wf.init_run("live")
    plan = {"stage": "pre_research", "candidate_id": "C1", "queries": [{"purpose": "policy", "query": "现行机制"}]}
    with pytest.raises(ValueError, match="人工选定"):
        execute_retrieval(settings, wf.db, live_run, plan)
    wf.scan(live_run)
    wf.select(live_run, ["C1"])
    plan["stage"] = "research"
    with pytest.raises(ValueError, match="预研闸门"):
        execute_retrieval(settings, wf.db, live_run, plan)


def test_material_hints_do_not_confuse_dates_or_call_reprints_independent():
    notes = material_notes("某机构培训贷退费课程报名", "来源：工人日报", "", updated_at="2026-09-06")
    assert notes["date_status"] == "needs_review" and notes["published_at"] == "" and notes["event_at"] == ""
    assert notes["promotion_suspected"]
    assert material_notes("现行管理条例", "", "2021-01-01", purpose="policy")["role_hint"] == "policy_reference"
    a = EventItem("a", "a", "s", 2, "培训贷退费困境调查", "https://a.test", "", "来源：工人日报", "全国", material=notes.copy())
    b = EventItem("b", "b", "s", 2, "培训贷退费困境调查追踪", "https://b.test", "", "来源：工人日报", "全国", material=notes.copy())
    mark_reposts([a, b])
    assert a.material["repost_group_hint"] == b.material["repost_group_hint"] != ""
    assert a.material["origin_status"] == "unverified"


def test_missing_date_clue_is_pending_not_invented(context):
    settings, wf, run = context
    file = settings.root / "clues.jsonl"
    file.write_text(json.dumps(dict(title="老年人医保报销反复提交材料投诉", url="https://forum.example.test/1", published_at="", summary="具体困难尚需官方核验", source_name="公开论坛", source_level=3), ensure_ascii=False))
    events, _, _ = SourceCollector(settings.root, settings.raw)._items_from_clue_file(run, file)
    assert len(events) == 1 and not events[0].published_at
    assert events[0].material["date_status"] == "needs_review"


def test_supplement_collects_once_freezes_input_and_skips_old_dates(context, monkeypatch):
    settings, wf, run = context
    collector = SourceCollector(settings.root, settings.raw)
    count = []
    monkeypatch.setattr(collector, "collect", lambda *args, **kwargs: count.append("rss") or [])
    def send(*args):
        count.append("tavily")
        data = response()
        data["results"] += [dict(title="旧闻", url="https://example.test/old", content="旧案", published_date="2020-01-01"), dict(title="无日期的办事困难", url="https://example.test/no-date", content="待核")]
        return data
    monkeypatch.setattr(TavilyClient, "_post", send)
    first = collect_discovery(collector, settings, wf.db, run)
    assert count.count("tavily") == 4 and count.count("rss") == 1
    assert not any(event.title == "旧闻" for event in first)
    assert any(event.material["date_status"] == "needs_review" for event in first)
    assert len({event.source_id for event in first if event.url == "https://www.cnr.cn/test"}) == 1
    assert collect_discovery(collector, settings, wf.db, run) == first
    assert count.count("tavily") == 4 and count.count("rss") == 1


def test_supplement_quota_failure_keeps_primary_and_stops_calls(context, monkeypatch):
    settings, wf, run = context
    collector = SourceCollector(settings.root, settings.raw)
    event = EventItem("a", "s", "s", 1, "医保困难", "https://example.gov.cn", "", "具体问题", "全国")
    monkeypatch.setattr(collector, "collect", lambda *args, **kwargs: [event])
    def fail(*args):
        raise TavilyError("http_429", "paused_quota")
    monkeypatch.setattr(TavilyClient, "_post", fail)
    assert len(collect_discovery(collector, settings, wf.db, run)) == 1
    assert retrieval_usage(wf.db, run)["actions"][0]["requests"] == 1


def test_fixture_mock_and_missing_key_do_not_call_supplement(context, monkeypatch):
    settings, wf, run = context
    collector = SourceCollector(settings.root, settings.raw)
    monkeypatch.setattr(collector, "collect", lambda *args, **kwargs: [])
    def unexpected(*args, **kwargs):
        pytest.fail("不应调用Tavily")
    monkeypatch.setattr(TavilyClient, "search", unexpected)
    assert collect_discovery(collector, settings, wf.db, run, fixture=Path("fixture")) == []
    settings.raw["model"]["provider"] = "mock"
    assert collect_discovery(collector, settings, wf.db, run) == []
    settings.raw["model"]["provider"] = "auto"
    monkeypatch.delenv("TAVILY_API_KEY")
    assert collect_discovery(collector, settings, wf.db, run) == []


def test_new_material_persists_and_forward_migration_is_idempotent(context):
    settings, wf, run = context
    event = EventItem("a", "s", "s", 1, "标题", "https://example.gov.cn", "", "摘要", "全国", material={"date_status": "needs_review"})
    LiveDiscovery(settings)._persist_events(run, [event])
    wf.db.initialize()
    wf.db.initialize()
    with wf.db.connect() as conn:
        assert json.loads(conn.execute("SELECT material_json FROM event_items").fetchone()[0]) == event.material
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_gap_queries_rotate_and_config_validation(context):
    settings, _, _ = context
    cfg = settings.raw["tavily"]
    day = datetime.now(timezone.utc).date()
    scenarios = discovery_queries([], cfg, day)
    assert len(scenarios) == 4
    event = EventItem("a", "s", "s", 2, scenarios[0]["signals"][0], "https://example.test", "", "", "全国")
    assert discovery_queries([event], cfg, day)[0]["id"] != scenarios[0]["id"]
    for value in (-1, float("nan"), True):
        bad = dict(cfg, action_credit_limit=value)
        with pytest.raises(ValueError):
            validate_config(bad)


def test_retrieve_cli_completes_and_reuses_without_real_model(context, monkeypatch, capsys):
    from sqmy.cli import main
    settings, wf, run = context
    monkeypatch.setattr("sqmy.cli.Settings.load", lambda *args: settings)
    monkeypatch.setattr(TavilyClient, "_post", lambda *args: response())
    plan = settings.root / "plan.json"
    plan.write_text(json.dumps({"stage": "diagnostic", "queries": [{"purpose": "policy", "query": "现行规定"}]}))
    assert main(["retrieve", "--plan", str(plan), "--run-id", run]) == 0
    assert main(["retrieve", "--plan", str(plan), "--run-id", run]) == 0
    assert main(["retrieval-usage", run]) == 0
    assert "completed" in capsys.readouterr().out
    assert retrieval_usage(wf.db, run)["actions"][0]["requests"] == 1
    with wf.db.connect() as conn:
        assert conn.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0] == "completed"


def test_legacy_event_metadata_migration_preserves_rows(context):
    settings, wf, run = context
    event = EventItem("a", "s", "s", 1, "原始标题", "https://example.gov.cn", "", "原始摘要", "全国")
    LiveDiscovery(settings)._persist_events(run, [event])
    with wf.db.connect() as conn:
        conn.execute("ALTER TABLE event_items DROP COLUMN material_json")
        before = tuple(conn.execute("SELECT * FROM event_items").fetchone())
    wf.db.initialize()
    wf.db.initialize()
    with wf.db.connect() as conn:
        after = conn.execute("SELECT * FROM event_items").fetchone()
        assert tuple(after)[:-1] == before and after["material_json"] == "{}"


def test_retrieval_plan_lock_prevents_parallel_duplicate_calls(context):
    import fcntl
    settings, wf, run = context
    directory = settings.root / "data/runs" / run
    directory.mkdir(parents=True)
    with (directory / ".retrieval-plan.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="已有有界检索"):
            execute_retrieval(settings, wf.db, run, {"stage": "diagnostic", "queries": [{"purpose": "policy", "query": "问题"}]})


def test_hard_interruption_is_marked_unknown_without_releasing_credits(context, monkeypatch):
    settings, wf, run = context
    monkeypatch.setattr(TavilyClient, "_post", lambda *args: response())
    client = TavilyClient(settings, wf.db, run, "diagnostic")
    search(client)
    with wf.db.connect() as conn:
        conn.execute("UPDATE retrieval_calls SET status='running',reported_credits=NULL,result_json=NULL,accounting_method='reserved_estimate'")
    with pytest.raises(TavilyError, match="interrupted_unknown"):
        search(client)
    with wf.db.connect() as conn:
        row = conn.execute("SELECT * FROM retrieval_calls").fetchone()
    assert row["status"] == "needs_review" and row["accounted_credits"] == 2
    assert row["error_code"] == "interrupted_unknown"


def test_unexpected_actual_overrun_keeps_result_and_blocks_followup(context, monkeypatch):
    settings, wf, run = context
    monkeypatch.setattr(TavilyClient, "_post", lambda *args: response(credits=11))
    client = TavilyClient(settings, wf.db, run, "diagnostic")
    result = search(client)
    assert result["results"] and result["budget_overrun"]["accounted_credits"] == 11
    assert search(client)["reused"]
    with pytest.raises(TavilyError, match="action_credit_or_cost_limit"):
        search(client, "另一问题")


def test_excerpt_windows_merge_and_keep_exception_near_later_target(context):
    settings, _, _ = context
    text = "导航" * 100 + "电费" * 20 + "规则背景" * 300 + "电费应核对抄表天数，但是换表过户时例外。" + "条款适用条件。" * 50
    result = select_excerpt(text, ["电费"], settings.raw["tavily"])
    passages = result["passages"]
    assert all(a["end"] < b["start"] for a, b in zip(passages, passages[1:]))
    assert sum(len(p["text"]) for p in passages) <= len(text)
    assert "换表过户时例外" in "".join(p["text"] for p in passages)


def test_scan_entry_uses_supplement_funnel_without_widening_model_pool(context, monkeypatch):
    settings, wf, _ = context
    root = Path(__file__).parents[1]
    shutil.copy(root / "config/policy_mechanisms.toml", settings.root / "config/policy_mechanisms.toml")
    settings.raw["shadow_verification"]["enabled"] = False
    settings.raw["budget"]["screening_tokens"] = 1
    monkeypatch.setattr(SourceCollector, "collect", lambda *args, **kwargs: [])
    def send(*args):
        result = response()
        result["results"][0]["title"] = "医院检查检验互认重复检查费用负担调查"
        return result
    monkeypatch.setattr(TavilyClient, "_post", send)
    run, candidates = LiveDiscovery(settings.with_model('offline-test-model')).run()
    assert candidates == []
    with wf.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run,)).fetchone()[0] == 0
        funnel = conn.execute("SELECT * FROM source_funnel WHERE run_id=? AND source_id='tavily_cnr.cn'", (run,)).fetchone()
        assert funnel["raw_item_count"] == 4 and funnel["model_input_count"] == 0
        assert conn.execute("SELECT COUNT(*) FROM retrieval_calls WHERE run_id=?", (run,)).fetchone()[0] == 4
        queued = conn.execute("SELECT event_json FROM discovery_queue").fetchone()
        assert json.loads(queued[0])["material"]["date_status"] == "source_reported_unverified"
