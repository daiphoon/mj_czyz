from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from sqmy.config import Settings
from sqmy.maintenance import CleanupManager, preflight
from sqmy.workflow import Workflow


def test_cleanup_keeps_live_and_latest_replay_and_backs_up_database():
    project = Path(__file__).parents[1]
    base = Settings.load(project / "config/settings.toml")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = Settings(root, deepcopy(base.raw))
        workflow = Workflow(settings)
        live = workflow.init_run("live")
        old_replay = workflow.init_run("replay")
        latest_replay = workflow.init_run("replay")
        mock = workflow.init_run("mock")
        candidate_dir = root / "outputs/candidates"
        candidate_dir.mkdir(parents=True)
        for run_id in (live, old_replay, latest_replay, mock):
            (candidate_dir / f"{run_id}.md").write_text(run_id, encoding="utf-8")

        manager = CleanupManager(settings)
        plan = manager.plan()
        assert live in plan["keep_run_ids"]
        assert latest_replay in plan["keep_run_ids"]
        assert old_replay in plan["delete_run_ids"]
        assert mock in plan["delete_run_ids"]

        result = manager.apply()
        assert Path(result["backup"]).exists()
        assert result["integrity_check"] == "ok"
        remaining = {row["id"] for row in workflow.status(include_all=True)}
        assert remaining == {live, latest_replay}


def test_refresh_preflight_does_not_require_scan_screening_reserve():
    project = Path(__file__).parents[1]
    base = Settings.load(project / "config/settings.toml")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "config").mkdir()
        (root / "templates").mkdir()
        for name in ("settings.toml", "sources.toml", "policy_mechanisms.toml"):
            shutil.copy(project / "config" / name, root / "config" / name)
        shutil.copy(project / "templates/submission_template.docx", root / "templates/submission_template.docx")
        for name in ("data", "outputs", "logs"):
            (root / name).mkdir()
        settings = Settings(root, deepcopy(base.raw))
        usage = {
            "token_used": settings.section("budget")["weekly_token_limit"] - 100,
            "estimated_cost_cny": settings.section("budget")["weekly_cost_limit_cny"],
            "calls": 0,
        }
        with (
            patch("sqmy.maintenance.weekly_usage", return_value=usage),
            patch("sqmy.maintenance.shutil.which", return_value=None),
        ):
            scan = preflight(settings, stage="scan")
            refresh = preflight(settings, stage="refresh")
        assert scan["screening_ready"] is False
        assert refresh["ready"]
        refresh_budget = next(
            item for item in refresh["checks"] if item["name"] == "stage_token_headroom"
        )
        assert "reserve=0" in refresh_budget["detail"]


def test_monday_preflight_blocks_invalid_candidate_scoring_before_model_use():
    project = Path(__file__).parents[1]
    base = Settings.load(project / "config/settings.toml")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "config").mkdir()
        (root / "templates").mkdir()
        for name in ("settings.toml", "sources.toml", "policy_mechanisms.toml"):
            shutil.copy(project / "config" / name, root / "config" / name)
        shutil.copy(project / "templates/submission_template.docx", root / "templates/submission_template.docx")
        for name in ("data", "outputs", "logs"):
            (root / name).mkdir()
        raw = deepcopy(base.raw)
        raw["scoring"]["timeliness"] = 19
        settings = Settings(root, raw)
        with patch("sqmy.maintenance.shutil.which", return_value="/usr/local/bin/codex"):
            result = preflight(settings, stage="monday")
        scoring_check = next(
            item for item in result["checks"] if item["name"] == "candidate_scoring_config"
        )
        assert scoring_check["ok"] is False
        assert scoring_check["blocking"] is True
        assert result["ready"] is False


def test_monday_preflight_blocks_invalid_discovery_clue_reserve():
    project = Path(__file__).parents[1]
    base = Settings.load(project / "config/settings.toml")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "config").mkdir()
        (root / "templates").mkdir()
        for name in ("settings.toml", "sources.toml", "policy_mechanisms.toml"):
            shutil.copy(project / "config" / name, root / "config" / name)
        shutil.copy(project / "templates/submission_template.docx", root / "templates/submission_template.docx")
        for name in ("data", "outputs", "logs"):
            (root / name).mkdir()
        raw = deepcopy(base.raw)
        raw["discovery"]["premodel_clue_reserve"] = raw["discovery"]["screened_max"] + 1
        settings = Settings(root, raw)

        with patch("sqmy.maintenance.shutil.which", return_value="/usr/local/bin/codex"):
            result = preflight(settings, stage="monday")

        check = next(
            item for item in result["checks"]
            if item["name"] == "discovery_source_config"
        )
        assert check["ok"] is False
        assert check["blocking"] is True
        assert result["ready"] is False


def test_monday_preflight_blocks_invalid_shadow_enforcement_mode():
    project = Path(__file__).parents[1]
    base = Settings.load(project / "config/settings.toml")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "config").mkdir()
        (root / "templates").mkdir()
        for name in ("settings.toml", "sources.toml", "policy_mechanisms.toml"):
            shutil.copy(project / "config" / name, root / "config" / name)
        shutil.copy(
            project / "templates/submission_template.docx",
            root / "templates/submission_template.docx",
        )
        for name in ("data", "outputs", "logs"):
            (root / name).mkdir()
        raw = deepcopy(base.raw)
        raw["shadow_verification"]["mode"] = "enforced"
        settings = Settings(root, raw)
        with patch("sqmy.maintenance.shutil.which", return_value="/usr/local/bin/codex"):
            result = preflight(settings, stage="monday")

        check = next(
            item for item in result["checks"]
            if item["name"] == "discovery_observability_config"
        )
        assert check["ok"] is False
        assert check["blocking"] is True
        assert result["ready"] is False
