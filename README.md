# 社情民意信息研究与生成工作流

这是一个面向海淀区民建区委报送场景的本地、可恢复、可追溯工作流。当前版本已接入真实公开来源的低成本发现层，并保留 mock 回归流程。所有自动候选和 mock 稿均带有“不得直接报送”约束。

## 快速开始

使用 Python 3.12：

```bash
uv sync --extra dev --python 3.12
source .venv/bin/activate
sqmy init
sqmy preflight
sqmy monday
sqmy monday-replay RUN_ID
# 从输出复制 RUN_ID
RUN_ID=2026-01-01-xxxxxxxx
sqmy candidates "$RUN_ID"
sqmy select "$RUN_ID" C1
sqmy pause "$RUN_ID" --quota
sqmy resume "$RUN_ID"
sqmy generate "$RUN_ID"
sqmy status "$RUN_ID"
sqmy budget
sqmy provider-check
sqmy provider-check --simulate-codex-quota
sqmy evidence-import data/sources/minor_online_disputes_evidence_2026-07-12.json
sqmy evidence-check minor-online-disputes-2026-07
sqmy novelty-report --days 21
sqmy skip-run RUN_ID --reason "候选质量不足"
sqmy cleanup
sqmy cleanup --apply
# 如后续复核发现早期阻断正确、误杀或应改写：
sqmy novelty-review RUN_ID:EVENT_ID confirmed --reason "一级来源已明确覆盖原缺口"
```

也可在未安装项目时运行：

```bash
PYTHONPATH=src python3 -m sqmy.cli init
```

## 运行规则

- 周一：`sqmy monday` 检索近90天公开信息，缓存RSS元数据，完成时间过滤、URL还原、去重、主题分类和规则筛选，生成最多5个候选。
- 模型前去重：真实运行会先对比之前真实运行的URL、“标题+发布日”及同日同机构同主题的跨站转述，内容未变化或属于同源转载时不再进入模型。测试、mock、诊断和回放运行不会污染真实历史。
- 小批次延迟：新事件不足4条时默认不调用模型，留到后续扫描合并处理；达到高分且命中突发事件或政策窗口词时例外。延迟事件不会被当成已经处理，阈值可在配置中调整。
- 地域判定：来源检索通道地域与事件实际地域分别保存；事件地域必须有可信地方域名或标题、摘要中的明确证据，“北京电”等发稿地不算北京事件。
- 筛选缓存：完整初筛输入哈希相同时，复用已完成的结构化结果，不再调用模型。
- 时效性：发布7天内20分、30天内15分、60天内8分、90天内3分；时间不明不得分。
- 离线回放：`sqmy monday-replay RUN_ID` 使用已保存的模型结果重新运行标题、历史、新意和报告步骤，新增模型调用为0。
- 强制重算：只有人工明确要求时使用 `sqmy monday --force`；它会跳过历史排除和模型结果缓存。
- 扫描前预检：`sqmy preflight` 检查SQLite、配置、目录、Codex、DeepSeek备用、密钥权限、残留任务和预算，模型调用为0。
- 全部放弃：候选质量不足时使用 `sqmy skip-run RUN_ID --reason "..."`，不让运行长期停在 `needs_review`。
- 安全清理：`sqmy cleanup` 只预览；`--apply` 会先使用SQLite在线备份，再清理测试、mock、空运行和临时输出，最后执行完整性检查。RSS缓存、真实运行、最新回放、提供商诊断、证据包和研究报告默认保留。
- 离线回归：`sqmy monday-mock` 生成5个纯 mock 候选；`sqmy monday --fixture PATH` 使用离线RSS夹具。
- 人工选择：`sqmy select RUN_ID C1 [C2]`，最多 2 个。
- 周四：第一版以 `sqmy generate RUN_ID` 模拟增量复核、研究、成稿和导出。
- 暂停/恢复：`sqmy pause RUN_ID --quota`、`sqmy resume RUN_ID`。
- 失败继续：`sqmy retry RUN_ID`。
- 提供商诊断：`sqmy provider-check` 验证Codex主通道；加 `--simulate-codex-quota` 时模拟额度错误并真实验证DeepSeek备用，同时写入审计库。
- 正式 DOCX 模板：`templates/submission_template.docx`。
- mock 稿：`outputs/review/RUN_ID/`，不会自动进入 `outputs/submission/`，也不会自动发送。
- 证据审查：`evidence-import` 导入轻量“主张—来源”包，`evidence-check` 核对独立来源链、直接反证和现有政策覆盖。闸门不通过时命令返回非零状态，不得进入正式写作。
- 制度新意快审：模型初筛时同步产生一句可被反证的缺口假设、缺口类型和最多2条定向检索词。程序先查本地政策机制库，再做标题和摘要级反证检索。只有“制度不存在”且被已核验机制直接覆盖时才自动阻断；执行、效果、协同和问责类缺口只标记复核并扣分，不自动删除。

## 安全和成本

密钥只放 `.env`，不得提交。默认 `provider = "auto"`：先通过本机已登录的 `codex exec` 使用ChatGPT/Codex订阅额度；只有返回明确的额度或速率错误时，才尝试DeepSeek API。普通网络错误、结构化结果错误和质量错误不会触发切换。未设置 `DEEPSEEK_API_KEY` 时，额度错误会安全进入 `paused_quota`。

DeepSeek密钥可通过终端环境变量或本地 `.env` 提供；`.env` 已被Git忽略。调用记录保存提供商、模型、提示哈希、可获得的Token和估算成本。Codex订阅通道成本记为0元；DeepSeek按配置单价估算。

当前备用模型固定为 `deepseek-v4-pro`，显式启用思考模式并设置 `reasoning_effort = "max"`。价格配置按官方当前标准：缓存未命中输入3元/百万Token、输出6元/百万Token；价格变化时应同步更新配置。

`sqmy budget` 直接按 `model_calls.created_at` 统计真实最近7天的全部模型调用，不再使用“最近10次运行”代替周预算。模型调用前同时核对周预算和筛选阶段预算，Codex CLI估算包含可配置的固定上下文安全垫。

来源配置位于 `config/sources.toml`。搜索RSS只承担发现功能，记录中保存的是还原后的原始页面URL；候选阶段不批量下载网页全文。

证据规则位于 `config/settings.toml` 的 `[evidence]`。多家媒体转述同一发布会、报告或数据源时只计为一个原始信息链；一个正式一级原始来源可以单独支撑其直接公布的事实。企业自报默认只证明“已公开宣称某机制”，不证明实际效果。

已核验的制度机制位于 `config/policy_mechanisms.toml`，每条都必须保存来源和最后核验时间。默认超过180天未重新核验的机制不再用于自动阻断，只作为复核线索。反证搜索会自动限定政府、法院和网信等一级来源，并排除候选新闻自身和同标题页面。周一扫描每次完成后会自动重新生成 `outputs/review/metrics/novelty_rolling_21d.json`，滚动统计审计量、阻断量、复核覆盖率、误杀标签、审计Token、模型前排除量、小批次延迟量、缓存命中和节省Token。其中“潜在浪费防止量”是代理指标，不得当作实际账单节省。

## 测试

```bash
uv run --python 3.12 --with python-docx --with pytest python -m pytest -q
```

详细设计见 `docs/architecture.md`，模板证据见 `docs/template_artifact.md`。
