from __future__ import annotations

from dataclasses import asdict
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import uuid

from .config import Settings
from .db import Database, now
from .document import export_submission
from .models import Candidate, Phase, TaskStatus


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

    def select(self, run_id: str, candidate_ids: list[str]) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE candidates SET selected=0 WHERE run_id=?", (run_id,))
            for cid in candidate_ids:
                conn.execute("UPDATE candidates SET selected=1 WHERE run_id=? AND id=?", (run_id, f"{run_id}:{cid}"))
        self.db.checkpoint(run_id, phase=Phase.INCREMENTAL_REVIEW, status=TaskStatus.PENDING, data={"selected": candidate_ids, "next": f"sqmy generate {run_id}"})

    def pause(self, run_id: str, quota: bool = False) -> None:
        status = TaskStatus.PAUSED_QUOTA if quota else TaskStatus.PAUSED_BUDGET
        self.db.checkpoint(run_id, phase=Phase.RESEARCH, status=status, data={"next": f"sqmy resume {run_id}"})

    def resume(self, run_id: str) -> None:
        self.db.checkpoint(run_id, phase=Phase.RESEARCH, status=TaskStatus.PENDING, data={"next": f"sqmy generate {run_id}"})

    def skip(self, run_id: str, reason: str) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT phase,checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()
            conn.execute("UPDATE candidates SET selected=0 WHERE run_id=?", (run_id,))
        if row is None:
            raise ValueError(f"未找到运行：{run_id}")
        data = json.loads(row["checkpoint_json"] or "{}")
        data.update({"skip_reason": reason, "next": "none"})
        self.db.checkpoint(run_id, phase=row["phase"], status=TaskStatus.SKIPPED, data=data)

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
