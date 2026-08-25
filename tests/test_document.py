from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest
from docx.document import Document as DocumentClass

from sqmy.document import export_submission


def test_failed_docx_save_does_not_replace_existing_output():
    project = Path(__file__).parents[1]
    template = project / "templates/submission_template.docx"
    sections = {
        "一、现状": ["这是现状。"],
        "二、问题和分析": ["（一）这是问题。"],
        "三、政策建议": ["（一）这是建议。"],
    }
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        output = root / "existing.docx"
        output.write_bytes(b"existing-safe-content")
        with patch.object(DocumentClass, "save", side_effect=RuntimeError("interrupted")):
            with pytest.raises(RuntimeError, match="interrupted"):
                export_submission(template, output, "关于测试原子写入的建议", sections)
        assert output.read_bytes() == b"existing-safe-content"
        assert not list(root.glob("*.tmp"))
        assert not list(root.glob(".*.tmp"))
