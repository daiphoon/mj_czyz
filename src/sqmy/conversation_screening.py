"""当前主对话判断、程序严格导入。SQLite保存不可替换的冻结包和接受回执。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re

from .budget import record_stage_usage
from .candidate_eligibility import CONTRACT as SHADOW_CONTRACT, attach_shadow
from .db import now
from .discovery_shadow import ShadowReview
from .models import EventItem, Phase, TaskStatus
from .research_stops import ResearchStopGate


CONTRACT = "conversation_screening_v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON包含重复字段，整份拒绝")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("JSON包含非有限数，整份拒绝")
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=invalid)


def validate_schema(value, schema, location="result"):
    kind = schema["type"]
    valid = {"object": type(value) is dict, "array": type(value) is list,
             "string": type(value) is str, "integer": type(value) is int,
             "boolean": type(value) is bool, "null": value is None}.get(kind)
    if valid is not True:
        raise ValueError(f"{location} 类型不符，整份拒绝")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location} 枚举不符，整份拒绝")
    if kind == "object":
        props = schema["properties"]
        if any(key not in value for key in schema.get("required", [])):
            raise ValueError(f"{location} 缺少必需字段，整份拒绝")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise ValueError(f"{location} 包含未知字段，整份拒绝")
        for key, item in value.items():
            validate_schema(item, props[key], f"{location}.{key}")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            raise ValueError(f"{location} 数量越界，整份拒绝")
        for index, item in enumerate(value):
            validate_schema(item, schema["items"], f"{location}[{index}]")
    elif kind == "integer":
        if not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
            raise ValueError(f"{location} 分数越界，整份拒绝")
    elif kind == "string" and not value.strip():
        raise ValueError(f"{location} 不得为空，整份拒绝")


def validate_result(data, schema, allowed_ids):
    validate_schema(data, schema)
    ids = [item["id"] for item in data["selections"]]
    if len(ids) != len(set(ids)):
        raise ValueError("候选ID重复，整份拒绝")
    if set(ids) - set(allowed_ids):
        raise ValueError("候选ID不在冻结输入中，整份拒绝")
    for item in data["selections"]:
        keys = [penalty["key"] for penalty in item["applied_penalties"]]
        if len(keys) != len(set(keys)):
            raise ValueError("扣分项重复，整份拒绝")


class ConversationScreening:
    def __init__(self, discovery):
        self.discovery = discovery
        self.s, self.wf = discovery.s, discovery.wf

    def _directory(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", run_id):
            raise ValueError("无效运行ID")
        return self.s.root / "data/runs" / run_id

    def _versions(self):
        config = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted((self.s.root / "config").glob("*.toml"))}
        code = Path(__file__).parent
        engine = {name: hashlib.sha256((code / name).read_bytes()).hexdigest()
                  for name in ("conversation_screening.py", "discovery.py", "research_stops.py",
                               "screener.py", "candidate_eligibility.py", "materials.py", "novelty.py",
                               "collector.py", "retrieval.py", "retrieval_pipeline.py", "search_types.py",
                               "search_providers.py", "search_router.py", "retrieval_ledger.py",
                               "source_registry.py", "fetch.py", "discovery_shadow.py")}
        return digest({"settings": self.s.raw, "files": config}), digest(engine)

    def _stop_state(self, state):
        events = [EventItem(**deepcopy(item)) for item in state["model_pool"]]
        allowed, excluded, decisions = ResearchStopGate(self.wf.db).partition(events)
        return digest({"allowed": [e.id for e in allowed], "decisions": decisions}), allowed

    def _prepared(self, run_id):
        with self.wf.db.connect() as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE run_id=? AND kind='conversation_screening_prepare'",
                                (run_id,)).fetchall()
        if len(rows) != 1:
            raise ValueError("缺少唯一的对话初筛冻结包；须先有界准备")
        return json.loads(rows[0]["result_json"])

    def prepare(self, run_id, model_pool):
        directory = self._directory(run_id)
        with self.wf.db.connect() as conn:
            exists = conn.execute("SELECT 1 FROM tasks WHERE run_id=? AND kind='conversation_screening_prepare'",
                                  (run_id,)).fetchone()
            accepted = conn.execute("SELECT 1 FROM tasks WHERE run_id=? AND kind='conversation_screening_import'",
                                    (run_id,)).fetchone()
        if accepted:
            raise ValueError(f"已有接受结果待收尾或已完成，请运行 sqmy scan-import {run_id} --resume；不要重新判断")
        if exists:
            packet = self._prepared(run_id)
            self._check_binding(run_id, packet)
        else:
            snapshot_path = directory / "scan_input.json"
            state = read_json(snapshot_path)
            if not state.get("conversation_only"):
                raise ValueError("仅能导入专门准备的对话初筛运行")
            pool, material, prompt, schema, _ = self.discovery._prepare_screening(
                run_id, model_pool, conversation=True)
            config_hash, engine_hash = self._versions()
            stop_hash, _ = self._stop_state(state)
            stamp = datetime.fromisoformat(material["frozen_at"])
            binding = {"contract": CONTRACT, "run_id": run_id,
                       "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
                       "schema_sha256": digest(schema), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                       "config_sha256": config_hash, "engine_sha256": engine_hash,
                       "stop_gate_sha256": stop_hash, "created_at": stamp.isoformat(),
                       "expires_at": (stamp + timedelta(hours=int(self.s.section("project")["refresh_after_hours"]))).isoformat()}
            packet = {"contract": CONTRACT, "run_id": run_id, "binding": binding,
                      "binding_sha256": digest(binding), "model_event_ids": [e.id for e in pool],
                      "materials": material, "execution_mode": "current_conversation", "model": "unknown",
                      "official_usage": None,
                      "result_envelope": {"contract": CONTRACT, "run_id": run_id,
                                          "binding_sha256": digest(binding), "result": {"selections": []}},
                      "instructions": "当前主对话阅读materials.prompt和schema后判断，替换result.selections并自动写入结果文件、调用scan-import。允许0题。不得调用Provider或让执行代理另选模型；不自动选题、预研、深研或报送。"}
            with self.wf.db.connect() as conn:
                conn.execute("INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,updated_at) VALUES(?,?,'conversation_screening_prepare',?,'completed',?,?)",
                             (f"{run_id}:conversation_prepare", run_id, packet["binding_sha256"],
                              json.dumps(packet, ensure_ascii=False), now()))
        self.discovery._atomic_json(directory / "conversation_screening.json", packet)
        with self.wf.db.connect() as conn:
            checkpoint = json.loads(conn.execute("SELECT checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        checkpoint.update(conversation_only=True, selected_model=None, execution_mode="current_conversation", model="unknown",
                          conversation_screening={"binding_sha256": packet["binding_sha256"]},
                          next="await_conversation_screening", resume_next=f"sqmy scan-prepare --resume {run_id}",
                          packet=str(directory / "conversation_screening.json"))
        self.wf.db.checkpoint(run_id, phase=Phase.DISCOVERY, status=TaskStatus.NEEDS_REVIEW, data=checkpoint)
        return packet

    def _check_binding(self, run_id, packet):
        directory = self._directory(run_id)
        binding = packet["binding"]
        if packet["contract"] != CONTRACT or binding["contract"] != CONTRACT or binding["run_id"] != run_id or digest(binding) != packet["binding_sha256"]:
            raise ValueError("冻结契约或运行绑定不符")
        created, expires = (datetime.fromisoformat(binding[k]) for k in ("created_at", "expires_at"))
        current = datetime.now(timezone.utc)
        if created.tzinfo is None or expires.tzinfo is None or current < created or current >= expires:
            raise ValueError("冻结材料已过期或时间无效；须重新有界准备")
        if self._versions() != (binding["config_sha256"], binding["engine_sha256"]):
            raise ValueError("配置或程序版本已变化，整份拒绝")
        snapshot = directory / "scan_input.json"
        if hashlib.sha256(snapshot.read_bytes()).hexdigest() != binding["snapshot_sha256"]:
            raise ValueError("冻结输入已变化，整份拒绝")
        material = packet["materials"]
        if digest(material["schema"]) != binding["schema_sha256"] or hashlib.sha256(material["prompt"].encode()).hexdigest() != binding["prompt_sha256"]:
            raise ValueError("冻结Schema或提示版本不符")
        if digest(read_json(directory / "screening_materials.json")) != digest(material):
            raise ValueError("冻结筛选材料已变化，整份拒绝")
        state = read_json(snapshot)
        stop_hash, allowed = self._stop_state(state)
        if stop_hash != binding["stop_gate_sha256"]:
            raise ValueError("研究停止或重开记录已变化，整份拒绝")
        return state, allowed

    def import_result(self, run_id, result_path=None):
        # 与采集和独立CLI筛选使用同一把锁；没有锁不允许接管。
        lock_path = self.s.root / "data/runs/.discovery.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("已有扫描或导入正在执行，请待其结束后恢复") from exc
            if result_path is None:
                packet = self._prepared(run_id)
                self._check_binding(run_id, packet)
                with self.wf.db.connect() as conn:
                    row = conn.execute("SELECT result_json FROM tasks WHERE id=?", (f"{run_id}:conversation_import",)).fetchone()
                if not row:
                    raise ValueError("尚无已接受结果，不能恢复导入")
                accepted = json.loads(row[0])
                envelope = {k: packet[k] for k in ("contract", "run_id", "binding_sha256")}
                envelope["result"] = accepted["result"]
                result_path = self._directory(run_id) / "conversation_accepted_result.json"
                self.discovery._atomic_json(result_path, envelope)
            return self._import(run_id, result_path)

    def _import(self, run_id, result_path):
        packet = self._prepared(run_id)
        envelope = read_json(result_path)
        if type(envelope) is not dict or set(envelope) != {"contract", "run_id", "binding_sha256", "result"}:
            raise ValueError("导入信封字段不完整或多出字段，整份拒绝")
        if any(envelope[k] != packet[k] for k in ("contract", "run_id", "binding_sha256")):
            raise ValueError("导入结果与冻结运行或版本不符，整份拒绝")
        state, allowed = self._check_binding(run_id, packet)
        result = envelope["result"]
        validate_result(result, packet["materials"]["schema"], packet["model_event_ids"])
        result_hash = digest(result)
        receipt = {"binding_sha256": packet["binding_sha256"], "result_sha256": result_hash,
                   "execution_mode": "current_conversation", "model": "unknown", "official_usage": None,
                   "accounting_method": "artifact_proxy_estimate",
                   "estimated_tokens": max(1, (len(packet["materials"]["prompt"]) +
                                                len(json.dumps(packet["materials"]["schema"], ensure_ascii=False)) +
                                                len(json.dumps(result, ensure_ascii=False))) // 2),
                   "usage_note": "只由冻结输入、Schema及结果长度估算，不覆盖完整上下文和思考，可能低估；不是官方Token或账单。"}
        task_id = f"{run_id}:conversation_import"
        with self.wf.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check_binding(run_id, packet)
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            run = conn.execute("SELECT phase,checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()
            checkpoint = json.loads(run["checkpoint_json"])
            if task:
                accepted = json.loads(task["result_json"])
                if accepted["receipt"]["result_sha256"] != result_hash:
                    raise ValueError("该冻结运行已接受其他结果，不能覆盖")
                receipt = accepted["receipt"]
            elif run["phase"] != Phase.DISCOVERY:
                raise ValueError("当前运行阶段不允许接受新的初筛结果")
            else:
                conn.execute("INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,updated_at) VALUES(?,?,'conversation_screening_import',?,'accepted',?,?)",
                             (task_id, run_id, packet["binding_sha256"],
                              json.dumps({"result": result, "receipt": receipt}, ensure_ascii=False), now()))
        # 接受与记账可分步崩溃；stage_usage按同一运行/阶段幂等，不伪造model_calls。
        record_stage_usage(self.wf.db, run_id=run_id, topic_id="conversation_screening", stage="screening",
                           token_used=receipt["estimated_tokens"], input_hash=packet["binding_sha256"],
                           provider="codex_subscription", model="unknown", note=receipt["usage_note"],
                           execution_mode="current_conversation", accounting_method="artifact_proxy_estimate")
        finished = checkpoint.get("conversation_screening", {}).get("result_sha256") == result_hash
        if not finished:
            if run["phase"] != Phase.DISCOVERY:
                raise ValueError("导入尚未收尾但运行阶段已变化；不能覆盖人工操作")
            by_id = {e.id: e for e in allowed}
            pool = [by_id[item] for item in packet["model_event_ids"]]
            material = packet["materials"]
            attach = self.discovery._attach_analyses
            screened = (attach_shadow(pool, deepcopy(result), material, attach)
                        if material["input_contract"] == SHADOW_CONTRACT else attach(pool, deepcopy(result)))
            for item in screened:
                if "_eligibility" in item.model_analysis:
                    item.model_analysis["_eligibility"]["assessed_by"] = "current_conversation"
            for key in ("collected", "premodel_pool", "fresh_pool", "model_pool", "rule_results"):
                state[key] = [EventItem(**item) for item in state[key]]
            for key in ("rule_exclusions", "history_exclusions", "pool_cap_exclusions"):
                for item in state[key]:
                    item["event"] = EventItem(**item["event"])
            state["shadow_reviews"] = [ShadowReview(**item) for item in state["shadow_reviews"]]
            self.discovery._write_screening_audit(run_id, result)
            self.discovery._finish_scan(run_id, **state, screened_override=screened,
                                        conversation_receipt=receipt,
                                        before_persist=lambda: self._check_binding(run_id, packet))
        with self.wf.db.connect() as conn:
            conn.execute("UPDATE tasks SET status='completed',updated_at=? WHERE id=?", (now(), task_id))
        return run_id, self.wf.candidates(run_id)
