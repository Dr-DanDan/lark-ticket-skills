# 项目票据同步协议

## 范围和绑定

一个本地项目绑定一个 Feishu/Lark 知识库母文档。母文档管理独立子文档及链接，知识库目录对应本地目录。先完成 setup；首次调用 `bind` 可补充缺失的 `docs/agents/feishu-sync.md` 和缓存忽略规则，不重跑 Git 设置。

默认文件范围是 `.scratch/` 下的项目 Markdown、根 CONTEXT/CONTEXT-MAP/TODO、docs/adr。`.scratch/.feishu-sync/` 是内部状态，工程配置和附件独立维护。多领域项目在本地约定的 `context_paths` 中列出 setup 已确定的 `src/<context>/CONTEXT.md`，同时允许其 docs/adr。

只新增和更新。标题用于阅读，token 保持身份，头部 path 决定本地位置。路径改名、远端缺失不自动搬移/删除/重建。边界或权限不明确时保留现场并报告。

## 标识与依赖

子文档从 `## 同步元信息` 开始，以 JSON 代码块记录：

```json
{
  "schema": "feishu-ticket/v1",
  "path": ".scratch/login/issues/01-login.md",
  "requires": [".scratch/login/spec.md", "CONTEXT.md"]
}
```

后面是脚本生成的 `## 基础依赖导航` 和 `## 票据正文`。只有正文导入本地；原文的状态、执行顺序和验收项保留。grill 产物按 spec、CONTEXT、ADR 等原位置归档，不另造访谈目录。正文只接受可可靠转换的 Markdown；附件、嵌入资源和未知 XML 扩展阻断该文件。YAML frontmatter 用受标记的代码块传输并恢复。

实施票据自动依赖同功能 spec。已有并登记的根 CONTEXT/CONTEXT-MAP 是基础资料。`Blocked by` 支持编号列表和带标题的编号，例如 `01, 02` 或 `01 — First`，在同功能 issues 内唯一匹配；模糊引用需要补正。其它必需资料在本地 `## 基础依赖` 中声明相对 Markdown 链接，或在远端 requires 中声明。普通正文链接只作参考。上传自动生成导航，不需要 Agent 推断关系。

HITL 选择主票据即包含规则确定的必要依赖。闭包去重；缺失、循环、无权限、跨项目或未解决冲突阻塞该组。依赖成功后才落地主票据，其它独立组继续。

## 版本与冲突

轻量目录和索引用于选择。选定集合使用精确 `revision_id`，时间戳只作提示。版本未变且本地 hash 匹配已同步基线时不重读正文；本地缺失的依赖仍需修复。首次导入只建立所选集合的基线。

本地和远端 hash 分别比较各自上次基线，避免格式转换产生假双端修改。本地未同步编辑、首次配对差异和远端未拉取的修改需要 HITL 决定。默认不自动合并；选择 JSON 只记录用户实际决定。失败/冲突不推进基线，部分成功保留。

上传使用官方 Markdown→block 转换和 native block diff，保留相同块的 ID，先插入替换内容，再移除已确认的旧块。每步带基准 revision 和操作标识；中途失败保留现场并核对，不用全文 overwrite。修改块上的评论锚点及并发语义需要实测，不能承诺跨文档原子更新。

单个改动组超过 Feishu 一次嵌套插入的 1000-block 上限时安全停止并报告；不拆开复杂块后冒险写入。

## 后台执行

`scripts/launch.ps1` 接受 `-WorkerArgs` 字符串数组，启动隐藏且独立的 Python 程序。例如从已安装 Skill 目录解析脚本，再传入：

```powershell
$workerArgs = @('--root', $projectRoot, 'plan-pull', '--select', '.scratch/login/issues/01-login.md')
& $ticketLauncher -WorkerArgs $workerArgs
```

启动返回 PID 和 `.scratch/.feishu-sync/last-run.json`。程序记录时间戳、阶段、失败和结果文件；首次验证进度已写入后结束轮次。后续只读取该文件及其指向的 progress/result 快照，不附着或操作运行中进程，不用 Codex 轮次循环跟随日志。

命令：`bind --wiki-url URL`、`catalog`、`push --select ... [--decisions JSON]`、`plan-pull --select ...`、`apply-pull --plan FILE [--decisions JSON]`。共享脚本入口位于 push Skill。退出码 0 为完成，1 为运行/配置失败，2 为部分结果需处理。以持久化结果判断每个文件，不能把非零退出误作全批回滚。

## API 边界

已有 lark-cli 负责凭据和 user 身份。Wiki 查询/建节点复用 +node-*；未提供 typed docx 命令的版本、块转换和嵌套块 API 通过 `lark-cli api` 调用，不另建 MCP 或直接管理令牌。

官方依据：[基本信息](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/get)、[Markdown 转换](https://open.feishu.cn/document/ukTMukTMukTM/uUDN04SN0QjL1QDN/document-docx/docx-v1/document/convert)、[创建嵌套块](https://open.feishu.cn/document/ukTMukTMukTM/uUDN04SN0QjL1QDN/document-docx/docx-v1/document-block-descendant/create)。在线 docx 没有在本次确认到服务端行补丁；正文 diff 在本地生成，导回字节/排版不保证与原文件完全一致。
