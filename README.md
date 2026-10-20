# 教育智库

基于 FastAPI、LangGraph 和 Milvus 的教育资料导入与 RAG 问答系统，支持课程介绍、教学讲义、项目资料与题库。

## 启动

需要 Python 3.12/3.13、uv，以及可用的 Milvus、MinIO、MongoDB 服务。

```powershell
uv sync
Copy-Item .env.example .env
```

`.env.example` 仅是脱敏模板，程序实际加载 `.env`，两者不会自动同步。已有 `.env` 时保留原配置，不要用模板覆盖。填写服务地址、凭据和模型配置后启动：

```powershell
uv run uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 1
```

- 检索问答：`http://127.0.0.1:8000/`，`/query` 为兼容入口。
- 资料工作台：`http://127.0.0.1:8000/import`，需要上传管理员登录。
- 接口文档：`http://127.0.0.1:8000/docs`。
- 局域网访问：将 `127.0.0.1` 换成本机局域网 IP，并允许 TCP 8000 入站。

首次使用在项目根目录创建上传账号，密码通过交互输入：

```powershell
uv run python -m scripts.create_upload_user
```

保持 **一个 worker**：导入进度、任务调度和同名文件锁保存在单进程内存中。

## 配置

配置项和默认值见 `.env.example`。进程环境变量优先于根目录 `.env`，配置与项目路径不依赖启动目录。

| 配置 | 用途 |
| --- | --- |
| `MILVUS_URL`、四个 `*_COLLECTION` | 文档主体索引、课程、文档片段、题目向量存储 |
| `MINIO_*` | 原文件、图片、完整记录 JSON 和发布清单 |
| `MONGO_*` | 会话历史、上传账号及登录会话 |
| `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`LLM_DEFAULT_MODEL` | OpenAI 兼容文本模型 |
| `BGE_M3_PATH` / `BGE_M3`、`BGE_DEVICE` | BGE-M3 稠密与稀疏向量，默认 CPU |
| `BGE_RERANKER_PATH` / `BGE_RERANKER_LARGE` | 本地重排模型及设备配置 |
| `MINERU_*` | PDF 云端解析；MD、DOCX 无需配置 |
| `IMAGE_UNDERSTANDING_ENABLED`、`VL_MODEL` | 可选图片理解，默认关闭，关闭时仍提取和归档原图 |
| `MCP_DASHSCOPE_BASE_URL`、`MCP_DASHSCOPE_API_KEY` | 可选百炼 MCP 联网搜索；独立密钥为空时复用 `OPENAI_API_KEY` |

本地模型路径优先于仓库名。未配置本地路径时，模型 SDK 可能下载模型；GPU 使用需配置相应设备及运行环境。导入和查询须使用相同的 Embedding 模型及向量维度。

导入时自动创建缺失的 MinIO 桶和 Milvus 四集合；已有集合仅校验，不覆盖。可提前执行：

```powershell
uv run python -m scripts.init_collections
```

## 资料导入

支持 MD、DOCX、PDF、ZIP，多文件上传，每个上传文件上限 200 MiB。MD 引用本地图片时，使用 ZIP 保留相对路径。旧 `.doc` 需先转换为 DOCX。

| 内容类型 | 适用资料 |
| --- | --- |
| 课程介绍 | 课程系列、课程简介和课程属性记录 |
| 文档片段 | Python、机器学习等教学讲义和知识文档 |
| 项目资料 | 项目实战章节与操作步骤 |
| 题目 | 带题号、题干、答案等字段的题库 |

同一批次使用同一种内容类型，混合类型需分批上传。课程和题目依赖原文中的明确结构，不让模型猜测课程关系或补写答案。

导入流程：文件校验与归档 → 按格式解析 → 图片归档与可选理解 → 内容构建与切分 → 元数据整理 → BGE-M3 向量化 → Milvus 入库 → 发布清单。

DOCX 本地提取正文、表格、代码和图片；PDF 通过 MinerU 解析，超限时物理分段。文档按标题组织和切分，代码块整体保留；超出存储或模型输入上限时明确失败，不截断代码入库。

**文件名（包含扩展名）是全库替换边界**：同名重传会先清理旧向量和旧对象，再导入新内容。新导入失败不会自动恢复旧版本，不同资料同名时请先改名。

导入任务在后台运行，关闭页面不会取消任务。服务关闭时请求停止导入，等待当前同步操作退出和清理完成；服务重启后内存进度丢失，已发布资料保留。

## 检索问答

正常检索执行本地知识库稠密/稀疏混合召回；按开关增加 HyDE 和联网搜索，随后进行 RRF 融合、重排、证据回答与历史保存。需要澄清的问题先追问。

- **流式输出**：默认开启；关闭后完整显示最终回答。
- **启用 HyDE**：用假设答案补充本地召回，假设答案本身不作为证据。
- **联网搜索**：与本地检索同时执行；网络失败时提示警告并使用可用的本地结果。

支持按课程、项目、章节、来源文件等条件筛选；题库、题号、题型仅在题目范围生效。回答支持 Markdown、表格、代码和引用定位，图片仅展示当前证据中已核验的原图。

MongoDB 保存会话与消息；MinIO 保存完整原始资料；Milvus 使用四个集合：`education_document_index`、`education_courses`、`education_document_chunks`、`education_questions`。检索核验发布清单，未发布内容不作为回答证据。

## 项目结构

```text
api/                         HTTP、SSE、上传鉴权与任务调度
common/config/               环境变量与服务配置，model_config.py 管理本地检索模型
common/prompt/               图片理解、问题改写、HyDE 与回答提示词
common/schemas/              Milvus 集合结构
processor/import_processor/  导入 State、LangGraph 与节点
processor/query_processor/   查询 State、LangGraph 与节点
utils/                       解析、内容构建、存储客户端、模型及检索工具
page/                        问答、导入、登录页面及静态资源
scripts/                     创建账号、初始化集合、查看对象、按文件名清理向量
assets/architecture/         流程与索引结构图
test/                       预留后续 RAG 测试（当前为空）
```

`/api/health` 仅表示 HTTP 进程可用，不代表依赖服务或模型可用。运行日志位于 `logs/`，上传临时文件位于 `.uploads/`；它们和 `.env`、`.venv` 均不提交 Git。

- 联网为本地知识库补充：本地混合检索始终执行，HyDE 仍召回本地。重排保持 0.15 相关性阈值，去重后本地与网页各自按真实分数排序，Top-K 优先保留 ceil(0.6*K) 条合格本地，余量给网页，不足再以其他合格资料补齐；不强塞低相关本地。回答优先依据本地，网页按需标注联网补充。没有可用本地而只有网页时显示 LOCAL_EVIDENCE_NOT_FOUND 警告。日志记录各路数量、合格数量与最终来源构成。未运行测试或真实检索验证。

### 文档切分边界

参照 shopkeeper-brain/node_document_split.py：每个完整标题小节独立组织，超过 600 字再细分，重叠 50 字；当前累计子块不超过 400 字时，仅可与同一原始小节的下一子块合并，合并上限 1000 字。参考项目的 parent_title 是细分前的完整小节标题，不是兄弟小节共有的上级标题；不同标题、父子标题与重复出现的同名标题均不跨界合并。代码块整体保留，可超过普通长度目标；原图关联保持。旧数据不会自动重切，需要重新导入对应文件。本轮仅代码和静态语法检查，未运行测试或操作数据库。
