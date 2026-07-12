from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Settings, load_dotenv
from .budget import weekly_usage
from .db import Database
from .document import create_template
from .discovery import LiveDiscovery
from .diagnostics import provider_check
from .evidence import assess_topic, import_evidence_package
from .novelty import record_review, rolling_evaluation, write_rolling_evaluation
from .maintenance import CleanupManager, preflight
from .workflow import Workflow


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sqmy", description="社情民意信息研究与生成工作流")
    p.add_argument("--config", default="config/settings.toml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化数据库和正式模板")
    monday = sub.add_parser("monday", help="运行周一真实来源候选扫描")
    monday.add_argument("--fixture", type=Path, help="使用离线 RSS 测试夹具")
    monday.add_argument("--force", action="store_true", help="人工强制重算，跳过历史排除和模型结果缓存")
    replay = sub.add_parser("monday-replay", help="使用已保存的模型结果离线回放周一流程")
    replay.add_argument("source_run_id")
    sub.add_parser("monday-mock", help="运行旧版纯 mock 候选扫描")
    show = sub.add_parser("candidates", help="查看候选题"); show.add_argument("run_id")
    sel = sub.add_parser("select", help="人工确认 1—2 个候选题"); sel.add_argument("run_id"); sel.add_argument("candidate_ids", nargs="+")
    gen = sub.add_parser("generate", help="生成 mock 正式稿"); gen.add_argument("run_id")
    status = sub.add_parser("status", help="查看运行状态"); status.add_argument("run_id", nargs="?"); status.add_argument("--all", action="store_true", help="包含测试、诊断和回放运行")
    pause = sub.add_parser("pause", help="暂停运行"); pause.add_argument("run_id"); pause.add_argument("--quota", action="store_true")
    resume = sub.add_parser("resume", help="恢复运行"); resume.add_argument("run_id")
    retry = sub.add_parser("retry", help="从失败或暂停步骤继续"); retry.add_argument("run_id")
    export = sub.add_parser("export", help="导出已确认选题的 DOCX"); export.add_argument("run_id")
    budget = sub.add_parser("budget", help="查看本周 Token 和成本统计")
    skip = sub.add_parser("skip-run", help="人工关闭本次运行且不进入深研")
    skip.add_argument("run_id"); skip.add_argument("--reason", required=True)
    sub.add_parser("preflight", help="零模型调用扫描前预检")
    cleanup = sub.add_parser("cleanup", help="预览或执行安全清理")
    cleanup.add_argument("--apply", action="store_true", help="自动备份SQLite后执行清理")
    provider = sub.add_parser("provider-check", help="验证模型提供商与自动备用切换")
    provider.add_argument("--simulate-codex-quota", action="store_true", help="模拟Codex额度耗尽并真实调用DeepSeek备用")
    evidence_import = sub.add_parser("evidence-import", help="导入结构化证据包")
    evidence_import.add_argument("path", type=Path)
    evidence_check = sub.add_parser("evidence-check", help="检查主张的独立验证和成稿闸门")
    evidence_check.add_argument("topic_id")
    novelty_report = sub.add_parser("novelty-report", help="生成制度新意闸门滚动评估")
    novelty_report.add_argument("--days", type=int, default=None, help="统计窗口，默认21天")
    novelty_review = sub.add_parser("novelty-review", help="为早期阻断结果补充后续复核标签")
    novelty_review.add_argument("audit_id")
    novelty_review.add_argument("outcome", choices=["confirmed", "reversed", "reframed"])
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
    elif args.command == "monday":
        run_id, candidates = LiveDiscovery(s).run(args.fixture, force=args.force)
        print(run_id)
        print(f"已生成 {len(candidates)} 个候选：outputs/candidates/{run_id}.md")
    elif args.command == "monday-mock":
        run_id = wf.init_run(); wf.scan(run_id); print(run_id)
    elif args.command == "monday-replay":
        run_id, candidates = LiveDiscovery(s).replay(args.source_run_id)
        print(run_id)
        print(f"已离线回放，生成 {len(candidates)} 个候选；新增模型调用 0 次")
    elif args.command == "candidates":
        for c in wf.scan(args.run_id): print(f"{c.id} {c.score} {c.title}")
    elif args.command == "select":
        if not 1 <= len(args.candidate_ids) <= s.section("project")["max_formal_topics"]: raise SystemExit("每次须选择 1—2 个题目")
        wf.select(args.run_id, args.candidate_ids); print("已记录人工选择")
    elif args.command in {"generate", "export"}:
        for path in wf.generate(args.run_id): print(path)
    elif args.command == "status": print(json.dumps(wf.status(args.run_id, include_all=args.all), ensure_ascii=False, indent=2))
    elif args.command == "pause": wf.pause(args.run_id, args.quota); print(f"恢复命令：sqmy resume {args.run_id}")
    elif args.command in {"resume", "retry"}: wf.resume(args.run_id); print(f"下一步：sqmy generate {args.run_id}")
    elif args.command == "budget":
        usage = weekly_usage(wf.db)
        usage.update({"weekly_token_limit": s.section("budget")["weekly_token_limit"], "weekly_cost_limit_cny": s.section("budget")["weekly_cost_limit_cny"]})
        print(json.dumps(usage, ensure_ascii=False, indent=2))
    elif args.command == "skip-run":
        wf.skip(args.run_id, args.reason); print("已标记为 skipped")
    elif args.command == "preflight":
        result = preflight(s); print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["ready"] else 2
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
    elif args.command == "novelty-report":
        path = write_rolling_evaluation(s, args.days)
        print(json.dumps({"report": str(path), "metrics": rolling_evaluation(s, args.days)}, ensure_ascii=False, indent=2))
    elif args.command == "novelty-review":
        record_review(s, args.audit_id, args.outcome, args.reason)
        print("已记录复核结果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
