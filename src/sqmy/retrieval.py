"""检索用途分离：发现补少量场景，已选题按固定问题单查政策和关键页面。"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import fcntl
import json
import re
from urllib.parse import urlparse

from .collector import canonical_url, infer_event_region, infer_source_level, parse_date
from .db import now
from .materials import material_notes, mark_reposts, split_discovery_summary
from .models import EventItem
from .snapshots import _public_http_url
from .tavily import TavilyClient, TavilyError, atomic_json, retrieval_usage
from .discovery_coverage import provenance, coverage_summary


def _parameters(cfg):
    return {key: cfg[key] for key in ("search_depth", "extract_depth", "max_results_per_query", "summary_chars", "excerpt_chars", "context_chars")}


def discovery_queries(events, cfg, day):
    scenarios = cfg.get("scenario_queries", [])
    if not scenarios:
        return []
    offset = day.toordinal() % len(scenarios)
    rotated = scenarios[offset:] + scenarios[:offset]
    def count(scenario):
        return sum(any(term in event.title + split_discovery_summary(event.summary)['reported_excerpt']
                       for term in scenario["signals"]) for event in events)
    # 只补当前结果较少的具体场景；配置规模不改变模型输入池。
    ordered = sorted(rotated, key=count)
    roles = cfg.get('discovery_roles', [])
    if not roles:
        return ordered[:cfg['max_searches_per_action']]
    selected, seen = [], set()
    for scenario in ordered:
        role = scenario.get('role')
        if role in roles and role not in seen:
            selected.append(scenario)
            seen.add(role)
    return selected[:cfg['max_searches_per_action']]


def collect_discovery(collector, settings, db, run_id, *, fixture=None, clue_file=None):
    cfg = settings.raw.get("tavily", {})
    if fixture or not cfg.get("enabled", False) or settings.section("model")["provider"] == "mock":
        events = collector.collect(run_id, fixture=fixture, clue_file=clue_file)
        atomic_json(settings.root / 'data/runs' / run_id / 'discovery_coverage.json', dict(
            run_id=run_id, **coverage_summary(events, collector.collection_stats)))
        return events
    directory = settings.root / "data/runs" / run_id
    path = directory / "collection_checkpoint.json"
    if settings.raw.get("search", {}).get("enabled", False) and (
        not path.exists() or json.loads(path.read_text()).get("contract") == "retrieval_pipeline_v1"
    ):
        from .retrieval_pipeline import collect_routed
        return collect_routed(collector, settings, db, run_id, clue_file=clue_file)
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("finished"):
            collector.collection_stats = state["stats"]
            events = [EventItem(**item) for item in state["events"]]
            atomic_json(directory / 'discovery_coverage.json', dict(
                run_id=run_id, **coverage_summary(events, state['stats'])))
            return events
        if state["parameters"] != _parameters(cfg):
            raise ValueError("本次发现输入已经固定；恢复前请还原检索参数，不混入新输入")
    else:
        primary = collector.collect(run_id, clue_file=clue_file)
        day = datetime.now(timezone.utc).date()
        state = {"events": [asdict(event) for event in primary], "stats": collector.collection_stats,
                 "queries": discovery_queries(primary, cfg, day), "processed_queries": [],
                 "start_date": (day - timedelta(days=settings.section("discovery")["lookback_days"])).isoformat(),
                 "end_date": day.isoformat(), "parameters": _parameters(cfg), "finished": False}
        atomic_json(path, state)
    client = TavilyClient(settings, db, run_id, "discovery")
    if client.available:
        for query in state["queries"]:
            if query["id"] in state["processed_queries"]:
                continue
            source_id = "tavily_" + query["id"]
            stat = {"source_id": source_id, "source_name": "Tavily场景补充·" + query["id"], "expansion_tier": 4,
                    "fetched_count": 0, "within_window_count": 0, "collected_count": 0,
                    "invalid_metadata_count": 0, "outside_window_count": 0, "fetch_error": None, "parse_error": None}
            source_stats = {}
            try:
                response = client.search(query["query"], start_date=state["start_date"], end_date=state["end_date"])
                for item in response["results"]:
                    # 以实际发布域名归并，不把4条查询当成4个独立来源。
                    domain = (urlparse(item["url"]).hostname or "unknown").removeprefix("www.")
                    publisher_id = "tavily_" + domain
                    publisher = source_stats.setdefault(publisher_id, dict(stat, source_id=publisher_id, source_name=domain))
                    publisher["fetched_count"] += 1
                    published = parse_date(item["published_at"])
                    if published and not state["start_date"] <= published.date().isoformat() <= state["end_date"]:
                        publisher["outside_window_count"] += 1
                        continue
                    publisher["within_window_count"] += 1
                    url = canonical_url(item["url"])
                    title, summary = item["title"], item["summary"]
                    region, region_evidence = infer_event_region(title, summary, url, "全国")
                    level = infer_source_level(url, 3)
                    stamp = published.isoformat() if published else ""
                    event = EventItem(id=hashlib.sha256((title + "\n" + url).encode()).hexdigest()[:16],
                                      source_id=publisher_id, source_name=domain,
                                      source_level=level, title=title, url=url, published_at=stamp, summary=summary,
                                      region=region, source_region="全国", region_evidence=region_evidence, expansion_tier=4,
                                      collected_at=now(), material=material_notes(title, summary, stamp, source_level=level, updated_at=item["updated_at"]))
                    event.material["retrieval_call_id"] = response["call_id"]
                    event.material['discovery_provenance'] = provenance(
                        url, channel='tavily', source_id=publisher_id, date_basis='provider_date',
                        query_id=query['id'], query_role=query.get('role'))
                    state["events"].append(asdict(event))
                    publisher["collected_count"] += 1
            except TavilyError as exc:
                stat["fetch_error"] = exc.code
                state["supplement_status"] = exc.status
            state["stats"].extend(source_stats.values() if source_stats else [stat])
            state["processed_queries"].append(query["id"])
            atomic_json(path, state)
            if stat["fetch_error"]:
                # 可选补充失败后停止该通道，已有RSS仍走原流程；不无限换词或换工具。
                break
    else:
        state["supplement_status"] = "skipped_missing_key"
    events = [EventItem(**item) for item in state["events"]]
    mark_reposts(events)
    state.update(finished=True, events=[asdict(item) for item in events])
    collector.collection_stats = state["stats"]
    atomic_json(path, state)
    atomic_json(directory / 'discovery_coverage.json', dict(
        run_id=run_id, **coverage_summary(events, state['stats'])))
    return events


def execute_retrieval(settings, db, run_id, plan, *, retry_failed=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("无效运行ID")
    directory = settings.root / "data/runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".retrieval-plan.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("本运行已有有界检索在执行；结束后用同一问题单恢复") from None
        return _execute_retrieval(settings, db, run_id, plan, retry_failed=retry_failed)


def _execute_retrieval(settings, db, run_id, plan, *, retry_failed=False):
    """人工固定问题单后的有界检索，不进入模型、证据闸门或自动深研。"""
    if not isinstance(plan, dict) or set(plan) - {"stage", "candidate_id", "queries", "pages", "repair"}:
        raise ValueError("问题单只接受stage、candidate_id、queries、pages及有界repair")
    stage, candidate_id = plan.get("stage"), plan.get("candidate_id", "")
    if stage not in {"diagnostic", "pre_research", "research"}:
        raise ValueError("检索阶段只能是diagnostic、pre_research或research")
    with db.connect() as conn:
        context = conn.execute("SELECT rc.mode,r.phase,r.status FROM run_context rc JOIN runs r ON r.id=rc.run_id WHERE rc.run_id=?", (run_id,)).fetchone()
        if not context:
            raise ValueError("未找到运行")
        if stage == "diagnostic":
            if context["mode"] != "diagnostic" or candidate_id:
                raise ValueError("诊断不得写入真实运行")
        else:
            selected = conn.execute("SELECT 1 FROM candidates WHERE run_id=? AND id=? AND selected=1", (run_id, run_id + ":" + candidate_id)).fetchone()
            if context["mode"] != "live" or not selected:
                raise ValueError("只有人工选定的真实候选才能执行预研检索")
            if context["phase"] != "research" or context["status"] in {"completed", "skipped", "failed", "paused_quota"}:
                raise ValueError("本题当前状态不允许启动研究检索")
            if stage == "research":
                review = conn.execute("SELECT human_decision,research_allowed FROM research_reviews WHERE run_id=? AND candidate_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (run_id, candidate_id)).fetchone()
                if not review or review["human_decision"] != "proceed" or not review["research_allowed"]:
                    raise ValueError("正式研究检索须先通过预研闸门和人工确认")
    cfg = settings.raw["tavily"]
    queries, pages = plan.get("queries", []), plan.get("pages", [])
    if not isinstance(queries, list) or not isinstance(pages, list) or not queries and not pages:
        raise ValueError("问题单必须包含搜索或具体页面")
    if len(queries) > cfg["max_searches_per_action"] or len(pages) > cfg["max_extracts_per_action"]:
        raise ValueError("问题单超过单行为检索范围")
    if any(not isinstance(q, dict) or q.get("purpose") not in {"discovery", "policy"} or not isinstance(q.get("query"), str) or not q["query"].strip() for q in queries):
        raise ValueError("每个检索词须明确discovery或policy用途")
    routed = settings.raw.get("search", {}).get("enabled", False)
    if any(not isinstance(page, dict) or not page.get("url") or not page.get("terms") or
           (not routed and page.get("reason") not in {"http_failed", "http_incomplete"}) for page in pages):
        raise ValueError("页面须有URL、目标词、普通HTTP失败或内容不完整的原因")
    action = stage + (":" + candidate_id if candidate_id else "")
    # 检查点按补查轮次分开，调用账本仍使用原action；不增加搜索、提取或积分额度。
    repair_round = _validate_repair(settings, db, run_id, plan, action)
    checkpoint_action = action + (f":repair:{repair_round}" if repair_round else "")
    serialized = json.dumps(plan, ensure_ascii=False, sort_keys=True)
    if routed and re.search(r"tvly-[A-Za-z0-9_-]{12,}|Bearer\s+\S+", serialized, re.I):
        raise ValueError("问题单不得包含凭证")
    for query in queries:
        allowed = {"purpose", "query", "intent", "domains", "exclude_domains"} if routed else {"purpose", "query"}
        if set(query) - allowed or len(query["query"]) > 500:
            raise ValueError("检索词须在500字内；只接受purpose和query")
        if routed:
            from .search_types import RetrievalIntent, SearchRequest, search_type_for_intent
            intent = RetrievalIntent(query.get("intent", "POLICY_SEARCH" if query["purpose"] == "policy" else "HOTSPOT_DISCOVERY"))
            SearchRequest(query["query"], intent=intent, search_type=search_type_for_intent(intent),
                          domains=tuple(query.get("domains", [])), exclude_domains=tuple(query.get("exclude_domains", [])))
    for page in pages:
        if set(page) - {"url", "terms", "reason"} or (not routed and set(page) != {"url", "terms", "reason"}):
            raise ValueError("提取页只接受url、terms、reason")
        _public_http_url(page["url"])
        if not isinstance(page["terms"], list) or not 1 <= len(page["terms"]) <= 8 or any(not isinstance(term, str) or not term.strip() or len(term) > 100 for term in page["terms"]):
            raise ValueError("每页需要1—8个100字以内的目标词")
    path = settings.root / "data/runs" / run_id / ("retrieval-" + checkpoint_action.replace(":", "-") + ".json")
    if routed:
        from .retrieval_pipeline import execute_plan
        return execute_plan(settings, db, run_id, plan, action, checkpoint_action, retry_failed=retry_failed)
    client = TavilyClient(settings, db, run_id, action)
    if client._text(serialized, len(serialized)) != serialized:
        raise ValueError("问题单不得包含凭证")
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["input_hash"] != digest or state["parameters"] != _parameters(cfg):
            raise ValueError("本行为问题单已固定；不可通过改词、改参数或重复执行扩大调研")
        if state["status"] == "completed":
            return {"run_id": run_id, "report": str(path), "status": "completed", "reused": True}
    else:
        day = datetime.now(timezone.utc).date()
        state = {"input_hash": digest, "plan": plan, "parameters": _parameters(cfg), "status": "pending", "results": [],
                 "start_date": (day - timedelta(days=settings.section("discovery")["lookback_days"])).isoformat(), "end_date": day.isoformat()}
        atomic_json(path, state)
    state["status"] = "running"
    def checkpoint():
        state["updated_at"] = now()
        atomic_json(path, state)
        with db.connect() as conn:
            conn.execute("""INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,error,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,kind,input_hash) DO UPDATE SET
                status=excluded.status,result_json=excluded.result_json,error=excluded.error,updated_at=excluded.updated_at""",
                (run_id + ":retrieval:" + checkpoint_action, run_id, "retrieval:" + checkpoint_action, digest, state["status"],
                 json.dumps({"report": str(path), "completed_steps": len(state["results"]), "next": "使用原问题单和run-id恢复"}, ensure_ascii=False), state.get("error"), now()))
    checkpoint()
    try:
        for index, query in enumerate(queries):
            if any(row["step"] == "query:" + str(index) for row in state["results"]):
                continue
            result = client.search(query["query"], purpose=query["purpose"], start_date=state["start_date"], end_date=state["end_date"], retry=retry_failed)
            for item in result["results"]:
                item["material"] = material_notes(item["title"], item["summary"], item["published_at"], purpose=query["purpose"], source_level=infer_source_level(item["url"], 3), updated_at=item["updated_at"])
            state["results"].append({"step": "query:" + str(index), "result": result})
            checkpoint()
        for index, page in enumerate(pages):
            if any(row["step"] == "page:" + str(index) for row in state["results"]):
                continue
            result = client.extract(page["url"], page["terms"], reason=page["reason"], retry=retry_failed)
            state["results"].append({"step": "page:" + str(index), "result": result})
            checkpoint()
        state["status"], state["error"] = "completed", None
    except TavilyError as exc:
        state["status"], state["error"] = exc.status, exc.code
    except KeyboardInterrupt:
        state["status"], state["error"] = "needs_review", "interrupted_unknown"
        checkpoint()
        raise
    except Exception:
        state["status"], state["error"] = "failed", "local_step_failed"
        raise
    finally:
        state["usage"] = retrieval_usage(db, run_id)
        checkpoint()
    return {"run_id": run_id, "report": str(path), "status": state["status"], "usage": state["usage"],
            "next": "完成来源和适用范围核验，不直接据搜索摘要下结论" if state["status"] == "completed" else "使用同一run-id及原问题单恢复；显式--retry-failed也受原行为预算限制"}


def _validate_repair(settings, db, run_id, plan, action):
    if "repair" not in plan:
        return 0
    repair = plan["repair"]
    if plan["stage"] not in {"pre_research", "research"} or not isinstance(repair, dict):
        raise ValueError("补查只能用于已选题的预研或深研，不用于发现或诊断扩题")
    if set(repair) != {"round", "review_id", "unknown_indexes", "reason"}:
        raise ValueError("repair须记录轮次、最新决策单、关键未知编号和补查理由")
    cfg = settings.raw["tavily"]
    limit, count = cfg.get("max_repair_rounds", 0), cfg.get("max_repair_unknowns", 0)
    number = repair["round"]
    if type(limit) is not int or type(count) is not int or limit < 0 or count < 1:
        raise ValueError("补查配置无效；轮数须为非负整数，问题数须为正整数")
    if type(number) is not int or not 1 <= number <= limit:
        raise ValueError("补查轮数超过配置上限或功能已关闭")
    indexes = repair["unknown_indexes"]
    if (not isinstance(indexes, list) or not 1 <= len(indexes) <= count
            or any(type(i) is not int or i < 0 for i in indexes) or len(set(indexes)) != len(indexes)):
        raise ValueError("每轮须针对不重复的1—2个关键未知编号")
    if not isinstance(repair["reason"], str) or not repair["reason"].strip():
        raise ValueError("须说明补查如何改变判断，不能重复原提示词")
    with db.connect() as conn:
        review = conn.execute(
            "SELECT * FROM research_reviews WHERE run_id=? AND candidate_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (run_id, plan["candidate_id"]),
        ).fetchone()
    if not review or repair["review_id"] != review["id"]:
        raise ValueError("补查必须绑定本候选最新决策单，不能继承旧题状态")
    payload = json.loads(review["data_json"])
    if review["human_decision"] == "stop" or payload.get("decision_reason") == "fact_contradicted":
        raise ValueError("本题已明确停止或核心事实已被否定，不能由补查自动重启")
    unknowns = payload.get("critical_unknowns", [])
    if any(i >= len(unknowns) or not isinstance(unknowns[i], dict)
           or not unknowns[i].get("question") or not unknowns[i].get("resolution_plan") for i in indexes):
        raise ValueError("补查编号未指向最新决策单的具体未知和获取路径")
    previous_action = action + (f":repair:{number - 1}" if number > 1 else "")
    previous = settings.root / "data/runs" / run_id / ("retrieval-" + previous_action.replace(":", "-") + ".json")
    if not previous.exists():
        raise ValueError("须先完成前一轮问题单，不能跳轮补查")
    previous_state = json.loads(previous.read_text(encoding="utf-8"))
    if previous_state["status"] not in {"completed", "failed", "needs_review"}:
        raise ValueError("前一轮仍在执行或预算/额度阻断，须在原行为内处理")
    return number
