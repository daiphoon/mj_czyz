"""冻结明确指定的回放材料；不访问网络、运行模型或写真实运行库。"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import shutil


REQUIRED_ROLES = {"materials", "policy_context", "history_context", "settings", "model_context"}
BUNDLE_VERSION = "replay_bundle_v1"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        raise ValueError("回放时间必须是含时区的 ISO 日期") from None


def _inside(root, name):
    if not isinstance(name, str) or Path(name).is_absolute():
        raise ValueError("回放材料路径必须相对且位于指定目录内")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("回放材料路径越界")
    return path


def freeze_bundle(spec, source_root, destination):
    """可知时间及依据由整理者如实声明；缺失即不称完整历史重建。"""
    source_root, destination = Path(source_root), Path(destination)
    if destination.exists():
        raise ValueError("目标已有材料；请加载原冻结包，不能覆盖")
    if not isinstance(spec, dict) or not isinstance(spec.get("baseline_version"), str) or not spec["baseline_version"].strip():
        raise ValueError("缺少 baseline_version")
    cases = spec.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases 必须是非空列表")
    ids = [c.get("case_id") if isinstance(c, dict) else None for c in cases]
    if any(not isinstance(i, str) or not i.strip() for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("case_id 必须非空且唯一")
    manifest = {"bundle_version": BUNDLE_VERSION, "execution_mode": "replay",
                "baseline_version": spec["baseline_version"], "cases": []}
    blobs = {}
    for case in cases:
        cutoff = _instant(case.get("as_of"))
        if not isinstance(case.get("files"), list):
            raise ValueError("case.files 必须为列表")
        result = {"case_id": case["case_id"], "as_of": cutoff.isoformat(),
                  "files": [], "limitations": [],
                  "review_label": case.get("review_label"),
                  "label_note": "历史决定不是客观真值；标签不得作为模型输入"}
        roles = set()
        for entry in case["files"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("role"), str) or not entry["role"].strip():
                raise ValueError("材料 role 必须为非空字符串")
            role = entry["role"]
            path = _inside(source_root, entry.get("path"))
            if path.suffix.lower() not in {".json", ".jsonl", ".toml", ".md", ".pdf", ".html", ".txt"}:
                raise ValueError("回放仅接收明确指定的研究材料，不接收凭证或可执行文件")
            if not path.is_file():
                result["limitations"].append(f"missing_file:{role}")
                continue
            available = entry.get("available_at")
            if available is None:
                result["limitations"].append(f"availability_unverified:{role}")
            elif _instant(available) > cutoff:
                result["limitations"].append(f"future_material:{role}")
                continue
            basis = entry.get("availability_basis")
            if not isinstance(basis, str) or not basis.strip():
                result["limitations"].append(f"availability_basis_missing:{role}")
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            relative = f"blobs/{digest}{path.suffix.lower()}"
            blobs[relative] = content
            roles.add(role)
            result["files"].append({"role": role, "path": relative, "sha256": digest,
                                    "original_name": path.name, "available_at": available,
                                    "availability_basis": basis})
        result["limitations"].extend(f"missing_role:{r}" for r in sorted(REQUIRED_ROLES - roles))
        result["status"] = "incomplete_replay" if result["limitations"] else "frozen_inputs_complete"
        manifest["cases"].append(result)
    manifest["manifest_sha256"] = _hash(manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".replay-", dir=destination.parent))
    try:
        (temporary / "blobs").mkdir()
        for name, content in blobs.items():
            (temporary / name).write_bytes(content)
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def load_bundle(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("bundle_version") != BUNDLE_VERSION or manifest.get("execution_mode") != "replay":
        raise ValueError("不支持的回放契约")
    if manifest.get("manifest_sha256") != _hash({k: v for k, v in manifest.items() if k != "manifest_sha256"}):
        raise ValueError("回放清单哈希不一致")
    for case in manifest["cases"]:
        cutoff = _instant(case["as_of"])
        for entry in case["files"]:
            path = _inside(directory, entry["path"])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("回放材料缺失或哈希不一致")
            if entry.get("available_at") and _instant(entry["available_at"]) > cutoff:
                raise ValueError("回放材料晚于截止时间")
    return manifest


def evaluate_bundle(directory):
    """仅回放预研确定性检查。缺依赖就停止该例，不读取今天的配置补齐。"""
    import tomllib
    from unittest.mock import patch
    from .config import Settings
    from .research_gate import validate_pre_research_payload
    directory = Path(directory)
    manifest = load_bundle(directory)
    results = []
    for case in manifest['cases']:
        result = {k: case[k] for k in ('case_id', 'as_of', 'status', 'limitations')}
        if case['status'] != 'frozen_inputs_complete':
            results.append(result)
            continue
        files = {role: [entry for entry in case['files'] if entry['role'] == role] for role in REQUIRED_ROLES}
        if len(files['materials']) != 1 or len(files['settings']) != 1:
            result.update(status='unsupported_case_format', limitations=['每例须指定一份预研JSON和一份配置快照'])
            results.append(result)
            continue
        settings_path = _inside(directory, files['settings'][0]['path'])
        raw = tomllib.loads(settings_path.read_text()) if settings_path.suffix == '.toml' else json.loads(settings_path.read_text())
        payload = json.loads(_inside(directory, files['materials'][0]['path']).read_text())
        cutoff = _instant(case['as_of'])
        class FrozenDate(datetime):
            @classmethod
            def now(cls, tz=None):
                return cutoff.astimezone(tz) if tz else cutoff.replace(tzinfo=None)
        with tempfile.TemporaryDirectory(prefix='sqmy-bundle-check-') as temp:
            settings = Settings(Path(temp), raw)
            with patch('sqmy.research_gate.datetime', FrozenDate), patch('socket.create_connection', side_effect=AssertionError('离线回放禁止网络')):
                result['gate'] = validate_pre_research_payload(settings, payload)
        result['status'] = 'deterministic_gate_checked'
        results.append(result)
    return dict(manifest_sha256=manifest['manifest_sha256'], cases=results, new_model_calls=0, new_paid_search_calls=0,
                scope='仅预研确定性字段与证据检查；政策/历史/模型上下文已冻结但不参与此检查，不代表完整发现回放或模型质量A/B')
