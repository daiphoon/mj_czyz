from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Settings, load_dotenv
from .budget import (
    budget_adjustments,
    record_budget_adjustment,
    review_budget_adjustment,
    weekly_usage,
)
from .db import Database
from .document import create_template
from .discovery import LiveDiscovery
from .diagnostics import provider_check
from .evidence import assess_topic, import_evidence_package
from .novelty import record_review, rolling_evaluation, write_rolling_evaluation
from .research_gate import check_pre_research, review_pre_research
from .maintenance import CleanupManager, preflight
from .workflow import Workflow


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
    pool = sub.add_parser("candidate-pool", help="查看近期仍可复核使用的未选候选")
    pool.add_argument("--days", type=int, default=None)
    sel = sub.add_parser("select", help="人工确认 1—2 个候选题"); sel.add_argument("run_id"); sel.add_argument("candidate_ids", nargs="+")
    gen = sub.add_parser("generate-mock", help="仅用于测试的 mock 稿生成"); gen.add_argument("run_id")
    refresh = sub.add_parser("refresh", help="运行零模型选题新鲜度复核，或记录人工复核决定")
    _add_refresh_arguments(refresh)
    thursday = sub.add_parser("thursday", help="兼容旧命令；等同于 refresh")
    _add_refresh_arguments(thursday)
    draft = sub.add_parser("draft", help="证据闸门通过后，将固定结构 Markdown 导出为送审 DOCX")
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
    sub.add_parser("budget", help="查看本周 Token、成本和预算调整记录")
    budget_adjust = sub.add_parser("budget-adjust", help="记录阶段预算调整的原因和预期收益")
    budget_adjust.add_argument(
        "--stage",
        required=True,
        choices=("weekly", "discovery", "research_reserve", "screening", "pre_research", "deep_research", "writing"),
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
        "--stage", choices=("scan", "refresh", "monday", "thursday", "research", "writing"), default="scan"
    )
    cleanup = sub.add_parser("cleanup", help="预览或执行安全清理")
    cleanup.add_argument("--apply", action="store_true", help="自动备份SQLite后执行清理")
    provider = sub.add_parser("provider-check", help="验证模型提供商与自动备用切换")
    provider.add_argument("--simulate-codex-quota", action="store_true", help="模拟Codex额度耗尽并真实调用DeepSeek备用")
    evidence_import = sub.add_parser("evidence-import", help="导入结构化证据包")
    evidence_import.add_argument("path", type=Path)
    evidence_check = sub.add_parser("evidence-check", help="检查主张的独立验证和成稿闸门")
    evidence_check.add_argument("topic_id")
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
        for c in wf.candidates(args.run_id): print(f"{c.id} {c.score} {c.title}")
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
        usage = weekly_usage(wf.db)
        budget_cfg = s.section("budget")
        usage.update({
            "weekly_token_limit": budget_cfg["weekly_token_limit"],
            "weekly_discovery_token_limit": budget_cfg["weekly_discovery_token_limit"],
            "research_writing_reserve_tokens": budget_cfg["research_writing_reserve_tokens"],
            "weekly_cost_limit_cny": budget_cfg["weekly_cost_limit_cny"],
            "discovery_usage": weekly_usage(wf.db, task_id="screening"),
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
        result = preflight(s, stage=args.stage); print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["ready"] else 2
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
    elif args.command == "novelty-review":
        record_review(s, args.audit_id, args.outcome, args.reason)
        print("已记录复核结果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
