# common 与 utils 检查及改造清单

检查日期：2026-10-19。本文依据当前源码、已安装依赖源码，以及两个 LangGraph 流程、三个 Milvus 集合和真实教育样本整理，供后续节点开发时执行。

初次检查仅更新 Markdown。2026-10-19 本轮已完成配置模块及最小调用方适配，详见第 6 节；其余未标记完成的改造仍为待办。`.env` 原样保留，未连接数据库、创建集合或下载模型。架构与节点合同分别见 [README.md](README.md) 和 [节点开发交接.md](节点开发交接.md)。

## 1. 检查范围与结论

初次检查覆盖 `common/`、`utils/` 下 34 个 Python 文件和 7 个提示词文件，共 41 个，其中 9 个包初始化文件、32 个功能/提示词文件。配置适配后新增 _env.py 和 mongo_config.py；本次核对为 36 个 Python 文件加 7 个提示词，共 43 个。下文部分问题位置是原检查行号，修改后应按函数名核对当前源码。包初始化文件保持轻量，不要加入模型下载、数据库连接或业务初始化。

三集合 Schema 和配置方向可以保留，不需要推倒重建。主要缺口集中在：任务持久化与状态、SSE 协议、数据库异常语义、聊天历史、异步日志、模型调用接口，以及教育领域提示词。

| 优先级 | 含义 | 执行时机 |
| --- | --- | --- |
| P0 | 会影响节点正确性，或与已确定的业务合同直接冲突 | 实现依赖该能力的节点之前完成 |
| P1 | 需要补齐的接口、校验或运行约束 | 随对应导入/查询功能完成 |
| P2 | 可保留、可暂不接入，或后续按实际需要调整 | 不阻塞首版主流程，不为清理而大改 |

### 已确认的具体问题

| 编号 | 源码位置 | 证据与影响 | 修改目标 |
| --- | --- | --- | --- |
| F01 | `common/logging/logger.py:118`、`:141` | 日志装饰器是同步 wrapper。装饰 async 节点后，协程尚未执行就记录完成，之后的异常不经过该捕获；已隔离复现 | 分别支持同步/异步函数，异步路径 await 后再计时、记录异常 |
| F02 | `common/logging/logger.py:88`、`:110` | 调用位置修补选中了自身 fix_log_position 帧；追踪 ID 仅识别 session_id/task_id，不识别 request_id/conversation_id；已隔离复现 | 修正调用位置，统一请求、会话、任务的结构化日志上下文 |
| F03 | `utils/clients/mongo_history_utils.py:209` | 查最近消息按 ts 降序，返回前没有反转；与注释声称的正序不符 | 先取最近 N 条，再按时间正序交给上下文节点 |
| F04 | `utils/clients/mongo_history_utils.py:64`、`:107` | 导入模块即尝试连接并创建索引；消息没有 request_id、结果状态、引用，仍使用 item_names | 改受控初始化，补幂等保存和教育结果字段 |
| F05 | `utils/task_utils.py:23`、`utils/sse_utils.py:8` | 任务仍为 pending/processing/completed/failed，事件仍为 ready/delta/final；与新状态、事件协议冲突 | 统一为任务 pending/running/succeeded/failed；SSE meta/progress/token/result/error/done |
| F06 | `utils/clients/milvus_utils.py:30`、`:112`、`:201` | 连接失败返回 None，批量读取失败可返回部分结果或 []，搜索失败返回 None | 基础设施错误必须可识别，不把失败、缺失、部分读取混成正常无结果 |
| F07 | `common/config/embedding_config.py`、`utils/lm/embedding_utils.py` | **配置部分已修复**：本地路径 > BGE_M3 > 默认仓库名，目录缺失提前报错 | 查询编码接口、向量输出与输入校验仍待后续实现 |
| F08 | `common/config/lm_config.py`、`utils/lm/lm_utils.py` | **已修复并离线验证**：缺省/空白温度为 0.1，合法 0 原样传入 SDK，非法/非有限值报变量名 | 已完成，本轮不改流式调用 |
| F09 | `utils/rate_limit_utils.py:23`、`:33` | 单个全局 deque 无锁并使用阻塞 sleep；max_requests=0 触发 IndexError，已隔离复现 | 参数校验、单调时钟、并发安全、资源分组；异步调用不能直接阻塞事件循环 |
| F10 | `utils/clients/minio_utils.py:23`、`:44` | 首次 getter 会创建不存在的桶，并为新桶设置允许匿名读取的策略 | 分离客户端获取与桶初始化，普通节点不顺带改桶策略 |

P0 不等于所有文件立即重写。例如历史、SSE 可以在查询流程实现前完成；只做纯 Markdown 解析时，不应因此被远程服务初始化阻塞。

## 2. common 逐文件结论

### 2.1 配置模块

| 文件 | 处理结论 / 优先级 | 具体改动与验收 |
| --- | --- | --- |
| [common/config/_env.py](common/config/_env.py) | 已实现并离线验证，优先复用 | 根目录环境加载、文本/布尔/数字解析、模型来源和配置异常已集中。后续配置扩展使用现有辅助函数，不在节点或新配置类中重写 |
| [common/config/mongo_config.py](common/config/mongo_config.py) | 配置已完成，仓储接入待办 | MongoDB URI、数据库、六类业务集合和毫秒超时已有集中配置；新客户端/仓储应读取该对象，不再手写 os.getenv 或重新定义另一套 MongoConfig |
| [common/config/lm_config.py](common/config/lm_config.py) | 已完成配置修复 | 默认模型/温度集中配置，温度允许 0-2；可选字段使用 None。创建客户端前 validate() 检查 OPENAI_API_KEY / OPENAI_BASE_URL |
| [common/config/embedding_config.py](common/config/embedding_config.py) | 配置适配已完成；版本固定待办 | 路径优先且验证目录；BGE_M3 生效；设备默认 cpu，FP16 请求值与实际生效值分开，CPU/MPS 关闭 FP16。不探测硬件、不固定仓库 revision |
| [common/config/reranker_config.py](common/config/reranker_config.py) | 配置适配已完成 | 新增 BGE_RERANKER_PATH；LARGE 兼容仓库名/旧目录；设备与 FP16 统一。已核对安装的 FlagEmbedding 使用 devices 参数并修正调用。启用/失败回退策略由后续节点处理 |
| [common/config/minio_config.py](common/config/minio_config.py) | 配置适配已完成 | 布尔解析统一；endpoint 为 host[:port]，无协议/凭据/路径；创建客户端前检查必要参数。自动建桶/匿名策略 F10 仍未改 |
| [common/config/mineru_config.py](common/config/mineru_config.py) | 配置补充已完成 | 可选地址/token，新增请求超时、轮询间隔/次数及正数校验；validate() 由未来 PDF 适配器调用，目前未接 SDK |
| [common/config/milvus_config.py](common/config/milvus_config.py) | 配置增强已完成 | 保留三集合及重名检查，补合法名称、URI 必填校验和超时（秒）；getter 已传入 timeout，既有异常返回语义 F06 未改。无第四个 Milvus 集合 |
| [common/config/bailian_mcp_config.py](common/config/bailian_mcp_config.py) | 可选配置已统一，P2 暂不接入 | 使用共享读取/延迟必填校验；不在 common/config/__init__.py 批量导入，不作为业务启动必填项 |

配置共同要求：根目录 `.env` 的定位与基础值解析保持一致；不要为了新增一个配置就复制一套不一致的 load_dotenv/类型转换逻辑。可增加小型共享辅助函数，不要求为此构建复杂配置框架。连接验证放在对应服务初始化阶段，不在纯模型定义模块导入时尝试联网。

### 2.2 日志、Schema 与提示词

| 文件 | 处理结论 / 优先级 | 具体改动与验收 |
| --- | --- | --- |
| [common/logging/logger.py](common/logging/logger.py) | 必改，P0 | 修复 F01/F02；初始化日志输出改为可控且幂等，避免模块重载移除其他 sink；节点计时使用单调时钟。日志可关联 task_id/request_id/conversation_id；诊断输出不能无差别打印配置、局部变量和整份 State |
| [common/schemas/milvus_schema.py](common/schemas/milvus_schema.py) | 保留，补配套校验 P1 | 维持现有 26/26/24 字段与字符串主键。业务枚举、非空、数组容量、UTF-8 字节长度、向量维度应在投影/入库校验器中补齐；Schema 不会替代这些校验。初始化时另核对服务端已有集合，不由节点自动删表 |
| [common/prompt/answer_out.prompt](common/prompt/answer_out.prompt) | 必改，P0 | 去掉 item_names 商品上下文；改成教育证据回答，输入使用 question/context/history 与固定证据编号，输出使用真实引用；明确无结果/证据不足。结构化课程/查题结果由代码组织，不强行经模型重写 |
| [common/prompt/rewritten_query_and_itemnames.prompt](common/prompt/rewritten_query_and_itemnames.prompt) | 替换为教育查询分析模板，P0 | 建议新建 query_analyze.prompt，输出独立问题、六类意图、实体线索及澄清信息；实体线索再由代码解析为有效 ID 和过滤条件。JSON 输出和占位符与 QueryState 保持一致，旧商品模板不接入新图 |
| [common/prompt/item_name_recognition.prompt](common/prompt/item_name_recognition.prompt) | 替换职责，P1 | 非结构化内容需要时新增 metadata_extract.prompt，提取教育元数据并保留证据位置与未知值。已有规则明确的 MD 课程/题库不依赖 LLM 抽取；旧商品模板不继续调用 |
| [common/prompt/product_recognition_system.prompt](common/prompt/product_recognition_system.prompt) | 暂不使用，P2 | 商品识别不在当前流程中，保留未引用文件即可，无需为了清理立即删除 |
| [common/prompt/hyde_prompt.prompt](common/prompt/hyde_prompt.prompt) | 暂不使用，P2 | 首版不接入 HyDE，不能把假设答案混成知识库证据 |
| [common/prompt/image_summary.prompt](common/prompt/image_summary.prompt) | 按需保留，P2 | 图片摘要若后续启用，需要适配真实视觉输入与来源关联；当前模板存在不代表已经实现图片理解，也不必作为全部文件导入的必经步骤 |
| [common/prompt/rerank_text_refine.prompt](common/prompt/rerank_text_refine.prompt) | 暂不使用，P2 | 这份模板是生成式精炼文本，不是候选打分重排。避免用它改写原题、公式、代码或来源片段；首版重排用模型相关性评分 |

## 3. utils 逐文件结论

### 3.1 数据库与模型工具

| 文件 | 处理结论 / 优先级 | 具体改动与验收 |
| --- | --- | --- |
| [utils/clients/milvus_utils.py](utils/clients/milvus_utils.py) | 继续改造，P0 | 保留已完成的 VARCHAR 主键、共有返回字段和默认 RRF。修复 F06，校验 limit/batch_size；连接超时已接入，操作级超时与明确的写入/删除/初始化接口仍待补充；返回统一 Candidate，跨集合融合放 retrieval 层，不把三种集合专属字段一并请求 |
| [utils/clients/mongo_history_utils.py](utils/clients/mongo_history_utils.py) | 必改，P0 | 修复历史顺序和导入联网；增加 request_id、结果状态、引用、会话元数据、幂等保存；移除新流程对商品 item_names 的依赖。消息 ID 与业务 record_id 分开，不能把业务 UUID 无条件转 ObjectId；失败和无历史必须区分 |
| [utils/clients/minio_utils.py](utils/clients/minio_utils.py) | 继续改造，P0/P1 | 先分离 F10 的副作用；再补原文件上传、流/下载读取、临时访问链接、按明确对象键清理等节点所需接口。失败需显式抛出，重试不产生随机重复对象，上传后能定位原文件 |
| [utils/lm/embedding_utils.py](utils/lm/embedding_utils.py) | 必补接口，P1 | BGE_M3 配置已修复；后续提供文档和查询两类入口，共用向量转换。校验空白文本、批量条数、数值有限性、稠密维度、稀疏索引和值，维护模型版本；受控初始化，不在模块导入时下载 |
| [utils/lm/lm_utils.py](utils/lm/lm_utils.py) | 必改，P0/P1 | 温度 0 与 OPENAI_BASE_URL 报错已修复；后续 enable_thinking 只在服务支持时发送。补超时/有限重试与流式调用适配；教育结构化输出在节点层校验，不能只靠 json_mode=True |
| [utils/lm/reranker_utils.py](utils/lm/reranker_utils.py) | 补齐，P1 | 模型加载的路径/设备/FP16 传参已适配，仍只有加载模型与 token 计数。后续增加 query-候选文本批量评分、稳定排序和截取接口，保留原候选 ID 与分数。不要仅为普通 token 计数就默认加载整个 Reranker，按实际模型/分词器预算控制上下文 |

Milvus 新增写入接口需要支持按目标集合批量 upsert、返回写入明细、按 task_id 清理，并在初始化层核对 Schema。任务成功状态由 MongoDB 仓储批量核验，客户端工具不得隐式把来源发布为成功。

### 3.2 状态、流式、基础工具与脚本

| 文件 | 处理结论 / 优先级 | 具体改动与验收 |
| --- | --- | --- |
| [utils/task_utils.py](utils/task_utils.py) | 必改，P0 | 内存字典只可当缓存；状态落 MongoDB。统一状态和图注册键，清除新节点映射对商品、知识图谱、网页搜索的依赖；进度按阶段和数量记录，支持失败原因及重启后未完成任务处理 |
| [utils/sse_utils.py](utils/sse_utils.py) | 必改，P0 | 对外事件改成新协议，队列按 request_id 隔离；增加有界缓冲/背压与取消清理，done 最多一次。保留线程安全队列或显式跨线程投递，不可未经分析直接换成不能跨线程使用的队列；异常不能只 print 后消失 |
| [utils/rate_limit_utils.py](utils/rate_limit_utils.py) | 适配，P1 | 修复 F09：正数参数、单调时间、加锁检查/登记、等待后重新检查；按模型/API 等资源分别限速。提供异步友好调用或在线程执行，另用并发上限保护解析和推理资源 |
| [utils/normalize_sparse_vector.py](utils/normalize_sparse_vector.py) | 默认不接入，P2 | 这是 L2 数学归一化，不是索引/类型转换。保留文件可行，但不得默认再归一化 BGE-M3 的原生稀疏权重；需要的类型转换放统一 embedding 输出适配器，并输出原生 int/float |
| [utils/escape_milvus_string_utils.py](utils/escape_milvus_string_utils.py) | 有限复用，P1 | 当前会将换行/制表符替换为空格，可能改变精确值；不作为任意查询表达式拼接器。优先按 SDK 实际支持使用值参数化；否则使用统一字符串字面量/列表编码，并对字段和运算符做白名单 |
| [utils/load_prompt.py](utils/load_prompt.py) | 可复用，补校验 P1 | 保留 UTF-8 文件加载；为新模板列明必填变量并报告缺失变量，约束模板名来自内部清单。JSON 示例需转义花括号，模板格式化不能改写资料中的代码/公式；测试不必初始化全套日志输出 |
| [utils/path_util.py](utils/path_util.py) | 配置路径适配已完成 | 项目根目录不依赖 .env 必须存在；PROJECT_ROOT 验证为目录；路径工具不再加载 .env，统一由 common/config/_env.py 加载 |
| [utils/format_utils.py](utils/format_utils.py) | 可复用，限制用途 P2 | 目前直接 json.dumps 整份 State，没有摘要或脱敏功能。调试日志需要单独的摘要函数，排除向量、密钥、完整资料和聊天正文；严格序列化与调试预览不要用同一套静默字符串转换 |
| [utils/download_model/download_bgem3.py](utils/download_model/download_bgem3.py) | 改为独立脚本，P1 | 当前导入即下载，且缓存目录硬编码在 D 盘。加 main 入口，模型与缓存目录从参数/配置读取；模块导入不得触发下载或写盘 |
| [utils/download_model/download_reranker.py](utils/download_model/download_reranker.py) | 改为独立脚本，P1 | 同上；下载结果应与运行时 Reranker 配置一致，不把固定机器路径当成所有环境的默认值 |

## 4. 需要新增的公共能力

以下业务能力大部分仍待实现；MongoDB 配置已经完成，需复用并接入，不能按“全部缺失”重新编写。按交接合同落到对应目录，具体文件可小幅合并，避免只有一个转发函数也拆成独立模块。

| 位置 | 需要新增的内容 | 依赖节点 |
| --- | --- | --- |
| common/schemas/ | SourceFile、ParsedBlock、ContentRecord、ChunkDraft、Candidate、Reference、NodeError、状态与意图枚举、QueryFilters | 全部节点 |
| common/config/mongo_config.py | 已实现，复用其 URI、数据库、业务集合名与超时配置；待新增的客户端/仓储接入，不导入旧历史模块获取配置 | 任务、内容和历史 |
| utils/clients/ 或 repositories/ | 共享 Mongo 客户端及任务、文档、完整内容、课程关系、会话仓储；明确任务发布校验、精确查询和幂等写入接口 | entry、persist、finish、各 search、load_context |
| utils/parsers/ | MD、DOCX、PDF 格式适配器，统一 ParsedBlock；保留表格/代码/图片引用和来源位置 | parse_file |
| utils/content_builders/ | 课程系列/子课程、题库/题目规则解析，文档切分，稳定 ID 及三集合字段投影校验 | extract_metadata、build_records |
| utils/retrieval/ | 实体/关系解析、集合专属过滤构造、Candidate 转换、跨集合 RRF、发布状态过滤、去重与受控补召回 | analyze_query、各 search、rerank |
| common/prompt/ | query_analyze.prompt、metadata_extract.prompt，以及重写后的 answer_out.prompt | analyze_query、extract_metadata、answer_output |
| 请求运行上下文 | 依赖注入、请求级 token 缓冲、取消信号、事件投递；不要把客户端和队列放进图 State | 两个图与 Service |

MongoDB 共享客户端负责连接生命周期，业务仓储负责具体数据访问；不要把任务、题库、课程、聊天全部堆进 mongo_history_utils.py。课程/题目/文档三类完整记录可以共用 content_records 集合和类型字段，无需机械照搬 Milvus 的物理分表方式。

`.env.example` 已补齐 MongoDB、MinIO、LLM、Embedding、Reranker、MinerU、可选 MCP 和现有日志配置的占位及用途；无真实凭据。`.env` 未修改，不新增或替换用户密钥。MongoDB/MinerU 新运行参数仍需后续客户端/适配器使用；日志模块保留原有解析，未纳入本轮重构。

## 5. 实施顺序和验收要求

### 5.1 工具依赖顺序

以下是工具之间的依赖关系，不是允许一次完成的交付批次。实际开发必须遵守用户审核顺序：每个流程先交 State/图，两份审核通过后，工具随单个节点按需适配并交审；详见交接文档第 8.1 节。

1. 统一配置读取、共享模型、错误与状态枚举；处理导入联网/下载的副作用，修复异步日志。
2. 实现纯 MD/DOCX 解析、内容构建、ID 与三集合字段校验；这些测试不依赖真实模型和数据库。
3. 补 MongoDB 任务/内容仓储、MinIO 文件能力、Milvus 写入与明确的失败语义，再接九节点导入图。
4. 补 Embedding 查询入口、过滤构造、候选融合、Reranker 与教育提示词，再接十一节点查询图。
5. 改造历史、SSE、请求级缓冲和取消管理，接普通/流式 API，完成并发和失败场景验证。

SSE/历史虽在最后联调，数据合同应在第一步确定；不要等节点各自写完再临时拼字段。每步只完成当前依赖的适配，不因 P2 清理扩大范围。

### 5.2 必须能说明的验收结果

| 改造项 | 至少验证 |
| --- | --- |
| 配置 | 温度 0 不变，缺省值明确；真假布尔值一致；三个集合名独立且名称不回退到旧商品变量 |
| 日志 | 同步/异步成功与失败均在实际执行后记录，能关联请求/任务，调用位置不是日志修补函数 |
| MongoDB 历史 | 最近 N 条按正序使用；同 request_id 重试不重复保存；无历史与查询失败有不同语义 |
| Milvus | 字符串主键原样保留；批量读取失败不伪装成完整成功；三个集合只请求自己的字段；写入和任务清理有明细 |
| Embedding | 文档/查询接口、模型配置优先级、条数、1024 维、稀疏格式、NaN/Inf 与空文本处理明确；稀疏权重不被重复归一化 |
| Reranker | 分数与候选 ID 对应，稳定排序，失败回退有 warning；不能把精炼模板当打分器 |
| 任务 | 只有任务最终成功才发布；基础设施失败不会算部分成功；重启遗留状态可解释 |
| SSE | 同会话两个 request_id 不串流；取消后资源释放；error/done 结束语义明确；缓冲有上限 |
| 限速 | 非法参数明确报错，并发请求不会越过同一窗口限制；等待不阻塞事件循环 |
| 纯解析与脚本 | 导入解析器、Schema 和下载脚本不会连接远程服务或自动下载；真实资料保持只读 |

### 5.3 已验证与尚未验证的边界

初次检查完成当时全部 34 个 Python 文件的语法解析，未通过 import 执行项目模块；本轮追加的配置导入与离线验证见第 6 节。隔离提取纯函数，复现了异步日志提前完成/漏记异常、请求 ID 未识别、调用位置选中自身，以及限速 max_requests=0 的 IndexError；历史倒序问题通过源码确认。

已查看当前安装的 BGEM3EmbeddingFunction：encode_queries 和 encode_documents 目前都调用同一个内部编码函数，因此“缺查询入口”属于接口合同缺口，不能宣称已经证实当前模型的查询向量算错。其 sparse 输出直接来自 lexical_weights，而 normalize_embeddings 的说明针对稠密向量，项目中“稠密和稀疏均自动 L2 归一化”的注释需要纠正。

已确认当前安装的 langchain_core.exceptions 包含 LangChainException，不将该导入列为错误。设备/FP16 实际兼容性、模型推理、Milvus/MinIO/MongoDB 连通和服务端索引兼容性未做集成验证；此前 WinError 10106 仍应由接手会话在当前环境重新核查，不能以静态检查通过代替真实运行通过。

本清单落实后，更新各项状态和对应代码/测试位置，再同步 README、节点开发交接文档。业务范围、三集合设计和节点数量保持原约定，除非用户后续明确调整。

## 6. 本轮落实：配置与 common / utils 的项目适配边界（2026-10-19）

检查结论：`common` / `utils` 仍是参考项目的公共工具底座，**不能整体认定已符合当前教育智库项目**。本轮完成配置适配及必要调用方修改；业务状态、任务/SSE、历史仓储、教育提示词和检索接口仍按原清单推进，未实现业务节点。

### 已完成

- 新增 `common/config/_env.py`，全部 8 类配置统一读取源码定位根目录的 `.env`，进程环境优先；缺失/空白回退，布尔解析一致，数字拒绝非法值/NaN/Inf；错误不回显密钥。`from_env({})` 支持完全隔离构造。
- LLM 默认温度 0.1，合法 0 保留至 SDK；地址错误提示统一为 `OPENAI_BASE_URL`。可选服务凭据延后到 `validate()` 检查，不要求纯配置导入就配置全部服务。
- Embedding / Reranker 默认 CPU、FP16 false；FP16 请求 true 仅 CUDA 生效。显式本地目录优先、相对路径基于项目根目录，目录缺失不回退仓库下载。仓库模式只在 getter 调用后由 SDK 加载（可能下载）；本轮未调用真实 getter。
- 核对本地安装的 SDK 源码，修正 Reranker 构造参数为 `devices`，避免配置的设备被旧 `device` 参数静默忽略。
- MongoDB 新配置集中定义六类业务集合和超时，但不创建客户端/集合/索引；Milvus 维持三个教育集合并传入连接超时；MinIO 客户端创建前增加配置校验。
- `.env.example` 仅含占位；真实 `.env` 修改前后 SHA-256 一致。未运行 Git 初始化/分支命令。

### 修改文件清单

- 新增：`common/config/_env.py`、`common/config/mongo_config.py`、`test/test_config.py`。
- 配置类：`common/config/lm_config.py`、`embedding_config.py`、`reranker_config.py`、`milvus_config.py`、`minio_config.py`、`mineru_config.py`、`bailian_mcp_config.py`。
- 路径与调用方：`utils/path_util.py`、`utils/lm/lm_utils.py`、`utils/lm/embedding_utils.py`、`utils/lm/reranker_utils.py`、`utils/clients/minio_utils.py`、`utils/clients/milvus_utils.py`。
- 模板与文档：`.env.example`、`README.md`、`节点开发交接.md`、`common与utils检查清单.md`。

### 验证结果

命令：`.venv/Scripts/python.exe -m unittest test.test_config -v`，**18 个测试全部通过**，覆盖多组边界子用例。

验证包含：无 `.env` / 无凭据隔离子进程导入、进程环境优先、空串、温度 0/缺省/越界/NaN/Inf、各布尔写法、设备及实际 FP16、本地路径优先/缺失、BGE_M3 到 SDK 的传参、Reranker devices 参数、三集合独立/重名/旧变量不回退、服务必填项、MinIO endpoint、MongoDB 副本集/SRV URI 的离线格式检查、超时和轮询正数约束。SDK 使用替身，网络连接和 DNS 调用设为立即失败；测试未连接真实服务或下载模型。

`common` / `utils` / `test` 共 **38 个 Python 文件 AST 语法检查通过**。本机 `unittest.mock` 经 asyncio 导入 `_overlapped` 仍触发 **WinError 10106**；测试改用同步轻量替身后完成离线验证，系统网络环境问题未修复。配置导入通过不能代替实际 SDK/服务验证。

### 剩余问题

- F01-F06、F09-F10 保持待办：异步日志、历史倒序/导入联网、数据库失败语义、任务/SSE 协议与队列、限速、MinIO getter 自动建桶/匿名策略。本轮未执行这些有副作用的工具。
- Embedding 查询接口/向量验证、Reranker 打分排序和 LLM 流式/超时/私有参数能力适配未完成；`enable_thinking` 仍固定发送，需后续按服务能力处理。
- 模型仓库 revision 尚未固定；设备实际可用性、FP16 推理兼容性、模型目录内容、服务连通、已有集合 Schema/索引兼容性均未验证。
- 新 MongoDB 配置尚未接入旧 `mongo_history_utils.py`，后者仍自行加载环境、导入联网并创建索引；应在独立历史/仓储改造中处理，不能把新增配置当成历史工具已修复。MongoDB/MinerU 新超时和轮询配置待对应适配器接入。
- 教育回答/查询分析/元数据提取提示词、共享对象和业务仓储仍缺失；下载脚本仍导入即下载。它们在当前项目中需要后续适配，未因本轮配置修复而视为可直接接入。

## 7. 节点开发必须落实的复用、日志和进度约定

本节将已确认的流程分析转为开发约束，完整规范见 [节点开发交接.md](节点开发交接.md) 第 4.3、4.4、5.4、6.4 节。

- 新增功能或公共方法前先检索 common/utils，阅读现有签名、错误语义和副作用；已有能力直接复用，不足时优先修复/扩展原模块。确需新增公共方法，说明检索范围和现有能力缺口。
- 节点保留业务判断、调用组织和增量 State；配置、存储、模型、日志、限流、事件经过公共工具。不能为了一个节点重新创建客户端、模型单例或配置读取流程，也不把所有局部简单业务函数强行抽成工具类。
- 每次交付提供工具“复用/扩展/新增”清单与理由，不能只说“使用了工具”而不说明接入范围。
- 先修复 node_log/step_log 的 async 支持、调用位置和请求标识，再用于异步节点。节点级记录生命周期，步骤级记录上传、解析、批次编码、读写、召回和模型耗时；日志只记必要统计和摘要。
- 统一节点执行包装负责阶段记录、进度、异常转 NodeError 和失败路由；日志装饰器不替代 LangGraph 错误路由，内存/SSE 不替代 MongoDB 持久任务状态。
- 综合查询的各集合内部混合召回可使用 RRF；跨集合 RRF 统一在 rerank 阶段执行一次。search_knowledge 收集候选和来源排名，不再提前重复融合。
- 源码已有函数、离线测试通过、真实服务集成通过分别标记。特别保留本轮已完成的配置、温度 0、模型来源及设备传参修复，不因旧 F07/F08 描述重新实现或回退。

2026-10-19 文档同步复核：重新执行 18 个 test.test_config 离线测试，全部通过；api/processor/common/utils/test 共 44 个 Python 文件 AST 解析通过。两个图仍为导入 0/9、查询 0/11，API 入口不存在。此次仅补充和统一开发文档，未修改 Python 源码、配置值或执行真实建表/导入；实际服务集成与此前 WinError 10106 问题仍未作为本次验证项目。

下一项任务（按用户最新审核顺序）：先交导入 state.py/main_graph.py 两份一起审核，通过后每次只开发一个节点并再次交审。共享对象、异步日志/统一执行包装、真实 Markdown 规则解析及三集合投影随当前节点按需补齐。代码补必要注释；单节点自测放本文件 __main__ 入口，流程完成后在 test/ 写流程测试。具体以交接文档第 8.1 节为准；每轮同步文档并停下等用户审核。

## 8. 参考节点源码与提示词写法补充

已静态阅读 shopkeeper-brain 的 14 个业务节点、两个图与 State、七份提示词及公共调用入口，整理为 [节点编写规范与提示词.md](节点编写规范与提示词.md)。包含参考流程、源码定位、node/step 分工、工具复用、异步日志适配、显式依赖绑定、模型模板草稿和开发 AI 任务模板。

这一补充仅更新文档，没有修复上述工具待办或新增业务节点。node_log/step_log 的异步支持、任务/SSE 改造、教育模型提示词落地等仍待实现；文中的 bind_node/encode_chunks 是拟议接口，当前不存在。既有配置修复、三集合设计以及导入 0/9、查询 0/11 的状态不变。
