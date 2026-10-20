# 本地 RAG 评测

本目录包含合成测试语料、标准答案、导入脚本、协程并发评测器和评分器。最新实测结论见 [并发评测总结](EVALUATION_SUMMARY.md)。测试代码与数据均放在 `test/`，运行产物写入 `test/results/`。

## 目录与语料

```text
test/
├── data/
│   ├── documents/          # 需要导入的 4 篇 MD、1 份 Word、1 份 PDF
│   ├── assets/             # Word/PDF 内嵌图片的原稿
│   ├── questions.jsonl     # 40 道问题、标准答案及证据
│   ├── evidence_sources.json # PDF 文本、图片标注及文件散列
│   └── versions/v1/        # 原始 4 篇短 MD 与 20 题，供历史结果评分
├── results/                # 本地响应、耗时、评分和人工复核表，Git 忽略
├── settings.py             # 测试服务与环境隔离
├── import_data.py          # 调用项目导入接口
├── async_client.py         # 异步 HTTP 与 SSE 客户端
├── http_client.py          # 导入使用的同步 HTTP 客户端
├── run_eval.py             # 协程并发评测
├── score.py                # 自动统计与人工评分汇总
├── build_fixtures.py       # 离线语料生成器
├── test_offline.py         # 数据与评分离线回归
├── test_async_eval.py      # 并发、SSE 时序、超时与计时回归
└── EVALUATION_SUMMARY.md    # 可随 Git 提交的实测总结
```

全部资料为虚构的教学评测内容，覆盖同主体多文档、长章节、长代码、表格、多轮问答、跨格式证据与无答案拒答。

问题集共 40 题，35 题有答案、5 题无答案。q33、q40 的答案只在图片中；默认排除，因此普通评测为 38 题。图片流程启用后用 `--include-vision` 测全部 40 题。

**只上传 `data/documents/` 中的文件。** 图片已经内嵌，无需另传 PNG。问题集、标准答案和证据侧记仅用于评分，上传它们会泄漏答案。

## 离线检查

在项目根目录运行，默认不联网、不启动服务、不写数据库：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m test.settings
.\.venv\Scripts\python.exe -X utf8 -B -m test.import_data
.\.venv\Scripts\python.exe -X utf8 -B -m test.run_eval
.\.venv\Scripts\python.exe -X utf8 -B -m unittest test.test_offline test.test_async_eval -v
```

离线回归共 40 项，覆盖语料证据、缺失结果分母、格式筛选、六项核心指标、并发上限、取消与超时、乱序评分、多轮 SSE 完成顺序和计时。Word/PDF 各 3 页已经渲染检查；临时渲染文件不纳入提交。

已有语料可直接使用，不必重新生成。生成器使用 `python-docx`、`reportlab`、`Pillow`、`pypdf` 和 Windows 中文字体；重新生成可能改变二进制文件散列与数据集摘要，需重新导入并评测。

## 启动、导入与并发评测

### 1. 启动测试服务

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m test.settings --serve
```

默认地址为 `http://127.0.0.1:8201`。业务连接和模型配置沿用项目环境，测试存储使用 `rag_test_v1_*` 集合、`rag-test-v1` 桶和 `rag_test_v1` Mongo 库。脚本会核验测试服务身份与数据集摘要。修改业务代码或语料后，先重启测试服务。

如需创建测试上传账号，在另一个终端运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m test.settings --create-user
```

已有测试账号可以继续使用。PDF 导入沿用项目 MinerU 流程，需要有效的 `MINERU_*` 配置。

### 2. 导入文档

打开 `http://127.0.0.1:8201/import`，登录后上传 6 份文档，内容类型选择“文档片段”；或执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m test.import_data --execute
```

等待全部导入成功后开始评测。同名文件会替换测试库中的旧版，正常重测不需要清空整库。导入等待超时不会取消服务端任务。

如果本轮仅导入 MD，后续评测加 `--formats md`；只导入 MD 与 Word 时用 `--formats md,docx`，以匹配已准备的资料范围。

### 3. 并发执行

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m test.run_eval --execute --mode both --concurrency 8
```

默认 Top-K=5，关闭联网与 HyDE。`--mode both` 分别运行搜索轮和问答轮。评测器使用 `asyncio` 与 `httpx.AsyncClient` 原生协程，默认并发 4，支持 `--concurrency 1` 至 `256`；搜索与问答共享并发上限。服务端自身容量决定实际同时执行的请求数，提高客户端并发可能增加服务端排队。

可用 `--limit 3` 先试运行，或 `--repeat 3` 观察波动。每个独立案例使用独立会话；多轮案例使用真实前置回答，不注入标准答案。结果按完成顺序写入，评分按案例编号对齐。

需要评测图片时，改用以下方式启动服务，**重新导入含图 Word/PDF**，然后加入图片题：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m test.settings --serve --images
# 重新导入成功后，在另一个终端执行
.\.venv\Scripts\python.exe -X utf8 -B -m test.run_eval --execute --mode both --concurrency 8 --include-vision
```

图片识别需要有效的视觉模型配置和业务流程支持，可能调用真实视觉服务。仅启用开关或重启服务不会补做旧文档的图片识别。

### 4. 评分与复核

运行结束后使用终端打印的实际目录：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m test.score --input "test/results/实际运行目录/responses.jsonl"
```

| 文件 | 用途 |
| --- | --- |
| `responses.jsonl` | 原始 SSE 事件、回答和逐题计时，评分不改写 |
| `run_summary.json` | 并发数、整轮耗时、完成数和失败数 |
| `report.md` | 六项核心指标及分类结果 |
| `metrics.json` | 分母、缺失案例、代理指标和逐题明细 |
| `review.csv` | 标准答案、实际回答、证据与人工评分 |

先检查 `report.md`；语义指标显示“待复核”时填写 `review.csv`，再执行相同评分命令。已有复核表不会被覆盖。原始 v1 结果重评分需要加 `--dataset v1`。

## 指标定义与人工评分

| 指标 | 定义 |
| --- | --- |
| 章节 Recall@5 | 每题按“来源文件＋章节”去重，前 5 条证据命中的必需章节比例，再对有答案题取平均；失败或缺失为 0 |
| 回答正确完整率 | 全部有答案案例中，`correctness=1` 的比例 |
| 回答依据一致率 | 有答案且确实给出实质回答的案例中，`faithfulness=1` 的比例 |
| 严格引用支持率 | 同一实质回答范围内，`citation_accuracy=1` 的比例，要求关键结论均有对应且支持结论的引用 |
| 无答案题正确拒答 | 全部无答案案例中，`refusal_correct=1` 的比例；接口报错不能算拒答成功 |
| 问答耗时 P50/P95 | 成功的目标问答请求从发送至 SSE `done` 的耗时分位数，同时查看成功率和样本数 |

章节召回表示找到相关章节，不保证找齐该章节的所有答案。报告优先展示问答轮章节召回；仅有搜索轮时会注明。有效 K 小于 5 时不能与完整 Top-5 实验直接比较。

人工复核字段：

- `substantive_answer`：有答案题给出事实性回答填 1，只拒答或要求澄清填 0；无答案题留空。
- `correctness`：1 为正确完整，0.5 为部分正确，0 为错误、主要遗漏或误拒答。
- `faithfulness`：全部事实由本轮实际检索证据支持填 1，部分支持填 0.5，不支持填 0；没有实质回答时留空。
- `citation_accuracy`：关键结论均由对应引用支持填 1，部分支持填 0.5，缺引用或引用不支持填 0；没有实质回答时留空。
- `refusal_correct`：仅无答案题填写，明确缺资料且无编造填 1，模糊但无编造填 0.5，编造或系统错误填 0。
- `reviewer`、`notes`：记录复核人和问题依据。

严格通过只计 1，不把 0.5 或空白计为满分。失败和缺失案例仍在正确完整率或正确拒答的分母中。关键词、精确摘录和接口状态仅是代理指标，不能替代语义复核；图片题还需对照原图。

## 多轮时序与计时

前一问完整 SSE `done` 后才发送追问，并为追问重新计时：

- `client_queue_ms`：等待客户端并发名额。
- `history_elapsed_ms`：前置对话耗时。
- `response.elapsed_ms`：本题发送至本题 `done`，用于核心 P50/P95；含网络、HTTP 连接池、服务端排队与模型执行。
- `response.first_delta_ms`：本题发送至首个非空正文片段，进度事件不算首字。
- `case_elapsed_ms`：获得并发名额后，整条案例耗时，包含前置对话。

客户端排队和前置对话不计入本题响应时间。`--timeout` 对每次请求从发送到完成生效。中断时关闭活跃请求，保留已写入结果，未完成案例仍进入评分分母。

## Git 提交范围与公平对比

提交代码、合成文档、问题集、图片原稿、证据侧记、v1 快照及 Markdown 总结。`results/` 中的运行日志、上传副本、临时文件和逐题复核表默认忽略，只保留 `.gitkeep`；本地原始记录可继续用于复核。

语料扩充、模型变更、图片能力或并发数变化时，应分别记录实验配置；对照实验尽量固定这些条件，避免把数据变化归因于代码修复。数据集摘要由 `test.settings` 输出，评分时核验一致性。
