from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED_QUOTA = "paused_quota"
    PAUSED_BUDGET = "paused_budget"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"


class Phase(StrEnum):
    DISCOVERY = "discovery"
    SELECTION = "selection"
    INCREMENTAL_REVIEW = "incremental_review"
    RESEARCH = "research"
    WRITING = "writing"
    EXPORT = "export"


@dataclass
class Candidate:
    id: str
    title: str
    summary: str
    event_date: str
    region: str
    affected_group: str
    institutional_conflict: str
    pain_point: str
    policy_gap: str
    policy_entry: str
    authority: str
    data_sufficiency: str
    policy_window: str
    history_relation: str
    priority: str
    risk: str
    recommendation: str
    score: int
    gap_hypothesis: str = ""
    gap_type: str = "unclear"
    coverage_status: str = "unchecked"
    counterevidence: list[dict[str, Any]] = field(default_factory=list)
    novelty_decision: str = "proceed"
    reframe_suggestion: str = ""
    score_reasons: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventItem:
    id: str
    source_id: str
    source_name: str
    source_level: int
    title: str
    url: str
    published_at: str
    summary: str
    region: str
    source_region: str = ""
    region_evidence: str = ""
    topics: list[str] = field(default_factory=list)
    rule_score: int = 0
    collected_at: str = ""
