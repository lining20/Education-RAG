# 澄石项目手册

本资料属于虚构的 RAG 测试语料，不代表真实项目。版本为 rag-test-v1。

## 导入预检查

澄石项目通过下面的命令进行导入预检查。--dry-run 只输出预检查清单，不提交写入任务。

```bash
python ingest.py --dry-run
```

## 批处理配置

澄石项目每批最多处理 64 条记录。澄石项目使用的命名空间为 cs_demo_v2。预检查成功不代表正式导入已经完成。

本手册不提供数据库密码，也不允许从命名空间推断数据库凭据。

## 正式导入流程

澄石项目先执行预检查，确认文件格式、资料归属和资源路径，再由操作人员明确执行正式导入。预检查清单包含计划处理的文件和警告，不会凭空生成发布状态。看到预检查通过后，仍需要等待正式导入任务完成。

正式导入首先归档原始文件，再解析正文和图片，之后构建片段、生成向量并写入存储。只有所有必要产物核验成功，才发布该版本的清单。失败任务需要保留可解释的错误信息，不应把半成品当作成功资料。

每批最多64条记录的限制针对写入批次，不是每篇文档只能有64个字或64个段落。较长文档可以生成多个片段，批次应在容量范围内分次提交。命名空间cs_demo_v2用于识别本项目的数据范围，不包含任何凭据信息。

## 配置与清单

澄石项目的正式发布清单文件名为publish_manifest.json。清单必须在向量和来源核验完成后生成，不允许先创建成功清单再补写片段。读取清单失败时，应明确报告依赖不可用，避免把未确认的数据作为证据。

清单记录资料版本、文件指纹和发布的记录编号。相同文件名重传后，旧版本的来源需要重新验证。历史对话可以继续显示先前的文字，但来源下载不能静默转成新版本，从而改变原回答的证据含义。

## 长代码保护案例

下面的脚本是供阅读和切分验证的静态示例。测试工具不运行它，不根据示例创建数据库连接。代码块应整体保留，即使长度超过普通正文片段的目标长度，也不能删除尾部的常量或改变命令参数。

```python
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

@dataclass(frozen=True)
class ImportPlan:
    source: Path
    namespace: str = "cs_demo_v2"
    batch_size: int = 64
    dry_run: bool = True

def source_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(65536)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()

def validate_plan(plan: ImportPlan) -> dict:
    if not plan.source.is_file():
        raise ValueError("source file is missing")
    if plan.batch_size < 1 or plan.batch_size > 64:
        raise ValueError("batch size must be between 1 and 64")
    if plan.namespace != "cs_demo_v2":
        raise ValueError("unexpected namespace")
    return {
        "source_file": plan.source.name,
        "source_sha256": source_digest(plan.source),
        "namespace": plan.namespace,
        "batch_size": plan.batch_size,
        "dry_run": plan.dry_run,
    }

def split_batches(records: list, batch_size: int = 64):
    if batch_size < 1 or batch_size > 64:
        raise ValueError("invalid batch size")
    for start in range(0, len(records), batch_size):
        yield records[start:start + batch_size]

def describe_plan(plan: ImportPlan) -> str:
    result = validate_plan(plan)
    fields = [
        f"source={result['source_file']}",
        f"namespace={result['namespace']}",
        f"batch_size={result['batch_size']}",
        f"dry_run={result['dry_run']}",
    ]
    return " | ".join(fields)

def preview_only(plan: ImportPlan) -> dict:
    result = validate_plan(plan)
    result["status"] = "previewed"
    result["published"] = False
    return result

CODE_END_MARKER = "CS-CODE-END-73"
```

代码块最后的校验标记为CS-CODE-END-73。审核时应同时看到函数定义与末尾标记。正文中的解释不能代替检查原始代码是否保留完整，脚本中的井号或字符串也不能被解释为新的Markdown章节。

## 异常与回滚

如果正式导入在发布前失败，先检查当前任务是否已经写出片段，再根据任务边界清理未发布产物。清理只针对本次任务，不能扫描并删除其他课程或其他项目的资料。原始文件可保留用于排查，但不能被标为成功版本。

如果收到向量维度不一致的错误，应检查导入和查询所用的Embedding模型是否一致。改变模型后继续使用旧向量不能得到可靠的比较结果。修复时应明确需要重建哪些资料，不能只修改错误提示文字。

如果返回的来源已经过期，应要求重新检索获取当前版本。系统不应拿同名新文件替换旧回答的来源，也不应在没有确认清单的情况下下载可能已经失效的对象。来源追溯是回答核验的一部分。
