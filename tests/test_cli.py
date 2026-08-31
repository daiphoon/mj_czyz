from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import pytest

from sqmy.cli import main, parser
from sqmy.config import Settings
from sqmy.db import Database


ROOT = Path(__file__).parents[1]


def test_mock_generation_command_is_explicit_and_legacy_export_is_removed():
    command = parser().parse_args(["generate-mock", "run-1"])
    assert command.command == "generate-mock"
    with pytest.raises(SystemExit):
        parser().parse_args(["export", "run-1"])


def test_cli_exposes_stage_aware_and_real_lifecycle_commands():
    scan = parser().parse_args(["scan", "--resume", "run-1"])
    assert scan.resume_run_id == "run-1"
    immediate = parser().parse_args(["scan", "--screen-now"])
    assert immediate.screen_now
    monday = parser().parse_args(["monday", "--resume", "run-1"])
    assert monday.resume_run_id == "run-1"
    expanded = parser().parse_args(
        ["monday", "--start-tier", "4", "--clues", "clues.jsonl"]
    )
    assert expanded.start_tier == 4
    assert expanded.clue_file == Path("clues.jsonl")
    preflight = parser().parse_args(["preflight", "--stage", "refresh"])
    assert preflight.stage == "refresh"
    pre_research_preflight = parser().parse_args(
        ["preflight", "--stage", "pre_research", "--run-id", "run-1"]
    )
    assert pre_research_preflight.run_id == "run-1"
    refresh = parser().parse_args(
        ["refresh", "run-1", "--decision", "keep", "--note", "人工确认无重大变化"]
    )
    assert refresh.decision == "keep"
    thursday = parser().parse_args(
        ["thursday", "run-1", "--decision", "keep", "--note", "人工确认无重大变化"]
    )
    assert thursday.decision == "keep"
    assert parser().parse_args(["candidate-pool"]).command == "candidate-pool"
    assert parser().parse_args(["scan-replay", "run-1"]).source_run_id == "run-1"
    assert parser().parse_args(["scan-mock"]).command == "scan-mock"
    draft = parser().parse_args(
        ["draft", "run-1", "topic-1", "--source", "formal.md", "--candidate-id", "C1"]
    )
    assert draft.topic_id == "topic-1"
    assert parser().parse_args(["approve", "topic-1"]).topic_id == "topic-1"
    submitted = parser().parse_args(
        ["mark-submitted", "topic-1", "--date", "2026-07-20", "--level", "海淀区"]
    )
    assert submitted.submission_level == "海淀区"
    pre_research = parser().parse_args(
        ["pre-research-check", "run-1", "C1", "--brief", "brief.json"]
    )
    assert pre_research.candidate_id == "C1"
    review = parser().parse_args(
        [
            "pre-research-review",
            "run-1",
            "C1",
            "--decision",
            "proceed",
            "--note",
            "人工确认",
        ]
    )
    assert review.decision == "proceed"
    adjustment = parser().parse_args(
        [
            "budget-adjust",
            "--stage",
            "screening",
            "--old-limit",
            "26000",
            "--new-limit",
            "45000",
            "--reason",
            "扩大检索",
            "--expected-benefit",
            "提高候选有效性",
        ]
    )
    assert adjustment.new_limit == 45000


def test_monday_cli_runs_observable_shadow_workflow_offline(capsys):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "config").mkdir()
        shutil.copy(ROOT / "config/sources.toml", root / "config/sources.toml")
        shutil.copy(
            ROOT / "config/policy_mechanisms.toml",
            root / "config/policy_mechanisms.toml",
        )
        raw = deepcopy(Settings.load(ROOT / "config/settings.toml").raw)
        raw["model"]["provider"] = "mock"
        settings = Settings(root, raw)
        fixture = ROOT / "tests/fixtures/monday_observability.json"

        with patch("sqmy.cli.Settings.load", return_value=settings):
            assert main(["--config", "unused.toml", "scan", "--fixture", str(fixture)]) == 0

        output = capsys.readouterr().out
        assert "已生成 5 个候选" in output
        db = Database(settings.database_path)
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM source_funnel").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM discovery_shadow_reviews").fetchone()[0] == 8
            assert conn.execute("SELECT COALESCE(SUM(enforced),0) FROM discovery_shadow_reviews").fetchone()[0] == 0


def test_cli_reports_a_safe_discovery_pause_without_claiming_candidates(capsys):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "config").mkdir()
        shutil.copy(ROOT / "config/sources.toml", root / "config/sources.toml")
        shutil.copy(ROOT / "config/policy_mechanisms.toml", root / "config/policy_mechanisms.toml")
        raw = deepcopy(Settings.load(ROOT / "config/settings.toml").raw)
        raw["model"]["provider"] = "codex_cli"
        raw["budget"]["screening_tokens"] = 1
        settings = Settings(root, raw)

        with patch("sqmy.cli.Settings.load", return_value=settings), patch(
            "sqmy.discovery.build_router"
        ):
            assert main(["--config", "unused.toml", "scan", "--fixture", str(ROOT / "tests/fixtures/monday_observability.json")]) == 0

        output = capsys.readouterr().out
        assert "已完成零模型采集" in output
        assert "已生成" not in output


def test_budget_cli_reports_action_limits_and_keeps_recent_tokens_observational(capsys):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        raw = deepcopy(Settings.load(ROOT / "config/settings.toml").raw)
        settings = Settings(root, raw)

        with patch("sqmy.cli.Settings.load", return_value=settings):
            assert main(["--config", "unused.toml", "budget"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["token_window_policy"] == "report_only"
        assert payload["action_token_limits"] == {
            "screening": 55_000,
            "pre_research": 30_000,
            "deep_research": 40_000,
            "writing": 15_000,
        }
        assert payload["max_calls_per_action"] == 3
        assert "weekly_token_limit" not in payload
        assert "不代表当前Token硬闸门" in payload["adjustment_history_note"]
