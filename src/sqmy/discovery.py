from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import fcntl
import json
import os
from pathlib import Path
from difflib import SequenceMatcher
import re

from .collector import SourceCollector, infer_event_region
from .budget import BudgetExceeded
from .model_calls import CallLedger
from .history_match import mechanism_hints
from .config import Settings
from .db import now
from .discovery_shadow import ShadowReview, ShadowVerifier, write_discovery_evaluation
from .models import Phase, TaskStatus
from .models import EventItem
from .novelty import NoveltyAudit, NoveltyAuditor, write_rolling_evaluation
from .providers import ProviderError, QuotaExceeded, RateLimited, build_router
from .retrieval import collect_discovery
from .discovery_coverage import coverage_summary
from .screener import (
    MODEL_SCORE_KEYS,
    local_date,
    normalize_title,
    problem_priority,
    reported_text,
    rule_screen_with_decisions,
    score as event_score,
    to_candidate,
)
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
        self._budget_overrun: dict | None = None

    def run(
        self, *args, **kwargs,
    ) -> tuple[str, list]:
        # 本地单用户扫描串行执行，避免两个进程同时消费同一队列。
        lock_path = self.s.root / "data/runs/.discovery.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("已有扫描正在执行，请待其结束后恢复") from exc
            self._active_run_id = None
            try:
                return self._run(*args, **kwargs)
            except (Exception, KeyboardInterrupt) as exc:
                if self._active_run_id:
                    status = (TaskStatus.PAUSED_BUDGET if isinstance(exc, BudgetExceeded)
                              else TaskStatus.PAUSED_QUOTA if isinstance(exc, (QuotaExceeded, RateLimited))
                              else TaskStatus.FAILED)
                    self._checkpoint_interruption(self._active_run_id, status, type(exc).__name__)
                raise

    def _run(
        self,
        fixture: Path | None = None,
        *,
        clue_file: Path | None = None,
        force: bool = False,
        screen_now: bool = False,
        start_tier: int = 1,
        resume_run_id: str | None = None,
    ) -> tuple[str, list]:
        self._budget_overrun = None
        mode = "test_fixture" if fixture else "live"
        if resume_run_id:
            with self.wf.db.connect() as conn:
                row = conn.execute(
                    """SELECT r.phase,r.status,r.checkpoint_json,rc.mode,rc.forced
                       FROM runs r JOIN run_context rc ON rc.run_id=r.id WHERE r.id=?""",
                    (resume_run_id,),
                ).fetchone()
            if row is None:
                raise ValueError(f"未找到运行：{resume_run_id}")
            if row["phase"] != Phase.DISCOVERY:
                raise ValueError("只有发现阶段的运行可以由 scan --resume 继续")
            if row["status"] not in {
                TaskStatus.PENDING,
                TaskStatus.PAUSED_BUDGET,
                TaskStatus.PAUSED_QUOTA,
                TaskStatus.FAILED,
                TaskStatus.RUNNING,
            }:
                raise ValueError("该运行当前状态不允许恢复发现阶段")
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
            run_id = resume_run_id
            mode = row["mode"]
            force = bool(row["forced"])
            start_tier = int(checkpoint.get("start_tier", start_tier))
            screen_now = bool(checkpoint.get("screen_now", screen_now))
            if clue_file is None and checkpoint.get("clue_file"):
                clue_file = Path(checkpoint["clue_file"])
        else:
            run_id = self.wf.init_run(mode, forced=force)
        self._active_run_id = run_id
        if resume_run_id:
            # 独占锁已证明旧扫描进程不在运行。在途用量无法确认，保留占额而不归零。
            with self.wf.db.connect() as conn:
                conn.execute("UPDATE model_calls SET status='failed:interrupted_unknown' WHERE run_id=? AND status='running'", (run_id,))
            CallLedger(self.wf.db, self.s, run_id, "screening", "").export_audit()
            snapshot = self.s.root / "data/runs" / run_id / "scan_input.json"
            if snapshot.exists():
                state = json.loads(snapshot.read_text(encoding="utf-8"))
                for key in ("collected", "premodel_pool", "fresh_pool", "model_pool", "rule_results"):
                    state[key] = [EventItem(**item) for item in state[key]]
                for key in ("rule_exclusions", "history_exclusions", "pool_cap_exclusions"):
                    for item in state[key]:
                        item["event"] = EventItem(**item["event"])
                state["shadow_reviews"] = [ShadowReview(**item) for item in state["shadow_reviews"]]
                return self._finish_scan(run_id, **state)
        self.wf.db.checkpoint(
            run_id,
            phase=Phase.DISCOVERY,
            status=TaskStatus.RUNNING,
            data={
                "start_tier": start_tier,
                "screen_now": screen_now,
                "clue_file": str(clue_file) if clue_file else None,
                "resume_next": f"sqmy scan --resume {run_id}",
                "next": "collect_sources",
            },
        )
        collector = SourceCollector(self.s.root, self.s.raw)
        collected = collect_discovery(collector, self.s, self.wf.db, run_id, fixture=fixture, clue_file=clue_file)
        if fixture:
            max_tier = max((item.expansion_tier for item in collected), default=1)
        else:
            max_tier = max(
                (int(source.get("expansion_tier", 1)) for source in collector.sources),
                default=1,
            )
            max_tier = max(
                max_tier,
                max((item.expansion_tier for item in collected), default=1),
            )
        if clue_file:
            max_tier = max(max_tier, 4)
        if not 1 <= start_tier <= max_tier:
            raise ValueError(f"扩展起始层必须在1到{max_tier}之间")
        tier_stats = []
        premodel_pool, fresh_pool, repeated_excluded, expansion_tier = [], [], 0, 1
        rule_results: list[EventItem] = []
        rule_exclusions: list[dict] = []
        history_exclusions: list[dict] = []
        pool_cap_exclusions: list[dict] = []
        for tier in range(start_tier, max_tier + 1):
            tier_items = [item for item in collected if start_tier <= item.expansion_tier <= tier]
            prepared = self._prepare_tier_pool(
                tier_items, run_id, mode=mode, force=force
            )
            rule_results = prepared["rule_results"]
            rule_exclusions = prepared["rule_exclusions"]
            history_exclusions = prepared["history_exclusions"]
            repeated_excluded = len(history_exclusions)
            premodel_pool = prepared["premodel_pool"]
            pool_cap_exclusions = prepared["pool_cap_exclusions"]
            fresh_pool = premodel_pool
            expansion_tier = tier
            tier_stats.append({"tier": tier, "collected": len(tier_items), "rule_qualified": len(rule_results), "repeated_excluded": repeated_excluded, "fresh_available": prepared["fresh_available"], "model_pool": len(fresh_pool)})
            if (
                not self.s.section("discovery").get("scan_all_source_tiers", False)
                and len(fresh_pool) >= self.s.section("discovery")["screened_min"]
            ):
                break
        current_new_count = 0
        reopened_count = 0
        oldest_pending_at: str | None = None
        pending_before_count = len(premodel_pool)
        pool_quality: dict = {"checked": False, "ok": True, "reasons": []}
        if mode == "live":
            self._persist_events(run_id, rule_results)
            current_new_count, reopened_count = self._enqueue_events(
                run_id, prepared["fresh_results"]
            )
            pending_records, oldest_pending_at = self._pending_event_records()
            cfg = self.s.section("discovery")
            premodel_pool = self._select_pending_pool(
                pending_records,
                int(cfg["screened_max"]),
                max_per_source=int(cfg.get("premodel_max_per_source", cfg["screened_max"])),
                external_reserve=int(cfg.get("premodel_external_reserve", 0)),
                fresh_reserve=int(cfg.get("premodel_fresh_reserve", 0)),
                aged_reserve=int(cfg.get("premodel_aged_reserve", 0)),
                current_clue_ids={e.id for e in collected if e.source_id.startswith('clue_')},
            )
            fresh_pool = premodel_pool
            pending_before_count = len(pending_records)
            pool_quality = self._assess_model_pool(pending_records, premodel_pool)
        else:
            self._persist_events(run_id, premodel_pool)
        shadow_verifier: ShadowVerifier | None = None
        shadow_reviews = []
        shadow_report = ""
        if self.s.section("shadow_verification")["enabled"]:
            shadow_verifier = ShadowVerifier(self.s)
            shadow_reviews = shadow_verifier.review(
                run_id, premodel_pool, live_search=fixture is None
            )
            shadow_report = str(shadow_verifier.path_for(run_id))
        should_model, batch_reason = self._should_run_model(
            fresh_pool,
            force=force,
            screen_now=screen_now,
            oldest_pending_at=oldest_pending_at,
            quality_report=pool_quality if mode == "live" else None,
        )
        model_pool = fresh_pool if should_model else []
        deferred_count = (
            max(0, pending_before_count - len(model_pool))
            if mode == "live"
            else 0 if should_model else len(fresh_pool)
        )
        observability_path = self._persist_discovery_observability(
            run_id,
            collection_stats=collector.collection_stats,
            start_tier=start_tier,
            expansion_tier=expansion_tier,
            rule_results=rule_results,
            rule_exclusions=rule_exclusions,
            history_exclusions=history_exclusions,
            premodel_pool=premodel_pool,
            pool_cap_exclusions=pool_cap_exclusions,
            model_pool=model_pool,
            screened=[],
            candidates=[],
            model_exclusions=[],
        )
        self.wf.db.checkpoint(
            run_id,
            phase=Phase.DISCOVERY,
            status=TaskStatus.RUNNING,
            data={
                "start_tier": start_tier,
                "screen_now": screen_now,
                "clue_file": str(self.s.root / "data/runs" / run_id / "discovery_clues.jsonl") if clue_file else None,
                "expansion_tier": expansion_tier,
                "collected": len(collected),
                "premodel": len(premodel_pool),
                "repeated_excluded": repeated_excluded,
                "model_input": len(model_pool),
                "model_pool_quality": pool_quality,
                "shadow_reviewed": len(shadow_reviews),
                "shadow_report": shadow_report,
                "shadow_enforced": False,
                "discovery_observability": str(observability_path),
                "resume_next": f"sqmy scan --resume {run_id}",
                "next": "model_screening" if model_pool else "finalize_discovery",
            },
        )
        state = dict(
            mode=mode, force=force, fixture=str(fixture) if fixture else None,
            clue_file=str(clue_file) if clue_file else None,
            start_tier=start_tier, max_tier=max_tier, expansion_tier=expansion_tier,
            screen_now=screen_now, collected=collected, premodel_pool=premodel_pool,
            fresh_pool=fresh_pool, model_pool=model_pool, rule_results=rule_results,
            rule_exclusions=rule_exclusions, history_exclusions=history_exclusions,
            pool_cap_exclusions=pool_cap_exclusions, collection_stats=collector.collection_stats,
            shadow_reviews=shadow_reviews, shadow_report=shadow_report,
            deferred_count=deferred_count, repeated_excluded=repeated_excluded,
            current_new_count=current_new_count, reopened_count=reopened_count,
            pending_before_count=pending_before_count, batch_reason=batch_reason,
            tier_stats=tier_stats, pool_quality=pool_quality,
        )
        # 只固化已筛过的轻量元数据，不保存网页正文。先固化输入，再允许模型调用。
        snapshot = self.s.root / "data/runs" / run_id / "scan_input.json"
        self._atomic_json(snapshot, json.loads(json.dumps(state, ensure_ascii=False, default=asdict)))
        return self._finish_scan(run_id, **state)

    def _finish_scan(
        self, run_id, *, mode, force, fixture, clue_file, start_tier, max_tier,
        expansion_tier, screen_now, collected, premodel_pool, fresh_pool, model_pool,
        rule_results, rule_exclusions, history_exclusions, pool_cap_exclusions,
        collection_stats, shadow_reviews, shadow_report, deferred_count,
        repeated_excluded, current_new_count, reopened_count, pending_before_count,
        batch_reason, tier_stats, pool_quality,
    ):
        shadow_verifier = ShadowVerifier(self.s) if shadow_report else None
        try:
            screened, cache_hit, tokens_saved = self._model_rank(
                run_id, model_pool, use_cache=not force
            )
        except (BudgetExceeded, QuotaExceeded, RateLimited):
            # _model_rank has already written a resumable discovery checkpoint.
            # Do not continue into ranking or overwrite that paused state.
            return run_id, []
        audit_input = hashlib.sha256(json.dumps([
            dict(event=asdict(item), analysis=getattr(item, "model_analysis", {}))
            for item in screened
        ], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        audit_checkpoint = self.s.root / "data/runs" / run_id / "scan_audits.json"
        saved_audits = json.loads(audit_checkpoint.read_text(encoding="utf-8")) if audit_checkpoint.exists() else {}
        if saved_audits.get("input_hash") == audit_input:
            audits = [NoveltyAudit(**item) for item in saved_audits["audits"]]
        else:
            auditor = NoveltyAuditor(self.s)
            audits = auditor.audit(run_id, screened, live_search=fixture is None)
            self._atomic_json(audit_checkpoint, {"input_hash": audit_input, "audits": [asdict(item) for item in audits]})
        candidates = self._rank_candidates(screened, audits)
        self._apply_history(candidates)
        self._persist_candidates(run_id, candidates)
        if mode == "live" and model_pool:
            self._mark_queue_screened(run_id, model_pool)
            deferred_count = self._pending_queue_count()
        from .materials import compact_reposts
        model_pool, _ = compact_reposts(model_pool)
        model_selected_ids = {item.id for item in screened}
        model_exclusions = [
            {"event": item, "reason_code": "model_not_selected"}
            for item in model_pool
            if item.id not in model_selected_ids
        ]
        if shadow_verifier:
            shadow_verifier.update_outcomes(run_id, screened, candidates, audits)
        observability_path = self._persist_discovery_observability(
            run_id,
            collection_stats=collection_stats,
            start_tier=start_tier,
            expansion_tier=expansion_tier,
            rule_results=rule_results,
            rule_exclusions=rule_exclusions,
            history_exclusions=history_exclusions,
            premodel_pool=premodel_pool,
            pool_cap_exclusions=pool_cap_exclusions,
            model_pool=model_pool,
            screened=screened,
            candidates=candidates,
            model_exclusions=model_exclusions,
        )
        self._record_efficiency(
            run_id, premodel_count=len(premodel_pool), repeated_excluded=repeated_excluded,
            model_input_count=len(model_pool), cache_hit=cache_hit,
            tokens_saved=tokens_saved, deferred_count=deferred_count,
            new_event_count=current_new_count, reopened_event_count=reopened_count,
            pending_before_count=pending_before_count,
            expansion_tier=expansion_tier, candidate_count=len(candidates),
        )
        report = self._report(
            run_id, candidates, audits, len(collected), len(screened), len(premodel_pool),
            repeated_excluded, cache_hit, tokens_saved, deferred_count, batch_reason, start_tier, expansion_tier,
            current_new_count=current_new_count,
            reopened_count=reopened_count,
            pending_before_count=pending_before_count,
            shadow_reviews=shadow_reviews,
            pool_quality=pool_quality,
        )
        metrics_path = write_rolling_evaluation(self.s)
        discovery_metrics_path = write_discovery_evaluation(self.s)
        exhausted_without_candidates = (
            not candidates
            and not deferred_count
            and expansion_tier == max_tier
        )
        if candidates:
            next_action = f"sqmy select {run_id} C1"
        elif batch_reason == "insufficient_pool_quality":
            next_action = "补充高质量公开线索后运行 sqmy scan --clues PATH --screen-now"
        elif deferred_count:
            next_action = "sqmy scan"
        elif exhausted_without_candidates:
            next_action = "none"
        else:
            next_action = f"sqmy scan --start-tier {expansion_tier + 1}"

        checkpoint = {
            "collected": len(collected),
            "start_tier": start_tier,
            "screen_now": screen_now,
            "clue_file": str(self.s.root / "data/runs" / run_id / "discovery_clues.jsonl") if clue_file else None,
            "premodel": len(premodel_pool),
            "repeated_excluded": repeated_excluded,
            "fresh_events": len(fresh_pool),
            "new_events": current_new_count,
            "reopened_events": reopened_count,
            "pending_before": pending_before_count,
            "model_input": len(model_pool),
            "deferred_count": deferred_count,
            "batch_reason": batch_reason,
            "model_pool_quality": pool_quality,
            "expansion_tier": expansion_tier,
            "tier_stats": tier_stats,
            "screening_cache_hit": cache_hit,
            "screened": len(screened),
            "novelty_audited": len(audits),
            "novelty_blocked": sum(a.decision == "block_original_gap" for a in audits),
            "candidates": len(candidates),
            "report": str(report),
            "rolling_metrics": str(metrics_path),
            "discovery_observability": str(observability_path),
            "discovery_evaluation": str(discovery_metrics_path),
            "shadow_reviewed": len(shadow_reviews),
            "shadow_report": shadow_report,
            "shadow_enforced": False,
            "next": next_action,
        }
        if exhausted_without_candidates:
            checkpoint["skip_reason"] = "已完成全部可用扩展层扫描，未发现通过事实、权限边界和制度新意门槛的候选。"
        status = TaskStatus.NEEDS_REVIEW if candidates else TaskStatus.SKIPPED
        if self._budget_overrun:
            checkpoint["budget_overrun"] = self._budget_overrun
            checkpoint["budget_action_required"] = (
                "当前有界行为已完成并保存；重新执行或扩大该行为前，"
                "应先复盘并调整对应的单行为额度。"
            )
        self.wf.db.checkpoint(
            run_id,
            phase=Phase.SELECTION,
            status=status,
            data=checkpoint,
            error="；".join(self._budget_overrun["reasons"]) if self._budget_overrun else None,
        )
        return run_id, candidates

    def _rank_candidates(self, screened: list[EventItem], audits: list) -> list:
        audit_by_id = {item.event_id: item for item in audits}
        eligible = [
            item for item in screened
            if audit_by_id.get(item.id) is None
            or audit_by_id[item.id].decision != "block_original_gap"
        ]
        model_rank = {item.id: index for index, item in enumerate(screened)}
        scored_candidates = []
        for item in eligible:
            audit = audit_by_id.get(item.id)
            penalty = self.s.section("novelty")["likely_covered_score_penalty"] if audit and audit.coverage_status == "likely_covered" else 0
            candidate = to_candidate(
                item,
                0,
                self.s.section("scoring"),
                self.s.section("penalties"),
                audit,
                penalty,
            )
            scored_candidates.append((candidate, model_rank[item.id], item.published_at))
        scored_candidates.sort(key=lambda row: (row[0].score, -row[1], row[2]), reverse=True)
        candidates = []
        for index, (candidate, _, _) in enumerate(
            scored_candidates[: self.s.section("project")["candidate_count"]], 1
        ):
            candidate.id = f"C{index}"
            candidates.append(candidate)
        return candidates

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
            material=json.loads(row["material_json"]),
        ) for row in rows]
        for event in events:
            event.rule_score = event_score(event)
        data = json.loads(source_path.read_text(encoding="utf-8"))
        screened = self._attach_analyses(events, data)
        run_id = self.wf.init_run("replay")
        self._persist_events(run_id, events)
        audits = NoveltyAuditor(self.s).audit(run_id, screened, live_search=False)
        candidates = self._rank_candidates(screened, audits)
        self._apply_history(candidates)
        self._persist_candidates(run_id, candidates)
        self._record_efficiency(
            run_id, premodel_count=len(events), repeated_excluded=0, model_input_count=0,
            cache_hit=True, tokens_saved=int(tokens), deferred_count=0,
            new_event_count=0, reopened_event_count=0, pending_before_count=0,
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
                """SELECT t.title, t.id AS ref, NULL AS data_json FROM topics t
                   LEFT JOIN run_context tc ON tc.run_id=t.run_id
                   WHERE tc.mode='live' OR t.run_id IS NULL
                   UNION ALL
                   SELECT c.title, c.run_id AS ref, c.data_json
                   FROM candidates c JOIN run_context rc ON rc.run_id=c.run_id
                   WHERE rc.mode='live'
                   ORDER BY title"""
            ).fetchall()
        history = [(row[0], row[1], json.loads(row[2]) if row[2] else None) for row in rows]
        mechanisms = self._approved_history()
        for candidate in candidates:
            hints = mechanism_hints(candidate.title + candidate.summary + candidate.institutional_conflict
                                    + candidate.policy_entry, mechanisms, self.s.section('history_review'))
            if hints:
                candidate.score_reasons['机制历史复核'] = hints
                candidate.history_relation = '机制重叠待人工复核：' + '；'.join(x['title'] for x in hints) + '（不自动排除，须比较新事实和新切口）'
                continue
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

    def _approved_history(self):
        with self.wf.db.connect() as conn:
            return conn.execute("""SELECT t.* FROM topics t
                LEFT JOIN run_context rc ON rc.run_id=t.run_id
                WHERE t.approval_status='approved' AND (rc.mode='live' OR t.run_id IS NULL)
                ORDER BY t.created_at DESC""").fetchall()

    def _exclude_unchanged(self, events: list, run_id: str, mode: str) -> tuple[list, int]:
        fresh, excluded = self._partition_unchanged(events, run_id, mode)
        return fresh, len(excluded)

    def _prepare_tier_pool(
        self,
        tier_items: list[EventItem],
        run_id: str,
        *,
        mode: str,
        force: bool,
    ) -> dict:
        """先排除已评估历史事件，再应用规初池和模型池容量上限。"""
        cfg = self.s.section("discovery")
        uncapped_cfg = dict(cfg)
        uncapped_cfg["initial_max"] = max(len(tier_items), int(cfg["initial_max"]))
        rule_results, rule_exclusions = rule_screen_with_decisions(
            tier_items, uncapped_cfg
        )
        if force:
            fresh_results, history_exclusions = rule_results, []
        else:
            fresh_results, history_exclusions = self._partition_unchanged(
                rule_results, run_id, mode
            )

        initial_max = int(cfg["initial_max"])
        clue_reserve = int(cfg.get("premodel_clue_reserve", 0))
        max_per_source = int(cfg.get("premodel_max_per_source", initial_max))
        initial_pool = self._select_diverse_pool(
            fresh_results,
            initial_max,
            clue_reserve=clue_reserve,
            max_per_source=max(1, max_per_source * 2),
        )
        initial_ids = {item.id for item in initial_pool}
        rule_exclusions += [
            {"event": item, "reason_code": "rule_rank_cap"}
            for item in fresh_results
            if item.id not in initial_ids
        ]
        screened_max = int(cfg["screened_max"])
        premodel_pool = self._select_diverse_pool(
            initial_pool,
            screened_max,
            clue_reserve=clue_reserve,
            max_per_source=max_per_source,
        )
        premodel_ids = {item.id for item in premodel_pool}
        pool_cap_exclusions = [
            {"event": item, "reason_code": "premodel_pool_cap"}
            for item in initial_pool
            if item.id not in premodel_ids
        ]
        return {
            "rule_results": rule_results,
            "rule_exclusions": rule_exclusions,
            "history_exclusions": history_exclusions,
            "fresh_results": fresh_results,
            "fresh_available": len(fresh_results),
            "premodel_pool": premodel_pool,
            "pool_cap_exclusions": pool_cap_exclusions,
        }

    @staticmethod
    def _select_diverse_pool(
        items: list[EventItem],
        limit: int,
        *,
        clue_reserve: int,
        max_per_source: int,
    ) -> list[EventItem]:
        """优先权威来源，同时给三级线索保留少量、可配置的核验名额。"""
        if limit <= 0:
            return []
        clues = [item for item in items if item.source_level >= 3]
        stronger = [item for item in items if item.source_level < 3]
        reserved = min(max(0, clue_reserve), len(clues), limit)
        selected: list[EventItem] = []
        counts: Counter[str] = Counter()

        def take(pool: list[EventItem], quota: int) -> None:
            for item in pool:
                if len(selected) >= quota:
                    break
                if item in selected or counts[item.source_id] >= max_per_source:
                    continue
                selected.append(item)
                counts[item.source_id] += 1

        take(stronger, limit - reserved)
        clue_target = min(limit, len(selected) + reserved)
        take(clues, clue_target)
        take(items, limit)
        if len(selected) < min(limit, len(items)):
            for item in items:
                if item not in selected:
                    selected.append(item)
                    if len(selected) >= limit:
                        break
        selected_ids = {item.id for item in selected}
        return [item for item in items if item.id in selected_ids][:limit]

    def _partition_unchanged(
        self, events: list, run_id: str, mode: str
    ) -> tuple[list, list[dict]]:
        if mode != "live":
            return events, []
        with self.wf.db.connect() as conn:
            rows = conn.execute(
                """SELECT e.url,e.title,e.summary,e.published_at,e.content_hash
                   FROM event_items e JOIN run_context rc ON rc.run_id=e.run_id
                   LEFT JOIN run_efficiency reff ON reff.run_id=e.run_id
                   WHERE rc.mode='live' AND e.run_id<>?
                     AND (reff.run_id IS NULL OR reff.model_input_count>0)""",
                (run_id,),
            ).fetchall()
        previous_by_url: dict[str, list] = {}
        for row in rows:
            previous_by_url.setdefault(row["url"], []).append(row)
        fresh = []
        excluded: list[dict] = []
        for event in events:
            content_hash = self._event_content_hash(event)
            same_url_rows = previous_by_url.get(event.url, [])
            normalized_title = re.sub(r"\s+", " ", event.title).strip()
            normalized_summary = re.sub(r"\s+", " ", event.summary).strip()
            exact = any(
                row["content_hash"] == content_hash
                or (
                    re.sub(r"\s+", " ", row["title"] or "").strip() == normalized_title
                    and re.sub(r"\s+", " ", row["summary"] or "").strip()
                    == normalized_summary
                )
                for row in same_url_rows
            )
            # 同一URL的标题或摘要发生实质变化时重新开放；跨URL同源转载仍排除。
            syndicated = (
                any(same_origin_event(event, row) for row in rows)
                if not exact and not same_url_rows
                else False
            )
            if not exact and not syndicated:
                fresh.append(event)
            else:
                excluded.append({
                    "event": event,
                    "reason_code": "history_exact" if exact else "history_same_origin",
                })
        return fresh, excluded

    def _should_run_model(
        self,
        events: list,
        *,
        force: bool,
        screen_now: bool = False,
        oldest_pending_at: str | None = None,
        quality_report: dict | None = None,
    ) -> tuple[bool, str]:
        if force:
            return bool(events), "forced"
        if not events:
            return False, "no_new_events"
        cfg = self.s.section("discovery")
        urgent = any(
            event.rule_score >= cfg["urgent_rule_score_threshold"]
            and any(word in reported_text(event) for word in cfg["urgent_keywords"])
            for event in events
        )
        if urgent:
            return True, "urgent_exception"
        if quality_report is not None and not quality_report.get("ok", False):
            return False, "insufficient_pool_quality"
        if screen_now:
            return True, "manual_screen_now"
        if len(events) >= cfg["min_new_events_for_model"]:
            return True, "minimum_batch_reached"
        if events and oldest_pending_at:
            try:
                oldest = datetime.fromisoformat(oldest_pending_at)
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=timezone.utc)
                max_wait = timedelta(hours=int(cfg["pending_batch_max_wait_hours"]))
                if datetime.now(timezone.utc) - oldest.astimezone(timezone.utc) >= max_wait:
                    return True, "pending_age_limit_reached"
            except ValueError:
                pass
        return False, "deferred_small_batch"

    def _model_rank(self, run_id: str, events: list, *, use_cache: bool = True) -> tuple[list, bool, int]:
        from .materials import compact_reposts, split_discovery_summary
        from .candidate_eligibility import CONTRACT, SCHEMA as ELIGIBILITY_SCHEMA, attach_shadow
        model_cfg = self.s.section("model")
        limit = self.s.section("discovery")["screened_max"]
        pool = events[:limit]
        if not pool:
            return [], False, 0
        if model_cfg["provider"] == "mock":
            return events, False, 0
        pool, merged = compact_reposts(pool)
        pool = sorted(pool, key=lambda x: (problem_priority(x, self.s.section('discovery')), x.rule_score), reverse=True)
        material_path = self.s.root / 'data/runs' / run_id / 'screening_materials.json'
        material_path.parent.mkdir(parents=True, exist_ok=True)
        # 旧运行恢复沿用原提示契约，避免已完成调用因升级而失去缓存。
        frozen_material = json.loads(material_path.read_text(encoding='utf-8')) if material_path.exists() else None
        mode = self.s.raw.get('candidate_eligibility', {}).get('mode', 'off')
        if mode not in {'off', 'shadow'}:
            raise ValueError('candidate_eligibility 仅支持 off/shadow；正式排序切换尚未验收')
        input_contract = (frozen_material.get('input_contract', 'legacy') if frozen_material
                          else CONTRACT if mode == 'shadow' else 'fact_first_v1')
        if input_contract not in {'legacy', 'fact_first_v1', CONTRACT}:
            raise ValueError('未知初筛输入契约，不能按旧提示恢复')
        compact = [
            {
                "id": x.id,
                "title": x.title,
                "date": local_date(x.published_at),
                "region": x.region,
                "source_name": x.source_name,
                "source_level": x.source_level,
                "source_region_hint": x.source_region,
                "region_evidence": x.region_evidence,
                "expansion_tier": x.expansion_tier,
                "summary": x.summary[:350],
                "rule_score": x.rule_score,
                "same_origin_ids": merged.get(x.id, []),
                "material": {key: value for key, value in x.material.items() if value and key in {
                    "role_hint", "date_status", "event_at", "updated_at", "promotion_suspected",
                    "original_source_hint", "repost_group_hint",
                }},
            }
            for x in pool
        ]
        history = [] if input_contract == CONTRACT and frozen_material and 'prompt' in frozen_material else self._approved_history()
        for item in compact:
            item['history_review_hints'] = mechanism_hints(item['title'] + item['summary'], history, self.s.section('history_review'))
        if input_contract in {'fact_first_v1', CONTRACT}:
            for item in compact:
                item.update(split_discovery_summary(item.pop('summary')))
        material = {
            'frozen_event_ids': [x.id for x in events[:limit]],
            'model_event_ids': [x.id for x in pool], 'merged_reposts': merged,
            'input_contract': input_contract, 'model_input': compact,
            'note': '仅整理冻结输入；不补题、不改原事件；分隔或合并不代表事实核验或政策覆盖判断',
        }
        prompt = (
            "你是社情民意选题初筛员。仅依据以下标题、摘要和元数据进行低成本判断，不得补造事实。"
            "source_name用于判断来源属性；source_region_hint只是检索频道提示，"
            "material为零模型用途提示，不是已核验结论或强制排除指令。event_at、published_at、updated_at不可混为发生时间；"
            "缺日期保留为待核线索，不编造日期。policy_reference用于现行规则反证，不单凭旧政策当新事件；"
            "promotion_suspected须结合内容核查；repost_group_hint相同不算独立证据，不同也不证明独立。"
            "国际信息只有明确涉及中国群体、企业或可比较的中国制度问题才保留，不凭中文检索词虚构中国关联。"
            "region_evidence为source_channel_only时不得将其写成事件发生地，但可保留为需要核验的地方线索。"
            "海淀和北京题优先，但不是准入条件。全国范围涉及国计民生、明确受影响群体、"
            "可验证制度缺口且存在有权执行主体、地方试点可能或向上反映路径的议题可直接保留。"
            "不得仅因没有北京落点而排除全国题，也不得因全国题缺少北京落点而使用no_local_landing扣分。"
            "政策已经完整覆盖、纯会议活动、企业软文、机构推广、单一宣传信息应排除。"
            "source_level=3只是投诉、论坛或社交平台线索：只有具体、可重复的痛点且能指向高等级核验路径时才保留，"
            "不得把单方陈述当作已证实事实。宁可少于5题，不得凑数。"
            "每个入选题均须根据现有元数据给出克制的初步分析；证据不足必须直说。"
            "history_review_hints来自已审核稿的群体和机制重叠匹配，不是自动排除指令；"
            "须比较是否有新事实、新群体或不同建议机制，只有重复而无新增价值才不入选，保留时在recommendation说明差异。"
            "只返回有价值备选，供制度新意审查后再取最终5个；不必填满selections。"
            f"每题全部解释字段合计控制在{model_cfg['screening_item_chars']}字以内，每字段一句，避免重复背景和逐项长论证。"
            "只输出Schema要求的JSON，不输出工作过程。same_origin_ids只表示已合并的转述，不新增独立证据。"
            "每题必须把拟议制度缺口写成一句可被反证的gap_hypothesis，"
            "gap_type只能是policy_absence、implementation_gap、coordination_gap、effectiveness_gap、accountability_gap或unclear；"
            "counter_queries提供两条优先查政府、法院或监管部门的精确反证检索词。"
            "score_components按给定上限独立打分；海淀、北京地域分和时效分由程序计算，"
            "其中beijing_relevance仅表示北京相关性；全国题该项可为0，不代表公共价值为0。"
            "applied_penalties只列元数据已有依据的扣分，不得为凑低分而猜测。"
            f"正向评分上限：{json.dumps(self.s.section('scoring'), ensure_ascii=False)}；"
            f"扣分配置：{json.dumps(self.s.section('penalties'), ensure_ascii=False)}。"
        )
        if input_contract in {'fact_first_v1', CONTRACT}:
            prompt += (
                "输入解释：reported_excerpt只是发现材料陈述，不等于已验证事实，可能仍有单方说法或未标注的推断；"
                "upstream_hypotheses是显式分隔的上游缺口、原因或切口设想，不得继承为事实。标题也须核验。"
                "没有分隔标记不代表陈述可靠；只读到了摘要不得声称已读原文。保留办理结果、各方分歧及时间限制。"
                "先从材料中具体可核验的问题形成命题，suggested_title用群体、场景和中性研究问题表达，未证实的原因或缺口留在gap_hypothesis。"
                "研究路径包括具体事件与执行问题、政策衔接与适用边界、前瞻性风险。"
                "后两类须有具体规则变化或接口、受影响对象及可反证机制，不必虚构已发生损害；缺口尚不明确时gap_type用unclear。"
                "不能仅凭热点或政策发布生成缺机制建议；已有政策不等于执行有效，投诉不等于机制失效，个案办结也不单独否定事前预防价值。"
                "仅有政策标题或摘要不得认定完整覆盖，须核对适用对象、权限与机制是否对应。"
                "data_assessment交代材料依据与决定性未知，recommendation交代研究价值与可信核验路径；无法从输入判断就写待核。"
                "决定性未知须区分问题真实性、权限与证据路径等前提，以及前提成立后留给深研的边界细化、成本和效果问题。"
                "不得为获得候选而将前提改为深研任务；本次只初筛，不表示预研通过。"
            )
        if input_contract == CONTRACT:
            prompt += (
                "candidate_shadow_v1：在同一调用给出eligibility影子意见，不按此字段改变原选题范围或评分。"
                "eligible仅表示具体问题、公共价值、权限路径和可信核验入口值得有限投入；不是事实已核验。"
                "needs_evidence表示关键研究前提尚待证；reject须有依据说明该版本命题不值得继续。"
                "元数据缺失不是反证。reason写判断依据；decisive_unknown写决定性未知或已被解决的前提；"
                "verification_entry写具体核验入口，确实无法判断写待核；这些字段计入原有每题字数上限。"
            )
        prompt += "\n" + json.dumps(compact, ensure_ascii=False)
        scoring_cfg = self.s.section("scoring")
        penalty_keys = list(self.s.section("penalties"))
        score_properties = {
            key: {"type": "integer", "minimum": 0, "maximum": int(scoring_cfg[key])}
            for key in MODEL_SCORE_KEYS
        }
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
            "score_components": {
                "type": "object",
                "additionalProperties": False,
                "properties": score_properties,
                "required": list(score_properties),
            },
            "applied_penalties": {
                "type": "array",
                "maxItems": len(penalty_keys),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "key": {"type": "string", "enum": penalty_keys},
                        "reason": {"type": "string"},
                    },
                    "required": ["key", "reason"],
                },
            },
        }
        if input_contract == CONTRACT:
            selection_properties['eligibility'] = ELIGIBILITY_SCHEMA
        schema = {"type": "object", "additionalProperties": False, "properties": {
            "selections": {"type": "array", "minItems": 0, "maxItems": self.s.section("novelty")["audit_pool_size"], "items": {
                "type": "object", "additionalProperties": False, "properties": selection_properties,
                "required": list(selection_properties),
            }}}, "required": ["selections"]}
        if input_contract == CONTRACT:
            if frozen_material and 'prompt' in frozen_material:
                material = frozen_material
                prompt, schema, model_cfg = material['prompt'], material['schema'], material['model_config']
                if material['model_event_ids'] != [x.id for x in pool]:
                    raise ValueError('恢复模型输入与冻结事件不一致，不能混入新线索')
                if hashlib.sha256(prompt.encode()).hexdigest() != material['prompt_sha256']:
                    raise ValueError('冻结提示哈希不匹配')
            else:
                material.update(prompt=prompt, schema=schema, model_config=model_cfg,
                                frozen_at=now(), prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
        self._atomic_json(material_path, material)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        attach = (lambda items, data: attach_shadow(items, data, material, self._attach_analyses)) if input_contract == CONTRACT else self._attach_analyses
        with self.wf.db.connect() as conn:
            cached_rows = conn.execute(
                """SELECT result_json,token_used,run_id FROM tasks
                   WHERE kind='model_screening' AND input_hash=? AND status=?
                   AND (run_id=? OR run_id IN (
                     SELECT run_id FROM run_context WHERE mode=(SELECT mode FROM run_context WHERE run_id=?)
                   ))
                   ORDER BY updated_at DESC""",
                (prompt_hash, TaskStatus.COMPLETED, run_id, run_id),
            ).fetchall()
        if cached_rows:
            for cached in cached_rows:
                if not use_cache and cached["run_id"] != run_id:
                    continue
                if not cached["result_json"]:
                    continue
                try:
                    data = json.loads(cached["result_json"])
                    screened = attach(pool, data)
                except (ProviderError, json.JSONDecodeError):
                    continue
                self._write_screening_audit(run_id, data)
                self._restore_budget_overrun(run_id)
                return screened, True, int(cached["token_used"])
        router = build_router(
            self.s.root, model_cfg,
            codex_model=model_cfg.get("screening_model", model_cfg.get("codex_model", "")),
        )
        if router is None:
            return events, False, 0
        ledger = CallLedger(
            self.wf.db, self.s, run_id, "screening", prompt_hash,
            validate=lambda data: attach(pool, data),
            task_kind="model_screening",
        )
        try:
            result, fallback_reason = router.analyze(prompt, schema, attempt=ledger.invoke)
        except BudgetExceeded as exc:
            self._checkpoint_interruption(run_id, TaskStatus.PAUSED_BUDGET, str(exc))
            raise
        except (QuotaExceeded, RateLimited) as exc:
            self._checkpoint_interruption(run_id, TaskStatus.PAUSED_QUOTA, str(exc))
            raise
        except ProviderError as exc:
            self._checkpoint_interruption(run_id, TaskStatus.FAILED, str(exc))
            raise
        self._budget_overrun = ledger.overrun
        screened = attach(pool, result.data)
        self._write_screening_audit(run_id, result.data)
        return screened, False, 0

    def _restore_budget_overrun(self, run_id: str) -> None:
        with self.wf.db.connect() as conn:
            call = conn.execute("SELECT * FROM model_calls WHERE run_id=? AND task_id='screening' AND over_budget=1 ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
        if call:
            self._budget_overrun = {
                "stage": "screening", "estimated_tokens": call["estimated_tokens"],
                "actual_tokens": call["input_tokens"] + call["output_tokens"],
                "action_limit": call["stage_limit"], "call_id": call["id"],
                "reasons": ["本运行先前调用已记录Token或费用超额；恢复只复用已完成结果，不代表已提高额度"],
            }

    def _checkpoint_interruption(self, run_id: str, status: TaskStatus, error: str) -> None:
        with self.wf.db.connect() as conn:
            row = conn.execute("SELECT checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()
        data = json.loads(row["checkpoint_json"] or "{}") if row else {}
        data["resume_next"] = f"sqmy scan --resume {run_id}"
        data["next"] = f"sqmy retry {run_id}" if status == TaskStatus.FAILED else f"sqmy resume {run_id}"
        self.wf.db.checkpoint(
            run_id,
            phase=Phase.DISCOVERY,
            status=status,
            data=data,
            error=error,
        )

    def _write_screening_audit(self, run_id: str, data: dict) -> None:
        audit_path = self.s.root / "data/runs" / run_id / "model_screening_result.json"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = audit_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, audit_path)

    def _persist_discovery_observability(
        self,
        run_id: str,
        *,
        collection_stats: list[dict],
        start_tier: int,
        expansion_tier: int,
        rule_results: list[EventItem],
        rule_exclusions: list[dict],
        history_exclusions: list[dict],
        premodel_pool: list[EventItem],
        pool_cap_exclusions: list[dict],
        model_pool: list[EventItem],
        screened: list[EventItem],
        candidates: list,
        model_exclusions: list[dict],
    ) -> Path:
        path = self.s.root / "data/runs" / run_id / "discovery_observability.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self.s.section("observability")["enabled"]:
            self._atomic_json(path, {"enabled": False, "run_id": run_id})
            return path

        grouped: dict[str, dict] = {}
        for item in collection_stats:
            source_id = item["source_id"]
            row = grouped.setdefault(source_id, {
                "source_id": source_id,
                "source_name": item["source_name"],
                "expansion_tier": int(item["expansion_tier"]),
                "fetched_count": 0,
                "within_window_count": 0,
                "collected_count": 0,
                "invalid_metadata_count": 0,
                "outside_window_count": 0,
                "fetch_errors": [],
                "parse_errors": [],
            })
            for key in (
                "fetched_count", "within_window_count", "collected_count",
                "invalid_metadata_count", "outside_window_count",
            ):
                row[key] += int(item.get(key, 0))
            if item.get("fetch_error"):
                row["fetch_errors"].append(str(item["fetch_error"]))
            if item.get("parse_error"):
                row["parse_errors"].append(str(item["parse_error"]))

        all_events = (
            list(rule_results)
            + [item["event"] for item in rule_exclusions]
            + [item["event"] for item in history_exclusions]
            + list(premodel_pool)
            + list(model_pool)
            + list(screened)
        )
        for event in all_events:
            grouped.setdefault(event.source_id, {
                "source_id": event.source_id,
                "source_name": event.source_name,
                "expansion_tier": int(event.expansion_tier),
                "fetched_count": 0,
                "within_window_count": 0,
                "collected_count": 0,
                "invalid_metadata_count": 0,
                "outside_window_count": 0,
                "fetch_errors": [],
                "parse_errors": [],
            })

        def counts(events: list[EventItem]) -> Counter:
            return Counter(item.source_id for item in events)

        rule_cap = [
            item["event"] for item in rule_exclusions
            if item["reason_code"] == "rule_rank_cap"
        ]
        rule_rejected = [
            item["event"] for item in rule_exclusions
            if item["reason_code"] != "rule_rank_cap"
        ]
        by_rule_qualified = counts(rule_results)
        by_rule_rejected = counts(rule_rejected)
        by_rule_cap = counts(rule_cap)
        by_history = counts([item["event"] for item in history_exclusions])
        by_premodel = counts(premodel_pool)
        by_pool_cap = counts([item["event"] for item in pool_cap_exclusions])
        by_model = counts(model_pool)
        by_screened = counts(screened)
        source_by_event = {item.id: item.source_id for item in all_events}
        by_candidate = Counter(
            source_by_event.get(candidate.score_reasons.get("事件ID"), "")
            for candidate in candidates
        )
        by_candidate.pop("", None)

        rows = []
        stamp = now()
        with self.wf.db.connect() as conn:
            for source_id, item in sorted(grouped.items()):
                row = {
                    "run_id": run_id,
                    "source_id": source_id,
                    "source_name": item["source_name"],
                    "expansion_tier": item["expansion_tier"],
                    "included_in_scan": int(
                        start_tier <= item["expansion_tier"] <= expansion_tier
                    ),
                    "raw_item_count": item["fetched_count"],
                    "within_window_count": item["within_window_count"],
                    "collected_count": item["collected_count"],
                    "invalid_metadata_count": item["invalid_metadata_count"],
                    "outside_window_count": item["outside_window_count"],
                    "rule_qualified_count": by_rule_qualified[source_id],
                    "rule_excluded_count": by_rule_rejected[source_id],
                    "rule_cap_excluded_count": by_rule_cap[source_id],
                    "history_excluded_count": by_history[source_id],
                    "premodel_count": by_premodel[source_id],
                    "pool_cap_excluded_count": by_pool_cap[source_id],
                    "model_input_count": by_model[source_id],
                    "model_selected_count": by_screened[source_id],
                    "candidate_count": by_candidate[source_id],
                    "fetch_error": "；".join(item["fetch_errors"]) or None,
                    "parse_error": "；".join(item["parse_errors"]) or None,
                    "updated_at": stamp,
                }
                rows.append(row)
                conn.execute(
                    """INSERT INTO source_funnel(
                         run_id,source_id,source_name,expansion_tier,included_in_scan,
                         raw_item_count,within_window_count,collected_count,
                         invalid_metadata_count,outside_window_count,
                         rule_qualified_count,rule_excluded_count,rule_cap_excluded_count,
                         history_excluded_count,premodel_count,pool_cap_excluded_count,
                         model_input_count,model_selected_count,candidate_count,
                         fetch_error,parse_error,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,source_id) DO UPDATE SET
                         source_name=excluded.source_name,
                         expansion_tier=excluded.expansion_tier,
                         included_in_scan=excluded.included_in_scan,
                         raw_item_count=excluded.raw_item_count,
                         within_window_count=excluded.within_window_count,
                         collected_count=excluded.collected_count,
                         invalid_metadata_count=excluded.invalid_metadata_count,
                         outside_window_count=excluded.outside_window_count,
                         rule_qualified_count=excluded.rule_qualified_count,
                         rule_excluded_count=excluded.rule_excluded_count,
                         rule_cap_excluded_count=excluded.rule_cap_excluded_count,
                         history_excluded_count=excluded.history_excluded_count,
                         premodel_count=excluded.premodel_count,
                         pool_cap_excluded_count=excluded.pool_cap_excluded_count,
                         model_input_count=excluded.model_input_count,
                         model_selected_count=excluded.model_selected_count,
                         candidate_count=excluded.candidate_count,
                         fetch_error=excluded.fetch_error,parse_error=excluded.parse_error,
                         updated_at=excluded.updated_at""",
                    tuple(row.values()),
                )

        stage_groups = {
            "rule_screen": [
                item for item in rule_exclusions
                if item["reason_code"] != "rule_rank_cap"
            ],
            "rule_rank_cap": [
                item for item in rule_exclusions
                if item["reason_code"] == "rule_rank_cap"
            ],
            "history_dedup": history_exclusions,
            "premodel_cap": pool_cap_exclusions,
            "model_screening": model_exclusions,
        }
        self._persist_exclusion_samples(run_id, stage_groups)
        with self.wf.db.connect() as conn:
            sample_rows = conn.execute(
                """SELECT id,source_id,stage,reason_code,title,url,sample_rank
                   FROM discovery_exclusion_samples WHERE run_id=?
                   ORDER BY stage,sample_rank""",
                (run_id,),
            ).fetchall()
        payload = {
            "run_id": run_id,
            "start_tier": start_tier,
            "expansion_tier": expansion_tier,
            "source_funnel": rows,
            "coverage_dimensions": {
                "rule_qualified": coverage_summary(rule_results, collection_stats),
                "model_input": coverage_summary(model_pool, []),
            },
            "exclusion_counts": {
                stage: len(items) for stage, items in stage_groups.items()
            },
            "exclusion_samples": [dict(row) for row in sample_rows],
            "notice": "误杀样本仅保存少量标题和URL元数据，不进入历史选题去重。",
        }
        self._atomic_json(path, payload)
        return path

    def _persist_exclusion_samples(
        self, run_id: str, stage_groups: dict[str, list[dict]]
    ) -> None:
        limit = int(self.s.section("observability")["exclusion_sample_per_stage"])
        if limit < 0:
            raise ValueError("每阶段排除样本数不得为负数")
        stamp = now()
        with self.wf.db.connect() as conn:
            for stage, items in stage_groups.items():
                ranked = sorted(
                    items,
                    key=lambda item: hashlib.sha256(
                        f"{stage}:{item['event'].id}".encode()
                    ).hexdigest(),
                )[:limit]
                for rank, item in enumerate(ranked, 1):
                    event = item["event"]
                    event_hash = hashlib.sha256(
                        f"{event.title}\n{event.url}".encode()
                    ).hexdigest()
                    sample_id = hashlib.sha256(
                        f"{run_id}:{stage}:{event_hash}".encode()
                    ).hexdigest()[:24]
                    conn.execute(
                        """INSERT INTO discovery_exclusion_samples(
                             id,run_id,event_hash,source_id,stage,reason_code,title,url,
                             published_at,region,rule_score,sample_rank,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(run_id,stage,event_hash) DO UPDATE SET
                             source_id=excluded.source_id,reason_code=excluded.reason_code,
                             title=excluded.title,url=excluded.url,
                             published_at=excluded.published_at,region=excluded.region,
                             rule_score=excluded.rule_score,sample_rank=excluded.sample_rank""",
                        (
                            sample_id, run_id, event_hash, event.source_id, stage,
                            item["reason_code"], event.title[:240], event.url,
                            event.published_at, event.region, event.rule_score, rank, stamp,
                        ),
                    )

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temp, path)

    @staticmethod
    def _attach_analyses(pool: list, data: dict) -> list:
        if not isinstance(data, dict):
            raise ProviderError("模型返回的候选结构无效")
        selections = data.get("selections")
        if not isinstance(selections, list):
            raise ProviderError("模型返回的候选结构无效")
        if not selections:
            return []
        by_id = {x.id: x for x in pool}
        ranked = []
        for analysis in selections:
            if not isinstance(analysis, dict) or not isinstance(analysis.get("id"), str):
                raise ProviderError("模型未返回有效候选ID")
            item = by_id.get(analysis["id"])
            if item is None:
                continue
            item.model_analysis = analysis
            ranked.append(item)
        if not ranked:
            raise ProviderError("模型未返回任何有效候选ID")
        return ranked

    @staticmethod
    def _event_content_hash(event: EventItem) -> str:
        stable = "\n".join(
            re.sub(r"\s+", " ", value).strip()
            for value in (event.title, event.url, event.summary)
        )
        return hashlib.sha256(stable.encode()).hexdigest()

    @staticmethod
    def _event_key(event: EventItem) -> str:
        return hashlib.sha256(event.url.encode()).hexdigest()

    def _enqueue_events(self, run_id: str, events: list[EventItem]) -> tuple[int, int]:
        """把跨日新增或内容变化事件写入轻量待筛选队列。"""
        stamp = now()
        new_count = 0
        reopened_count = 0
        with self.wf.db.connect() as conn:
            for event in events:
                event_key = self._event_key(event)
                content_hash = self._event_content_hash(event)
                event_json = json.dumps(asdict(event), ensure_ascii=False)
                existing = conn.execute(
                    "SELECT content_hash FROM discovery_queue WHERE event_key=?",
                    (event_key,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """INSERT INTO discovery_queue(
                             event_key,content_hash,status,event_json,first_seen_at,last_seen_at,
                             first_run_id,last_run_id
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            event_key, content_hash, "pending", event_json,
                            stamp, stamp, run_id, run_id,
                        ),
                    )
                    new_count += 1
                elif existing["content_hash"] != content_hash:
                    conn.execute(
                        """UPDATE discovery_queue SET
                             content_hash=?,status='pending',event_json=?,last_seen_at=?,
                             last_run_id=?,screened_run_id=NULL,screened_at=NULL,
                             reopen_count=reopen_count+1
                           WHERE event_key=?""",
                        (content_hash, event_json, stamp, run_id, event_key),
                    )
                    reopened_count += 1
                else:
                    conn.execute(
                        """UPDATE discovery_queue SET event_json=?,last_seen_at=?,last_run_id=?
                           WHERE event_key=?""",
                        (event_json, stamp, run_id, event_key),
                    )
        return new_count, reopened_count

    def _pending_event_records(
        self,
    ) -> tuple[list[tuple[EventItem, datetime]], str | None]:
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=int(self.s.section("discovery")["lookback_days"])
        )
        with self.wf.db.connect() as conn:
            conn.execute(
                """UPDATE discovery_queue SET status='expired'
                   WHERE status='pending' AND first_seen_at<?""",
                (cutoff.isoformat(),),
            )
            rows = conn.execute(
                """SELECT event_json,first_seen_at FROM discovery_queue
                   WHERE status='pending' AND first_seen_at>=?
                   ORDER BY first_seen_at""",
                (cutoff.isoformat(),),
            ).fetchall()
        records: list[tuple[EventItem, datetime]] = []
        for row in rows:
            try:
                first_seen = datetime.fromisoformat(row["first_seen_at"])
                if first_seen.tzinfo is None:
                    first_seen = first_seen.replace(tzinfo=timezone.utc)
                event = EventItem(**json.loads(row["event_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            # 新行为重新核验含显式假设的旧待筛记录；不改写原队列或已冻结运行。
            from .materials import split_discovery_summary
            if split_discovery_summary(event.summary)['upstream_hypotheses']:
                event.region, event.region_evidence = infer_event_region(
                    event.title, event.summary, event.url, event.source_region)
                qualified, _ = rule_screen_with_decisions([event], self.s.section('discovery'))
                if not qualified:
                    continue
            records.append((event, first_seen.astimezone(timezone.utc)))
        records.sort(
            key=lambda item: (
                -item[0].rule_score,
                item[1],
            )
        )
        oldest = min((first_seen for _, first_seen in records), default=None)
        return records, oldest.isoformat() if oldest else None

    def _pending_events(self) -> tuple[list[EventItem], str | None]:
        records, oldest = self._pending_event_records()
        return [event for event, _ in records], oldest

    def _select_pending_pool(
        self,
        records: list[tuple[EventItem, datetime]],
        limit: int,
        *,
        max_per_source: int,
        external_reserve: int,
        fresh_reserve: int,
        aged_reserve: int,
        current_clue_ids: set[str] | None = None,
    ) -> list[EventItem]:
        """在高分优先的基础上，给外部线索、新鲜事件和久候事件分别留位。"""
        if limit <= 0:
            return []
        from .materials import compact_reposts
        unique, _ = compact_reposts([event for event, _ in records])
        unique_ids = {event.id for event in unique}
        records = [record for record in records if record[0].id in unique_ids]
        current = datetime.now(timezone.utc)
        max_wait = timedelta(
            hours=int(self.s.section("discovery")["pending_batch_max_wait_hours"])
        )

        def is_fresh(record: tuple[EventItem, datetime]) -> bool:
            return current - record[1] < max_wait

        def is_aged(record: tuple[EventItem, datetime]) -> bool:
            return not is_fresh(record)

        def is_external(record: tuple[EventItem, datetime]) -> bool:
            return record[0].expansion_tier >= 4

        def is_current_clue(record):
            return record[0].id in (current_clue_ids or set())

        ranked = sorted(
            records,
            key=lambda item: (-problem_priority(item[0], self.s.section('discovery')), -item[0].rule_score, 0 if is_fresh(item) else 1, item[1]),
        )
        selected: list[tuple[EventItem, datetime]] = []
        selected_keys: set[str] = set()
        counts: Counter[str] = Counter()

        def take_until(
            pool: list[tuple[EventItem, datetime]],
            predicate,
            target: int,
        ) -> None:
            for record in pool:
                if len(selected) >= limit or sum(predicate(item) for item in selected) >= target:
                    break
                event = record[0]
                key = self._event_key(event)
                source_key = event.source_id or event.source_name
                if key in selected_keys or counts[source_key] >= max_per_source:
                    continue
                selected.append(record)
                selected_keys.add(key)
                counts[source_key] += 1

        # 本次导入占用既有外部名额，不叠加扩大输入池；仍受来源上限和质量闸门约束。
        take_until([item for item in ranked if is_current_clue(item)], is_current_clue,
                   min(external_reserve, int(self.s.section('discovery').get('premodel_current_clue_reserve', 0))))
        take_until([item for item in ranked if is_external(item)], is_external, external_reserve)
        take_until([item for item in ranked if is_fresh(item)], is_fresh, fresh_reserve)
        take_until([item for item in ranked if is_aged(item)], is_aged, aged_reserve)
        take_until(ranked, lambda _: True, limit)

        # 数据源数量确实不足时允许补满，但后续质量闸门仍会检查来源多样性。
        if len(selected) < min(limit, len(records)):
            for record in ranked:
                key = self._event_key(record[0])
                if key not in selected_keys:
                    selected.append(record)
                    selected_keys.add(key)
                    if len(selected) >= limit:
                        break
        selected.sort(
            key=lambda item: (-problem_priority(item[0], self.s.section('discovery')), -item[0].rule_score, 0 if is_fresh(item) else 1, item[1])
        )
        return [event for event, _ in selected]

    def _assess_model_pool(
        self,
        records: list[tuple[EventItem, datetime]],
        selected: list[EventItem],
    ) -> dict:
        """用零模型指标阻止“久候但低质”的队列批次消耗筛选Token。"""
        cfg = self.s.section("discovery")
        current = datetime.now(timezone.utc)
        max_wait = timedelta(hours=int(cfg["pending_batch_max_wait_hours"]))
        first_seen = {self._event_key(event): stamp for event, stamp in records}
        selected_records = [
            (event, first_seen.get(self._event_key(event), current)) for event in selected
        ]
        fresh_count = sum(current - stamp < max_wait for _, stamp in selected_records)
        aged_count = len(selected_records) - fresh_count
        external_count = sum(event.expansion_tier >= 4 for event, _ in selected_records)
        high_score_count = sum(
            event.rule_score >= int(cfg["premodel_quality_score_threshold"])
            for event, _ in selected_records
        )
        source_count = len(
            {event.source_id or event.source_name for event, _ in selected_records}
        )
        thresholds = {
            "minimum_total": int(cfg["screened_min"]),
            "minimum_sources": int(cfg["premodel_quality_min_sources"]),
            "minimum_fresh": int(cfg["premodel_quality_min_fresh"]),
            "minimum_high_score": int(cfg["premodel_quality_min_high_score"]),
            "strong_aged_high_score": int(cfg["premodel_quality_strong_aged_min"]),
            "high_score_threshold": int(cfg["premodel_quality_score_threshold"]),
        }
        reasons = []
        if len(selected_records) < thresholds["minimum_total"]:
            reasons.append("pool_below_screened_min")
        if source_count < thresholds["minimum_sources"]:
            reasons.append("insufficient_source_diversity")
        if high_score_count < thresholds["minimum_high_score"]:
            reasons.append("insufficient_high_score_events")
        if (
            fresh_count < thresholds["minimum_fresh"]
            and high_score_count < thresholds["strong_aged_high_score"]
        ):
            reasons.append("insufficient_fresh_or_strong_aged_events")
        return {
            "checked": True,
            "ok": not reasons,
            "reasons": reasons,
            "total": len(selected_records),
            "fresh_count": fresh_count,
            "aged_count": aged_count,
            "external_count": external_count,
            "high_score_count": high_score_count,
            "source_count": source_count,
            "thresholds": thresholds,
        }

    def _mark_queue_screened(self, run_id: str, events: list[EventItem]) -> None:
        stamp = now()
        with self.wf.db.connect() as conn:
            for event in events:
                conn.execute(
                    """UPDATE discovery_queue SET status='screened',screened_run_id=?,
                         screened_at=?,last_seen_at=? WHERE event_key=?""",
                    (run_id, stamp, stamp, self._event_key(event)),
                )

    def _pending_queue_count(self) -> int:
        with self.wf.db.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM discovery_queue WHERE status='pending'"
                ).fetchone()[0]
            )

    def _persist_events(self, run_id: str, events: list) -> None:
        with self.wf.db.connect() as conn:
            for event in events:
                content_hash = self._event_content_hash(event)
                conn.execute("""INSERT OR IGNORE INTO event_items(
                    id,run_id,source_id,source_name,source_level,title,url,published_at,summary,
                    region,topics_json,rule_score,content_hash,collected_at,source_region,region_evidence,expansion_tier,material_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    f"{run_id}:{event.id}", run_id, event.source_id, event.source_name, event.source_level, event.title,
                    event.url, event.published_at, event.summary, event.region, json.dumps(event.topics, ensure_ascii=False),
                    event.rule_score, content_hash, event.collected_at, event.source_region, event.region_evidence, event.expansion_tier,
                    json.dumps(event.material, ensure_ascii=False),
                ))

    def _persist_candidates(self, run_id: str, candidates: list) -> None:
        with self.wf.db.connect() as conn:
            conn.execute("DELETE FROM candidates WHERE run_id=?", (run_id,))
            for candidate in candidates:
                conn.execute("INSERT INTO candidates(id,run_id,title,data_json,score,created_at) VALUES(?,?,?,?,?,?)", (
                    f"{run_id}:{candidate.id}", run_id, candidate.title, json.dumps(asdict(candidate), ensure_ascii=False), candidate.score, now(),
                ))

    def _record_efficiency(self, run_id: str, *, premodel_count: int, repeated_excluded: int,
                           model_input_count: int, cache_hit: bool, tokens_saved: int,
                           deferred_count: int, new_event_count: int,
                           reopened_event_count: int, pending_before_count: int,
                           expansion_tier: int, candidate_count: int) -> None:
        with self.wf.db.connect() as conn:
            conn.execute(
                """INSERT INTO run_efficiency(
                     run_id,premodel_count,repeated_excluded,model_input_count,
                     screening_cache_hit,screening_tokens_saved,deferred_count,
                     new_event_count,reopened_event_count,pending_before_count,
                     expansion_tier,candidate_count,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     premodel_count=excluded.premodel_count,
                     repeated_excluded=excluded.repeated_excluded,
                     model_input_count=excluded.model_input_count,
                     screening_cache_hit=excluded.screening_cache_hit,
                     screening_tokens_saved=excluded.screening_tokens_saved,
                     deferred_count=excluded.deferred_count,
                     new_event_count=excluded.new_event_count,
                     reopened_event_count=excluded.reopened_event_count,
                     pending_before_count=excluded.pending_before_count,
                     expansion_tier=excluded.expansion_tier,
                     candidate_count=excluded.candidate_count,updated_at=excluded.updated_at""",
                (run_id, premodel_count, repeated_excluded, model_input_count,
                 int(cache_hit), tokens_saved, deferred_count, new_event_count,
                 reopened_event_count, pending_before_count, expansion_tier,
                 candidate_count, now()),
            )

    def _report(self, run_id: str, candidates: list, audits: list, collected: int, screened: int,
                premodel: int, repeated_excluded: int, cache_hit: bool, tokens_saved: int,
                deferred_count: int, batch_reason: str, start_tier: int, expansion_tier: int,
                *, current_new_count: int = 0, reopened_count: int = 0,
                pending_before_count: int = 0,
                shadow_reviews: list | None = None,
                pool_quality: dict | None = None) -> Path:
        from .candidate_eligibility import candidate_hash, current_candidate_review
        path = self.s.root / "outputs/candidates" / f"{run_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        blocked = [item for item in audits if item.decision == "block_original_gap"]
        lines = [
            f"# 候选选题报告（{run_id}）",
            "",
            f"从第 {start_tier} 层开始；扩展到第 {expansion_tier} 层；采集 {collected} 条；"
            f"本轮新增 {current_new_count} 条；内容变化后重新开放 {reopened_count} 条；"
            f"筛选前待处理 {pending_before_count} 条；模型前 {premodel} 条；"
            f"排除历史未变化或同源事件 {repeated_excluded} 条；仍待后续合并 {deferred_count} 条；"
            f"模型初筛 {screened} 条；制度新意审查 {len(audits)} 条；"
            f"阻断原缺口 {len(blocked)} 条；输出 {len(candidates)} 个候选。",
            f"批处理决策：{batch_reason}；筛选结果缓存：{'命中' if cache_hit else '未命中'}；"
            f"本次复用节省Token：{tokens_saved}。",
            "",
            "> 扩展层级：1=海淀和北京主题源，2=北京增补权威源，3=全国权威与调查源，"
            "4=投诉、论坛和社交平台待核线索；自动候选不得直接用于报送。",
            "> 以下为模型初筛结果，分数不是成稿概率。人工推荐前按 docs/candidate_entry_trial.md "
            "填写研究入口；未填写不表示已通过复核，不改变自动排序或闸门。",
            "",
        ]
        if pool_quality and pool_quality.get("checked"):
            lines += [
                "> 模型前质量闸门："
                f"{'通过' if pool_quality.get('ok') else '暂缓'}；"
                f"新鲜 {pool_quality.get('fresh_count', 0)} 条，久候 {pool_quality.get('aged_count', 0)} 条，"
                f"外部补充 {pool_quality.get('external_count', 0)} 条，高分 {pool_quality.get('high_score_count', 0)} 条，"
                f"来源标识 {pool_quality.get('source_count', 0)} 个（不代表独立原始信息链）；"
                f"原因：{','.join(pool_quality.get('reasons', [])) or 'none'}。",
                "",
            ]
        if shadow_reviews:
            shadow_counts = Counter(item.recommendation for item in shadow_reviews)
            lines += [
                f"> 候选前影子核验 {len(shadow_reviews)} 条："
                + "；".join(f"{key} {value}" for key, value in sorted(shadow_counts.items()))
                + "。影子结果不参与本轮排序、阻断或模型输入取舍。",
                "",
            ]
        from .materials import split_discovery_summary
        for c in candidates:
            material_parts = split_discovery_summary(c.summary)
            components = c.score_reasons.get("正向分项", {})
            component_text = "；".join(
                f"{name}{value.get('得分', 0)}/{value.get('满分', 0)}"
                for name, value in components.items()
            )
            deductions = c.score_reasons.get("扣分项", [])
            deduction_text = "；".join(
                f"{item.get('key', '')}-{item.get('points', 0)}（{item.get('reason', '')}）"
                for item in deductions
            ) or "无"
            lines += [
                f"## {c.id}｜{c.title}", "",
                f"- 得分：{c.score}；优先级：{c.priority}",
                f"- 正向分项：{component_text}",
                f"- 扣分项：{deduction_text}",
                f"- 时间与地域：{c.event_date}；{c.region}",
                f"- 发现材料陈述（未回源，不等于已核事实）：{material_parts['reported_excerpt']}",
                f"- 上游缺口/原因设想（待核）：{material_parts['upstream_hypotheses'] or '未显式标注；不代表材料中没有推断'}",
                f"- 缺口假设（待核）：{c.gap_hypothesis}（{c.gap_type}）",
                f"- 制度覆盖审查：{c.coverage_status}；{c.novelty_decision}",
                f"- 制度矛盾：{c.institutional_conflict}",
                f"- 权限判断：{c.authority}",
                f"- 数据条件：{c.data_sufficiency}",
                f"- 历史关系：{c.history_relation}",
                f"- 风险：{c.risk}",
                f"- 模型初筛意见（非人工推荐）：{c.recommendation}",
                f"- 研究入口影子意见（不参与排序）：{json.dumps(c.eligibility, ensure_ascii=False) if c.eligibility else 'legacy / 未做新版评估'}",
                f"- 候选材料版本：{candidate_hash(c)}",
                f"- 已登记回源推荐：{json.dumps(current_candidate_review(self.wf.db, run_id, c), ensure_ascii=False)}；最新记录以 candidates --json 为准。",
                f"- 来源：{c.score_reasons.get('来源名称', '')}；{c.score_reasons.get('来源URL', '')}",
                "",
                "### 人工研究入口复核（待填写）",
                "",
                "- 事实依据：待填写具体群体、场景、受阻环节、事件日期及来源；元数据与已核事实分开。",
                "- 现行机制与历史增量：待填写已有规则、适用边界及相对历史研究的新证据。",
                "- 待证问题与证据入口：待填写具体材料、获取路径、预期能回答的问题及无法取得时的处理。",
                "- 核验投入与限制：待填写复用材料、本次实际请求及预算占用、获取失败；占位项不代表已执行。",
                "- 人工推荐：待填写主选、备选或仅保留线索及理由；不据此自动选题或放行深研。",
                "",
            ]
        if not candidates:
            lines += ["> 本轮未形成可进入有限预研的候选；不得以旧题、换标题或未经高等级来源核验的三级线索补足数量。", ""]
        if blocked:
            lines += ["## 被制度新意闸门阻断的原缺口", ""]
            for item in blocked:
                evidence = item.policy_matches[0] if item.policy_matches else {}
                lines += [f"- {item.title}：{item.gap_hypothesis}", f"  - 直接覆盖：{evidence.get('name', '')}；{evidence.get('source_url', '')}", f"  - 复核记录ID：{run_id}:{item.event_id}"]
        temp = path.with_suffix(".tmp")
        temp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(temp, path)
        return path
