from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from .budget import record_stage_usage
from .collector import SourceCollector
from .cadence import refresh_due, require_source_freshness
from .config import Settings
from .delivery import require_review, verify_export, verify_approved_integrity
from .db import Database, now
from .document import atomic_copy, export_submission, parse_submission_markdown
from .evidence import assess_topic
from .models import Candidate, Phase, TaskStatus
from .research_gate import require_pre_research_approval
from .screener import local_date, rule_screen


MOCK_TOPICS = [
    ("关于完善海淀区生成式人工智能教育应用风险分级机制的建议", "教育和未成年人", 92),
    ("关于优化北京市灵活就业人员职业伤害协同认定机制的建议", "平台经济与劳动权益", 89),
    ("关于推动海淀区科技型中小企业公共数据合规沙盒试点的建议", "数据治理", 87),
    ("关于完善社区居家养老服务异常预警纠错机制的建议", "养老与医疗", 84),
    ("关于减少青年就业服务重复填报和证明负担的建议", "青年就业", 81),
]


class Workflow:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = Database(settings.database_path)
        self.db.initialize()

    def init_run(self, mode: str = "mock", *, forced: bool = False) -> str:
        run_id = f"{date.today().isoformat()}-{uuid.uuid4().hex[:8]}"
        cfg_hash = hashlib.sha256(json.dumps(self.s.raw, sort_keys=True).encode()).hexdigest()
        stamp = now()
        with self.db.connect() as conn:
            conn.execute("INSERT INTO runs(id,phase,status,config_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                         (run_id, Phase.DISCOVERY, TaskStatus.PENDING, cfg_hash, stamp, stamp))
            conn.execute("INSERT INTO run_context(run_id,mode,forced,created_at) VALUES(?,?,?,?)",
                         (run_id, mode, int(forced), stamp))
        return run_id

    def scan(self, run_id: str) -> list[Candidate]:
        with self.db.connect() as conn:
            existing = conn.execute("SELECT data_json FROM candidates WHERE run_id=? ORDER BY score DESC", (run_id,)).fetchall()
        if existing:
            return [Candidate(**json.loads(r[0])) for r in existing]
        self.db.checkpoint(run_id, phase=Phase.DISCOVERY, status=TaskStatus.RUNNING, data={"next": "generate_candidates"})
        result = []
        for i, (title, category, score) in enumerate(MOCK_TOPICS, 1):
            c = Candidate(
                id=f"C{i}", title=title,
                summary=f"模拟候选：近期公开信息显示，{category}领域存在跨主体协作和纠错成本问题，适合开展制度性预研。",
                event_date=date.today().isoformat(), region="海淀/北京", affected_group="相关居民、劳动者或中小企业",
                institutional_conflict="信息掌握者与成本承担者不一致", pain_point="多头提交、结果不可核验、纠错周期长",
                policy_gap="现行政策有原则要求，但缺少触发条件、统一口径和纠错闭环", policy_entry="小范围试点统一凭证、限时纠错和抽查核验",
                authority="海淀区可试点，北京市可协调", data_sufficiency="需在正式研究阶段交叉验证", policy_window="存在",
                history_relation="mock 历史库暂无重复记录", priority="高" if score >= 87 else "中", risk="mock 数据不得用于正式报送",
                recommendation="推荐有限预研" if score >= 84 else "保留观察", score=score,
                score_reasons={"时效性": 18, "地方落点": 23, "痛点真实性": 14, "政策窗口": 14, "数据": 8, "可操作性": 10, "机制创新": 5, "扣分": 0},
            )
            result.append(c)
        out = self.s.root / "outputs/candidates" / f"{run_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_suffix(out.suffix + ".tmp")
        temp.write_text(json.dumps([asdict(c) for c in result], ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, out)
        with self.db.connect() as conn:
            for c in result:
                conn.execute("INSERT INTO candidates(id,run_id,title,data_json,score,created_at) VALUES(?,?,?,?,?,?)",
                             (f"{run_id}:{c.id}", run_id, c.title, json.dumps(asdict(c), ensure_ascii=False), c.score, now()))
        self.db.checkpoint(run_id, phase=Phase.SELECTION, status=TaskStatus.NEEDS_REVIEW, data={"candidate_file": str(out), "next": f"sqmy select {run_id} C1"})
        return result

    def candidates(self, run_id: str) -> list[Candidate]:
        with self.db.connect() as conn:
            run = conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
            rows = conn.execute(
                "SELECT data_json FROM candidates WHERE run_id=? ORDER BY score DESC", (run_id,)
            ).fetchall()
        if run is None:
            raise ValueError(f"未找到运行：{run_id}")
        return [Candidate(**json.loads(row["data_json"])) for row in rows]

    def candidate_pool(self, days: int | None = None) -> list[dict]:
        window_days = days or int(self.s.section("project")["candidate_pool_days"])
        if window_days <= 0:
            raise ValueError("候选池回看天数必须大于0")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        limit = int(self.s.section("project")["candidate_count"])
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT c.id,c.run_id,c.title,c.score,c.data_json,c.created_at,
                          r.created_at AS run_created_at,r.status
                   FROM candidates c JOIN runs r ON r.id=c.run_id
                   JOIN run_context rc ON rc.run_id=c.run_id
                   WHERE rc.mode='live' AND c.selected=0 AND r.status<>'skipped'
                     AND c.created_at>=?
                   ORDER BY c.score DESC,c.created_at DESC LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "candidate_id": row["id"].split(":", 1)[1],
                "title": row["title"],
                "score": row["score"],
                "event_date": json.loads(row["data_json"]).get("event_date", ""),
                "created_at": row["created_at"],
                "run_status": row["status"],
                "refresh_required": refresh_due(self.s, row["run_created_at"]),
                "eligibility": json.loads(row["data_json"]).get("eligibility"),
            }
            for row in rows
        ]

    def select(self, run_id: str, candidate_ids: list[str]) -> None:
        if not 1 <= len(candidate_ids) <= self.s.section("project")["max_formal_topics"]:
            raise ValueError("每次须选择 1—2 个题目")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("候选题编号不得重复")
        with self.db.connect() as conn:
            run = conn.execute(
                "SELECT status,checkpoint_json,created_at FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"未找到运行：{run_id}")
            if run["status"] == TaskStatus.SKIPPED:
                raise ValueError("运行已关闭，不得选择题目")
            available = {
                row["id"].split(":", 1)[1]
                for row in conn.execute("SELECT id FROM candidates WHERE run_id=?", (run_id,))
            }
            unknown = sorted(set(candidate_ids) - available)
            if unknown:
                raise ValueError(f"候选题不存在：{', '.join(unknown)}")
            conn.execute("UPDATE candidates SET selected=0 WHERE run_id=?", (run_id,))
            for cid in candidate_ids:
                conn.execute("UPDATE candidates SET selected=1 WHERE run_id=? AND id=?", (run_id, f"{run_id}:{cid}"))
        checkpoint = json.loads(run["checkpoint_json"] or "{}")
        refresh_required = refresh_due(self.s, run["created_at"])
        if refresh_required:
            phase = Phase.INCREMENTAL_REVIEW
            next_action = f"sqmy refresh {run_id}"
        else:
            phase = Phase.RESEARCH
            next_action = f"sqmy pre-research-check {run_id} CANDIDATE_ID --brief PATH"
        checkpoint.update({
            "selected": candidate_ids,
            "refresh_required": refresh_required,
            "next": next_action,
        })
        self.db.checkpoint(
            run_id,
            phase=phase,
            status=TaskStatus.PENDING,
            data=checkpoint,
        )

    def pause(self, run_id: str, quota: bool = False) -> None:
        status = TaskStatus.PAUSED_QUOTA if quota else TaskStatus.PAUSED_BUDGET
        with self.db.connect() as conn:
            row = conn.execute("SELECT phase,status,checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"未找到运行：{run_id}")
        if row["status"] in {TaskStatus.COMPLETED, TaskStatus.SKIPPED}:
            raise ValueError("已完成或已关闭的运行不能暂停")
        data = json.loads(row["checkpoint_json"] or "{}")
        data["resume_next"] = data.get("resume_next") or data.get("next") or self._default_next(run_id, row["phase"])
        data["next"] = f"sqmy resume {run_id}"
        self.db.checkpoint(run_id, phase=row["phase"], status=status, data=data)

    def resume(self, run_id: str) -> str:
        with self.db.connect() as conn:
            row = conn.execute("SELECT phase,status,checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"未找到运行：{run_id}")
        allowed = {TaskStatus.PAUSED_QUOTA, TaskStatus.PAUSED_BUDGET, TaskStatus.FAILED}
        if row["status"] not in allowed:
            raise ValueError("只有暂停或失败的运行可以恢复")
        data = json.loads(row["checkpoint_json"] or "{}")
        next_action = data.pop("resume_next", None) or self._default_next(run_id, row["phase"])
        data["next"] = next_action
        data["resumed_from"] = row["status"]
        self.db.checkpoint(run_id, phase=row["phase"], status=TaskStatus.PENDING, data=data)
        return next_action

    @staticmethod
    def _default_next(run_id: str, phase: str) -> str:
        return {
            Phase.DISCOVERY: f"sqmy scan --resume {run_id}",
            Phase.SELECTION: f"sqmy candidates {run_id}",
            Phase.INCREMENTAL_REVIEW: f"sqmy refresh {run_id}",
            Phase.RESEARCH: f"sqmy pre-research-check {run_id} CANDIDATE_ID --brief PATH",
            Phase.WRITING: "sqmy draft RUN_ID TOPIC_ID --source PATH",
            Phase.EXPORT: f"sqmy status {run_id}",
        }[Phase(phase)]

    def skip(self, run_id: str, reason: str) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT phase,checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()
            conn.execute("UPDATE candidates SET selected=0 WHERE run_id=?", (run_id,))
        if row is None:
            raise ValueError(f"未找到运行：{run_id}")
        data = json.loads(row["checkpoint_json"] or "{}")
        data.update({"skip_reason": reason, "next": "none"})
        self.db.checkpoint(run_id, phase=row["phase"], status=TaskStatus.SKIPPED, data=data)

    def incremental_review(self, run_id: str, fixture: Path | None = None) -> dict:
        with self.db.connect() as conn:
            run = conn.execute(
                "SELECT phase,status,created_at,checkpoint_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            selected = conn.execute(
                "SELECT id,title FROM candidates WHERE run_id=? AND selected=1 ORDER BY score DESC",
                (run_id,),
            ).fetchall()
        if run is None:
            raise ValueError(f"未找到运行：{run_id}")
        if run["status"] == TaskStatus.SKIPPED:
            raise ValueError("运行已关闭，不得执行增量复核")
        if not selected:
            raise ValueError("尚未人工确认选题")
        cutoff = datetime.fromisoformat(run["created_at"])
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        events = SourceCollector(self.s.root, self.s.raw).collect(run_id, fixture=fixture)

        def is_recent(item) -> bool:
            if not item.published_at:
                return False
            try:
                published = datetime.fromisoformat(item.published_at.replace("Z", "+00:00"))
            except ValueError:
                return False
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            return published.astimezone(timezone.utc) >= cutoff.astimezone(timezone.utc)

        recent = [item for item in events if is_recent(item)]
        qualified = rule_screen(recent, self.s.section("discovery"))
        stable = {
            "run_id": run_id,
            "cutoff": cutoff.isoformat(),
            "selected": [{"id": row["id"].split(":", 1)[1], "title": row["title"]} for row in selected],
            "collected_count": len(events),
            "since_cutoff_count": len(recent),
            "rule_qualified_count": len(qualified),
            "items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "url": item.url,
                    "published_at": item.published_at,
                    "region": item.region,
                    "source_name": item.source_name,
                    "source_level": item.source_level,
                    "rule_score": item.rule_score,
                }
                for item in qualified
            ],
        }
        input_hash = hashlib.sha256(
            json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        with self.db.connect() as conn:
            cached = conn.execute(
                """SELECT id,result_json FROM tasks
                   WHERE run_id=? AND kind='incremental_review' AND input_hash=? AND status=?""",
                (run_id, input_hash, TaskStatus.COMPLETED),
            ).fetchone()
        if cached:
            result = json.loads(cached["result_json"])
            stale_decision = bool(result.get("decision")) and refresh_due(
                self.s, result.get("decided_at") or ""
            )
            result["checked_at"] = now()
            if stale_decision:
                for key in ("decision", "decision_note", "decided_at"):
                    result.pop(key, None)
                result["decision"] = None
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE tasks SET result_json=?,updated_at=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False), now(), cached["id"]),
                )
        else:
            report = self.s.root / "outputs/review" / run_id / f"incremental_review_{date.today().isoformat()}.md"
            lines = [
                f"# 选题增量复核（{run_id}）",
                "",
                f"- 原候选扫描截止点：{cutoff.isoformat()}",
                f"- 受控来源元数据：{len(events)} 条",
                f"- 截止点后信息：{len(recent)} 条",
                f"- 规则初筛：{len(qualified)} 条",
                "- 项目模型调用：0 次",
                "",
                "> 本报告只完成低成本增量发现。必须人工核对已选题核心页面、最新政策及替换必要性。",
                "",
                "## 规则初筛结果",
                "",
            ]
            lines.extend(
                f"- {local_date(item.published_at)}｜{item.region}｜{item.title}｜{item.url}"
                for item in qualified
            )
            if not qualified:
                lines.append("- 无")
            report.parent.mkdir(parents=True, exist_ok=True)
            temporary = report.with_suffix(report.suffix + ".tmp")
            temporary.write_text("\n".join(lines), encoding="utf-8")
            os.replace(temporary, report)
            result = stable | {
                "input_hash": input_hash,
                "report_path": str(report),
                "model_calls": 0,
                "token_used": 0,
                "checked_at": now(),
                "decision": None,
            }
            with self.db.connect() as conn:
                conn.execute(
                    """INSERT INTO tasks(
                         id,run_id,kind,input_hash,status,result_json,token_used,attempts,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        f"{run_id}:incremental_review:{input_hash[:12]}",
                        run_id,
                        "incremental_review",
                        input_hash,
                        TaskStatus.COMPLETED,
                        json.dumps(result, ensure_ascii=False),
                        0,
                        1,
                        now(),
                    ),
                )
        checkpoint = json.loads(run["checkpoint_json"] or "{}")
        checkpoint["incremental_review"] = result
        decision = result.get("decision")
        current_phase = Phase(run["phase"])
        current_status = TaskStatus(run["status"])
        preserve_later_state = (
            current_phase in {Phase.RESEARCH, Phase.WRITING, Phase.EXPORT}
            and decision in {None, "keep"}
        )
        if preserve_later_state:
            phase = current_phase
            status = current_status
            next_action = checkpoint.get("next") or self._default_next(run_id, current_phase)
            checkpoint["incremental_review_decision_next"] = (
                f"sqmy refresh {run_id} --decision keep --note REVIEW_NOTE"
            )
        elif decision in {"keep", "revise", "replace"}:
            phase, status, next_action = self._incremental_transition(run_id, decision)
        else:
            phase = Phase.INCREMENTAL_REVIEW
            status = TaskStatus.NEEDS_REVIEW
            next_action = f"sqmy refresh {run_id} --decision keep --note REVIEW_NOTE"
        checkpoint["next"] = next_action
        self.db.checkpoint(run_id, phase=phase, status=status, data=checkpoint)
        return result

    def record_incremental_decision(self, run_id: str, decision: str, note: str) -> str:
        if decision not in {"keep", "revise", "replace"}:
            raise ValueError("增量复核决定只能是 keep、revise 或 replace")
        if not note.strip():
            raise ValueError("必须记录人工复核理由")
        with self.db.connect() as conn:
            run = conn.execute("SELECT phase,status,checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()
            task = conn.execute(
                """SELECT id,result_json FROM tasks
                   WHERE run_id=? AND kind='incremental_review' AND status=?
                   ORDER BY updated_at DESC LIMIT 1""",
                (run_id, TaskStatus.COMPLETED),
            ).fetchone()
            if run is None:
                raise ValueError(f"未找到运行：{run_id}")
            if task is None:
                raise ValueError("尚未执行选题增量复核")
            result = json.loads(task["result_json"])
            result.update({"decision": decision, "decision_note": note, "decided_at": now()})
            conn.execute(
                "UPDATE tasks SET result_json=?,updated_at=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), now(), task["id"]),
            )
        checkpoint = json.loads(run["checkpoint_json"] or "{}")
        checkpoint["incremental_review"] = result
        current_phase = Phase(run["phase"])
        current_status = TaskStatus(run["status"])
        if current_phase in {Phase.RESEARCH, Phase.WRITING, Phase.EXPORT} and decision == "keep":
            phase = current_phase
            status = current_status
            next_action = checkpoint.get("next") or self._default_next(run_id, current_phase)
            checkpoint.pop("incremental_review_decision_next", None)
        else:
            phase, status, next_action = self._incremental_transition(run_id, decision)
        checkpoint["next"] = next_action
        self.db.checkpoint(run_id, phase=phase, status=status, data=checkpoint)
        return next_action

    @staticmethod
    def _incremental_transition(run_id: str, decision: str) -> tuple[Phase, TaskStatus, str]:
        if decision == "replace":
            return Phase.SELECTION, TaskStatus.NEEDS_REVIEW, f"sqmy candidates {run_id}"
        if decision == "revise":
            return Phase.RESEARCH, TaskStatus.NEEDS_REVIEW, "修订研究问题和有限预研决策单"
        return (
            Phase.RESEARCH,
            TaskStatus.PENDING,
            f"sqmy pre-research-check {run_id} CANDIDATE_ID --brief PATH",
        )

    def draft(self, run_id: str, topic_id: str, source: Path, *, candidate_id: str | None = None) -> Path:
        gate = assess_topic(self.s, topic_id)
        if not gate["draft_allowed"]:
            reasons = gate["gate_errors"] + gate["blocking_claims"]
            raise ValueError("证据闸门未通过：" + "、".join(reasons))
        with self.db.connect() as conn:
            run = conn.execute("SELECT checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()
            rows = conn.execute(
                "SELECT id,data_json FROM candidates WHERE run_id=? AND selected=1 ORDER BY score DESC",
                (run_id,),
            ).fetchall()
            existing = conn.execute("SELECT approval_status FROM topics WHERE id=?", (topic_id,)).fetchone()
        if run is None:
            raise ValueError(f"未找到运行：{run_id}")
        require_source_freshness(self.s, self.db, run_id)
        if existing and existing["approval_status"] == "approved":
            raise ValueError("该题目已经人工通过，不得自动覆盖")
        if not rows:
            raise ValueError("尚未人工确认选题")
        candidates = {row["id"].split(":", 1)[1]: Candidate(**json.loads(row["data_json"])) for row in rows}
        if candidate_id is None:
            if len(candidates) != 1:
                raise ValueError("选择多个题目时必须指定 candidate_id")
            candidate_id = next(iter(candidates))
        if candidate_id not in candidates:
            raise ValueError(f"已选候选题中不存在：{candidate_id}")
        candidate = candidates[candidate_id]
        require_pre_research_approval(self.s, run_id, candidate_id, topic_id)
        title, sections = parse_submission_markdown(source)
        quality = require_review(self.s, run_id, topic_id, source)
        template = self.s.root / self.s.section("document")["template_path"]
        input_hash = hashlib.sha256(
            source.read_bytes() + template.read_bytes() + json.dumps(gate, sort_keys=True).encode()
            + topic_id.encode() + quality["evidence_sha256"].encode()
            + (quality["research_brief_sha256"] or "").encode()
        ).hexdigest()
        record_stage_usage(
            self.db,
            run_id=run_id,
            topic_id=topic_id,
            stage="writing",
            token_used=int(self.s.section("budget")["writing_tokens"]),
            input_hash=input_hash,
            provider="codex_subscription",
            model=self.s.section("model")["codex_model"],
            note="正式稿进入确定性DOCX导出时按写作阶段上限保守记账；不是Plus官方Token统计。",
        )
        with self.db.connect() as conn:
            cached = conn.execute(
                """SELECT result_json FROM tasks
                   WHERE run_id=? AND kind='draft_export' AND input_hash=? AND status=?""",
                (run_id, input_hash, TaskStatus.COMPLETED),
            ).fetchone()
        if cached:
            cached_result = json.loads(cached["result_json"])
            cached_path = Path(cached_result["path"])
            cached_hash = cached_result.get("output_sha256")
            if (
                cached_path.exists()
                and cached_hash
                and hashlib.sha256(cached_path.read_bytes()).hexdigest() == cached_hash
            ):
                return cached_path
        safe_title = "".join("_" if char in '/\\:*?\"<>|' else char for char in title).strip()
        output = self.s.root / "outputs/review" / run_id / f"{safe_title}_送审稿.docx"
        export_submission(template, output, title, sections)
        output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        topics = candidate.score_reasons.get("主题", [])
        category = "、".join(topics) if isinstance(topics, list) else str(topics)
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO topics(
                     id,title,created_at,category,region,affected_group,core_problem,
                     mechanism_entry,recommendation_summary,data_used,run_id,candidate_id,
                     draft_source_path,review_path,approval_status,final_path,actually_submitted
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title,category=excluded.category,region=excluded.region,
                     affected_group=excluded.affected_group,core_problem=excluded.core_problem,
                     mechanism_entry=excluded.mechanism_entry,
                     recommendation_summary=excluded.recommendation_summary,
                     data_used=excluded.data_used,run_id=excluded.run_id,
                     candidate_id=excluded.candidate_id,draft_source_path=excluded.draft_source_path,
                     review_path=excluded.review_path,approval_status='pending_review',final_path=NULL""",
                (
                    topic_id,
                    title,
                    now(),
                    category,
                    candidate.region,
                    candidate.affected_group,
                    candidate.institutional_conflict,
                    candidate.policy_entry,
                    candidate.recommendation,
                    json.dumps([item["claim_id"] for item in gate["claims"]], ensure_ascii=False),
                    run_id,
                    candidate_id,
                    str(source.resolve()),
                    str(output),
                    "pending_review",
                    None,
                ),
            )
            conn.execute(
                """INSERT INTO delivery_events(topic_id,run_id,ready_at,output_path,output_sha256,evidence_sha256,review_id)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(topic_id) DO NOTHING""",
                (topic_id, run_id, now(), str(output), output_hash, quality["evidence_sha256"], quality["review_id"]),
            )
            prior_exports = conn.execute(
                """SELECT id,result_json FROM tasks
                   WHERE run_id=? AND kind='draft_export' AND input_hash<>? AND status=?""",
                (run_id, input_hash, TaskStatus.COMPLETED),
            ).fetchall()
            for prior in prior_exports:
                try:
                    prior_topic_id = json.loads(prior["result_json"] or "{}").get("topic_id")
                except json.JSONDecodeError:
                    continue
                if prior_topic_id == topic_id:
                    conn.execute(
                        """UPDATE tasks SET status=?,error=?,updated_at=? WHERE id=?""",
                        (
                            TaskStatus.SKIPPED,
                            f"已由新版本 {input_hash[:12]} 替代",
                            now(),
                            prior["id"],
                        ),
                    )
            conn.execute(
                """INSERT INTO tasks(
                     id,run_id,kind,input_hash,status,result_json,token_used,attempts,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     status=excluded.status,result_json=excluded.result_json,
                     token_used=excluded.token_used,attempts=tasks.attempts+1,
                     error=NULL,updated_at=excluded.updated_at""",
                (
                    f"{run_id}:draft_export:{input_hash[:12]}",
                    run_id,
                    "draft_export",
                    input_hash,
                    TaskStatus.COMPLETED,
                    json.dumps(
                        {"topic_id": topic_id, "path": str(output), "output_sha256": output_hash,
                         "source_sha256": quality["source_sha256"], "evidence_sha256": quality["evidence_sha256"],
                         "research_brief_sha256": quality["research_brief_sha256"],
                         "quality_review_id": quality["review_id"], "delivery_verified": True},
                        ensure_ascii=False,
                    ),
                    0,
                    1,
                    now(),
                ),
            )
        checkpoint = json.loads(run["checkpoint_json"] or "{}")
        checkpoint.update(
            {
                "topic_id": topic_id,
                "evidence_gate": "passed",
                "review_path": str(output),
                "next": f"sqmy approve {topic_id}",
            }
        )
        self.db.checkpoint(run_id, phase=Phase.EXPORT, status=TaskStatus.NEEDS_REVIEW, data=checkpoint)
        return output

    def approve(self, topic_id: str) -> Path:
        with self.db.connect() as conn:
            topic = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        if topic is None:
            raise ValueError(f"未找到题目：{topic_id}")
        if topic["approval_status"] == "approved" and topic["final_path"]:
            final = Path(topic["final_path"])
            if final.exists():
                verify_approved_integrity(self.s, topic)
                return final
        review = Path(topic["review_path"] or "")
        if not review.is_file():
            raise ValueError("送审稿不存在，不能记录人工通过")
        verify_export(self.s, topic, review)
        safe_title = "".join("_" if char in '/\\:*?\"<>|' else char for char in topic["title"]).strip()
        final = self.s.root / "outputs/submission" / f"{safe_title}.docx"
        atomic_copy(review, final)
        approved_at = now()
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE topics SET approval_status='approved',approved_at=?,final_path=?,
                   actually_submitted=0 WHERE id=?""",
                (approved_at, str(final), topic_id),
            )
        if topic["run_id"]:
            with self.db.connect() as conn:
                run = conn.execute("SELECT checkpoint_json FROM runs WHERE id=?", (topic["run_id"],)).fetchone()
            checkpoint = json.loads(run["checkpoint_json"] or "{}")
            checkpoint.update(
                {
                    "human_review": "approved",
                    "actually_submitted": False,
                    "final_path": str(final),
                    "warning": "已通过人工审核，尚未对外发送",
                    "next": f"实际报送后运行 sqmy mark-submitted {topic_id} --date YYYY-MM-DD --level LEVEL",
                }
            )
            self.db.checkpoint(
                topic["run_id"], phase=Phase.EXPORT, status=TaskStatus.COMPLETED, data=checkpoint
            )
        return final

    def mark_submitted(self, topic_id: str, submitted_at: str, submission_level: str) -> None:
        try:
            date.fromisoformat(submitted_at)
        except ValueError as exc:
            raise ValueError("报送日期必须为 YYYY-MM-DD") from exc
        if not submission_level.strip():
            raise ValueError("必须填写报送层级")
        with self.db.connect() as conn:
            topic = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        if topic is None:
            raise ValueError(f"未找到题目：{topic_id}")
        if topic["approval_status"] != "approved":
            raise ValueError("稿件尚未人工通过，不能登记实际报送")
        if not topic["final_path"] or not Path(topic["final_path"]).is_file():
            raise ValueError("待报送正式文件不存在")
        verify_approved_integrity(self.s, topic)
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE topics SET submitted_at=?,submission_level=?,actually_submitted=1
                   WHERE id=?""",
                (submitted_at, submission_level.strip(), topic_id),
            )
        if topic["run_id"]:
            with self.db.connect() as conn:
                run = conn.execute("SELECT checkpoint_json FROM runs WHERE id=?", (topic["run_id"],)).fetchone()
            checkpoint = json.loads(run["checkpoint_json"] or "{}")
            checkpoint.update(
                {
                    "actually_submitted": True,
                    "submitted_at": submitted_at,
                    "submission_level": submission_level.strip(),
                    "next": "等待并登记采用情况或反馈",
                }
            )
            checkpoint.pop("warning", None)
            self.db.checkpoint(
                topic["run_id"], phase=Phase.EXPORT, status=TaskStatus.COMPLETED, data=checkpoint
            )

    def generate(self, run_id: str) -> list[Path]:
        with self.db.connect() as conn:
            run = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            rows = conn.execute("SELECT data_json FROM candidates WHERE run_id=? AND selected=1 ORDER BY score DESC", (run_id,)).fetchall()
        if run is None:
            raise ValueError(f"未找到运行：{run_id}")
        if run["status"] == TaskStatus.SKIPPED:
            raise ValueError("运行已关闭，不得生成稿件")
        if not rows:
            raise ValueError("尚未人工确认选题")
        self.db.checkpoint(run_id, phase=Phase.WRITING, status=TaskStatus.RUNNING, data={"selected_count": len(rows)})
        outputs = []
        for row in rows[: self.s.section("project")["max_formal_topics"]]:
            c = Candidate(**json.loads(row[0]))
            sections = {
                "一、现状": ["　　【MOCK稿，不得直接报送】近期相关治理已形成原则性要求，但公开信息仍显示执行口径、信息反馈和申诉纠错之间存在衔接空间。正式研究应补充两项独立来源，并核对北京及海淀权限边界。"],
                "二、问题和分析": ["　　（一）信息掌握者与成本承担者不一致。受影响主体难以及时获得可核验的处理依据。", "　　（二）个案纠错与制度改进缺少稳定联动，重复问题可能被逐案处理而不触发规则调整。", "　　（三）分类和统计口径由执行主体单方掌握，存在规避监督和转嫁成本的空间。"],
                "三、政策建议": ["　　（一）在海淀区开展小范围试点，建立最小信息凭证和一口受理机制。", "　　（二）设置分类限时、抽查核验和超时纠错规则，降低守规主体维权成本。", "　　（三）建立问题指标与执行权限联动机制，按试点—评估—修订—扩围路径推进，并设置退出条件。"],
            }
            safe = "".join(ch for ch in c.id if ch.isalnum() or ch in "-_")
            path = self.s.root / "outputs/review" / run_id / f"{safe}.docx"
            export_submission(self.s.root / self.s.section("document")["template_path"], path, c.title, sections)
            outputs.append(path)
        self.db.checkpoint(run_id, phase=Phase.EXPORT, status=TaskStatus.NEEDS_REVIEW, data={"outputs": [str(p) for p in outputs], "warning": "mock稿不得直接报送"})
        return outputs

    def status(self, run_id: str | None = None, *, include_all: bool = False) -> list[dict]:
        with self.db.connect() as conn:
            if run_id:
                rows = conn.execute("SELECT r.*,rc.mode FROM runs r LEFT JOIN run_context rc ON rc.run_id=r.id WHERE r.id=?", (run_id,)).fetchall()
            elif include_all:
                rows = conn.execute("SELECT r.*,rc.mode FROM runs r LEFT JOIN run_context rc ON rc.run_id=r.id ORDER BY r.created_at DESC LIMIT 100").fetchall()
            else:
                rows = conn.execute("SELECT r.*,rc.mode FROM runs r JOIN run_context rc ON rc.run_id=r.id WHERE rc.mode='live' ORDER BY r.created_at DESC LIMIT 20").fetchall()
        return [dict(r) for r in rows]
