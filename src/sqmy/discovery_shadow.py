from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .db import Database, now
from .models import EventItem
from .novelty import NoveltyAuditor
from .screener import normalize_title


@dataclass
class ShadowReview:
    event_id: str
    source_id: str
    title: str
    url: str
    event_role: str
    original_source_status: str
    original_source_url: str | None
    local_landing_status: str
    coverage_status: str
    recommendation: str
    reason_codes: list[str]
    policy_matches: list[dict]
    search_hits: list[dict]
    error: str | None = None


class ShadowVerifier:
    """
    在模型初筛前做元数据级信源和政策覆盖核验。

    结果只记录、不进入排序、不阻断事件，也不发起任何模型调用。
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self.cfg = settings.section("shadow_verification")
        if self.cfg.get("mode") != "shadow":
            raise ValueError("当前版本只支持不影响候选的 shadow 模式")
        if not 0 <= int(self.cfg["max_events"]) <= int(
            settings.section("discovery")["screened_max"]
        ):
            raise ValueError("影子核验事件数必须介于0与模型前池上限之间")
        if int(self.cfg["max_live_queries_per_run"]) < 0:
            raise ValueError("影子核验查询上限不得为负数")
        if int(self.cfg["max_hits_per_query"]) <= 0:
            raise ValueError("影子核验每次查询命中上限必须为正数")
        threshold = float(self.cfg["original_title_similarity_threshold"])
        if not 0 <= threshold <= 1:
            raise ValueError("原始信源标题相似度阈值必须介于0与1之间")
        self.auditor = NoveltyAuditor(settings)
        self.db = self.auditor.db
        self.collector = self.auditor.collector
        self._searches_used = 0

    def path_for(self, run_id: str) -> Path:
        return self.s.root / "data/runs" / run_id / "candidate_shadow_review.json"

    def review(
        self,
        run_id: str,
        events: list[EventItem],
        *,
        live_search: bool,
    ) -> list[ShadowReview]:
        self._searches_used = 0
        self._search_router = None
        if live_search and self.s.raw.get("search", {}).get("enabled", False):
            from .search_router import build_search_router
            self._search_router = build_search_router(self.s, self.db, run_id, "candidate_shadow", collector=self.collector)
        reviews: list[tuple[ShadowReview, str]] = []
        for event in events[: int(self.cfg["max_events"])]:
            input_hash = self._input_hash(event)
            cached = self._cached_review(run_id, event.id, input_hash)
            reviews.append((
                cached or self._review_one(event, live_search=live_search),
                input_hash,
            ))
        stamp = now()
        with self.db.connect() as conn:
            for item, input_hash in reviews:
                conn.execute(
                    """INSERT INTO discovery_shadow_reviews(
                         id,run_id,event_id,source_id,input_hash,title,url,event_role,
                         original_source_status,original_source_url,local_landing_status,
                         coverage_status,recommendation,reason_codes_json,
                         policy_matches_json,search_hits_json,enforced,error,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,event_id) DO UPDATE SET
                         source_id=excluded.source_id,input_hash=excluded.input_hash,
                         title=excluded.title,url=excluded.url,
                         event_role=excluded.event_role,
                         original_source_status=excluded.original_source_status,
                         original_source_url=excluded.original_source_url,
                         local_landing_status=excluded.local_landing_status,
                         coverage_status=excluded.coverage_status,
                         recommendation=excluded.recommendation,
                         reason_codes_json=excluded.reason_codes_json,
                         policy_matches_json=excluded.policy_matches_json,
                         search_hits_json=excluded.search_hits_json,
                         enforced=0,error=excluded.error,updated_at=excluded.updated_at""",
                    (
                        f"{run_id}:{item.event_id}", run_id, item.event_id, item.source_id,
                        input_hash, item.title, item.url, item.event_role, item.original_source_status,
                        item.original_source_url, item.local_landing_status,
                        item.coverage_status, item.recommendation,
                        json.dumps(item.reason_codes, ensure_ascii=False),
                        json.dumps(item.policy_matches, ensure_ascii=False),
                        json.dumps(item.search_hits, ensure_ascii=False),
                        0, item.error, stamp, stamp,
                    ),
                )
        self._write_current_report(run_id)
        return [item for item, _ in reviews]

    def _input_hash(self, event: EventItem) -> str:
        payload = {
            "event": {
                "id": event.id,
                "title": event.title,
                "url": event.url,
                "summary": event.summary,
                "region": event.region,
                "region_evidence": event.region_evidence,
                "source_level": event.source_level,
            },
            "config": self.cfg,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    def _cached_review(
        self, run_id: str, event_id: str, input_hash: str
    ) -> ShadowReview | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT * FROM discovery_shadow_reviews
                   WHERE run_id=? AND event_id=? AND input_hash=?""",
                (run_id, event_id, input_hash),
            ).fetchone()
        if row is None:
            return None
        return ShadowReview(
            event_id=row["event_id"],
            source_id=row["source_id"],
            title=row["title"],
            url=row["url"],
            event_role=row["event_role"],
            original_source_status=row["original_source_status"],
            original_source_url=row["original_source_url"],
            local_landing_status=row["local_landing_status"],
            coverage_status=row["coverage_status"],
            recommendation=row["recommendation"],
            reason_codes=json.loads(row["reason_codes_json"]),
            policy_matches=json.loads(row["policy_matches_json"]),
            search_hits=json.loads(row["search_hits_json"]),
            error=row["error"],
        )

    def _review_one(self, event: EventItem, *, live_search: bool) -> ShadowReview:
        text = f"{event.title} {event.summary}"
        role = self._event_role(text)
        if event.region == "全国":
            local_status = "national_scope"
        elif (
            event.region in {"海淀", "北京"}
            and not event.region_evidence.startswith("source_channel_only:")
        ):
            local_status = "supported"
        else:
            local_status = "unverified"
        original_status = "direct_primary" if self._is_official(event.url) else "unresolved"
        original_url = event.url if original_status == "direct_primary" else None
        search_hits: list[dict] = []
        errors: list[str] = []

        if original_status == "unresolved" and live_search:
            hits, error = self._search(
                f'"{event.title[:60]}" {self._official_scope()}'
            )
            if error:
                errors.append(error)
            search_hits.extend(self._metadata(hits, "original_source"))
            threshold = float(self.cfg["original_title_similarity_threshold"])
            possible = next((
                hit for hit in hits
                if self._is_official(hit.url)
                and SequenceMatcher(
                    None, normalize_title(event.title), normalize_title(hit.title)
                ).ratio() >= threshold
            ), None)
            if possible:
                original_status = "possible_primary"
                original_url = possible.url

        policy_matches = self.auditor.policy_matches(text)
        coverage_hits: list[EventItem] = []
        if not policy_matches and live_search:
            hits, error = self._search(
                f'{event.title[:50]} 政策 办法 通知 试点 {self._official_scope()}'
            )
            if error:
                errors.append(error)
            coverage_hits = [
                hit for hit in hits
                if self._is_official(hit.url)
                and any(
                    signal in hit.title + " " + hit.summary
                    for signal in self.cfg["coverage_signals"]
                )
            ]
            search_hits.extend(self._metadata(coverage_hits, "policy_coverage"))

        if policy_matches:
            coverage_status = "known_mechanism_match"
        elif coverage_hits:
            coverage_status = "possible_coverage"
        else:
            coverage_status = "unclear"

        reasons: list[str] = []
        if local_status == "unverified":
            reasons.append("local_landing_unverified")
        if original_status == "unresolved":
            reasons.append("original_source_unresolved")
        if coverage_status == "known_mechanism_match":
            reasons.append("known_policy_mechanism_match")
        elif coverage_status == "possible_coverage":
            reasons.append("possible_policy_coverage")
        if role in {"policy_announcement", "general_information"}:
            reasons.append("no_explicit_problem_signal")

        if role in {"policy_announcement", "general_information"}:
            recommendation = "monitor"
        elif local_status == "unverified" or original_status == "unresolved":
            recommendation = "needs_verification"
        elif coverage_status == "known_mechanism_match":
            recommendation = "reframe_or_monitor"
        else:
            recommendation = "eligible_for_comparison"
        return ShadowReview(
            event_id=event.id,
            source_id=event.source_id,
            title=event.title,
            url=event.url,
            event_role=role,
            original_source_status=original_status,
            original_source_url=original_url,
            local_landing_status=local_status,
            coverage_status=coverage_status,
            recommendation=recommendation,
            reason_codes=reasons,
            policy_matches=policy_matches,
            search_hits=search_hits,
            error="；".join(errors) if errors else None,
        )

    def _event_role(self, text: str) -> str:
        if any(signal in text for signal in self.cfg["problem_signals"]):
            return "problem_signal"
        if any(signal in text for signal in self.cfg["case_signals"]):
            return "case_signal"
        if any(signal in text for signal in self.cfg["data_signals"]):
            return "data_signal"
        if any(signal in text for signal in self.cfg["policy_signals"]):
            return "policy_announcement"
        return "general_information"

    def _official_scope(self) -> str:
        return "(" + " OR ".join(
            f"site:{domain}" for domain in self.cfg["official_domains"]
        ) + ")"

    def _is_official(self, url: str) -> bool:
        domain = urlparse(url).netloc.lower().split(":", 1)[0]
        return any(
            domain == configured or domain.endswith("." + configured)
            for configured in self.cfg["official_domains"]
        )

    def _search(self, query: str) -> tuple[list[EventItem], str | None]:
        if self._searches_used >= int(self.cfg["max_live_queries_per_run"]):
            return [], "shadow_search_budget_exhausted"
        self._searches_used += 1
        try:
            if getattr(self, "_search_router", None):
                from .retrieval_pipeline import search_events
                return search_events(self._search_router, query, limit=int(self.cfg["max_hits_per_query"]),
                                     domains=self.cfg["official_domains"]), None
            return self.collector.search_query(
                query,
                limit=int(self.cfg["max_hits_per_query"]),
                lookback_days=int(self.cfg["search_lookback_days"]),
            ), None
        except Exception:
            return [], "search_unavailable"

    @staticmethod
    def _metadata(events: list[EventItem], purpose: str) -> list[dict]:
        return [{
            "purpose": purpose,
            "title": item.title,
            "url": item.url,
            "source_level": item.source_level,
            "published_at": item.published_at,
            "excerpt": item.summary[:160],
        } for item in events]

    def update_outcomes(
        self,
        run_id: str,
        screened: list[EventItem],
        candidates: list,
        audits: list,
    ) -> None:
        screened_ids = {item.id for item in screened}
        candidate_ids = {
            item.score_reasons.get("事件ID") for item in candidates
            if item.score_reasons.get("事件ID")
        }
        audit_by_id = {item.event_id: item.decision for item in audits}
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT event_id FROM discovery_shadow_reviews WHERE run_id=?",
                (run_id,),
            ).fetchall()
            for row in rows:
                event_id = row["event_id"]
                conn.execute(
                    """UPDATE discovery_shadow_reviews
                       SET model_selected=?,candidate_selected=?,novelty_decision=?,updated_at=?
                       WHERE run_id=? AND event_id=?""",
                    (
                        int(event_id in screened_ids),
                        int(event_id in candidate_ids),
                        audit_by_id.get(event_id),
                        now(), run_id, event_id,
                    ),
                )
        self._write_current_report(run_id)

    def _write_current_report(self, run_id: str) -> None:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT event_id,source_id,title,url,event_role,
                          original_source_status,original_source_url,
                          local_landing_status,coverage_status,recommendation,
                          reason_codes_json,policy_matches_json,search_hits_json,
                          model_selected,candidate_selected,novelty_decision,error
                   FROM discovery_shadow_reviews WHERE run_id=? ORDER BY rowid""",
                (run_id,),
            ).fetchall()
        reviews = []
        for row in rows:
            item = dict(row)
            item["reason_codes"] = json.loads(item.pop("reason_codes_json"))
            item["policy_matches"] = json.loads(item.pop("policy_matches_json"))
            item["search_hits"] = json.loads(item.pop("search_hits_json"))
            item["model_selected"] = bool(item["model_selected"])
            item["candidate_selected"] = bool(item["candidate_selected"])
            reviews.append(item)
        path = self.path_for(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": "shadow",
            "enforced": False,
            "model_calls": 0,
            "notice": "本记录不参与本轮候选排序、阻断或模型输入取舍。",
            "reviews": reviews,
        }
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)


def write_discovery_evaluation(settings: Settings) -> Path:
    """生成21天滚动比较；只呈现漏斗和相关性，不自动改规则。"""
    cfg = settings.section("observability")
    cutoff = (datetime.now(timezone.utc) - timedelta(
        days=int(cfg["rolling_evaluation_days"])
    )).isoformat()
    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as conn:
        live_runs = conn.execute(
            """SELECT COUNT(DISTINCT sf.run_id)
               FROM source_funnel sf JOIN run_context rc ON rc.run_id=sf.run_id
               WHERE rc.mode='live' AND sf.updated_at>=?""",
            (cutoff,),
        ).fetchone()[0]
        observation_bounds = conn.execute(
            """SELECT MIN(sf.updated_at) AS earliest,MAX(sf.updated_at) AS latest
               FROM source_funnel sf JOIN run_context rc ON rc.run_id=sf.run_id
               WHERE rc.mode='live' AND sf.updated_at>=?""",
            (cutoff,),
        ).fetchone()
        source_rows = conn.execute(
            """SELECT sf.source_id,MAX(sf.source_name) AS source_name,
                      COUNT(DISTINCT sf.run_id) AS runs,
                      SUM(sf.included_in_scan) AS included_runs,
                      SUM(sf.raw_item_count) AS raw_items,
                      SUM(sf.collected_count) AS collected,
                      SUM(CASE WHEN sf.included_in_scan=1
                               THEN sf.collected_count ELSE 0 END) AS considered_collected,
                      SUM(sf.rule_qualified_count) AS rule_qualified,
                      SUM(sf.model_input_count) AS model_input,
                      SUM(sf.model_selected_count) AS model_selected,
                      SUM(sf.candidate_count) AS candidates
               FROM source_funnel sf JOIN run_context rc ON rc.run_id=sf.run_id
               WHERE rc.mode='live' AND sf.updated_at>=?
               GROUP BY sf.source_id ORDER BY candidates DESC,model_selected DESC""",
            (cutoff,),
        ).fetchall()
        source_history_rows = conn.execute(
            """SELECT sf.* FROM source_funnel sf
               JOIN run_context rc ON rc.run_id=sf.run_id
               WHERE rc.mode='live' AND sf.updated_at>=? AND sf.included_in_scan=1
               ORDER BY sf.source_id,sf.updated_at DESC""",
            (cutoff,),
        ).fetchall()
        shadow_rows = conn.execute(
            """SELECT ds.recommendation,COUNT(*) AS reviewed,
                      SUM(ds.model_selected) AS model_selected,
                      SUM(ds.candidate_selected) AS candidates
               FROM discovery_shadow_reviews ds
               JOIN run_context rc ON rc.run_id=ds.run_id
               WHERE rc.mode='live' AND ds.updated_at>=?
               GROUP BY ds.recommendation ORDER BY ds.recommendation""",
            (cutoff,),
        ).fetchall()
        shadow_detail_rows = conn.execute(
            """SELECT ds.* FROM discovery_shadow_reviews ds
               JOIN run_context rc ON rc.run_id=ds.run_id
               WHERE rc.mode='live' AND ds.updated_at>=?
               ORDER BY ds.updated_at""",
            (cutoff,),
        ).fetchall()
        candidate_rows = conn.execute(
            """SELECT c.run_id,c.id,c.data_json FROM candidates c
               JOIN run_context rc ON rc.run_id=c.run_id
               WHERE rc.mode='live' AND EXISTS(
                 SELECT 1 FROM source_funnel sf
                 WHERE sf.run_id=c.run_id AND sf.updated_at>=?
               )""",
            (cutoff,),
        ).fetchall()
        event_rows = conn.execute(
            """SELECT e.run_id,e.id,e.source_id FROM event_items e
               JOIN run_context rc ON rc.run_id=e.run_id
               WHERE rc.mode='live' AND EXISTS(
                 SELECT 1 FROM source_funnel sf
                 WHERE sf.run_id=e.run_id AND sf.updated_at>=?
               )""",
            (cutoff,),
        ).fetchall()
        review_rows = conn.execute(
            """SELECT rr.rowid AS row_number,rr.* FROM research_reviews rr
               JOIN run_context rc ON rc.run_id=rr.run_id
               WHERE rc.mode='live' AND EXISTS(
                 SELECT 1 FROM source_funnel sf
                 WHERE sf.run_id=rr.run_id AND sf.updated_at>=?
               ) ORDER BY rr.created_at,rr.rowid""",
            (cutoff,),
        ).fetchall()
        topic_rows = conn.execute(
            """SELECT t.* FROM topics t JOIN run_context rc ON rc.run_id=t.run_id
               WHERE rc.mode='live' AND EXISTS(
                 SELECT 1 FROM source_funnel sf
                 WHERE sf.run_id=t.run_id AND sf.updated_at>=?
               )""",
            (cutoff,),
        ).fetchall()
        sample_rows = conn.execute(
            """SELECT de.stage,COUNT(*) AS sampled,
                      SUM(CASE WHEN de.review_outcome IS NOT NULL THEN 1 ELSE 0 END) AS reviewed
               FROM discovery_exclusion_samples de
               JOIN run_context rc ON rc.run_id=de.run_id
               JOIN runs r ON r.id=de.run_id
               WHERE rc.mode='live' AND r.updated_at>=?
               GROUP BY de.stage ORDER BY de.stage""",
            (cutoff,),
        ).fetchall()
    minimum = int(cfg["minimum_live_runs_for_comparison"])
    minimum_span = int(cfg["minimum_observation_span_days"])
    observation_span = 0
    if observation_bounds["earliest"] and observation_bounds["latest"]:
        earliest = datetime.fromisoformat(observation_bounds["earliest"])
        latest = datetime.fromisoformat(observation_bounds["latest"])
        observation_span = max(0, (latest - earliest).days)
    ready = live_runs >= minimum and observation_span >= minimum_span

    event_sources: dict[tuple[str, str], str] = {}
    for row in event_rows:
        prefix = f"{row['run_id']}:"
        event_id = row["id"][len(prefix):] if row["id"].startswith(prefix) else row["id"]
        event_sources[(row["run_id"], event_id)] = row["source_id"]

    candidate_for_event: dict[tuple[str, str], str] = {}
    source_for_candidate: dict[tuple[str, str], str] = {}
    for row in candidate_rows:
        try:
            data = json.loads(row["data_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        logical_id = str(data.get("id") or row["id"].rsplit(":", 1)[-1])
        event_id = str((data.get("score_reasons") or {}).get("事件ID") or "")
        if not event_id:
            continue
        source_id = event_sources.get((row["run_id"], event_id))
        candidate_for_event[(row["run_id"], event_id)] = logical_id
        if source_id:
            source_for_candidate[(row["run_id"], logical_id)] = source_id

    latest_reviews: dict[tuple[str, str], Any] = {}
    for row in review_rows:
        candidate_id = str(row["candidate_id"])
        prefix = f"{row['run_id']}:"
        if candidate_id.startswith(prefix):
            candidate_id = candidate_id[len(prefix):]
        latest_reviews[(row["run_id"], candidate_id)] = row

    downstream: dict[str, dict[str, set[str]]] = {}
    for key, review in latest_reviews.items():
        source_id = source_for_candidate.get(key)
        if not source_id:
            continue
        metrics = downstream.setdefault(source_id, {
            "pre_research_reviewed": set(), "pre_research_proceed": set(),
            "drafts": set(), "approved": set(), "submitted": set(),
        })
        marker = f"{key[0]}:{key[1]}"
        metrics["pre_research_reviewed"].add(marker)
        if review["research_allowed"] and review["human_decision"] == "proceed":
            metrics["pre_research_proceed"].add(marker)
    for row in topic_rows:
        candidate_id = str(row["candidate_id"] or "")
        prefix = f"{row['run_id']}:"
        if candidate_id.startswith(prefix):
            candidate_id = candidate_id[len(prefix):]
        source_id = source_for_candidate.get((row["run_id"], candidate_id))
        if not source_id:
            continue
        metrics = downstream.setdefault(source_id, {
            "pre_research_reviewed": set(), "pre_research_proceed": set(),
            "drafts": set(), "approved": set(), "submitted": set(),
        })
        metrics["drafts"].add(row["id"])
        if row["approval_status"] == "approved":
            metrics["approved"].add(row["id"])
        if row["actually_submitted"]:
            metrics["submitted"].add(row["id"])

    histories: dict[str, list[Any]] = {}
    for row in source_history_rows:
        histories.setdefault(row["source_id"], []).append(row)
    zero_streak_limit = int(cfg["source_health_zero_result_streak"])
    irrelevant_min_runs = int(cfg["source_health_irrelevant_min_runs"])
    source_health = []
    for aggregate in source_rows:
        item = dict(aggregate)
        history = histories.get(item["source_id"], [])
        zero_streak = 0
        error_streak = 0
        for row in history:
            if int(row["collected_count"]) == 0:
                zero_streak += 1
            else:
                break
        for row in history:
            if row["fetch_error"] or row["parse_error"]:
                error_streak += 1
            else:
                break
        reasons = []
        if zero_streak >= zero_streak_limit:
            reasons.append("consecutive_zero_effective_results")
        if error_streak >= zero_streak_limit:
            reasons.append("consecutive_fetch_or_parse_errors")
        if (
            len(history) >= irrelevant_min_runs
            and sum(int(row["collected_count"]) for row in history) > 0
            and sum(int(row["rule_qualified_count"]) for row in history) == 0
        ):
            reasons.append("no_rule_qualified_results_in_window")
        if not ready or len(history) < zero_streak_limit:
            health_status = "insufficient_observation"
        elif reasons:
            health_status = "degraded"
        else:
            health_status = "active"
        item.update({
            "health_status": health_status,
            "health_reasons": reasons,
            "latest_zero_result_streak": zero_streak,
            "latest_error_streak": error_streak,
            "recommended_action": (
                "review_fetch_or_query_do_not_disable"
                if health_status == "degraded" else "keep_observing"
            ),
        })
        source_downstream = downstream.get(item["source_id"], {})
        for key in (
            "pre_research_reviewed", "pre_research_proceed", "drafts", "approved", "submitted"
        ):
            item[key] = len(source_downstream.get(key, set()))
        source_health.append(item)

    policy_outcomes: dict[str, int] = {}
    reviewed_followups = 0
    warning_count = 0
    decisive_count = 0
    confirmed_warning = 0
    false_warning = 0
    missed_coverage = 0
    for row in shadow_detail_rows:
        warning = row["coverage_status"] in {"known_mechanism_match", "possible_coverage"}
        warning_count += int(warning)
        candidate_id = candidate_for_event.get((row["run_id"], row["event_id"]))
        review = latest_reviews.get((row["run_id"], candidate_id)) if candidate_id else None
        if review is None:
            outcome = "not_observed_after_shadow"
        else:
            reviewed_followups += 1
            try:
                review_data = json.loads(review["data_json"] or "{}")
            except json.JSONDecodeError:
                review_data = {}
            reason = str(review_data.get("decision_reason") or "")
            feedback = str((review_data.get("novelty_feedback") or {}).get("outcome") or "")
            if reason == "policy_covered" or feedback == "missed_coverage":
                if warning:
                    outcome = "confirmed_coverage_warning"
                    confirmed_warning += 1
                else:
                    outcome = "missed_policy_coverage"
                    missed_coverage += 1
                decisive_count += 1
            elif warning and (
                reason == "original_gap_supported" or feedback == "supported_gap"
            ):
                outcome = "false_coverage_warning"
                false_warning += 1
                decisive_count += 1
            elif review["decision"] == "reframe" or feedback == "reframed":
                outcome = "reframed_after_warning" if warning else "reframed_without_warning"
            else:
                outcome = "other_followup"
        policy_outcomes[outcome] = policy_outcomes.get(outcome, 0) + 1

    minimum_outcomes = int(cfg["minimum_shadow_outcomes_for_assessment"])
    policy_ready = ready and decisive_count >= minimum_outcomes
    warning_precision = (
        confirmed_warning / (confirmed_warning + false_warning)
        if confirmed_warning + false_warning else None
    )
    coverage_recall = (
        confirmed_warning / (confirmed_warning + missed_coverage)
        if confirmed_warning + missed_coverage else None
    )
    if not policy_ready:
        policy_conclusion = "insufficient_followup"
    elif missed_coverage:
        policy_conclusion = "improve_policy_coverage_recall"
    elif false_warning:
        policy_conclusion = "review_false_warnings"
    else:
        policy_conclusion = "continue_shadow_observation"
    payload = {
        "generated_at": now(),
        "window_days": int(cfg["rolling_evaluation_days"]),
        "live_runs": live_runs,
        "status": "ready_for_comparison" if ready else "insufficient_observation",
        "minimum_live_runs_for_comparison": minimum,
        "observation_span_days": observation_span,
        "minimum_observation_span_days": minimum_span,
        "interpretation_limit": "影子建议与后续入选的关系只用于比较，不证明因果，不自动改变筛选规则。",
        "source_health_policy": {
            "automatic_source_changes": False,
            "zero_result_streak_threshold": zero_streak_limit,
            "irrelevant_minimum_runs": irrelevant_min_runs,
            "downstream_attribution_limit": (
                "只回连保留事件ID的自动候选；人工新增且无事件ID的候选不归因到来源。"
            ),
        },
        "source_funnel": source_health,
        "shadow_comparison": [dict(row) for row in shadow_rows],
        "policy_coverage_shadow_evaluation": {
            "mode": "shadow",
            "enforced": False,
            "status": "ready_for_assessment" if policy_ready else "insufficient_followup",
            "minimum_decisive_outcomes": minimum_outcomes,
            "reviewed_followups": reviewed_followups,
            "coverage_warning_count": warning_count,
            "decisive_outcome_count": decisive_count,
            "confirmed_coverage_warning_count": confirmed_warning,
            "false_coverage_warning_count": false_warning,
            "missed_policy_coverage_count": missed_coverage,
            "observed_warning_precision": (
                round(warning_precision, 4) if warning_precision is not None else None
            ),
            "observed_coverage_recall": (
                round(coverage_recall, 4) if coverage_recall is not None else None
            ),
            "outcome_counts": policy_outcomes,
            "automatic_conclusion": policy_conclusion,
            "interpretation_limit": (
                "只有后续预研明确标为policy_covered或original_gap_supported的结果"
                "才用于准确率；reframe不自动视为政策覆盖。报告不得自动开启阻断。"
            ),
        },
        "exclusion_samples": [dict(row) for row in sample_rows],
    }
    path = settings.root / "outputs/review/discovery_observability_rolling.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path
