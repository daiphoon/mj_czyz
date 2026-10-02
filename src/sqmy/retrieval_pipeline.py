"""原人工问题单的检索接续；搜索与已知URL获取各自完成、各自留痕。"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json

from .collector import canonical_url, infer_event_region, infer_source_level, parse_date
from .db import now
from .discovery_coverage import provenance, coverage_summary
from .fetch import FetchResult
from .materials import material_notes, mark_reposts, split_discovery_summary
from .models import EventItem
from .search_router import build_search_router
from .search_types import RetrievalIntent, SearchError, SearchRequest, search_type_for_intent
from .source_registry import SourceRegistry
from .tavily import atomic_json, retrieval_usage
from .transport_audit import TransportAudit


CONTRACT = "retrieval_pipeline_v1"
PLAN_CONTRACT = "retrieval_pipeline_v2"
PAGE_FETCH_POLICY = "isolated_v1"


def search_events(router, query, *, limit, domains):
    """旧影子核验消费统一搜索元数据，仍不把摘要当已读原文。"""
    response = router.search(SearchRequest(query, intent=RetrievalIntent.COUNTER_EVIDENCE,
                                          max_results=limit, domains=tuple(domains)))
    if response.status != "completed":
        raise SearchError("search_unavailable", "failure")
    events = []
    for item in response.results:
        published = parse_date(item.published_at or "")
        region, evidence = infer_event_region(item.title, item.snippet, item.url, "全国")
        events.append(EventItem(hashlib.sha256(item.url.encode()).hexdigest()[:16], item.provider,
            item.provider, infer_source_level(item.url, 3), item.title, item.url,
            published.isoformat() if published else "", item.snippet, region,
            source_region="全国", region_evidence=evidence,
            material={"verification_status": "unverified", "date_basis": item.date_basis}))
    return events


def parameters(settings):
    path = settings.root / "config/source_registry.toml"
    return dict(contract=CONTRACT, search=settings.raw["search"], fetch=settings.raw["fetch"],
                provider_parameters={"tavily": {k: settings.raw["tavily"].get(k) for k in
                    ("search_depth", "extract_depth", "max_results_per_query", "summary_chars", "max_searches_per_action", "max_extracts_per_action", "action_cost_limit_usd")},
                                     "brave": settings.raw.get("brave", {})},
                registry_sha256=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None)


def collect_routed(collector, settings, db, run_id, *, clue_file=None):
    from .retrieval import discovery_queries
    path = settings.root / "data/runs" / run_id / "collection_checkpoint.json"
    cfg = settings.raw["tavily"]
    frozen = parameters(settings)
    if path.exists():
        state = json.loads(path.read_text())
        if state["parameters"] != frozen:
            raise ValueError("本次发现输入已经固定；不能改变检索入口或参数恢复")
        if state.get("finished"):
            collector.collection_stats = state["stats"]
            return [EventItem(**item) for item in state["events"]]
    else:
        events = collector.collect(run_id, clue_file=clue_file)
        day = datetime.now(timezone.utc).date()
        state = dict(contract=CONTRACT, events=[asdict(e) for e in events], stats=collector.collection_stats,
                     queries=discovery_queries(events, cfg, day), processed_queries=[],
                     start_date=(day - timedelta(days=settings.raw["discovery"]["lookback_days"])).isoformat(),
                     end_date=day.isoformat(), parameters=frozen, finished=False)
        atomic_json(path, state)
    router = build_search_router(settings, db, run_id, "discovery", collector=collector)
    registry = SourceRegistry(settings.root / "config/source_registry.toml")
    for query in state["queries"]:
        if query["id"] in state["processed_queries"]:
            continue
        request = SearchRequest(query["query"], intent=RetrievalIntent.HOTSPOT_DISCOVERY, search_type="news",
                                max_results=min(5, cfg["max_results_per_query"]), start_date=state["start_date"], end_date=state["end_date"])
        response = router.search(request)
        stats = {}
        for item in response.results:
            from urllib.parse import urlparse
            host = (urlparse(item.url).hostname or "unknown").removeprefix("www.")
            source_id = "search_" + host
            row = stats.setdefault(source_id, dict(source_id=source_id, source_name=host, expansion_tier=4,
                fetched_count=0, within_window_count=0, collected_count=0, invalid_metadata_count=0,
                outside_window_count=0, fetch_error=None, parse_error=None))
            row["fetched_count"] += 1
            published = parse_date(item.published_at or "")
            if published and not state["start_date"] <= published.date().isoformat() <= state["end_date"]:
                row["outside_window_count"] += 1
                continue
            row["within_window_count"] += 1
            url = canonical_url(item.url)
            region, evidence = infer_event_region(item.title, item.snippet, url, "全国")
            level, stamp = infer_source_level(url, 3), published.isoformat() if published else ""
            material = material_notes(item.title, item.snippet, stamp, source_level=level)
            material.update(source_attributes=registry.lookup(url), verification_status="unverified")
            material["discovery_provenance"] = provenance(url, channel=item.provider, source_id=source_id,
                                                          date_basis=item.date_basis, query_id=query["id"])
            event = EventItem(id=hashlib.sha256((item.title + "\n" + url).encode()).hexdigest()[:16],
                source_id=source_id, source_name=host, source_level=level, title=item.title, url=url,
                published_at=stamp, summary=item.snippet, region=region, source_region="全国", region_evidence=evidence,
                expansion_tier=4, collected_at=now(), material=material)
            state["events"].append(asdict(event))
            row["collected_count"] += 1
        if not stats:
            source_id = "search_" + query["id"]
            stats[source_id] = dict(source_id=source_id, source_name="搜索补充·" + query["id"], expansion_tier=4,
                fetched_count=0, within_window_count=0, collected_count=0, invalid_metadata_count=0, outside_window_count=0,
                fetch_error=";".join(x["code"] for x in response.errors) if response.status == "unavailable" else None, parse_error=None)
        state["stats"].extend(stats.values())
        state["processed_queries"].append(query["id"])
        state["supplement_status"] = response.status
        atomic_json(path, state)
        if response.status == "unavailable":
            break
    events = [EventItem(**item) for item in state["events"]]
    mark_reposts(events)
    state.update(finished=True, events=[asdict(e) for e in events])
    atomic_json(path, state)
    collector.collection_stats = state["stats"]
    atomic_json(settings.root / "data/runs" / run_id / "discovery_coverage.json", dict(run_id=run_id, **coverage_summary(events, state["stats"])))
    return events


def execute_plan(settings, db, run_id, plan, action, checkpoint_action, *, retry_failed=False):
    path = settings.root / "data/runs" / run_id / ("retrieval-" + checkpoint_action.replace(":", "-") + ".json")
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    frozen = parameters(settings)
    if path.exists():
        state = json.loads(path.read_text())
        contract = state.get("contract")
        if contract not in {None, CONTRACT, PLAN_CONTRACT}:
            raise ValueError("未知检索契约；不可降级到旧检索入口")
        if contract is None:
            from .retrieval import _parameters
            expected = _parameters(settings.raw["tavily"])
        else:
            expected = dict(frozen, page_fetch_policy=PAGE_FETCH_POLICY) if contract == PLAN_CONTRACT else frozen
        if state["input_hash"] != digest or state["parameters"] != expected:
            raise ValueError("本行为问题单已固定；不可改变输入或入口扩大调研")
        if contract != PLAN_CONTRACT:
            # 保留原检查点及用量；不能在旧行为里替换传输策略或恢复付费入口。
            research_status = state.get("research_status", "RESEARCH_INCOMPLETE")
            return dict(run_id=run_id, report=str(path), reused=True, historical=True,
                        checkpoint_status=state["status"], research_status=research_status,
                        status="completed" if state["status"] == "completed" and research_status == "MATERIALS_READY_AWAITING_VERIFICATION" else "needs_review",
                        usage=state.get("usage", {}), resume_blocked="legacy_transport_policy",
                        next="旧检索检查点仅复用；不重试或改写历史，不自动新建行为扩大额度。")
        if state.get("page_fetch_policy") != PAGE_FETCH_POLICY:
            raise ValueError("正文获取策略已固定；不能恢复自动传输")
        records = {row["step"]: row["result"] for row in state["results"]}
        if any(row["step"].startswith("page:") and (row["result"].get("error_code") != "unsupported_transport" or
                   row["result"].get("body_read") is not False) for row in state["results"]):
            raise ValueError("正文记录与隔离策略不一致")
        retryable_queries = any(records.get("query:" + str(i), {}).get("status") != "completed"
                                for i in range(len(plan.get("queries", []))))
        missing_pages = any("page:" + str(i) not in records for i in range(len(plan.get("pages", []))))
        if state["status"] == "completed" or state["status"] == "needs_review" and (not retry_failed or not retryable_queries and not missing_pages):
            return dict(run_id=run_id, report=str(path), status=state["status"], research_status=state["research_status"], reused=True)
    else:
        state = dict(contract=PLAN_CONTRACT, page_fetch_policy=PAGE_FETCH_POLICY, input_hash=digest, plan=plan,
                     parameters=dict(frozen, page_fetch_policy=PAGE_FETCH_POLICY), status="running", results=[])

    def checkpoint():
        state["updated_at"] = now()
        atomic_json(path, state)
        with db.connect() as conn:
            conn.execute("""INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,error,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,kind,input_hash) DO UPDATE SET
                status=excluded.status,result_json=excluded.result_json,error=excluded.error,updated_at=excluded.updated_at""",
                (run_id + ":retrieval:" + checkpoint_action, run_id, "retrieval:" + checkpoint_action, digest, state["status"],
                 json.dumps({"report": str(path), "completed_steps": len(state["results"]), "research_status": state.get("research_status", "RESEARCH_INCOMPLETE")}, ensure_ascii=False),
                 state.get("error"), now()))

    router = build_search_router(settings, db, run_id, action)
    checkpoint()
    try:
        for index, query in enumerate(plan.get("queries", [])):
            step = "query:" + str(index)
            old = next((x for x in state["results"] if x["step"] == step), None)
            if old and (old["result"]["status"] == "completed" or not retry_failed):
                continue
            intent = RetrievalIntent(query.get("intent", "POLICY_SEARCH" if query["purpose"] == "policy" else "HOTSPOT_DISCOVERY"))
            request = SearchRequest(query["query"], intent=intent, domains=tuple(query.get("domains", [])), exclude_domains=tuple(query.get("exclude_domains", [])),
                                    search_type=search_type_for_intent(intent),
                                    recency_days=settings.raw["discovery"]["lookback_days"] if intent == RetrievalIntent.HOTSPOT_DISCOVERY else None)
            result = router.search(request, retry_failed=retry_failed).to_dict()
            result["intent"] = intent.value
            if old:
                state["results"].remove(old)
            state["results"].append({"step": step, "result": result})
            checkpoint()
        # 正文传输尚未验收；查询失败、显式重试或已有缓存均不能重新开启它。
        for index, page in enumerate(plan.get("pages", [])):
            step = "page:" + str(index)
            old = next((x for x in state["results"] if x["step"] == step), None)
            if old:
                continue
            audit = TransportAudit(tracked=True)
            audit.phase = "transport_policy"
            result = FetchResult(page["url"], "", "unsupported", now(), error_code="unsupported_transport",
                                 content_hash_kind="not_acquired", transport_metadata=audit.snapshot()).record(page["terms"], settings.raw["fetch"])
            result.update(body_read=False, page_fetch_policy=PAGE_FETCH_POLICY)
            state["results"].append({"step": step, "result": result})
            checkpoint()
        incomplete = [x["step"] for x in state["results"] if (
            x["step"].startswith("query:") and (x["result"]["status"] != "completed" or
                x["result"]["intent"] != "HOTSPOT_DISCOVERY" and not x["result"]["results"])) or (
            x["step"].startswith("page:") and (x["result"]["status"] != "fetched" or not x["result"].get("target_found")))]
        state.update(status="needs_review" if incomplete else "completed", incomplete_steps=incomplete,
                     research_status="RESEARCH_INCOMPLETE" if incomplete else "MATERIALS_READY_AWAITING_VERIFICATION")
    except KeyboardInterrupt:
        state.update(status="needs_review", research_status="RESEARCH_INCOMPLETE", error="interrupted_unknown")
        raise
    except Exception:
        state.update(status="needs_review", research_status="RESEARCH_INCOMPLETE", error="retrieval_interrupted")
        raise
    finally:
        state["usage"] = retrieval_usage(db, run_id)
        checkpoint()
    return dict(run_id=run_id, report=str(path), status=state["status"], research_status=state["research_status"], usage=state["usage"],
                next="自动正文获取暂隔离；未读材料保持RESEARCH_INCOMPLETE，不转送其他抓取入口，不自动深研或写稿。")
