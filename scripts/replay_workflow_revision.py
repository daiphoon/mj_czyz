"""冻结历史材料的离线新旧回放。只写报告和临时SQLite，不调用模型或网络。"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


def worker(code_root, frozen, database):
    sys.path.insert(0, str(code_root / "src"))
    from sqmy.config import Settings
    from sqmy.evidence import assess_topic
    from sqmy.research_gate import validate_pre_research_payload, _render_report
    from unittest.mock import patch

    samples = json.loads(frozen.read_text())["pre_research"]
    cfg = Settings.load(code_root / "config/settings.toml").raw
    with tempfile.TemporaryDirectory(prefix="sqmy-offline-replay-") as directory:
        root = Path(directory)
        settings = Settings(root, cfg)
        settings.database_path.parent.mkdir(parents=True)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source:
            with sqlite3.connect(settings.database_path) as target:
                source.backup(target)
        rows = []
        with patch("socket.create_connection", side_effect=AssertionError("回放禁止网络")):
            for index, sample in enumerate(samples):
                payload = sample["payload"]
                gate = validate_pre_research_payload(settings, payload)
                report = _render_report(payload, gate)
                excerpts = [s["excerpt"] for s in payload["sources"] if s.get("excerpt")]
                limits = [s["limitation"] for s in payload["sources"] if isinstance(s.get("limitation"), str)]
                rows.append({"review_id": sample["review_id"], "title": payload["working_title"],
                             "split": "held_out" if index >= len(samples) - 5 else "diagnosis",
                             "gate": gate, "source_count": len(payload["sources"]),
                             "excerpt_count": len(excerpts),
                             "excerpts_in_report": sum(e in report for e in excerpts),
                             "limits_in_report": sum(e in report for e in limits),
                             "limitation_count": len(limits)})
            # 同一证据快照按它自己的最后核验时点回放，不将旧来源刷新成今天。
            with sqlite3.connect(settings.database_path) as conn:
                topics = {r[0] for r in conn.execute("SELECT DISTINCT topic_id FROM claims")}
                evidence = []
                for topic in sorted(topics & {s["payload"]["topic_id"] for s in samples}):
                    dates = [r[0] for r in conn.execute("SELECT as_of_date FROM claims WHERE topic_id=?", (topic,))]
                    dates += [json.loads(r[0]).get("checked_at") for r in conn.execute("SELECT metadata_json FROM source_usages WHERE topic_id=?", (topic,))]
                    instants = []
                    for value in dates:
                        try:
                            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                            instants.append(stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp)
                        except (ValueError, AttributeError):
                            continue
                    reference = max(instants)  # 字段异常依旧交给证据闸门；本冻结集均有核验日期。
                    class ReplayDate(datetime):
                        @classmethod
                        def now(cls, tz=None):
                            return reference.astimezone(tz) if tz else reference.replace(tzinfo=None)
                    with patch("sqmy.evidence.datetime", ReplayDate):
                        result = assess_topic(settings, topic)
                    result["replay_as_of"] = reference.isoformat()
                    evidence.append(result)
        return {"pre_research": rows, "evidence": evidence, "new_model_calls": 0,
                "new_paid_search_calls": 0, "new_paid_cost": 0,
                "scope": "确定性闸门、材料传递和历史证据检查，不是模型质量A/B或新检索实验"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    directory = args.artifact_dir.resolve()
    frozen = directory / "frozen_samples.json"
    database = directory / "baseline/workflow.db"
    if args.worker:
        print(json.dumps(worker(args.code_root.resolve(), frozen, database), ensure_ascii=False))
        return
    results = {}
    for name, code_root in (("old", directory / "baseline"), ("new", args.code_root)):
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--artifact-dir", str(directory), "--code-root", str(code_root)]
        results[name] = json.loads(subprocess.check_output(command, text=True))
    old, new = results["old"]["pre_research"], results["new"]["pre_research"]
    changed = [n["review_id"] for o, n in zip(old, new) if o["gate"]["research_allowed"] != n["gate"]["research_allowed"]]
    summary = {"frozen_sha256": hashlib.sha256(frozen.read_bytes()).hexdigest(),
               "pre_research_candidates": len(old), "candidate_only": len(json.loads(frozen.read_text())["candidate_only"]),
               "old_allowed": sum(r["gate"]["research_allowed"] for r in old),
               "new_allowed": sum(r["gate"]["research_allowed"] for r in new),
               "changed_decisions": changed,
               "new_dispositions": dict(Counter(r["gate"]["diagnostics"]["disposition"] for r in new)),
               "old_excerpts_in_report": sum(r["excerpts_in_report"] for r in old),
               "new_excerpts_in_report": sum(r["excerpts_in_report"] for r in new),
               "excerpt_count": sum(r["excerpt_count"] for r in new),
               "old_limits_in_report": sum(r["limits_in_report"] for r in old),
               "new_limits_in_report": sum(r["limits_in_report"] for r in new),
               "limitation_count": sum(r["limitation_count"] for r in new),
               "old_evidence_allowed": sum(r["draft_allowed"] for r in results["old"]["evidence"]),
               "new_evidence_allowed": sum(r["draft_allowed"] for r in results["new"]["evidence"]),
               "evidence_topics": len(results["new"]["evidence"]),
               "new_paid_search_calls": 0, "new_model_calls": 0,
               "quality_effect": "只证明可追溯性与指定安全分支改善，未证明真实选题质量、误杀率或单位成稿成本改善"}
    results["summary"] = summary
    path = directory / "replay_results.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
