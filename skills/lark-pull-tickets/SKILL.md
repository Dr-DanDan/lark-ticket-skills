---
name: lark-pull-tickets
license: MIT
description: "从飞书项目母文档管理的在线子文档导入 Markdown 票据、执行标准、CONTEXT 和 ADR 到 setup 约定目录。HITL 选择主票据，规则自动补齐基础依赖；已就绪且未变化的正文不重读。不导入附件或覆盖本地工程配置。"
metadata:
  requires:
    bins: ["python", "lark-cli"]
---

# 导入项目票据

配套安装 `lark-push-tickets`；本 Skill 复用其 [同步协议](../lark-push-tickets/references/protocol.md) 和 `scripts/ticket_sync.py`，先读协议再执行。

## 执行

1. 确定目标本地项目，读取 `docs/agents/feishu-sync.md`。项目须已有 setup 的 domain 和 issue-tracker 约定；新项目先使用 setup 初始化。未绑定时提供母文档 Wiki 链接并执行 `bind`。
2. 用户给出明确的文件路径/子文档链接时视为已完成 HITL 选择。否则执行 `catalog`，按目录展示标题、已知路径和修改时间，让用户选主票据；此阶段不逐篇读取票据正文。候选目录或疑似改动不构成拉取授权。
3. 执行 `plan-pull --select <路径、node_token或Wiki链接> ...`。脚本为选定主票据解析必要依赖、核对精确 revision，并仅暂存需要读取的正文。检查返回的主票据、自动补入依赖、diff 和错误；不扩大到未选票据。
4. 无冲突直接执行 `apply-pull --plan <返回的计划路径>`，完成已授权的导入。冲突由 HITL 查看 `.diff` 并选择保留本地或使用远端；写选择 JSON，例如 `{ "docs/adr/0001-auth.md": "remote" }`，传入 `--decisions`。保留本地用 `local`，其同步基线不推进。人工合并后重新生成计划。
5. 必要依赖全部经验证就绪后脚本才写对应主票据。某组失败时原主票据保持原样，其它独立组继续；报告具体阻塞、成功和复用项。文件依赖就绪不表示其任务已经完成。

参数顺序：`python <配套push Skill>/scripts/ticket_sync.py --root <项目绝对路径> <命令> ...`。计划路径由脚本返回；不自行伪造或改写计划。可能较长的任务按协议启动后台程序，读取程序日志快照，不保持 Codex 轮次轮询等待。

认证和 scope 错误按已安装 `lark-shared` 处理。普通链接不是依赖；跨项目、路径变更、未知标识或转换失败按错误报告，不让 Agent 猜路径、猜依赖或擅自润色正文。

## 完成条件

每篇主票据均明确为已导入、已复用、HITL 保留本地或被阻塞；依赖验证结果完整。元信息、生成的导航和附件不写入本地正文；本地工程配置仍由 setup 维护。
