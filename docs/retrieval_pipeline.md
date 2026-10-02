# 检索流水线一期

发现、政策搜索、已知URL获取、主张核验沿用现有Python与SQLite工作流。此次不建立多Agent编排，不自动重开停止题、深研、写稿或报送。

## 入口与职责

- `search_types.py` 定义9类检索用途和统一请求/结果。结果始终为 `unverified`，没有日期时保留空值。
- `search_providers.py` 提供Brave Web、Tavily keyless Search/Extract和旧Bing News RSS适配器。Bing当前实现是公开新闻RSS，不是已退役的Azure Bing Search API，也不作为通用政策搜索。
- `search_router.py` 按用途、能力和明确授权选择入口。默认顺序为keyless、RSS、Brave；RSS仅支持news，图片请求没有可用入口时明确返回unavailable。合法空结果停止fallback，不换词凑数。并行默认关闭，显式开启后至多两个入口。
- `retrieve` 的新行为保留 `queries` 搜索，暂时隔离 `pages` 自动正文获取。每页记录 `unsupported/unsupported_transport`、`body_read=false`、`source_unread/unverified` 和零HTTP派发，不解析DNS、不读取Fetch缓存、不调用HTTP或Extract。搜索失败和 `--retry-failed` 都不会打开页面传输。需要正文的研究保持 `RESEARCH_INCOMPLETE`。
- `fetch.py` 保留原URL、DNS、重定向安全检查及有界摘录实现，供离线回归和后续传输设计验证；当前 `retrieve` 不实例化它。未取得正文的记录没有最终响应URL、正文哈希或片段；`provider=direct_http` 表示原拟用入口，不表示发出了HTTP请求。
- `source_registry.py` 读取栏目/域名属性，供回源规划。域名匹配按主机及真实子域，不接受 `gov.cn.evil.example`。来源属性不证明原始发布链或主张可靠。
- 原有 `evidence.py` 的 `EvidenceStore` 整理Fetch记录，默认 `source_unread/unverified`；人工逐片段核验后才能写 `excerpt_verified`。不完整、未命中目标或哈希不一致的记录不能被此接口标为已核验。继续使用原sources/claims关系和evidence_v2，不自动导入、通过证据闸门或代替主张核验。

## 免费、计费与熔断

默认 `[search] allow_paid=false`、`[brave] enabled=false`。Tavily keyless固定发送 `X-Tavily-Access-Mode: keyless`，不读取或发送账户密钥、不携带Authorization；免费入口失败不会隐式切换为密钥计费请求。Brave须同时显式开启计费授权、Brave开关并配置密钥。费用记录为保守估算，不是实际账户账单；免费服务没有可由代码保证的永久额度。

检索供应商和认证模式分别维护CLOSED/OPEN/HALF_OPEN状态；429尊重Retry-After，认证、退役及明确配额错误停止该入口，普通超时/5xx按阈值冷却，冷却后仅放行一个探测。未验证的免费限流响应记为错误。熔断不阻止其他合格搜索入口；页面隔离独立生效。状态保存在现有缓存目录，可由维护者在查明原因后处理；不自动重置账户或抹除失败用量。

原 `retrieval_calls` 向前增加provider、auth_mode、intent、query_id、latency_ms、retry_count、fallback_from、cache_hit；旧行保留legacy_unknown。真实请求前先占额。整个行动最多4次Search、2次Extract和6次合计尝试，fallback/重试与旧记录共用额度；旧配置若更低则沿用更低限制。默认不自动重试。缓存不重发网络请求，但跨行动复用记入行动范围。未知中断仍保留running预留，恢复重试须显式指定且受剩余额度限制。

这8个字段均属于`retrieval_calls`：provider登记入口，auth_mode区分keyless/keyed/none，intent保留检索用途，query_id登记请求哈希短ID，latency_ms登记耗时，retry_count登记重试序号，fallback_from保留上个入口，cache_hit区分缓存复用。它们不是来源事实认证字段。

`requests`是兼容保留的**预留条数**，新输出同时显示`reservations`。新请求在`result_json.transport`记录HTTP派发尝试、收到响应和失败阶段；不新增数据库列，不追填旧行。真实派发前先持久化标记；进程在标记后中断或派发后无响应时，送达状态仍未知，不能解释为供应商没收到或实际费用为零。旧入口/旧记录没有传输证据时明确计入`unknown_transport_records`。缓存复用不重记派发或供应商credits，仍占原行为范围；发送前失败预留也不退款。

请求构造或观察包装在进入网络传输前抛出的本地错误标为`local_before_dispatch`，不据此打开供应商故障熔断。HTTP收到后解析、规范化失败分别保留阶段。供应商返回的有效`usage.credits`写入`reported_credits`；keyless的计价字段保持零，credits不被解释为账户新增账单。审计内容只含阶段和数值，不记录URL、请求正文、认证头或错误响应原文。

Bing旧RSS适配层同样检查真实派发状态：缓存读取、Request/TLS准备或审计持久化在发送前失败时保留本地错误和零派发，不触发供应商熔断；已派发并收到HTML、坏RSS等不合格响应时仍按invalid_response冷却。离线回归使用实际SourceCollector→Bing适配器→Router路径，不能用统一stub绕过包装层验证。

Fetch单列`transport_metadata`和`destination_error`。原引擎对非公网DNS结果仍返回`refused/unsafe_destination/non_global_dns_address`，不调用Extract或发送HTTP；这项检查没有绑定实际连接IP，不能独自证明代理的实际终点安全。新页面隔离是应用策略，不是来源已通过安全校验。不能把fake-IP地址白名单化、删除检查，或将受拒URL转送旧collector、curl或Extract。此次不改变Clash、DNS、TUN、Tailscale或系统权限。完整自动正文获取仍需要另行验证解析、连接和每次重定向的实际终点约束，隔离不算该修复的验收。人工补读不能冒充程序通过。

## 冻结、恢复和取证

原引擎的Search与Extract熔断仍按供应商、认证模式及端点分开保存，离线回归继续覆盖冷却、拒绝和额度。当前 `retrieve pages` 不执行Extract。API端点404/410标为endpoint_unavailable并冷却，不据单个410断言供应商退役；普通来源网页410只是该页获取失败。HTTP 410的语义是目标资源不可用，不能据此证明整个服务退役，参见 [RFC 9110 §15.5.11](https://www.rfc-editor.org/rfc/rfc9110.html#name-410-gone)。

问题单的IMAGE_SEARCH明确映射到image能力；一期适配器均不支持时保留unavailable与RESEARCH_INCOMPLETE，不发送网页查询。独立Fetch引擎的离线测试仍覆盖PDF内容类型、requires_manual_extraction、补提取时的原响应类型/哈希/截断以及人工核验边界。新隔离策略不读取页面，因而不声称已知其响应内容类型、哈希或适用条件。

发现采集继续使用 `retrieval_pipeline_v1` 及原冻结参数，不因页面隔离改动配置或历史采集。新的 `retrieve` 使用 `retrieval_pipeline_v2`，单独冻结 `page_fetch_policy=isolated_v1`；问题单、Search/Fetch参数和Registry哈希仍绑定原行为。已完成查询与隔离页面幂等复用，`--retry-failed` 只重试失败查询，继续共用原用量和额度，不改写已有隔离页面。

启用新Search路由时，旧v1及旧无版本检索检查点仅核对原输入和原参数后返回历史视图，不写检查点、运行状态或用量，不重试HTTP、Search或Extract。返回 `historical/reused`、原 `checkpoint_status` 和恢复限制；旧未完成材料保持 `RESEARCH_INCOMPLETE/needs_review`。旧已完成、待核验材料保留其状态，不升级证据。CLI复用或旧输入校验失败也不覆盖原运行检查点。未知契约、删除契约标识或改动隔离策略均拒绝，不降级到旧付费入口。程序不自动创建新run、修复轮次或退款来继续旧行为。

问题单继续指定 `stage/candidate_id/queries/pages/repair`。查询保留 `purpose`，可附 `intent/domains/exclude_domains`；政策、反证等不受发现新闻90天窗口机械限制。pages登记URL和1—8个目标词，无需提前声称HTTP失败；登记后明确为正文未读。选题、预研、停止记录、最新决策单和补查轮次保持原闸门。

Search空政策结果、入口失败、Fetch失败或未找到目标会写 `RESEARCH_INCOMPLETE/needs_review`；材料齐备只写 `MATERIALS_READY_AWAITING_VERIFICATION`。不会把不完整检索写成研究通过。

Fetch只落必要摘录及限制条件、元数据、原始响应哈希和摘录哈希，不落全文。`content_hash_kind=response_bytes`表示收到的响应字节，`extract_returned_text`表示供应商返回文本；截断标记独立保留，二者都不证明完整原页。网页metadata日期也标为未核验。文稿仍无超链接、按既有DOCX模板和署名输出；内部来源URL可用于核验。

## 本机路径与验证

公开配置不携带私人参考稿路径。可用 `SQMY_REFERENCE_PATH` 指定本机原始参考文档，或保留本地 `document.reference_path`；缺少原始参考文档时预检明确失败，版本化模板不被冒充为原始稿。测试只用隔离样本和临时数据库。

离线测试覆盖供应商HTTP响应、免费认证分离、错误分类、熔断探测、fallback/parallel/域名筛选、缓存及共享额度，以及页面隔离、查询继续、CLI诊断/绑定运行/显式重试、旧历史只复用和冻结兼容。原Fetch引擎的安全拒绝、条件摘录与人工核验边界仍单独回归。普通测试阻止真实网络和工作区数据库连接，不迁移用户数据库或改历史输出。

后续仍需在另获真实运行授权后观察召回和研究质量。图片检索、反向检索策略、跨域原始链自动识别、自动PDF解析和复杂Agent编排不在一期内。
