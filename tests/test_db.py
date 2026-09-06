from pathlib import Path
import json
import sqlite3
import tempfile

from sqmy.db import Database, SCHEMA, now


def test_legacy_model_calls_migrate_without_rewriting_usage(tmp_path):
    db = Database(tmp_path / "workflow.db")
    with db.connect() as conn:
        conn.execute("""CREATE TABLE model_calls (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT,
            provider TEXT NOT NULL, model TEXT NOT NULL, prompt_hash TEXT NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
            estimated_cost_cny REAL NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO model_calls VALUES(1,'old','screening','deepseek','old','hash',100,20,0.1,'completed','2026-08-01')")
        original = tuple(conn.execute("SELECT * FROM model_calls").fetchone())
    db.initialize()
    db.initialize()
    with db.connect() as conn:
        migrated = conn.execute("SELECT * FROM model_calls").fetchone()
        assert tuple(migrated)[:len(original)] == original
        assert migrated["accounting_method"] == "provider_reported"
        assert migrated["response_id"] is None
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_evidence_migration_preserves_legacy_ids_links_and_marks_shared_sources(tmp_path):
    path = tmp_path / "workflow.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA.replace("id TEXT PRIMARY KEY, local_id TEXT, topic_id", "id TEXT PRIMARY KEY, topic_id"))
        conn.execute("DROP TABLE source_usages")
        for i, topic in enumerate(("topic-a", "topic-b"), 1):
            conn.execute(
                "INSERT INTO claims(id,topic_id,claim_text,claim_type,importance,created_at) VALUES(?,?,?,?,?,?)",
                (f"c{i}", topic, topic, "policy", "critical", now()),
            )
        conn.execute(
            "INSERT INTO sources(topic_id,source_name,page_title,url,fetched_at,excerpt,checked_at) VALUES(?,?,?,?,?,?,?)",
            ("topic-a", "共享", "共享文件", "https://example.test/shared", now(), "原摘录", now()),
        )
        conn.execute(
            "INSERT INTO sources(topic_id,source_name,page_title,url,fetched_at,excerpt,checked_at) VALUES(?,?,?,?,?,?,?)",
            ("topic-a", "单题", "单题文件", "https://example.test/single", now(), "单题摘录", now()),
        )
        for claim, source in (("c1", 1), ("c2", 1), ("c1", 2)):
            conn.execute(
                "INSERT INTO claim_sources(claim_id,source_id,evidence_role,origin_group,source_level) VALUES(?,?,?,?,?)",
                (claim, source, "supports", "origin", 2),
            )
        before_sources = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        before_links = conn.execute("SELECT * FROM claim_sources ORDER BY claim_id,source_id").fetchall()
    database = Database(path)
    database.initialize()
    database.initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM sources ORDER BY id").fetchall() == before_sources
        assert conn.execute("SELECT * FROM claim_sources ORDER BY claim_id,source_id").fetchall() == before_links
        assert conn.execute("SELECT id,local_id,topic_id,claim_text FROM claims ORDER BY id").fetchall() == [
            ("c1", "c1", "topic-a", "topic-a"), ("c2", "c2", "topic-b", "topic-b"),
        ]
        usages = conn.execute("SELECT topic_id,source_id,needs_review FROM source_usages ORDER BY topic_id,source_id").fetchall()
        assert usages == [("topic-a", 1, 1), ("topic-a", 2, 0), ("topic-b", 1, 1)]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


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
            "stage_usage",
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
