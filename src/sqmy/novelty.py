from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Any
from zoneinfo import ZoneInfo

from .collector import SourceCollector
from .config import Settings
from .db import Database, now
from .models import EventItem
from .screener import normalize_title


ABSENCE_SIGNALS = ("尚未", "未建立", "没有", "缺少", "缺乏", "空白")
COVERAGE_SIGNALS = ("建立", "完善", "出台", "印发", "实施", "试点", "指引", "司法建议", "双向反馈", "协同机制")
GENERIC_POLICY_MATCH_KEYWORDS = {
    "平台", "投诉", "举报", "网络", "规则", "机制", "纠错", "合规", "保护机制",
}
GAP_TYPES = {"policy_absence", "implementation_gap", "coordination_gap", "effectiveness_gap", "accountability_gap", "unclear"}
REVIEW_OUTCOMES = {
    "confirmed",
    "reversed",
    "reframed",
    "confirmed_block",
    "confirmed_warning",
    "false_block",
    "missed_coverage",
    "supported_gap",
    "stopped_other",
    "unclassified",
    "not_reviewed",
}


@dataclass
class NoveltyAudit:
    event_id: str
    title: str
    gap_hypothesis: str
    gap_type: str
    coverage_status: str
    decision: str
    queries: list[str]
    counterevidence: list[dict[str, Any]]
    policy_matches: list[dict[str, Any]]
    audit_tokens: int = 0
    potential_waste_tokens: int = 0


class NoveltyAuditor:
    def __init__(self, settings: Settings):
        self.s = settings
        self.cfg = settings.section("novelty")
        self.db = Database(settings.database_path)
        self.db.initialize()
        self.collector = SourceCollector(settings.root, settings.raw)
        self._sync_policy_mechanisms()

    def _sync_policy_mechanisms(self) -> None:
        path = self.s.root / "config/policy_mechanisms.toml"
        if not path.exists():
            return
        with path.open("rb") as fh:
            mechanisms = tomllib.load(fh).get("mechanisms", [])
        with self.db.connect() as conn:
            for item in mechanisms:
                conn.execute(
                    """INSERT INTO policy_mechanisms(
                         id,name,problem_type,jurisdiction,actor,summary,keywords_json,
                         source_url,valid_from,last_verified_at,active
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(id) DO UPDATE SET
                         name=excluded.name,problem_type=excluded.problem_type,
                         jurisdiction=excluded.jurisdiction,actor=excluded.actor,
                         summary=excluded.summary,keywords_json=excluded.keywords_json,
                         source_url=excluded.source_url,valid_from=excluded.valid_from,
                         last_verified_at=excluded.last_verified_at,active=1""",
                    (
                        item["id"], item["name"], item["problem_type"], item["jurisdiction"],
                        item["actor"], item["summary"], json.dumps(item["keywords"], ensure_ascii=False),
                        item["source_url"], item.get("valid_from"), item["last_verified_at"],
                    ),
                )

    def audit(self, run_id: str, events: list[EventItem], *, live_search: bool) -> list[NoveltyAudit]:
        audits = [self._audit_one(event, live_search=live_search) for event in events[: self.cfg["audit_pool_size"]]]
        with self.db.connect() as conn:
            for audit in audits:
                conn.execute(
                    """INSERT INTO novelty_audits(
                         id,run_id,event_id,title,gap_hypothesis,gap_type,coverage_status,
                         decision,queries_json,counterevidence_json,policy_matches_json,
                         audit_tokens,potential_waste_tokens,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,event_id) DO UPDATE SET
                         title=excluded.title,gap_hypothesis=excluded.gap_hypothesis,
                         gap_type=excluded.gap_type,coverage_status=excluded.coverage_status,
                         decision=excluded.decision,queries_json=excluded.queries_json,
                         counterevidence_json=excluded.counterevidence_json,
                         policy_matches_json=excluded.policy_matches_json,
                         audit_tokens=excluded.audit_tokens,
                         potential_waste_tokens=excluded.potential_waste_tokens""",
                    (
                        f"{run_id}:{audit.event_id}", run_id, audit.event_id, audit.title,
                        audit.gap_hypothesis, audit.gap_type, audit.coverage_status, audit.decision,
                        json.dumps(audit.queries, ensure_ascii=False),
                        json.dumps(audit.counterevidence, ensure_ascii=False),
                        json.dumps(audit.policy_matches, ensure_ascii=False), audit.audit_tokens,
                        audit.potential_waste_tokens, now(),
                    ),
                )
        self._write_run_audit(run_id, audits)
        return audits

    def _audit_one(self, event: EventItem, *, live_search: bool) -> NoveltyAudit:
        analysis = getattr(event, "model_analysis", {})
        gap = analysis.get("gap_hypothesis") or analysis.get("policy_gap") or "需核对现行机制是否已覆盖"
        gap_type = analysis.get("gap_type", "unclear")
        if gap_type not in GAP_TYPES:
            gap_type = "unclear"
        queries = self._query_variants(analysis.get("counter_queries", []))
        text = " ".join((
            event.title, event.summary, gap, analysis.get("policy_gap", ""),
            analysis.get("policy_entry", ""), analysis.get("institutional_issue", ""),
        ))
        policy_matches = self.policy_matches(text)
        counterevidence: list[dict[str, Any]] = []
        seen_hits: set[str] = set()

        if live_search:
            for query in queries:
                try:
                    hits = self.collector.search_query(
                        query,
                        limit=self.cfg["max_hits_per_query"],
                        lookback_days=self.cfg["counterevidence_lookback_days"],
                    )
                except Exception as exc:
                    counterevidence.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                for hit in hits:
                    normalized_hit = normalize_title(hit.title)
                    if hit.url == event.url or SequenceMatcher(None, normalized_hit, normalize_title(event.title)).ratio() >= 0.92:
                        continue
                    origin_group = normalized_hit
                    if origin_group in seen_hits:
                        continue
                    seen_hits.add(origin_group)
                    hit_text = hit.title + " " + hit.summary
                    if hit.source_level == 1 and any(signal in hit_text for signal in COVERAGE_SIGNALS):
                        counterevidence.append({
                            "query": query, "title": hit.title, "url": hit.url,
                            "source_level": hit.source_level, "summary": hit.summary[:400],
                            "origin_group": origin_group,
                        })

        absence_claim = gap_type == "policy_absence" or (
            gap_type == "unclear" and any(signal in gap for signal in ABSENCE_SIGNALS)
        )
        fresh_policy_matches = [item for item in policy_matches if item["fresh"]]
        if fresh_policy_matches and absence_claim:
            status, decision = "covered", "block_original_gap"
        elif policy_matches or counterevidence:
            status, decision = "likely_covered", "keep_with_novelty_warning"
        else:
            status, decision = "unclear", "proceed_limited_research"
        potential = self.cfg["potential_waste_tokens_per_block"] if decision == "block_original_gap" else 0
        return NoveltyAudit(
            event_id=event.id, title=event.title, gap_hypothesis=gap, gap_type=gap_type,
            coverage_status=status, decision=decision, queries=queries,
            counterevidence=counterevidence, policy_matches=policy_matches,
            potential_waste_tokens=potential,
        )

    def _official_query(self, query: str) -> str:
        if "site:" in query:
            return query
        domains = self.cfg["official_counterevidence_domains"]
        scope = " OR ".join(f"site:{domain}" for domain in domains)
        return f"{query} ({scope})"

    def _query_variants(self, raw_queries: list[Any]) -> list[str]:
        """保留原辖区检索，并为显式 site 查询补一个跨层级官方检索。"""
        queries: list[str] = []
        limit = int(self.cfg["max_queries_per_candidate"])
        for value in raw_queries:
            raw = str(value).strip()
            if not raw:
                continue
            local_query = self._official_query(raw)
            if local_query not in queries:
                queries.append(local_query)
            if len(queries) >= limit:
                break
            if "site:" in raw:
                widened = re.sub(r"(?:^|\s)site:[^\s()]+", " ", raw, flags=re.IGNORECASE)
                widened = re.sub(r"\s+", " ", widened).strip(" ()")
                if widened:
                    widened_query = self._official_query(widened)
                    if widened_query not in queries:
                        queries.append(widened_query)
            if len(queries) >= limit:
                break
        return queries[:limit]

    def policy_matches(self, text: str) -> list[dict[str, Any]]:
        matches = []
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM policy_mechanisms WHERE active=1").fetchall()
        for row in rows:
            keywords = json.loads(row["keywords_json"])
            matched = [word for word in keywords if word in text]
            distinctive = [
                word for word in matched
                if word not in GENERIC_POLICY_MATCH_KEYWORDS
            ]
            if (
                len(matched) >= self.cfg["local_mechanism_min_keyword_matches"]
                and distinctive
            ):
                verified = datetime.fromisoformat(row["last_verified_at"]).replace(tzinfo=timezone.utc)
                fresh = datetime.now(timezone.utc) - verified <= timedelta(days=self.cfg["mechanism_max_age_days"])
                matches.append({
                    "id": row["id"], "name": row["name"], "matched_keywords": matched,
                    "summary": row["summary"], "source_url": row["source_url"],
                    "last_verified_at": row["last_verified_at"],
                    "fresh": fresh,
                })
        return matches

    def _write_run_audit(self, run_id: str, audits: list[NoveltyAudit]) -> None:
        path = self.s.root / "data/runs" / run_id / "novelty_audit.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps([asdict(item) for item in audits], ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)


def record_review(settings: Settings, audit_id: str, outcome: str, reason: str) -> None:
    if outcome not in REVIEW_OUTCOMES - {"not_reviewed"}:
        raise ValueError(f"不支持的复核结果：{outcome}")
    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as conn:
        cursor = conn.execute(
            "UPDATE novelty_audits SET review_outcome=?,review_reason=?,reviewed_at=? WHERE id=?",
            (outcome, reason, now(), audit_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"未找到审计记录：{audit_id}")


def record_pre_research_feedback(
    settings: Settings,
    run_id: str,
    candidate_id: str,
    payload: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    """把有效预研决定自动反馈到对应的制度新意审计；找不到关联时安全跳过。"""
    if not gate.get("valid"):
        return {"status": "not_recorded_invalid_gate"}
    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as conn:
        candidate = conn.execute(
            "SELECT data_json FROM candidates WHERE run_id=? AND id=?",
            (run_id, f"{run_id}:{candidate_id}"),
        ).fetchone()
        if candidate is None:
            return {"status": "not_linked", "reason": "candidate_not_found"}
        try:
            candidate_data = json.loads(candidate["data_json"] or "{}")
        except json.JSONDecodeError:
            candidate_data = {}
        score_reasons = candidate_data.get("score_reasons", {})
        event_id = score_reasons.get("事件ID") if isinstance(score_reasons, dict) else None
        if not event_id and isinstance(score_reasons, dict):
            source_url = score_reasons.get("来源URL")
            if source_url:
                event = conn.execute(
                    "SELECT id FROM event_items WHERE run_id=? AND url=? ORDER BY collected_at DESC LIMIT 1",
                    (run_id, source_url),
                ).fetchone()
                if event:
                    prefix = f"{run_id}:"
                    event_id = event["id"][len(prefix):] if event["id"].startswith(prefix) else event["id"]
        if not event_id:
            return {"status": "not_linked", "reason": "event_id_unavailable"}
        audit = conn.execute(
            "SELECT id,coverage_status,decision,review_reason FROM novelty_audits WHERE run_id=? AND event_id=?",
            (run_id, event_id),
        ).fetchone()
        if audit is None:
            return {"status": "not_linked", "reason": "novelty_audit_not_found", "event_id": event_id}
        if audit["review_reason"] and not audit["review_reason"].startswith("有限预研自动反馈："):
            return {
                "status": "manual_review_preserved",
                "audit_id": audit["id"],
                "event_id": event_id,
            }

        decision = str(payload.get("decision") or "")
        decision_reason = str(payload.get("decision_reason") or "unclassified")
        if audit["decision"] == "block_original_gap":
            if decision == "stop" and decision_reason == "policy_covered":
                outcome = "confirmed_block"
            elif decision == "proceed":
                outcome = "false_block"
            elif decision == "reframe":
                outcome = "reframed"
            else:
                outcome = "stopped_other"
        elif audit["coverage_status"] == "likely_covered" and decision_reason == "policy_covered":
            outcome = "confirmed_warning" if decision == "stop" else "reframed"
        elif decision_reason == "policy_covered" and decision in {"stop", "reframe"}:
            outcome = "missed_coverage"
        elif decision == "reframe":
            outcome = "reframed"
        elif decision == "stop":
            outcome = "stopped_other"
        elif decision == "proceed":
            outcome = "supported_gap"
        else:
            outcome = "unclassified"
        review_reason = (
            "有限预研自动反馈："
            f"analysis_decision={decision}; decision_reason={decision_reason}; "
            f"early_coverage={audit['coverage_status']}; early_decision={audit['decision']}"
        )
        conn.execute(
            "UPDATE novelty_audits SET review_outcome=?,review_reason=?,reviewed_at=? WHERE id=?",
            (outcome, review_reason, now(), audit["id"]),
        )
    return {
        "status": "recorded",
        "audit_id": audit["id"],
        "event_id": event_id,
        "outcome": outcome,
        "decision_reason": decision_reason,
    }


def _local_week_start(value: datetime, local_tz: ZoneInfo) -> date:
    local_date_value = value.astimezone(local_tz).date()
    return local_date_value - timedelta(days=local_date_value.weekday())


def production_funnel(
    settings: Settings,
    weeks: int | None = None,
    *,
    reference: datetime | None = None,
) -> dict[str, Any]:
    """按真实运行的发现周归集后续转化，避免跨周处理造成重复计数。"""
    quality_cfg = settings.section("quality")
    window_weeks = int(weeks or quality_cfg["production_evaluation_weeks"])
    if window_weeks <= 0:
        raise ValueError("自然周统计窗口必须为正整数")
    local_tz = ZoneInfo(settings.section("project")["timezone"])
    ref = reference or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    current_start = _local_week_start(ref, local_tz)
    first_start = current_start - timedelta(weeks=window_weeks - 1)
    cutoff_utc = datetime.combine(first_start, time.min, tzinfo=local_tz).astimezone(timezone.utc).isoformat()

    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as conn:
        runs = conn.execute(
            """SELECT r.* FROM runs r JOIN run_context rc ON rc.run_id=r.id
               WHERE r.created_at>=? AND rc.mode='live' ORDER BY r.created_at""",
            (cutoff_utc,),
        ).fetchall()
        candidate_rows = conn.execute(
            """SELECT c.run_id,c.id,c.selected FROM candidates c
               JOIN run_context rc ON rc.run_id=c.run_id JOIN runs r ON r.id=c.run_id
               WHERE r.created_at>=? AND rc.mode='live'""",
            (cutoff_utc,),
        ).fetchall()
        review_rows = conn.execute(
            """SELECT rr.rowid AS row_number,rr.* FROM research_reviews rr
               JOIN run_context rc ON rc.run_id=rr.run_id JOIN runs r ON r.id=rr.run_id
               WHERE r.created_at>=? AND rc.mode='live'
               ORDER BY rr.created_at,rr.rowid""",
            (cutoff_utc,),
        ).fetchall()
        topic_rows = conn.execute(
            """SELECT t.* FROM topics t JOIN run_context rc ON rc.run_id=t.run_id
               JOIN runs r ON r.id=t.run_id
               WHERE r.created_at>=? AND rc.mode='live'""",
            (cutoff_utc,),
        ).fetchall()

    candidates_by_run: dict[str, list[Any]] = {}
    for row in candidate_rows:
        candidates_by_run.setdefault(row["run_id"], []).append(row)
    latest_reviews: dict[tuple[str, str], Any] = {}
    for row in review_rows:
        latest_reviews[(row["run_id"], row["candidate_id"])] = row
    reviews_by_run: dict[str, list[Any]] = {}
    for (run_id, _), row in latest_reviews.items():
        reviews_by_run.setdefault(run_id, []).append(row)
    topics_by_run: dict[str, list[Any]] = {}
    for row in topic_rows:
        topics_by_run.setdefault(row["run_id"], []).append(row)

    weekly_rows = []
    for offset in range(window_weeks):
        start = first_start + timedelta(weeks=offset)
        weekly_rows.append({
            "week_start": start.isoformat(),
            "week_end": (start + timedelta(days=6)).isoformat(),
            "is_current_week": start == current_start,
            "is_closed_week": start < current_start,
            "live_runs": 0,
            "candidate_count": 0,
            "selected_topic_count": 0,
            "pre_research_count": 0,
            "pre_research_decisions": {"proceed": 0, "reframe": 0, "stop": 0},
            "human_proceed_count": 0,
            "human_stop_count": 0,
            "deep_research_supported_count": 0,
            "evidence_gate_passed_count": 0,
            "draft_count": 0,
            "approved_count": 0,
            "submitted_count": 0,
            "recorded_token_used": 0,
            "recorded_cost_cny": 0.0,
            "stop_reasons": [],
        })

    for run in runs:
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        index = (_local_week_start(created, local_tz) - first_start).days // 7
        if not 0 <= index < window_weeks:
            continue
        bucket = weekly_rows[index]
        run_id = run["id"]
        try:
            checkpoint = json.loads(run["checkpoint_json"] or "{}")
        except json.JSONDecodeError:
            checkpoint = {}
        run_candidates = candidates_by_run.get(run_id, [])
        run_reviews = reviews_by_run.get(run_id, [])
        run_topics = topics_by_run.get(run_id, [])
        selected_ids = {
            row["id"].split(":")[-1] for row in run_candidates if row["selected"]
        }
        selected_ids.update(str(item) for item in checkpoint.get("selected", []) if item)
        selected_ids.update(row["candidate_id"] for row in run_reviews)
        selected_ids.update(row["candidate_id"] for row in run_topics if row["candidate_id"])

        bucket["live_runs"] += 1
        bucket["candidate_count"] += len(run_candidates)
        bucket["selected_topic_count"] += len(selected_ids)
        bucket["pre_research_count"] += len(run_reviews)
        for review in run_reviews:
            decision = review["decision"]
            if decision in bucket["pre_research_decisions"]:
                bucket["pre_research_decisions"][decision] += 1
            if review["human_decision"] == "proceed":
                bucket["human_proceed_count"] += 1
            elif review["human_decision"] == "stop":
                bucket["human_stop_count"] += 1
            if decision == "stop":
                try:
                    review_data = json.loads(review["data_json"] or "{}")
                except json.JSONDecodeError:
                    review_data = {}
                bucket["stop_reasons"].append({
                    "run_id": run_id,
                    "stage": "pre_research",
                    "reason": review_data.get("decision_reason", "unclassified"),
                })

        deep = checkpoint.get("deep_research")
        deep_supported = False
        if isinstance(deep, dict):
            verdict = str(deep.get("verdict") or deep.get("conclusion") or "")
            deep_supported = bool(deep.get("draft_allowed")) or verdict.startswith(("support", "supported"))
            if not deep_supported:
                bucket["stop_reasons"].append({
                    "run_id": run_id,
                    "stage": "deep_research",
                    "reason": verdict or "not_supported_for_drafting",
                })
        draft_count = len(run_topics)
        evidence_passed = max(
            draft_count,
            int(bool(isinstance(deep, dict) and deep.get("draft_allowed"))),
            int(checkpoint.get("evidence_gate") == "passed"),
        )
        bucket["deep_research_supported_count"] += max(int(deep_supported), evidence_passed)
        bucket["evidence_gate_passed_count"] += evidence_passed
        bucket["draft_count"] += draft_count
        bucket["approved_count"] += sum(row["approval_status"] == "approved" for row in run_topics)
        bucket["submitted_count"] += sum(bool(row["actually_submitted"]) for row in run_topics)
        bucket["recorded_token_used"] += int(run["token_used"])
        bucket["recorded_cost_cny"] = round(
            bucket["recorded_cost_cny"] + float(run["estimated_cost_cny"]), 4
        )
        if run["status"] == "skipped":
            bucket["stop_reasons"].append({
                "run_id": run_id,
                "stage": str(run["phase"]),
                "reason": checkpoint.get("skip_reason", "unclassified"),
            })

    target = int(quality_cfg["minimum_drafts_per_week"])
    stretch = int(quality_cfg["stretch_drafts_per_week"])
    for bucket in weekly_rows:
        bucket["minimum_target_met"] = bucket["evidence_gate_passed_count"] >= target
        bucket["stretch_target_met"] = bucket["evidence_gate_passed_count"] >= stretch
    totals = {
        key: sum(int(row[key]) for row in weekly_rows)
        for key in (
            "live_runs", "candidate_count", "selected_topic_count", "pre_research_count",
            "human_proceed_count", "human_stop_count", "deep_research_supported_count",
            "evidence_gate_passed_count", "draft_count", "approved_count", "submitted_count",
            "recorded_token_used",
        )
    }
    totals["recorded_cost_cny"] = round(sum(row["recorded_cost_cny"] for row in weekly_rows), 4)
    decision_totals = {
        decision: sum(row["pre_research_decisions"][decision] for row in weekly_rows)
        for decision in ("proceed", "reframe", "stop")
    }
    totals["pre_research_decisions"] = decision_totals
    totals["pre_research_stop_rate"] = (
        round(decision_totals["stop"] / totals["pre_research_count"], 4)
        if totals["pre_research_count"] else None
    )
    totals["candidates_per_evidence_passed_draft"] = (
        round(totals["candidate_count"] / totals["evidence_gate_passed_count"], 2)
        if totals["evidence_gate_passed_count"] else None
    )
    totals["recorded_tokens_per_evidence_passed_draft"] = (
        round(totals["recorded_token_used"] / totals["evidence_gate_passed_count"])
        if totals["evidence_gate_passed_count"] else None
    )
    stable_window = int(quality_cfg["stable_production_weeks"])
    closed_weeks = [row for row in weekly_rows if row["is_closed_week"]]
    evaluated = closed_weeks[-stable_window:] if len(closed_weeks) >= stable_window else []
    return {
        "timezone": str(local_tz),
        "window_weeks": window_weeks,
        "cohort_rule": "后续预研、成稿、通过和报送归入其发现运行所在自然周",
        "minimum_drafts_per_week": target,
        "stretch_drafts_per_week": stretch,
        "weeks": weekly_rows,
        "totals": totals,
        "stable_window_weeks": stable_window,
        "stability_evaluation_ready": bool(evaluated),
        "stable_minimum_output": bool(evaluated) and all(row["minimum_target_met"] for row in evaluated),
        "stable_stretch_output": bool(evaluated) and all(row["stretch_target_met"] for row in evaluated),
        "measurement_note": (
            "成稿按已通过证据闸门并写入topics表计数；交互式Codex订阅用量若未写入项目账本，"
            "recorded_token_used不会包含该部分。"
        ),
    }


def rolling_evaluation(settings: Settings, days: int | None = None) -> dict[str, Any]:
    cfg = settings.section("novelty")
    window_days = days or cfg["rolling_evaluation_days"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT n.* FROM novelty_audits n
               JOIN run_context rc ON rc.run_id=n.run_id
               WHERE n.created_at>=? AND rc.mode='live' ORDER BY n.created_at""",
            (cutoff,),
        ).fetchall()
        screening_tokens = conn.execute(
            """SELECT COALESCE(SUM(m.input_tokens+m.output_tokens),0)
               FROM model_calls m JOIN run_context rc ON rc.run_id=m.run_id
               WHERE m.created_at>=? AND m.task_id='screening' AND rc.mode='live'""",
            (cutoff,),
        ).fetchone()[0]
        efficiency = conn.execute(
            """SELECT COALESCE(SUM(e.premodel_count),0),
                      COALESCE(SUM(e.repeated_excluded),0),
                      COALESCE(SUM(e.model_input_count),0),
                      COALESCE(SUM(e.screening_cache_hit),0),
                      COALESCE(SUM(e.screening_tokens_saved),0),
                      COALESCE(SUM(e.deferred_count),0),
                      COALESCE(SUM(e.new_event_count),0),
                      COALESCE(SUM(e.reopened_event_count),0),
                      COALESCE(SUM(e.pending_before_count),0)
               FROM run_efficiency e
               JOIN runs r ON r.id=e.run_id JOIN run_context rc ON rc.run_id=e.run_id
               WHERE r.created_at>=? AND rc.mode='live'""",
            (cutoff,),
        ).fetchone()
        tier_rows = conn.execute(
            """SELECT e.expansion_tier,COUNT(*)
               FROM run_efficiency e
               JOIN runs r ON r.id=e.run_id JOIN run_context rc ON rc.run_id=e.run_id
               WHERE r.created_at>=? AND rc.mode='live'
               GROUP BY e.expansion_tier ORDER BY e.expansion_tier""",
            (cutoff,),
        ).fetchall()
    total = len(rows)
    counts = {status: sum(row["coverage_status"] == status for row in rows) for status in ("covered", "likely_covered", "unclear")}
    blocked = [row for row in rows if row["decision"] == "block_original_gap"]
    reviewed = [
        row for row in blocked
        if row["review_outcome"] in {"confirmed", "confirmed_block", "reversed", "false_block"}
    ]
    confirmed = sum(row["review_outcome"] in {"confirmed", "confirmed_block"} for row in reviewed)
    reversed_count = sum(row["review_outcome"] in {"reversed", "false_block"} for row in reviewed)
    feedback_counts = {
        outcome: sum(row["review_outcome"] == outcome for row in rows)
        for outcome in sorted(REVIEW_OUTCOMES - {"not_reviewed"})
    }
    feedback_count = sum(bool(row["review_outcome"]) for row in rows)
    early_missed_count = feedback_counts["missed_coverage"]
    observed_rate = len(reviewed) / len(blocked) if blocked else None
    precision = confirmed / len(reviewed) if reviewed else None
    enough_volume = total >= cfg["minimum_audits_for_decision"]
    enough_reviews = bool(blocked) and len(reviewed) >= cfg["minimum_reviewed_blocks_for_precision"] and observed_rate is not None and observed_rate >= cfg["minimum_observed_outcome_rate"]
    if not enough_volume:
        conclusion = "insufficient_sample"
    elif early_missed_count:
        conclusion = "improve_early_coverage_detection"
    elif not blocked:
        conclusion = "no_block_cases_yet"
    elif blocked and not enough_reviews:
        conclusion = "needs_more_review_labels"
    elif precision is not None and reversed_count:
        conclusion = "review_false_blocks_before_tightening"
    else:
        conclusion = "continue_current_gate"
    return {
        "window_days": window_days, "audit_count": total, "status_counts": counts,
        "blocked_count": len(blocked), "reviewed_block_count": len(reviewed),
        "confirmed_block_count": confirmed, "reversed_block_count": reversed_count,
        "feedback_count": feedback_count,
        "feedback_coverage_rate": round(feedback_count / total, 4) if total else None,
        "feedback_outcome_counts": feedback_counts,
        "early_missed_coverage_count": early_missed_count,
        "observed_outcome_rate": round(observed_rate, 4) if observed_rate is not None else None, "observed_block_precision": precision,
        "audit_tokens": sum(row["audit_tokens"] for row in rows),
        "screening_tokens": int(screening_tokens),
        "premodel_count": int(efficiency[0]),
        "repeated_events_excluded_before_model": int(efficiency[1]),
        "model_input_count": int(efficiency[2]),
        "screening_cache_hits": int(efficiency[3]),
        "screening_tokens_saved_by_cache": int(efficiency[4]),
        "events_deferred_for_batch": int(efficiency[5]),
        "new_events_queued": int(efficiency[6]),
        "changed_events_reopened": int(efficiency[7]),
        "pending_events_before_screening": int(efficiency[8]),
        "runs_by_expansion_tier": {str(row[0]): int(row[1]) for row in tier_rows},
        "potential_waste_tokens_prevented": sum(row["potential_waste_tokens"] for row in blocked),
        "measurement_note": "potential_waste_tokens_prevented是代理指标，不等于实际账单节省；误杀率需要人工或后续研究结果标签。",
        "production_funnel": production_funnel(settings),
        "automatic_conclusion": conclusion,
    }


def write_rolling_evaluation(settings: Settings, days: int | None = None) -> Path:
    result = rolling_evaluation(settings, days)
    path = settings.root / "outputs/review/metrics" / f"novelty_rolling_{result['window_days']}d.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path
