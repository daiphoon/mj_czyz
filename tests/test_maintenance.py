from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from sqmy.budget import record_stage_usage
from sqmy.config import Settings
from sqmy.maintenance import CleanupManager, preflight
from sqmy.workflow import Workflow


def test_cleanup_preserves_unknown_directories_and_symlinks(tmp_path):
    base = Settings.load(Path(__file__).parents[1] / "config/settings.toml")
    settings = Settings(tmp_path, deepcopy(base.raw))
    workflow = Workflow(settings)
    mock = workflow.init_run("mock")
    for parent in ("data/runs", "outputs/review"):
        manual = tmp_path / parent / "manual-research"
        manual.mkdir(parents=True)
        (manual / "notes.md").write_text("人工研究", encoding="utf-8")
    # Even a disposable run name must not authorize traversing a symlink.
    link = tmp_path / "outputs/review" / mock
    link.symlink_to(tmp_path / "outputs/review/manual-research", target_is_directory=True)
    manager = CleanupManager(settings)
    plan = manager.plan()
    assert not any("manual-research" in p for p in plan["delete_paths"])
    assert str(link.relative_to(tmp_path)) not in plan["delete_paths"]
    manager.apply()
    assert (tmp_path / "outputs/review/manual-research/notes.md").exists()
    assert (tmp_path / "data/runs/manual-research/notes.md").exists()
    assert link.is_symlink()


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
        special = workflow.init_run("special_pre_research")
        record_stage_usage(
            workflow.db,
            run_id=mock,
            topic_id="mock-topic",
            stage="pre_research",
            token_used=1_000,
            input_hash="mock-hash",
            provider="codex_subscription",
            model="mock-model",
            note="验证清理外键顺序",
        )
        candidate_dir = root / "outputs/candidates"
        candidate_dir.mkdir(parents=True)
        for run_id in (live, old_replay, latest_replay, mock, special):
            (candidate_dir / f"{run_id}.md").write_text(run_id, encoding="utf-8")
        (candidate_dir / "manual-final-review.md").write_text(
            "人工候选终审", encoding="utf-8"
        )

        manager = CleanupManager(settings)
        plan = manager.plan()
        assert live in plan["keep_run_ids"]
        assert latest_replay in plan["keep_run_ids"]
        assert special in plan["keep_run_ids"]
        assert old_replay in plan["delete_run_ids"]
        assert mock in plan["delete_run_ids"]
        assert "outputs/candidates/manual-final-review.md" not in plan["delete_paths"]

        result = manager.apply()
        assert Path(result["backup"]).exists()
        assert result["integrity_check"] == "ok"
        remaining = {row["id"] for row in workflow.status(include_all=True)}
        assert remaining == {live, latest_replay, special}


def test_cleanup_delays_intermediate_docx_qa_and_always_keeps_latest_version():
    project = Path(__file__).parents[1]
    base = Settings.load(project / "config/settings.toml")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = Settings(root, deepcopy(base.raw))
        workflow = Workflow(settings)
        run_id = workflow.init_run("live")
        qa = root / "data/runs" / run_id / "docx_qa"
        old = (
            datetime.now(timezone.utc) - timedelta(
                days=settings.section("cleanup")["docx_qa_intermediate_retention_days"] + 1
            )
        ).timestamp()
        for name in ("render-v1", "render-v2", "render-v3", "fidelity-v1", "fidelity-v2"):
            directory = qa / name
            directory.mkdir(parents=True)
            artifact = directory / "page-1.png"
            artifact.write_bytes(b"qa")
            if name != "render-v2":
                os.utime(artifact, (old, old))
                os.utime(directory, (old, old))
        (qa / "final-style-evidence.json").write_text("{}", encoding="utf-8")

        manager = CleanupManager(settings)
        plan = manager.plan()
        deleted = set(plan["delete_paths"])

        assert f"data/runs/{run_id}/docx_qa/render-v1" in deleted
        assert f"data/runs/{run_id}/docx_qa/fidelity-v1" in deleted
        assert f"data/runs/{run_id}/docx_qa/render-v2" not in deleted
        assert f"data/runs/{run_id}/docx_qa/render-v3" not in deleted
        assert f"data/runs/{run_id}/docx_qa/fidelity-v2" not in deleted
        assert f"data/runs/{run_id}/docx_qa/final-style-evidence.json" not in deleted
        qa_details = {
            item["path"]: item["reason"] for item in plan["delete_path_details"]
        }
        assert qa_details[f"data/runs/{run_id}/docx_qa/render-v1"] == "expired_intermediate_docx_qa"

        result = manager.apply()
        assert result["integrity_check"] == "ok"
        assert not (qa / "render-v1").exists()
        assert (qa / "render-v3/page-1.png").exists()


def test_recent_token_usage_is_report_only_in_scan_and_refresh_preflight():
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
            "window_days": 7,
            "token_used": 999_999,
            "estimated_cost_cny": 0.0,
            "model_calls": 20,
        }
        with (
            patch("sqmy.maintenance.recent_usage", return_value=usage),
            patch("sqmy.maintenance.shutil.which", return_value="/usr/local/bin/codex"),
        ):
            scan = preflight(settings, stage="scan")
            refresh = preflight(settings, stage="refresh")
        assert scan["ready"]
        assert scan["screening_ready"] is True
        assert refresh["ready"]
        scan_budget = next(
            item for item in scan["checks"] if item["name"] == "action_token_limit"
        )
        assert "recent_7d_usage=999999 is report_only" in scan_budget["detail"]


def test_pre_research_preflight_uses_distinct_budget_and_bounded_run_context():
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
        settings = Settings(root, deepcopy(base.raw))
        workflow = Workflow(settings)
        run_id = workflow.init_run("live")
        workflow.scan(run_id)
        workflow.select(run_id, ["C1"])
        usage = {
            "window_days": 7,
            "token_used": 999_999,
            "estimated_cost_cny": 0.0,
            "model_calls": 0,
        }
        with (
            patch("sqmy.maintenance.recent_usage", return_value=usage),
            patch("sqmy.maintenance.shutil.which", return_value="/usr/local/bin/codex"),
        ):
            ordinary = preflight(settings, stage="pre_research")
            bounded = preflight(settings, stage="pre_research", run_id=run_id)
        assert ordinary["ready"] is False
        assert bounded["ready"] is True
        assert bounded["action_scope_confirmed"] is True
        budget_check = next(
            item for item in bounded["checks"] if item["name"] == "action_token_limit"
        )
        assert "action_limit=30000" in budget_check["detail"]
        assert "report_only" in budget_check["detail"]


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


def test_scan_preflight_blocks_invalid_source_health_window():
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
        raw["observability"]["source_health_zero_result_streak"] = 0
        settings = Settings(root, raw)

        with patch("sqmy.maintenance.shutil.which", return_value="/usr/local/bin/codex"):
            result = preflight(settings, stage="scan")

        check = next(
            item for item in result["checks"]
            if item["name"] == "discovery_observability_config"
        )
        assert check["ok"] is False
        assert result["ready"] is False
