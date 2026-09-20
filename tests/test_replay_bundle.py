from copy import deepcopy
import json

import pytest

from sqmy.replay import freeze_bundle, load_bundle, evaluate_bundle


def spec_at(root):
    roles = ("materials", "policy_context", "history_context", "settings", "model_context")
    for role in roles:
        (root / f"{role}.json").write_text(json.dumps({"role": role}))
    return {"baseline_version": "test-commit", "cases": [{
        "case_id": "test-only", "as_of": "2026-09-07T08:00:00+08:00",
        "files": [{"role": role, "path": f"{role}.json",
                   "available_at": "2026-09-06T08:00:00+08:00",
                   "availability_basis": "离线测试记录"} for role in roles],
    }]}


def test_freeze_is_immutable_and_does_not_follow_new_live_material(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    spec = spec_at(source)
    frozen = tmp_path / "bundle"
    first = freeze_bundle(spec, source, frozen)
    assert load_bundle(frozen)["manifest_sha256"] == first["manifest_sha256"]
    (source / "materials.json").write_text('{"later":"must not enter replay"}')
    assert load_bundle(frozen)["manifest_sha256"] == first["manifest_sha256"]
    with pytest.raises(ValueError, match="已有"):
        freeze_bundle(spec, source, frozen)
    blob = frozen / first["cases"][0]["files"][0]["path"]
    blob.write_text("tampered")
    with pytest.raises(ValueError, match="哈希"):
        load_bundle(frozen)


def test_future_and_missing_materials_are_incomplete_not_silently_backfilled(tmp_path):
    spec = spec_at(tmp_path)
    spec["cases"][0]["files"][0]["available_at"] = "2026-09-08T00:00:00+08:00"
    spec["cases"][0]["files"].pop()
    result = freeze_bundle(spec, tmp_path, tmp_path / "bundle")
    case = result["cases"][0]
    assert case["status"] == "incomplete_replay"
    assert "materials" not in [f["role"] for f in case["files"]]
    assert "future_material:materials" in case["limitations"]
    assert "missing_role:model_context" in case["limitations"]


def test_bundle_rejects_path_escape_and_duplicate_case_ids(tmp_path):
    spec = spec_at(tmp_path)
    bad = deepcopy(spec)
    bad["cases"].append(deepcopy(bad["cases"][0]))
    with pytest.raises(ValueError, match="case_id"):
        freeze_bundle(bad, tmp_path, tmp_path / "bad")
    spec["cases"][0]["files"][0]["path"] = "../outside.json"
    with pytest.raises(ValueError, match="路径"):
        freeze_bundle(spec, tmp_path, tmp_path / "escape")


def test_missing_availability_basis_does_not_claim_complete_reconstruction(tmp_path):
    spec = spec_at(tmp_path)
    spec["cases"][0]["files"][0].pop("availability_basis")
    result = freeze_bundle(spec, tmp_path, tmp_path / "bundle")
    assert result["cases"][0]["status"] == "incomplete_replay"
    assert result["execution_mode"] == "replay"


def test_incomplete_bundle_cannot_borrow_live_settings(tmp_path):
    spec = spec_at(tmp_path)
    spec['cases'][0]['files'] = spec['cases'][0]['files'][:1]
    frozen = tmp_path / 'frozen'
    freeze_bundle(spec, tmp_path, frozen)
    result = evaluate_bundle(frozen)
    assert result['cases'][0]['status'] == 'incomplete_replay'
    assert 'gate' not in result['cases'][0]


def test_frozen_gate_uses_frozen_configuration_and_cutoff(tmp_path):
    from datetime import datetime, timezone
    from test_research_gate import valid_brief, make_settings
    source = tmp_path / 'inputs'; source.mkdir()
    spec = spec_at(source)
    settings = make_settings(str(tmp_path / 'live'))
    (source / 'settings.json').write_text(json.dumps(settings.raw))
    (source / 'materials.json').write_text(json.dumps(valid_brief('fixture')))
    stamp = datetime.now(timezone.utc).isoformat()
    spec['cases'][0]['as_of'] = stamp
    for entry in spec['cases'][0]['files']: entry['available_at'] = stamp
    frozen = tmp_path / 'frozen'
    freeze_bundle(spec, source, frozen)
    first = evaluate_bundle(frozen)
    (source / 'settings.json').write_text('{}')
    (source / 'materials.json').write_text('{}')
    assert evaluate_bundle(frozen) == first
    assert first['cases'][0]['gate']['research_allowed']
