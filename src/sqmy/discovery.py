from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from difflib import SequenceMatcher
import re

from .collector import SourceCollector
from .budget import BudgetExceeded, BudgetGuard, estimate_model_call_tokens, weekly_usage
from .config import Settings
from .db import now
from .models import Phase, TaskStatus
from .models import EventItem
from .novelty import NoveltyAuditor, write_rolling_evaluation
from .providers import ProviderError, QuotaExceeded, RateLimited, build_router
from .screener import normalize_title, rule_screen, score as event_score, to_candidate
from .workflow import Workflow


ORIGIN_TOPIC_TERMS = (
    "未成年人", "网络", "纠纷", "游戏充值", "直播打赏", "个人信息",
    "职业伤害", "新就业形态", "贴息", "中小企业", "人工智能", "养老", "医疗",
)
INSTITUTION_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,16}(?:互联网法院|人民法院|检察院|委员会|人民政府|研究院|协会)")


def _origin_institutions(text: str) -> set[str]:
    found = set()
    for segment in re.split(r"[\s，。；：“”《》（）()]+", text):
        found.update(INSTITUTION_PATTERN.findall(segment))
    return found


def same_origin_event(event: EventItem, previous) -> bool:
    event_day = (event.published_at or "")[:10]
    previous_day = (previous["published_at"] or "")[:10]
    if not event_day or not previous_day or event_day != previous_day:
        return False
    if SequenceMatcher(None, normalize_title(event.title), normalize_title(previous["title"])).ratio() >= 0.78:
        return True
    current_text = event.title + " " + event.summary
    previous_text = previous["title"] + " " + (previous["summary"] or "")
    institutions = _origin_institutions(current_text) & _origin_institutions(previous_text)
    shared_terms = {term for term in ORIGIN_TOPIC_TERMS if term in current_text and term in previous_text}
    return bool(institutions) and len(shared_terms) >= 2


class LiveDiscovery:
    def __init__(self, settings: Settings):
        self.s = settings
        self.wf = Workflow(settings)

    def run(self, fixture: Path | None = None, *, force: bool = False, start_tier: int = 1) -> tuple[str, list]:
        mode = "test_fixture" if fixture else "live"
        run_id = self.wf.init_run(mode, forced=force)
        self.wf.db.checkpoint(run_id, phase=Phase.DISCOVERY, status=TaskStatus.RUNNING, data={"next": "collect_sources"})
        collector = SourceCollector(self.s.root, self.s.raw)
        collected = collector.collect(run_id, fixture=fixture)
        max_tier = max((int(source.get("expansion_tier", 1)) for source in collector.sources), default=1)
        if fixture:
            max_tier = max((item.expansion_tier for item in collected), default=1)
        if not 1 <= start_tier <= max_tier:
            raise ValueError(f"扩展起始层必须在1到{max_tier}之间")
        tier_stats = []
        premodel_pool, fresh_pool, repeated_excluded, expansion_tier = [], [], 0, 1
        for tier in range(start_tier, max_tier + 1):
            tier_items = [item for item in collected if start_tier <= item.expansion_tier <= tier]
            rule_results = rule_screen(tier_items, self.s.section("discovery"))
            fresh_results, repeated_excluded = (rule_results, 0) if force else self._exclude_unchanged(rule_results, run_id, mode)
            premodel_pool = fresh_results[: self.s.section("discovery")["screened_max"]]
            fresh_pool = premodel_pool
            expansion_tier = tier
            tier_stats.append({"tier": tier, "collected": len(tier_items), "rule_qualified": len(rule_results), "repeated_excluded": repeated_excluded, "fresh_available": len(fresh_results), "model_pool": len(fresh_pool)})
            if len(fresh_pool) >= self.s.section("discovery")["screened_min"]:
                break
        self._persist_events(run_id, premodel_pool)
        should_model, batch_reason = self._should_run_model(
            fresh_pool, force=force,
            final_attempt=mode == "live" and expansion_tier == max_tier,
        )
        model_pool = fresh_pool if should_model else []
        deferred_count = 0 if should_model else len(fresh_pool)
        screened, cache_hit, tokens_saved = self._model_rank(run_id, model_pool, use_cache=not force)
        auditor = NoveltyAuditor(self.s)
        audits = auditor.audit(run_id, screened, live_search=fixture is None)
        audit_by_id = {item.event_id: item for item in audits}
        eligible = [item for item in screened if audit_by_id.get(item.id) is None or audit_by_id[item.id].decision != "block_original_gap"]
        model_rank = {item.id: index for index, item in enumerate(screened)}
        def candidate_rank(item):
            audit = audit_by_id.get(item.id)
            penalty = self.s.section("novelty")["likely_covered_score_penalty"] if audit and audit.coverage_status == "likely_covered" else 0
            return ((len(screened) - model_rank[item.id]) * 10 + item.rule_score - penalty, item.published_at)
        eligible.sort(key=candidate_rank, reverse=True)
        candidates = []
        for item in eligible[: self.s.section("project")["candidate_count"]]:
            audit = audit_by_id.get(item.id)
            penalty = self.s.section("novelty")["likely_covered_score_penalty"] if audit and audit.coverage_status == "likely_covered" else 0
            candidates.append(to_candidate(item, len(candidates) + 1, audit, penalty))
        self._apply_history(candidates)
        self._persist_candidates(run_id, candidates)
        self._record_efficiency(
            run_id, premodel_count=len(premodel_pool), repeated_excluded=repeated_excluded,
            model_input_count=len(model_pool), cache_hit=cache_hit,
            tokens_saved=tokens_saved, deferred_count=deferred_count,
            expansion_tier=expansion_tier, candidate_count=len(candidates),
        )
        report = self._report(
            run_id, candidates, audits, len(collected), len(screened), len(premodel_pool),
            repeated_excluded, cache_hit, tokens_saved, deferred_count, batch_reason, start_tier, expansion_tier,
        )
        metrics_path = write_rolling_evaluation(self.s)
        self.wf.db.checkpoint(
            run_id, phase=Phase.SELECTION, status=TaskStatus.NEEDS_REVIEW if candidates else TaskStatus.SKIPPED,
            data={"collected": len(collected), "start_tier": start_tier, "premodel": len(premodel_pool), "repeated_excluded": repeated_excluded, "fresh_events": len(fresh_pool), "model_input": len(model_pool), "deferred_count": deferred_count, "batch_reason": batch_reason, "expansion_tier": expansion_tier, "tier_stats": tier_stats, "screening_cache_hit": cache_hit, "screened": len(screened), "novelty_audited": len(audits), "novelty_blocked": sum(a.decision == "block_original_gap" for a in audits), "candidates": len(candidates), "report": str(report), "rolling_metrics": str(metrics_path), "next": f"sqmy select {run_id} C1" if candidates else ("新事件已延迟到下一批合并初筛" if deferred_count else "当前扩展层无可用新事件，需进入下一层或检查来源")},
        )
        return run_id, candidates

    def replay(self, source_run_id: str) -> tuple[str, list]:
        source_path = self.s.root / "data/runs" / source_run_id / "model_screening_result.json"
        if not source_path.exists():
            raise ValueError(f"运行缺少可回放的模型结果：{source_run_id}")
        with self.wf.db.connect() as conn:
            rows = conn.execute("SELECT * FROM event_items WHERE run_id=? ORDER BY rule_score DESC", (source_run_id,)).fetchall()
            tokens = conn.execute(
                "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM model_calls WHERE run_id=? AND task_id='screening'",
                (source_run_id,),
            ).fetchone()[0]
        events = [EventItem(
            id=row["id"].split(":", 1)[1], source_id=row["source_id"], source_name=row["source_name"],
            source_level=row["source_level"], title=row["title"], url=row["url"],
            published_at=row["published_at"] or "", summary=row["summary"] or "", region=row["region"],
            source_region=row["source_region"] if "source_region" in row.keys() else "",
            region_evidence=row["region_evidence"] if "region_evidence" in row.keys() else "",
            expansion_tier=row["expansion_tier"] if "expansion_tier" in row.keys() else 1,
            topics=json.loads(row["topics_json"]), rule_score=row["rule_score"], collected_at=row["collected_at"],
        ) for row in rows]
        for event in events:
            event.rule_score = event_score(event)
        data = json.loads(source_path.read_text(encoding="utf-8"))
        screened = self._attach_analyses(events, data)
        run_id = self.wf.init_run("replay")
        self._persist_events(run_id, events)
        audits = NoveltyAuditor(self.s).audit(run_id, screened, live_search=False)
        audit_by_id = {item.event_id: item for item in audits}
        eligible = [item for item in screened if audit_by_id[item.id].decision != "block_original_gap"]
        candidates = []
        for item in eligible[: self.s.section("project")["candidate_count"]]:
            audit = audit_by_id[item.id]
            penalty = self.s.section("novelty")["likely_covered_score_penalty"] if audit.coverage_status == "likely_covered" else 0
            candidates.append(to_candidate(item, len(candidates) + 1, audit, penalty))
        self._apply_history(candidates)
        self._persist_candidates(run_id, candidates)
        self._record_efficiency(
            run_id, premodel_count=len(events), repeated_excluded=0, model_input_count=0,
            cache_hit=True, tokens_saved=int(tokens), deferred_count=0,
            expansion_tier=1, candidate_count=len(candidates),
        )
        report = self._report(
            run_id, candidates, audits, len(events), len(screened), len(events), 0, True, int(tokens), 0, "replay", 1, 1,
        )
        self.wf.db.checkpoint(
            run_id, phase=Phase.SELECTION, status=TaskStatus.COMPLETED,
            data={"replay_of": source_run_id, "model_calls": 0, "candidates": len(candidates), "report": str(report)},
        )
        return run_id, candidates

    def _apply_history(self, candidates: list) -> None:
        with self.wf.db.connect() as conn:
            rows = conn.execute(
                """SELECT title, id AS ref, NULL AS data_json FROM topics
                   UNION ALL
                   SELECT c.title, c.run_id AS ref, c.data_json
                   FROM candidates c JOIN run_context rc ON rc.run_id=c.run_id
                   WHERE rc.mode='live'
                   ORDER BY title"""
            ).fetchall()
        history = [(row[0], row[1], json.loads(row[2]) if row[2] else None) for row in rows]
        for candidate in candidates:
            if not history:
                candidate.history_relation = "历史库暂无可比较记录"
                continue
            source_url = candidate.score_reasons.get("来源URL")
            same_source = next((
                (title, ref) for title, ref, data in history
                if data and data.get("score_reasons", {}).get("来源URL") == source_url
            ), None)
            if same_source:
                candidate.history_relation = f"同一来源事件已于 {same_source[1]} 进入候选：{same_source[0]}"
                continue
            best_title, best_ref, best_score = max(
                ((title, ref, SequenceMatcher(None, normalize_title(candidate.title), normalize_title(title)).ratio()) for title, ref, _ in history),
                key=lambda pair: pair[2],
            )
            candidate.history_relation = f"最高标题相似度 {best_score:.2f}：{best_title}（{best_ref}）" if best_score >= 0.45 else f"最高标题相似度 {best_score:.2f}，未发现明显重复"

    def _exclude_unchanged(self, events: list, run_id: str, mode: str) -> tuple[list, int]:
        if mode != "live":
            return events, 0
        with self.wf.db.connect() as conn:
            rows = conn.execute(
                """SELECT e.url,e.title,e.summary,e.published_at
                   FROM event_items e JOIN run_context rc ON rc.run_id=e.run_id
                   LEFT JOIN run_efficiency reff ON reff.run_id=e.run_id
                   WHERE rc.mode='live' AND e.run_id<>?
                     AND (reff.run_id IS NULL OR reff.model_input_count>0)""",
                (run_id,),
            ).fetchall()
        previous_urls = {row["url"] for row in rows}
        previous_titles = {(normalize_title(row["title"]), (row["published_at"] or "")[:10]) for row in rows}
        fresh = []
        for event in events:
            exact = event.url in previous_urls or (normalize_title(event.title), (event.published_at or "")[:10]) in previous_titles
            syndicated = any(same_origin_event(event, row) for row in rows) if not exact else False
            if not exact and not syndicated:
                fresh.append(event)
        return fresh, len(events) - len(fresh)

    def _should_run_model(self, events: list, *, force: bool, final_attempt: bool = False) -> tuple[bool, str]:
        if force:
            return bool(events), "forced"
        cfg = self.s.section("discovery")
        if len(events) >= cfg["min_new_events_for_model"]:
            return True, "minimum_batch_reached"
        urgent = any(
            event.rule_score >= cfg["urgent_rule_score_threshold"]
            and any(word in event.title + " " + event.summary for word in cfg["urgent_keywords"])
            for event in events
        )
        if urgent:
            return True, "urgent_exception"
        if final_attempt and events:
            return True, "cascade_exhausted_floor"
        return False, "deferred_small_batch" if events else "no_new_events"

    def _model_rank(self, run_id: str, events: list, *, use_cache: bool = True) -> tuple[list, bool, int]:
        model_cfg = self.s.section("model")
        limit = self.s.section("discovery")["screened_max"]
        pool = events[:limit]
        if not pool:
            return [], False, 0
        router = build_router(self.s.root, model_cfg)
        if router is None:
            return events, False, 0
        compact = [{"id": x.id, "title": x.title, "date": x.published_at[:10], "region": x.region, "source_level": x.source_level, "expansion_tier": x.expansion_tier, "summary": x.summary[:350], "rule_score": x.rule_score} for x in pool]
        prompt = (
            "你是社情民意选题初筛员。仅依据以下标题、摘要和元数据进行低成本判断，不得补造事实。"
            "优先选择存在制度缺口、北京或海淀落点、明确受影响群体且可进一步核验的事件；"
            "政策已经完整覆盖、纯会议活动、企业软文、机构推广、单一宣传信息、没有北京落点的外地补贴政策应排除。"
            "全国事件只有能明确转化为北京或海淀试点时才可保留。宁可少于5题，不得凑数。"
            "每个入选题均须根据现有元数据给出克制的初步分析；证据不足必须直说。"
            "请返回最多的有价值备选，供制度新意审查后再取最终5个。"
            "每题必须把拟议制度缺口写成一句可被反证的gap_hypothesis，"
            "gap_type只能是policy_absence、implementation_gap、coordination_gap、effectiveness_gap、accountability_gap或unclear；"
            "counter_queries提供两条优先查政府、法院或监管部门的精确反证检索词。\n"
            + json.dumps(compact, ensure_ascii=False)
        )
        budget_cfg = self.s.section("budget")
        estimated = estimate_model_call_tokens(prompt, model_cfg, budget_cfg)
        usage = weekly_usage(self.wf.db)
        guard = BudgetGuard(
            budget_cfg["weekly_token_limit"], usage["token_used"],
            stage_limit=budget_cfg["screening_tokens"],
        )
        try:
            guard.reserve(estimated)
        except BudgetExceeded as exc:
            self.wf.db.checkpoint(run_id, phase=Phase.DISCOVERY, status=TaskStatus.PAUSED_BUDGET, data={"next": f"sqmy resume {run_id}"}, error=str(exc))
            raise
        selection_properties = {
            "id": {"type": "string"}, "affected_group": {"type": "string"},
            "institutional_issue": {"type": "string"}, "pain_point": {"type": "string"},
            "policy_gap": {"type": "string"}, "policy_entry": {"type": "string"},
            "authority": {"type": "string"}, "data_assessment": {"type": "string"},
            "policy_window": {"type": "string"}, "recommendation": {"type": "string"}, "risk": {"type": "string"},
            "gap_hypothesis": {"type": "string"},
            "gap_type": {"type": "string", "enum": ["policy_absence", "implementation_gap", "coordination_gap", "effectiveness_gap", "accountability_gap", "unclear"]},
            "counter_queries": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "string"}},
            "suggested_title": {"type": "string"},
        }
        schema = {"type": "object", "additionalProperties": False, "properties": {
            "selections": {"type": "array", "minItems": 1, "maxItems": self.s.section("novelty")["audit_pool_size"], "items": {
                "type": "object", "additionalProperties": False, "properties": selection_properties,
                "required": list(selection_properties),
            }}}, "required": ["selections"]}
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        with self.wf.db.connect() as conn:
            cached = conn.execute(
                """SELECT result_json,token_used FROM tasks
                   WHERE kind='model_screening' AND input_hash=? AND status=?
                   ORDER BY updated_at DESC LIMIT 1""",
                (prompt_hash, TaskStatus.COMPLETED),
            ).fetchone()
        if use_cache and cached and cached["result_json"]:
            data = json.loads(cached["result_json"])
            self._write_screening_audit(run_id, data)
            return self._attach_analyses(pool, data), True, int(cached["token_used"])
        try:
            result, fallback_reason = router.analyze(prompt, schema)
        except (QuotaExceeded, RateLimited) as exc:
            self.wf.db.checkpoint(run_id, phase=Phase.DISCOVERY, status=TaskStatus.PAUSED_QUOTA, data={"next": f"sqmy resume {run_id}"}, error=str(exc))
            raise
        except ProviderError as exc:
            self.wf.db.checkpoint(run_id, phase=Phase.DISCOVERY, status=TaskStatus.FAILED, data={"next": f"sqmy retry {run_id}"}, error=str(exc))
            raise
        if result.provider == "deepseek":
            input_price = model_cfg["deepseek_input_price_cny_per_million"]
            output_price = model_cfg["deepseek_output_price_cny_per_million"]
        else:
            input_price = output_price = 0.0
        cost = (result.input_tokens * input_price + result.output_tokens * output_price) / 1_000_000
        with self.wf.db.connect() as conn:
            conn.execute("INSERT INTO model_calls(run_id,task_id,provider,model,prompt_hash,input_tokens,output_tokens,estimated_cost_cny,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (run_id, "screening", result.provider, result.model, prompt_hash, result.input_tokens, result.output_tokens, cost, "fallback:" + fallback_reason if fallback_reason else "completed", now()))
            conn.execute("UPDATE runs SET token_used=token_used+?, estimated_cost_cny=estimated_cost_cny+?, updated_at=? WHERE id=?",
                         (result.input_tokens + result.output_tokens, cost, now(), run_id))
            conn.execute(
                """INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,token_used,attempts,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (f"{run_id}:screening:{prompt_hash[:12]}", run_id, "model_screening", prompt_hash,
                 TaskStatus.COMPLETED, json.dumps(result.data, ensure_ascii=False),
                 result.input_tokens + result.output_tokens, 1, now()),
            )
        self._write_screening_audit(run_id, result.data)
        return self._attach_analyses(pool, result.data), False, 0

    def _write_screening_audit(self, run_id: str, data: dict) -> None:
        audit_path = self.s.root / "data/runs" / run_id / "model_screening_result.json"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = audit_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, audit_path)

    @staticmethod
    def _attach_analyses(pool: list, data: dict) -> list:
        by_id = {x.id: x for x in pool}
        ranked = []
        for analysis in data["selections"]:
            item = by_id.get(analysis["id"])
            if item is None:
                continue
            item.model_analysis = analysis
            ranked.append(item)
        if not ranked:
            raise ProviderError("模型未返回任何有效候选ID")
        return ranked

    def _persist_events(self, run_id: str, events: list) -> None:
        with self.wf.db.connect() as conn:
            for event in events:
                content_hash = hashlib.sha256((event.title + event.url + event.summary).encode()).hexdigest()
                conn.execute("""INSERT OR IGNORE INTO event_items(
                    id,run_id,source_id,source_name,source_level,title,url,published_at,summary,
                    region,topics_json,rule_score,content_hash,collected_at,source_region,region_evidence,expansion_tier
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    f"{run_id}:{event.id}", run_id, event.source_id, event.source_name, event.source_level, event.title,
                    event.url, event.published_at, event.summary, event.region, json.dumps(event.topics, ensure_ascii=False),
                    event.rule_score, content_hash, event.collected_at, event.source_region, event.region_evidence, event.expansion_tier,
                ))

    def _persist_candidates(self, run_id: str, candidates: list) -> None:
        with self.wf.db.connect() as conn:
            for candidate in candidates:
                conn.execute("INSERT INTO candidates(id,run_id,title,data_json,score,created_at) VALUES(?,?,?,?,?,?)", (
                    f"{run_id}:{candidate.id}", run_id, candidate.title, json.dumps(asdict(candidate), ensure_ascii=False), candidate.score, now(),
                ))

    def _record_efficiency(self, run_id: str, *, premodel_count: int, repeated_excluded: int,
                           model_input_count: int, cache_hit: bool, tokens_saved: int,
                           deferred_count: int, expansion_tier: int, candidate_count: int) -> None:
        with self.wf.db.connect() as conn:
            conn.execute(
                """INSERT INTO run_efficiency(
                     run_id,premodel_count,repeated_excluded,model_input_count,
                     screening_cache_hit,screening_tokens_saved,deferred_count,expansion_tier,candidate_count,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     premodel_count=excluded.premodel_count,
                     repeated_excluded=excluded.repeated_excluded,
                     model_input_count=excluded.model_input_count,
                     screening_cache_hit=excluded.screening_cache_hit,
                     screening_tokens_saved=excluded.screening_tokens_saved,
                     deferred_count=excluded.deferred_count,
                     expansion_tier=excluded.expansion_tier,
                     candidate_count=excluded.candidate_count,updated_at=excluded.updated_at""",
                (run_id, premodel_count, repeated_excluded, model_input_count,
                 int(cache_hit), tokens_saved, deferred_count, expansion_tier, candidate_count, now()),
            )

    def _report(self, run_id: str, candidates: list, audits: list, collected: int, screened: int,
                premodel: int, repeated_excluded: int, cache_hit: bool, tokens_saved: int,
                deferred_count: int, batch_reason: str, start_tier: int, expansion_tier: int) -> Path:
        path = self.s.root / "outputs/candidates" / f"{run_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        blocked = [item for item in audits if item.decision == "block_original_gap"]
        lines = [f"# 周一候选选题报告（{run_id}）", "", f"从第 {start_tier} 层开始；扩展到第 {expansion_tier} 层；采集 {collected} 条；模型前 {premodel} 条；排除历史未变化或同源事件 {repeated_excluded} 条；延迟合并 {deferred_count} 条；模型初筛 {screened} 条；制度新意审查 {len(audits)} 条；阻断原缺口 {len(blocked)} 条；输出 {len(candidates)} 个候选。", f"批处理决策：{batch_reason}；筛选结果缓存：{'命中' if cache_hit else '未命中'}；本次复用节省Token：{tokens_saved}。", "", "> 扩展层级：1=扩大选题内容，2=扩大北京信息来源，3=扩大到全国；自动候选不得直接用于报送。", ""]
        for c in candidates:
            lines += [f"## {c.id}｜{c.title}", "", f"- 得分：{c.score}；优先级：{c.priority}", f"- 时间与地域：{c.event_date}；{c.region}", f"- 事件概述：{c.summary}", f"- 可反证缺口：{c.gap_hypothesis}（{c.gap_type}）", f"- 制度覆盖审查：{c.coverage_status}；{c.novelty_decision}", f"- 制度矛盾：{c.institutional_conflict}", f"- 权限判断：{c.authority}", f"- 数据条件：{c.data_sufficiency}", f"- 历史关系：{c.history_relation}", f"- 风险：{c.risk}", f"- 结论：{c.recommendation}", f"- 来源：{c.score_reasons.get('来源名称', '')}；{c.score_reasons.get('来源URL', '')}", ""]
        if blocked:
            lines += ["## 被制度新意闸门阻断的原缺口", ""]
            for item in blocked:
                evidence = item.policy_matches[0] if item.policy_matches else {}
                lines += [f"- {item.title}：{item.gap_hypothesis}", f"  - 直接覆盖：{evidence.get('name', '')}；{evidence.get('source_url', '')}", f"  - 复核记录ID：{run_id}:{item.event_id}"]
        temp = path.with_suffix(".tmp")
        temp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(temp, path)
        return path
