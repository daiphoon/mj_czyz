from copy import deepcopy
from pathlib import Path
import tempfile

from sqmy.config import Settings
from sqmy.maintenance import CleanupManager
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
