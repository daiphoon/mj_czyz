from pathlib import Path
import json
import sqlite3
import tempfile

from sqmy.db import Database, now


def test_legacy_topics_table_migrates_forward_and_preserves_approved_file_state():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "workflow.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE topics(
                     id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,
                     final_path TEXT,actually_submitted INTEGER NOT NULL DEFAULT 0
                   )"""
            )
            conn.execute(
                "INSERT INTO topics(id,title,created_at,final_path) VALUES(?,?,?,?)",
                (
                    "legacy-approved",
                    "历史已通过稿",
                    "2026-07-01T00:00:00+00:00",
                    "/tmp/project/outputs/submission/历史已通过稿.docx",
                ),
            )
        Database(path).initialize()
        with sqlite3.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(topics)")}
            source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
            claim_columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)")}
            call_columns = {row[1] for row in conn.execute("PRAGMA table_info(model_calls)")}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            status = conn.execute(
                "SELECT approval_status FROM topics WHERE id='legacy-approved'"
            ).fetchone()[0]
        assert {"run_id", "candidate_id", "review_path", "approval_status", "approved_at"} <= columns
        assert {"source_role", "checked_at", "effective_at"} <= source_columns
        assert {"epistemic_status", "confidence", "falsifier", "scope_json"} <= claim_columns
        assert {"estimated_tokens", "stage_limit", "over_budget"} <= call_columns
        assert {
            "research_reviews",
            "budget_adjustments",
            "discovery_queue",
            "source_funnel",
            "discovery_exclusion_samples",
            "discovery_shadow_reviews",
        } <= tables
        assert status == "approved"


def test_legacy_run_efficiency_migrates_daily_queue_metrics_forward():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "workflow.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE run_efficiency(
                     run_id TEXT PRIMARY KEY,premodel_count INTEGER NOT NULL DEFAULT 0,
                     repeated_excluded INTEGER NOT NULL DEFAULT 0,
                     model_input_count INTEGER NOT NULL DEFAULT 0,
                     screening_cache_hit INTEGER NOT NULL DEFAULT 0,
                     screening_tokens_saved INTEGER NOT NULL DEFAULT 0,
                     candidate_count INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL
                   )"""
            )
        Database(path).initialize()
        with sqlite3.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(run_efficiency)")}
        assert {
            "deferred_count",
            "new_event_count",
            "reopened_event_count",
            "pending_before_count",
            "expansion_tier",
        } <= columns


def test_topic_run_link_is_backfilled_from_existing_checkpoint():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "workflow.db"
        database = Database(path)
        database.initialize()
        stamp = now()
        with database.connect() as conn:
            conn.execute(
                """INSERT INTO runs(
                     id,phase,status,config_hash,checkpoint_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    "run-1",
                    "export",
                    "completed",
                    "hash",
                    json.dumps({"topic_id": "topic-1"}),
                    stamp,
                    stamp,
                ),
            )
            conn.execute(
                "INSERT INTO topics(id,title,created_at) VALUES(?,?,?)",
                ("topic-1", "已通过稿", stamp),
            )
        database.initialize()
        with database.connect() as conn:
            run_id = conn.execute(
                "SELECT run_id FROM topics WHERE id='topic-1'"
            ).fetchone()[0]
        assert run_id == "run-1"
