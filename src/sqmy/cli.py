from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from . import __version__

from .config import Settings, load_dotenv
from .delivery import check_draft, register_review
from .budget import (
    budget_adjustments,
    recent_usage,
    record_budget_adjustment,
    review_budget_adjustment,
)
from .db import Database
from .document import create_template
from .discovery import LiveDiscovery
from .discovery_shadow import write_discovery_evaluation
from .diagnostics import provider_check
from .evidence import assess_topic, import_evidence_package
from .novelty import record_review, rolling_evaluation, write_rolling_evaluation
from .research_gate import check_pre_research, review_pre_research
from .maintenance import CleanupManager, preflight
from .snapshots import SNAPSHOT_REASONS, capture_evidence_snapshot
from .workflow import Workflow
from .retrieval import execute_retrieval
from .tavily import retrieval_usage


def _add_scan_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--fixture", type=Path, help="使用离线 RSS 测试夹具")
    command.add_argument(
        "--clues", dest="clue_file", type=Path,
        help="导入Codex网页检索生成的JSONL投诉、论坛或社交平台待核线索",
    )
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--force", action="store_true", help="人工强制重算，跳过历史排除和模型结果缓存")
    mode.add_argument("--resume", dest="resume_run_id", help="从暂停或失败的发现阶段运行继续")
    command.add_argument("--screen-now", action="store_true", help="人工要求对不足批量阈值的待处理事件立即初筛；不绕过去重和预算")
    command.add_argument("--start-tier", type=int, choices=(1, 2, 3, 4), default=1, help="从指定扩展层开始续扫")


def _add_refresh_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("run_id")
    command.add_argument("--fixture", type=Path, help="使用离线 RSS 测试夹具")
    command.add_argument("--decision", choices=("keep", "revise", "replace"))
    command.add_argument("--note", default="", help="人工复核理由；记录决定时必填")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sqmy", description="社情民意信息研究与生成工作流")
    p.add_argument('--version', action='version', version=f'sqmy {__version__}')
    p.add_argument("--config", default="config/settings.toml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化数据库和正式模板")
    scan = sub.add_parser("scan", help="在任意日期运行真实来源候选扫描")
    _add_scan_arguments(scan)
    monday = sub.add_parser("monday", help="兼容旧命令；等同于 scan")
    _add_scan_arguments(monday)
    replay = sub.add_parser("scan-replay", help="使用已保存的模型结果离线回放候选流程")
    replay.add_argument("source_run_id")
    legacy_replay = sub.add_parser("monday-replay", help="兼容旧命令；等同于 scan-replay")
    legacy_replay.add_argument("source_run_id")
    sub.add_parser("scan-mock", help="运行纯 mock 候选扫描")
    sub.add_parser("monday-mock", help="兼容旧命令；等同于 scan-mock")
    show = sub.add_parser("candidates", help="查看候选题"); show.add_argument("run_id")
    show.add_argument("--json", action="store_true", help="含材料版本、影子意见及回源推荐记录")
    candidate_review = sub.add_parser("candidate-review", help="登记绑定材料版本的回源推荐，不自动选题")
    candidate_review.add_argument("run_id")
    candidate_review.add_argument("candidate_id")
    candidate_review.add_argument("--record", type=Path, required=True)
    pool = sub.add_parser("candidate-pool", help="查看近期仍可复核使用的未选候选")
    pool.add_argument("--days", type=int, default=None)
    sel = sub.add_parser("select", help="人工确认 1—2 个候选题"); sel.add_argument("run_id"); sel.add_argument("candidate_ids", nargs="+")
    gen = sub.add_parser("generate-mock", help="仅用于测试的 mock 稿生成"); gen.add_argument("run_id")
    refresh = sub.add_parser("refresh", help="运行零模型选题新鲜度复核，或记录人工复核决定")
    _add_refresh_arguments(refresh)
    thursday = sub.add_parser("thursday", help="兼容旧命令；等同于 refresh")
    _add_refresh_arguments(thursday)
    draft_check = sub.add_parser("draft-check", help="零模型检查稿件并输出稿件、证据指纹")
    draft_check.add_argument("topic_id")
    draft_check.add_argument("--source", type=Path, required=True)
    draft_check.add_argument("--major", action="store_true")
    draft_review = sub.add_parser("draft-review", help="登记绑定当前稿件与证据的四项内容审查")
    draft_review.add_argument("run_id")
    draft_review.add_argument("topic_id")
    draft_review.add_argument("--source", type=Path, required=True)
    draft_review.add_argument("--record", type=Path, required=True)
    research_brief = sub.add_parser("research-brief", help="登记或读取深研的唯一版本化写作输入")
    research_brief.add_argument("run_id")
    research_brief.add_argument("topic_id")
    research_brief.add_argument("--record", type=Path)
    semantic = sub.add_parser("semantic-review", help="准备或执行独立上下文的证据影子复核；默认零调用")
    semantic.add_argument("run_id")
    semantic.add_argument("topic_id")
    semantic.add_argument("--package", type=Path, help="可在深研证据导入记账前，复核已整理的证据包")
    semantic.add_argument("--run", action="store_true", help="显式执行，须已启用且原深研行为仍有额度")
    semantic.add_argument("--retry", action="store_true", help="重试同一失败记录；仍受原行为额度限制")
    semantic.add_argument("--adjudication", type=Path, help="登记回到原文后的内容裁决，不发请求")
    semantic.add_argument("--show", action="store_true", help="读取当前证据对应的最新意见")
    draft = sub.add_parser("draft", help="证据和送审检查通过后导出 DOCX")
    draft.add_argument("run_id")
    draft.add_argument("topic_id")
    draft.add_argument("--source", type=Path, required=True)
    draft.add_argument("--candidate-id")
    approve = sub.add_parser("approve", help="记录人工通过并复制到待报送目录，不对外发送")
    approve.add_argument("topic_id")
    submitted = sub.add_parser("mark-submitted", help="人工实际报送后登记日期和层级")
    submitted.add_argument("topic_id")
    submitted.add_argument("--date", dest="submitted_at", required=True)
    submitted.add_argument("--level", dest="submission_level", required=True)
    status = sub.add_parser("status", help="查看运行状态"); status.add_argument("run_id", nargs="?"); status.add_argument("--all", action="store_true", help="包含测试、诊断和回放运行")
    pause = sub.add_parser("pause", help="暂停运行"); pause.add_argument("run_id"); pause.add_argument("--quota", action="store_true")
    resume = sub.add_parser("resume", help="恢复运行"); resume.add_argument("run_id")
    retry = sub.add_parser("retry", help="从失败或暂停步骤继续"); retry.add_argument("run_id")
    sub.add_parser("budget", help="查看单行为 Token上限、近期用量和调整记录")
    budget_adjust = sub.add_parser("budget-adjust", help="记录单行为预算调整的原因和预期收益")
    budget_adjust.add_argument(
        "--stage",
        required=True,
        choices=("screening", "pre_research", "deep_research", "writing"),
    )
    budget_adjust.add_argument("--old-limit", type=int, required=True)
    budget_adjust.add_argument("--new-limit", type=int, required=True)
    budget_adjust.add_argument("--reason", required=True)
    budget_adjust.add_argument("--expected-benefit", required=True)
    budget_adjust.add_argument("--run-id")
    budget_review = sub.add_parser("budget-review", help="记录预算调整的实际收益和保留结论")
    budget_review.add_argument("adjustment_id", type=int)
    budget_review.add_argument("--actual-tokens", type=int, required=True)
    budget_review.add_argument("--actual-benefit", required=True)
    budget_review.add_argument("--decision", choices=("retain", "revert", "reassess"), required=True)
    skip = sub.add_parser("skip-run", help="人工关闭本次运行且不进入深研")
    skip.add_argument("run_id"); skip.add_argument("--reason", required=True)
    preflight_parser = sub.add_parser("preflight", help="零模型调用分阶段预检")
    preflight_parser.add_argument(
        "--stage", choices=("scan", "refresh", "monday", "thursday", "pre_research", "research", "writing"), default="scan"
    )
    preflight_parser.add_argument("--run-id", help="用于核验已人工限定的预研、深研或写作步骤")
    cleanup = sub.add_parser("cleanup", help="预览或执行安全清理")
    cleanup.add_argument("--apply", action="store_true", help="自动备份SQLite后执行清理")
    provider = sub.add_parser("provider-check", help="验证模型提供商与自动备用切换")
    provider.add_argument("--simulate-codex-quota", action="store_true", help="模拟Codex额度耗尽并真实调用DeepSeek备用")
    evidence_import = sub.add_parser("evidence-import", help="导入结构化证据包")
    evidence_import.add_argument("path", type=Path)
    evidence_check = sub.add_parser("evidence-check", help="检查主张的独立验证和成稿闸门")
    evidence_check.add_argument("topic_id")
    evidence_snapshot = sub.add_parser("evidence-snapshot", help="固化一项易变化的核心公开证据")
    evidence_snapshot.add_argument("package", type=Path, help="结构化证据包JSON")
    evidence_snapshot.add_argument("source_key", help="证据包中的来源key")
    evidence_snapshot.add_argument("--reason", required=True, choices=sorted(SNAPSHOT_REASONS))
    evidence_snapshot.add_argument("--file", type=Path, help="已有PDF或HTML；省略时抓取公开URL")
    pre_research_check = sub.add_parser("pre-research-check", help="导入并检查有限预研决策单")
    pre_research_check.add_argument("run_id")
    pre_research_check.add_argument("candidate_id")
    pre_research_check.add_argument("--brief", type=Path, required=True)
    pre_research_review = sub.add_parser("pre-research-review", help="人工决定是否进入深研")
    pre_research_review.add_argument("run_id")
    pre_research_review.add_argument("candidate_id")
    pre_research_review.add_argument("--decision", choices=("proceed", "stop"), required=True)
    pre_research_review.add_argument("--note", required=True)
    novelty_report = sub.add_parser("novelty-report", help="生成制度新意闸门和自然周产出漏斗滚动评估")
    novelty_report.add_argument("--days", type=int, default=None, help="统计窗口，默认21天")
    sub.add_parser("discovery-report", help="生成来源健康和制度覆盖影子滚动评估")
    retrieve = sub.add_parser("retrieve", help="按人工固定问题单进行有界Tavily补充，不调用写作模型")
    retrieve.add_argument("--plan", type=Path, required=True, help="具体检索问题及页面JSON，不放凭证")
    target = retrieve.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-id", help="绑定已有运行；恢复必须用原运行")
    target.add_argument("--diagnostic", action="store_true", help="新建隔离诊断；会真实调用搜索API")
    retrieve.add_argument("--retry-failed", action="store_true", help="明确重试失败或未知请求；可能重复付费，仍计入原行为上限")
    retrieval_report = sub.add_parser("retrieval-usage", help="查看单运行搜索积分、缓存和等价费用；零网络调用")
    retrieval_report.add_argument("run_id")
    novelty_review = sub.add_parser("novelty-review", help="为早期阻断结果补充后续复核标签")
    novelty_review.add_argument("audit_id")
    novelty_review.add_argument(
        "outcome",
        choices=[
            "confirmed_block", "confirmed_warning", "false_block", "missed_coverage",
            "reframed", "supported_gap", "stopped_other", "unclassified",
        ],
    )
    novelty_review.add_argument("--reason", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parser().parse_args(argv)
    s = Settings.load(args.config)
    wf = Workflow(s)
    if args.command == "init":
        Database(s.database_path).initialize()
        cfg = s.section("document")
        create_template(Path(cfg["reference_path"]), s.root / cfg["template_path"], cfg, s.section("project")["signature"])
        print(f"已初始化：{s.root}")
    elif args.command in {"scan", "monday"}:
        run_id, candidates = LiveDiscovery(s).run(
            args.fixture,
            clue_file=args.clue_file,
            force=args.force,
            screen_now=args.screen_now,
            start_tier=args.start_tier,
            resume_run_id=args.resume_run_id,
        )
        print(run_id)
        run_status = wf.status(run_id, include_all=True)[0]["status"]
        if run_status in {"paused_budget", "paused_quota"}:
            print("已完成零模型采集；模型筛选已安全暂停。请按运行状态中的恢复命令继续。")
        else:
            print(f"已生成 {len(candidates)} 个候选：outputs/candidates/{run_id}.md")
            checkpoint = json.loads(
                wf.status(run_id, include_all=True)[0]["checkpoint_json"] or "{}"
            )
            if checkpoint.get("budget_overrun"):
                print("当前有界步骤已完成，但已超出配置预算；启动下一个新模型任务前请提额或等待额度释放。")
        if not candidates and run_status not in {"paused_budget", "paused_quota"}:
            pool = wf.candidate_pool()
            print(f"近期候选池仍有 {len(pool)} 个未选候选；查看命令：sqmy candidate-pool")
    elif args.command in {"scan-mock", "monday-mock"}:
        run_id = wf.init_run(); wf.scan(run_id); print(run_id)
    elif args.command in {"scan-replay", "monday-replay"}:
        run_id, candidates = LiveDiscovery(s).replay(args.source_run_id)
        print(run_id)
        print(f"已离线回放，生成 {len(candidates)} 个候选；新增模型调用 0 次")
    elif args.command == "candidates":
        from .candidate_eligibility import candidate_hash, current_candidate_review
        candidates = wf.candidates(args.run_id)
        if args.json:
            print(json.dumps([asdict(c) | {"candidate_sha256": candidate_hash(c),
                "source_review": current_candidate_review(wf.db, args.run_id, c)} for c in candidates], ensure_ascii=False, indent=2))
        else:
            for c in candidates:
                print(f"{c.id} {c.score} {c.title} [研究入口影子：{(c.eligibility or {}).get('status', 'legacy/未评估')}]")
    elif args.command == "candidate-review":
        from .candidate_eligibility import register_candidate_review
        print(json.dumps(register_candidate_review(s, args.run_id, args.candidate_id, args.record), ensure_ascii=False, indent=2))
    elif args.command == "candidate-pool":
        print(json.dumps(wf.candidate_pool(args.days), ensure_ascii=False, indent=2))
    elif args.command == "select":
        if not 1 <= len(args.candidate_ids) <= s.section("project")["max_formal_topics"]: raise SystemExit("每次须选择 1—2 个题目")
        wf.select(args.run_id, args.candidate_ids); print("已记录人工选择")
    elif args.command == "generate-mock":
        for path in wf.generate(args.run_id): print(path)
    elif args.command in {"refresh", "thursday"}:
        if args.decision:
            print(f"下一步：{wf.record_incremental_decision(args.run_id, args.decision, args.note)}")
        else:
            print(json.dumps(wf.incremental_review(args.run_id, args.fixture), ensure_ascii=False, indent=2))
    elif args.command == "draft-check":
        result = check_draft(s, args.topic_id, args.source, major=args.major)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    elif args.command == "draft-review":
        print(register_review(s, args.run_id, args.topic_id, args.source, args.record))
    elif args.command == "research-brief":
        from .research_brief import register_brief, latest_brief
        result = register_brief(s, args.run_id, args.topic_id, args.record) if args.record else latest_brief(wf.db, args.topic_id, args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "semantic-review":
        from .semantic_review import semantic_review, current_review, register_adjudication
        if sum(bool(v) for v in (args.adjudication, args.show, args.run)) > 1:
            raise ValueError("执行、读取和登记裁决须分别操作")
        if args.adjudication:
            result = register_adjudication(s, args.run_id, args.topic_id, args.adjudication)
        elif args.show:
            result = current_review(s, args.topic_id, args.run_id)
        else:
            result = semantic_review(s, args.run_id, args.topic_id, package_path=args.package, execute=args.run, retry=args.retry)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "draft":
        print(wf.draft(args.run_id, args.topic_id, args.source, candidate_id=args.candidate_id))
    elif args.command == "approve":
        print(wf.approve(args.topic_id))
        print("已记录人工通过；文件未自动发送或报送")
    elif args.command == "mark-submitted":
        wf.mark_submitted(args.topic_id, args.submitted_at, args.submission_level)
        print("已登记实际报送信息")
    elif args.command == "status": print(json.dumps(wf.status(args.run_id, include_all=args.all), ensure_ascii=False, indent=2))
    elif args.command == "pause": wf.pause(args.run_id, args.quota); print(f"恢复命令：sqmy resume {args.run_id}")
    elif args.command in {"resume", "retry"}: print(f"下一步：{wf.resume(args.run_id)}")
    elif args.command == "budget":
        usage = recent_usage(wf.db)
        budget_cfg = s.section("budget")
        usage.update({
            "token_window_policy": "report_only",
            "action_token_limits": {
                "screening": budget_cfg["screening_tokens"],
                "pre_research": budget_cfg["pre_research_tokens"],
                "deep_research": budget_cfg["deep_research_tokens"],
                "writing": budget_cfg["writing_tokens"],
                "diagnostic": budget_cfg["diagnostic_tokens"],
            },
            "max_calls_per_action": s.section("model")["max_calls_per_action"],
            "weekly_cost_limit_cny": budget_cfg["weekly_cost_limit_cny"],
            "recent_screening_usage": recent_usage(wf.db, task_id="screening"),
            "adjustment_history_note": (
                "weekly、discovery和research_reserve等旧类型记录仅作历史审计，"
                "不代表当前Token硬闸门。"
            ),
        })
        usage["adjustments"] = budget_adjustments(wf.db)
        print(json.dumps(usage, ensure_ascii=False, indent=2))
    elif args.command == "budget-adjust":
        adjustment_id = record_budget_adjustment(
            wf.db,
            stage=args.stage,
            old_limit=args.old_limit,
            new_limit=args.new_limit,
            reason=args.reason,
            expected_benefit=args.expected_benefit,
            run_id=args.run_id,
        )
        print(adjustment_id)
    elif args.command == "budget-review":
        review_budget_adjustment(
            wf.db,
            args.adjustment_id,
            actual_tokens=args.actual_tokens,
            actual_benefit=args.actual_benefit,
            decision=args.decision,
        )
        print("已记录预算复盘")
    elif args.command == "skip-run":
        wf.skip(args.run_id, args.reason); print("已标记为 skipped")
    elif args.command == "preflight":
        result = preflight(s, stage=args.stage, run_id=args.run_id); print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["ready"] else 2
    elif args.command == "cleanup":
        manager = CleanupManager(s)
        print(json.dumps(manager.apply() if args.apply else manager.plan(), ensure_ascii=False, indent=2))
    elif args.command == "provider-check":
        print(json.dumps(provider_check(s, simulate_codex_quota=args.simulate_codex_quota), ensure_ascii=False, indent=2))
    elif args.command == "evidence-import":
        print(import_evidence_package(s, args.path))
    elif args.command == "evidence-check":
        result = assess_topic(s, args.topic_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["draft_allowed"] else 2
    elif args.command == "evidence-snapshot":
        print(json.dumps(
            capture_evidence_snapshot(
                s,
                args.package,
                args.source_key,
                reason=args.reason,
                local_file=args.file,
            ),
            ensure_ascii=False,
            indent=2,
        ))
    elif args.command == "pre-research-check":
        result = check_pre_research(s, args.run_id, args.candidate_id, args.brief)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["gate"]["research_allowed"] else 2
    elif args.command == "pre-research-review":
        print(
            "下一步："
            + review_pre_research(
                s,
                args.run_id,
                args.candidate_id,
                decision=args.decision,
                note=args.note,
            )
        )
    elif args.command == "novelty-report":
        path = write_rolling_evaluation(s, args.days)
        print(json.dumps({"report": str(path), "metrics": rolling_evaluation(s, args.days)}, ensure_ascii=False, indent=2))
    elif args.command == "discovery-report":
        path = write_discovery_evaluation(s)
        print(json.dumps({"report": str(path)}, ensure_ascii=False, indent=2))
    elif args.command == "retrieve":
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise ValueError("检索问题单必须是JSON对象")
        run_id = wf.init_run("diagnostic") if args.diagnostic else args.run_id
        print(f"检索运行：{run_id}", flush=True)
        try:
            result = execute_retrieval(s, wf.db, run_id, plan, retry_failed=args.retry_failed)
        except (Exception, KeyboardInterrupt):
            if args.diagnostic or plan.get("stage") == "diagnostic":
                wf.db.checkpoint(run_id, phase="discovery", status="needs_review", data={"next": "使用原run-id和问题单恢复，检查retrieval_calls审计"})
            raise
        if args.diagnostic or plan.get("stage") == "diagnostic":
            wf.db.checkpoint(run_id, phase="discovery", status=result["status"], data=result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 2
    elif args.command == "retrieval-usage":
        print(json.dumps(retrieval_usage(wf.db, args.run_id), ensure_ascii=False, indent=2))
    elif args.command == "novelty-review":
        record_review(s, args.audit_id, args.outcome, args.reason)
        print("已记录复核结果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
