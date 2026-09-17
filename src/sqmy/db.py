from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, phase TEXT NOT NULL, status TEXT NOT NULL,
  config_hash TEXT NOT NULL, token_used INTEGER NOT NULL DEFAULT 0,
  estimated_cost_cny REAL NOT NULL DEFAULT 0, checkpoint_json TEXT NOT NULL DEFAULT '{}',
  error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_context (
  run_id TEXT PRIMARY KEY REFERENCES runs(id), mode TEXT NOT NULL,
  forced INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_efficiency (
  run_id TEXT PRIMARY KEY REFERENCES runs(id), premodel_count INTEGER NOT NULL DEFAULT 0,
  repeated_excluded INTEGER NOT NULL DEFAULT 0, model_input_count INTEGER NOT NULL DEFAULT 0,
  screening_cache_hit INTEGER NOT NULL DEFAULT 0,
  screening_tokens_saved INTEGER NOT NULL DEFAULT 0,
  deferred_count INTEGER NOT NULL DEFAULT 0,
  new_event_count INTEGER NOT NULL DEFAULT 0,
  reopened_event_count INTEGER NOT NULL DEFAULT 0,
  pending_before_count INTEGER NOT NULL DEFAULT 0,
  expansion_tier INTEGER NOT NULL DEFAULT 1,
  candidate_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL,
  input_hash TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
  token_used INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT, updated_at TEXT NOT NULL, UNIQUE(run_id, kind, input_hash)
);
CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), title TEXT NOT NULL,
  data_json TEXT NOT NULL, score INTEGER NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
  submitted_at TEXT, submission_level TEXT, category TEXT, region TEXT,
  affected_group TEXT, core_problem TEXT, mechanism_entry TEXT,
  recommendation_summary TEXT, data_used TEXT, final_path TEXT,
  actually_submitted INTEGER NOT NULL DEFAULT 0, adopted INTEGER,
  feedback TEXT, later_changes TEXT, similarity REAL, suitable_for_reresearch INTEGER,
  run_id TEXT, candidate_id TEXT, draft_source_path TEXT, review_path TEXT,
  approval_status TEXT NOT NULL DEFAULT 'not_reviewed', approved_at TEXT
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id TEXT, source_name TEXT NOT NULL,
  page_title TEXT NOT NULL, url TEXT NOT NULL, publisher TEXT, published_at TEXT,
  fetched_at TEXT NOT NULL, excerpt TEXT, data_scope TEXT, used_at TEXT,
  second_verified INTEGER NOT NULL DEFAULT 0, second_source_json TEXT,
  content_hash TEXT, source_role TEXT NOT NULL DEFAULT 'unclassified',
  checked_at TEXT, effective_at TEXT, UNIQUE(url, content_hash)
);
CREATE TABLE IF NOT EXISTS delivery_events (
  topic_id TEXT PRIMARY KEY REFERENCES topics(id), run_id TEXT NOT NULL REFERENCES runs(id),
  ready_at TEXT NOT NULL, output_path TEXT NOT NULL, output_sha256 TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL, review_id TEXT NOT NULL REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY, local_id TEXT, topic_id TEXT NOT NULL, claim_text TEXT NOT NULL,
  claim_type TEXT NOT NULL, importance TEXT NOT NULL,
  novelty_required INTEGER NOT NULL DEFAULT 0,
  policy_coverage_status TEXT NOT NULL DEFAULT 'unchecked',
  epistemic_status TEXT NOT NULL DEFAULT 'unclassified',
  confidence TEXT NOT NULL DEFAULT 'unrated', uncertainty_reason TEXT,
  falsifier TEXT, as_of_date TEXT, scope_json TEXT, reasoning TEXT,
  conflicts_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_sources (
  claim_id TEXT NOT NULL REFERENCES claims(id),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  evidence_role TEXT NOT NULL, origin_group TEXT NOT NULL,
  source_level INTEGER NOT NULL, primary_source INTEGER NOT NULL DEFAULT 0,
  notes TEXT, evidence_detail_json TEXT, PRIMARY KEY (claim_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_topic ON claims(topic_id);
CREATE INDEX IF NOT EXISTS idx_claim_sources_claim ON claim_sources(claim_id);
CREATE TABLE IF NOT EXISTS source_usages (
  topic_id TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
  metadata_json TEXT NOT NULL, needs_review INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(topic_id, source_id)
);
CREATE TABLE IF NOT EXISTS policy_mechanisms (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, problem_type TEXT NOT NULL,
  jurisdiction TEXT NOT NULL, actor TEXT NOT NULL, summary TEXT NOT NULL,
  keywords_json TEXT NOT NULL, source_url TEXT NOT NULL, valid_from TEXT,
  last_verified_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS novelty_audits (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), event_id TEXT NOT NULL,
  title TEXT NOT NULL, gap_hypothesis TEXT NOT NULL, gap_type TEXT NOT NULL,
  coverage_status TEXT NOT NULL, decision TEXT NOT NULL,
  queries_json TEXT NOT NULL, counterevidence_json TEXT NOT NULL,
  policy_matches_json TEXT NOT NULL, audit_tokens INTEGER NOT NULL DEFAULT 0,
  potential_waste_tokens INTEGER NOT NULL DEFAULT 0,
  review_outcome TEXT, review_reason TEXT, reviewed_at TEXT, created_at TEXT NOT NULL,
  UNIQUE(run_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_novelty_audits_created ON novelty_audits(created_at);
CREATE INDEX IF NOT EXISTS idx_novelty_audits_run ON novelty_audits(run_id);
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT,
  provider TEXT NOT NULL, model TEXT NOT NULL, prompt_hash TEXT NOT NULL,
  input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  estimated_cost_cny REAL NOT NULL, status TEXT NOT NULL,
  estimated_tokens INTEGER, stage_limit INTEGER,
  accounting_method TEXT NOT NULL DEFAULT 'provider_reported',
  response_id TEXT, error_code TEXT,
  over_budget INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id), topic_id TEXT NOT NULL,
  stage TEXT NOT NULL, execution_mode TEXT NOT NULL DEFAULT 'interactive_codex',
  accounting_method TEXT NOT NULL DEFAULT 'declared_stage_cap',
  provider TEXT NOT NULL, model TEXT NOT NULL,
  token_used INTEGER NOT NULL, input_hash TEXT NOT NULL,
  note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(run_id,topic_id,stage,execution_mode)
);
CREATE TABLE IF NOT EXISTS retrieval_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  action TEXT NOT NULL, endpoint TEXT NOT NULL, request_hash TEXT NOT NULL,
  status TEXT NOT NULL, reserved_credits REAL NOT NULL,
  reported_credits REAL, accounted_credits REAL NOT NULL,
  cost_equivalent_usd REAL NOT NULL, accounting_method TEXT NOT NULL,
  result_json TEXT, error_code TEXT, cached_from INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_retrieval_calls_action
  ON retrieval_calls(run_id, action, request_hash);
CREATE INDEX IF NOT EXISTS idx_stage_usage_updated
  ON stage_usage(updated_at DESC, stage);
CREATE TABLE IF NOT EXISTS research_reviews (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  candidate_id TEXT NOT NULL, topic_id TEXT NOT NULL, input_hash TEXT NOT NULL,
  decision TEXT NOT NULL, confidence TEXT NOT NULL,
  research_allowed INTEGER NOT NULL DEFAULT 0,
  data_json TEXT NOT NULL, report_path TEXT NOT NULL,
  human_decision TEXT, human_note TEXT, reviewed_at TEXT,
  created_at TEXT NOT NULL, UNIQUE(run_id,candidate_id,input_hash)
);
CREATE INDEX IF NOT EXISTS idx_research_reviews_latest
  ON research_reviews(run_id,candidate_id,created_at DESC);
CREATE TABLE IF NOT EXISTS budget_adjustments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
  stage TEXT NOT NULL, old_limit INTEGER NOT NULL, new_limit INTEGER NOT NULL,
  reason TEXT NOT NULL, expected_benefit TEXT NOT NULL,
  actual_tokens INTEGER NOT NULL DEFAULT 0, actual_benefit TEXT,
  decision TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_budget_adjustments_created
  ON budget_adjustments(created_at DESC);
CREATE TABLE IF NOT EXISTS event_items (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), source_id TEXT NOT NULL,
  source_name TEXT NOT NULL, source_level INTEGER NOT NULL, title TEXT NOT NULL,
  url TEXT NOT NULL, published_at TEXT, summary TEXT, region TEXT,
  topics_json TEXT NOT NULL, rule_score INTEGER NOT NULL, content_hash TEXT NOT NULL,
  collected_at TEXT NOT NULL, source_region TEXT NOT NULL DEFAULT '',
  region_evidence TEXT NOT NULL DEFAULT '', expansion_tier INTEGER NOT NULL DEFAULT 1,
  UNIQUE(run_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_event_items_run_score ON event_items(run_id, rule_score DESC);
CREATE TABLE IF NOT EXISTS discovery_queue (
  event_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
  status TEXT NOT NULL, event_json TEXT NOT NULL,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  first_run_id TEXT NOT NULL, last_run_id TEXT NOT NULL,
  screened_run_id TEXT, screened_at TEXT,
  reopen_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_discovery_queue_status_seen
  ON discovery_queue(status, first_seen_at);
CREATE TABLE IF NOT EXISTS source_funnel (
  run_id TEXT NOT NULL REFERENCES runs(id), source_id TEXT NOT NULL,
  source_name TEXT NOT NULL, expansion_tier INTEGER NOT NULL,
  included_in_scan INTEGER NOT NULL DEFAULT 0,
  raw_item_count INTEGER NOT NULL DEFAULT 0,
  within_window_count INTEGER NOT NULL DEFAULT 0,
  collected_count INTEGER NOT NULL DEFAULT 0,
  invalid_metadata_count INTEGER NOT NULL DEFAULT 0,
  outside_window_count INTEGER NOT NULL DEFAULT 0,
  rule_qualified_count INTEGER NOT NULL DEFAULT 0,
  rule_excluded_count INTEGER NOT NULL DEFAULT 0,
  rule_cap_excluded_count INTEGER NOT NULL DEFAULT 0,
  history_excluded_count INTEGER NOT NULL DEFAULT 0,
  premodel_count INTEGER NOT NULL DEFAULT 0,
  pool_cap_excluded_count INTEGER NOT NULL DEFAULT 0,
  model_input_count INTEGER NOT NULL DEFAULT 0,
  model_selected_count INTEGER NOT NULL DEFAULT 0,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  fetch_error TEXT, parse_error TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY(run_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_source_funnel_updated
  ON source_funnel(updated_at DESC, source_id);
CREATE TABLE IF NOT EXISTS discovery_exclusion_samples (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  event_hash TEXT NOT NULL, source_id TEXT NOT NULL,
  stage TEXT NOT NULL, reason_code TEXT NOT NULL,
  title TEXT NOT NULL, url TEXT NOT NULL, published_at TEXT,
  region TEXT, rule_score INTEGER NOT NULL DEFAULT 0,
  sample_rank INTEGER NOT NULL, review_outcome TEXT,
  review_note TEXT, reviewed_at TEXT, created_at TEXT NOT NULL,
  UNIQUE(run_id, stage, event_hash)
);
CREATE INDEX IF NOT EXISTS idx_discovery_exclusion_samples_run
  ON discovery_exclusion_samples(run_id, stage, sample_rank);
CREATE TABLE IF NOT EXISTS discovery_shadow_reviews (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  event_id TEXT NOT NULL, source_id TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  title TEXT NOT NULL, url TEXT NOT NULL,
  event_role TEXT NOT NULL, original_source_status TEXT NOT NULL,
  original_source_url TEXT, local_landing_status TEXT NOT NULL,
  coverage_status TEXT NOT NULL, recommendation TEXT NOT NULL,
  reason_codes_json TEXT NOT NULL, policy_matches_json TEXT NOT NULL,
  search_hits_json TEXT NOT NULL, model_selected INTEGER NOT NULL DEFAULT 0,
  candidate_selected INTEGER NOT NULL DEFAULT 0, novelty_decision TEXT,
  enforced INTEGER NOT NULL DEFAULT 0, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(run_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_discovery_shadow_reviews_run
  ON discovery_shadow_reviews(run_id, recommendation);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise ValueError("数据库版本高于当前程序支持范围，不得使用旧程序写入")
            conn.executescript(SCHEMA)
            link_columns = {row[1] for row in conn.execute("PRAGMA table_info(claim_sources)")}
            if "evidence_detail_json" not in link_columns:
                conn.execute("ALTER TABLE claim_sources ADD COLUMN evidence_detail_json TEXT")
            efficiency_columns = {row[1] for row in conn.execute("PRAGMA table_info(run_efficiency)")}
            efficiency_migrations = {
                "deferred_count": "INTEGER NOT NULL DEFAULT 0",
                "new_event_count": "INTEGER NOT NULL DEFAULT 0",
                "reopened_event_count": "INTEGER NOT NULL DEFAULT 0",
                "pending_before_count": "INTEGER NOT NULL DEFAULT 0",
                "expansion_tier": "INTEGER NOT NULL DEFAULT 1",
            }
            for column, declaration in efficiency_migrations.items():
                if column not in efficiency_columns:
                    conn.execute(
                        f"ALTER TABLE run_efficiency ADD COLUMN {column} {declaration}"
                    )
            event_columns = {row[1] for row in conn.execute("PRAGMA table_info(event_items)")}
            if "source_region" not in event_columns:
                conn.execute("ALTER TABLE event_items ADD COLUMN source_region TEXT NOT NULL DEFAULT ''")
            if "region_evidence" not in event_columns:
                conn.execute("ALTER TABLE event_items ADD COLUMN region_evidence TEXT NOT NULL DEFAULT ''")
            if "expansion_tier" not in event_columns:
                conn.execute("ALTER TABLE event_items ADD COLUMN expansion_tier INTEGER NOT NULL DEFAULT 1")
            if "material_json" not in event_columns:
                conn.execute("ALTER TABLE event_items ADD COLUMN material_json TEXT NOT NULL DEFAULT '{}'")
            topic_columns = {row[1] for row in conn.execute("PRAGMA table_info(topics)")}
            topic_migrations = {
                "run_id": "TEXT",
                "candidate_id": "TEXT",
                "draft_source_path": "TEXT",
                "review_path": "TEXT",
                "approval_status": "TEXT NOT NULL DEFAULT 'not_reviewed'",
                "approved_at": "TEXT",
            }
            for column, declaration in topic_migrations.items():
                if column not in topic_columns:
                    conn.execute(f"ALTER TABLE topics ADD COLUMN {column} {declaration}")
            source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
            source_migrations = {
                "source_role": "TEXT NOT NULL DEFAULT 'unclassified'",
                "checked_at": "TEXT",
                "effective_at": "TEXT",
            }
            for column, declaration in source_migrations.items():
                if column not in source_columns:
                    conn.execute(f"ALTER TABLE sources ADD COLUMN {column} {declaration}")
            claim_columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)")}
            claim_migrations = {
                "local_id": "TEXT",
                "epistemic_status": "TEXT NOT NULL DEFAULT 'unclassified'",
                "confidence": "TEXT NOT NULL DEFAULT 'unrated'",
                "uncertainty_reason": "TEXT",
                "falsifier": "TEXT",
                "as_of_date": "TEXT",
                "scope_json": "TEXT",
                "reasoning": "TEXT",
                "conflicts_json": "TEXT",
            }
            for column, declaration in claim_migrations.items():
                if column not in claim_columns:
                    conn.execute(f"ALTER TABLE claims ADD COLUMN {column} {declaration}")
            conn.execute("UPDATE claims SET local_id=id WHERE local_id IS NULL")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_local_id ON claims(topic_id,local_id)"
            )
            self._backfill_source_usages(conn)
            model_call_columns = {row[1] for row in conn.execute("PRAGMA table_info(model_calls)")}
            model_call_migrations = {
                "accounting_method": "TEXT NOT NULL DEFAULT 'provider_reported'",
                "response_id": "TEXT",
                "error_code": "TEXT",
                "estimated_tokens": "INTEGER",
                "stage_limit": "INTEGER",
                "over_budget": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, declaration in model_call_migrations.items():
                if column not in model_call_columns:
                    conn.execute(f"ALTER TABLE model_calls ADD COLUMN {column} {declaration}")
            shadow_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(discovery_shadow_reviews)")
            }
            if "input_hash" not in shadow_columns:
                conn.execute(
                    "ALTER TABLE discovery_shadow_reviews ADD COLUMN input_hash TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """UPDATE topics SET approval_status='approved'
                   WHERE approval_status='not_reviewed'
                     AND (actually_submitted=1 OR final_path LIKE '%/outputs/submission/%')"""
            )
            unlinked_topics = conn.execute(
                "SELECT id FROM topics WHERE run_id IS NULL"
            ).fetchall()
            run_checkpoints = conn.execute(
                "SELECT id,checkpoint_json FROM runs ORDER BY updated_at"
            ).fetchall()
            topic_runs: dict[str, str] = {}
            for run in run_checkpoints:
                try:
                    topic_id = json.loads(run["checkpoint_json"] or "{}").get("topic_id")
                except json.JSONDecodeError:
                    continue
                if topic_id:
                    topic_runs[topic_id] = run["id"]
            for topic in unlinked_topics:
                if topic["id"] in topic_runs:
                    conn.execute(
                        "UPDATE topics SET run_id=? WHERE id=?",
                        (topic_runs[topic["id"]], topic["id"]),
                    )
            # Backfill legacy runs so old fixture/mock candidates cannot pollute
            # real-run history and rolling quality metrics.
            conn.execute(
                """INSERT OR IGNORE INTO run_context(run_id,mode,forced,created_at)
                   SELECT r.id,
                     CASE
                       WHEN r.token_used > 0 AND EXISTS(
                         SELECT 1 FROM event_items e WHERE e.run_id=r.id
                       ) THEN 'live'
                       WHEN EXISTS(SELECT 1 FROM event_items e WHERE e.run_id=r.id) THEN 'test_legacy'
                       ELSE 'legacy'
                     END,
                     0,r.created_at
                   FROM runs r"""
            )
            conn.execute("PRAGMA user_version=1")

    @staticmethod
    def _backfill_source_usages(conn: sqlite3.Connection) -> None:
        # Additive migration: retain legacy sources, claim IDs and all links unchanged.
        associations = conn.execute(
            """SELECT c.topic_id,cs.source_id FROM claims c
               JOIN claim_sources cs ON cs.claim_id=c.id
               UNION SELECT topic_id,id FROM sources WHERE topic_id IS NOT NULL"""
        ).fetchall()
        owners: dict[int, set[str]] = {}
        for row in associations:
            owners.setdefault(row["source_id"], set()).add(row["topic_id"])
        missing = conn.execute(
            """SELECT a.topic_id AS usage_topic,s.* FROM (
                 SELECT c.topic_id,cs.source_id FROM claims c
                 JOIN claim_sources cs ON cs.claim_id=c.id
                 UNION SELECT topic_id,id FROM sources WHERE topic_id IS NOT NULL
               ) a JOIN sources s ON s.id=a.source_id
               LEFT JOIN source_usages u ON u.topic_id=a.topic_id AND u.source_id=a.source_id
               WHERE u.source_id IS NULL"""
        ).fetchall()
        for row in missing:
            metadata = dict(row)
            topic_id = metadata.pop("usage_topic")
            conn.execute(
                "INSERT INTO source_usages(topic_id,source_id,metadata_json,needs_review) VALUES(?,?,?,?)",
                (topic_id, row["id"], json.dumps(metadata, ensure_ascii=False),
                 int(len(owners[row["id"]]) > 1)),
            )

    def checkpoint(self, run_id: str, *, phase: str, status: str, data: dict, error: str | None = None) -> None:
        stamp = now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET phase=?, status=?, checkpoint_json=?, error=?, updated_at=? WHERE id=?",
                (phase, status, json.dumps(data, ensure_ascii=False), error, stamp, run_id),
            )
