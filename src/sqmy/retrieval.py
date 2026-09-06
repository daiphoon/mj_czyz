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
from .materials import material_notes, mark_reposts
from .models import EventItem
from .snapshots import _public_http_url
from .tavily import TavilyClient, TavilyError, atomic_json, retrieval_usage


def _parameters(cfg):
    return {key: cfg[key] for key in ("search_depth", "extract_depth", "max_results_per_query", "summary_chars", "excerpt_chars", "context_chars")}


def discovery_queries(events, cfg, day):
    scenarios = cfg.get("scenario_queries", [])
    if not scenarios:
        return []
    offset = day.toordinal() % len(scenarios)
    rotated = scenarios[offset:] + scenarios[:offset]
    def count(scenario):
        return sum(any(term in event.title + event.summary for term in scenario["signals"]) for event in events)
    # 只补当前结果较少的具体场景；配置规模不改变模型输入池。
    return sorted(rotated, key=count)[:cfg["max_searches_per_action"]]


def collect_discovery(collector, settings, db, run_id, *, fixture=None, clue_file=None):
    cfg = settings.raw.get("tavily", {})
    if fixture or not cfg.get("enabled", False) or settings.section("model")["provider"] == "mock":
        return collector.collect(run_id, fixture=fixture, clue_file=clue_file)
    directory = settings.root / "data/runs" / run_id
    path = directory / "collection_checkpoint.json"
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("finished"):
            collector.collection_stats = state["stats"]
            return [EventItem(**item) for item in state["events"]]
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
    if not isinstance(plan, dict) or set(plan) - {"stage", "candidate_id", "queries", "pages"}:
        raise ValueError("问题单只接受stage、candidate_id、queries、pages")
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
                review = conn.execute("SELECT human_decision,research_allowed FROM research_reviews WHERE run_id=? AND candidate_id=? ORDER BY created_at DESC LIMIT 1", (run_id, candidate_id)).fetchone()
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
    if any(not isinstance(page, dict) or not page.get("url") or not page.get("terms") or page.get("reason") not in {"http_failed", "http_incomplete"} for page in pages):
        raise ValueError("页面须有URL、目标词、普通HTTP失败或内容不完整的原因")
    action = stage + (":" + candidate_id if candidate_id else "")
    client = TavilyClient(settings, db, run_id, action)
    serialized = json.dumps(plan, ensure_ascii=False, sort_keys=True)
    if client._text(serialized, len(serialized)) != serialized:
        raise ValueError("问题单不得包含凭证")
    for query in queries:
        if set(query) != {"purpose", "query"} or len(query["query"]) > 500:
            raise ValueError("检索词须在500字内；只接受purpose和query")
    for page in pages:
        if set(page) != {"url", "terms", "reason"}:
            raise ValueError("提取页只接受url、terms、reason")
        _public_http_url(page["url"])
        if not isinstance(page["terms"], list) or not 1 <= len(page["terms"]) <= 8 or any(not isinstance(term, str) or not term.strip() or len(term) > 100 for term in page["terms"]):
            raise ValueError("每页需要1—8个100字以内的目标词")
    path = settings.root / "data/runs" / run_id / ("retrieval-" + action.replace(":", "-") + ".json")
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
                (run_id + ":retrieval:" + action, run_id, "retrieval:" + action, digest, state["status"],
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
