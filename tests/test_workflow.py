from pathlib import Path
from copy import deepcopy
import tempfile
import unittest

from sqmy.config import Settings
from sqmy.document import create_template
from sqmy.workflow import Workflow


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        project_root = Path(__file__).parents[1]
        base = Settings.load(project_root / "config/settings.toml")
        self.settings = Settings(Path(self.tempdir.name), deepcopy(base.raw))
        cfg = self.settings.section("document")
        create_template(Path(cfg["reference_path"]), self.settings.root / cfg["template_path"], cfg, self.settings.section("project")["signature"])

    def tearDown(self):
        self.tempdir.cleanup()

    def test_mock_flow(self):
        wf = Workflow(self.settings)
        run_id = wf.init_run()
        candidates = wf.scan(run_id)
        self.assertEqual(len(candidates), 5)
        self.assertEqual(wf.scan(run_id)[0].id, candidates[0].id)
        wf.select(run_id, ["C1"])
        wf.pause(run_id, quota=True)
        self.assertEqual(wf.status(run_id)[0]["status"], "paused_quota")
        wf.resume(run_id)
        outputs = wf.generate(run_id)
        self.assertEqual(len(outputs), 1)
        self.assertTrue(outputs[0].exists())

    def test_requires_manual_selection(self):
        wf = Workflow(self.settings)
        run_id = wf.init_run()
        with self.assertRaisesRegex(ValueError, "人工确认"):
            wf.generate(run_id)

    def test_run_can_be_explicitly_skipped(self):
        wf = Workflow(self.settings)
        run_id = wf.init_run("live")
        wf.skip(run_id, "候选质量不足")
        status = wf.status(run_id)[0]
        self.assertEqual(status["status"], "skipped")
        self.assertIn("候选质量不足", status["checkpoint_json"])


if __name__ == "__main__":
    unittest.main()
