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
  feedback TEXT, later_changes TEXT, similarity REAL, suitable_for_reresearch INTEGER
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id TEXT, source_name TEXT NOT NULL,
  page_title TEXT NOT NULL, url TEXT NOT NULL, publisher TEXT, published_at TEXT,
  fetched_at TEXT NOT NULL, excerpt TEXT, data_scope TEXT, used_at TEXT,
  second_verified INTEGER NOT NULL DEFAULT 0, second_source_json TEXT,
  content_hash TEXT, UNIQUE(url, content_hash)
);
CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, claim_text TEXT NOT NULL,
  claim_type TEXT NOT NULL, importance TEXT NOT NULL,
  novelty_required INTEGER NOT NULL DEFAULT 0,
  policy_coverage_status TEXT NOT NULL DEFAULT 'unchecked',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_sources (
  claim_id TEXT NOT NULL REFERENCES claims(id),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  evidence_role TEXT NOT NULL, origin_group TEXT NOT NULL,
  source_level INTEGER NOT NULL, primary_source INTEGER NOT NULL DEFAULT 0,
  notes TEXT, PRIMARY KEY (claim_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_topic ON claims(topic_id);
CREATE INDEX IF NOT EXISTS idx_claim_sources_claim ON claim_sources(claim_id);
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
  estimated_cost_cny REAL NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
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
            conn.executescript(SCHEMA)
            efficiency_columns = {row[1] for row in conn.execute("PRAGMA table_info(run_efficiency)")}
            if "deferred_count" not in efficiency_columns:
                conn.execute("ALTER TABLE run_efficiency ADD COLUMN deferred_count INTEGER NOT NULL DEFAULT 0")
            if "expansion_tier" not in efficiency_columns:
                conn.execute("ALTER TABLE run_efficiency ADD COLUMN expansion_tier INTEGER NOT NULL DEFAULT 1")
            event_columns = {row[1] for row in conn.execute("PRAGMA table_info(event_items)")}
            if "source_region" not in event_columns:
                conn.execute("ALTER TABLE event_items ADD COLUMN source_region TEXT NOT NULL DEFAULT ''")
            if "region_evidence" not in event_columns:
                conn.execute("ALTER TABLE event_items ADD COLUMN region_evidence TEXT NOT NULL DEFAULT ''")
            if "expansion_tier" not in event_columns:
                conn.execute("ALTER TABLE event_items ADD COLUMN expansion_tier INTEGER NOT NULL DEFAULT 1")
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

    def checkpoint(self, run_id: str, *, phase: str, status: str, data: dict, error: str | None = None) -> None:
        stamp = now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET phase=?, status=?, checkpoint_json=?, error=?, updated_at=? WHERE id=?",
                (phase, status, json.dumps(data, ensure_ascii=False), error, stamp, run_id),
            )
