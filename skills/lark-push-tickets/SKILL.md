---
name: lark-push-tickets
license: MIT
description: "将已初始化本地项目的 Markdown 票据、执行标准、grill 澄清产物、CONTEXT 和 ADR 上传或更新为飞书知识库子文档，自动建立依赖链接并维护母文档索引。用于明确的项目票据上传请求；不上传附件或替换 issue tracker。"
metadata:
  requires:
    bins: ["python", "lark-cli"]
---

# 上传项目票据

首次使用先读 [同步协议](references/protocol.md)，从本 Skill 目录定位 `scripts/ticket_sync.py`。配套安装 `lark-pull-tickets`。

## 执行

1. 从用户指令确定本地项目及文件/功能目录范围。读取 `docs/agents/feishu-sync.md`；项目须已有 setup 的 domain 和 issue-tracker 约定。用户已给范围就直接沿用；未给范围时只确认一次。`.` 表示全部允许的项目 Markdown。
2. 未绑定时请用户提供项目母文档的 `/wiki/` 链接，执行 `bind --wiki-url <链接>`。绑定只写本地约定；不会创建应用、改权限或改 issue tracker。
3. 执行 `push --select <相对文件或目录> ...`。脚本自动解析结构依赖和明确声明、复用 token、检查版本、生成链接、比较文档块并维护索引。依赖先上传，成功后才处理对应票据。
4. 有冲突时读取返回的 `.diff`，让 HITL 决定保留远端或使用本地。仅用户选择使用本地时，在选择文件中写 `{ "相对路径": "local" }`，通过 `--decisions <JSON文件>` 重跑相同范围；保留远端则跳过该票据并按需调用导入技能。人工合并后重新生成差异与计划。
5. 以脚本结果和进度记录报告成功、跳过、阻塞以及索引是否验证成功；退出码 2 表示部分结果需要处理，已成功文件保留。创建结果不明时按错误定位已有节点，禁止盲目重建。

参数顺序：`python <本Skill>/scripts/ticket_sync.py --root <项目绝对路径> <命令> ...`。使用参数数组传递路径及选择项。运行可能较长时按 [后台执行](references/protocol.md#后台执行) 启动；只通过程序的进度/结果文件观察。

认证、身份或 scope 错误才读取已安装 `lark-shared` 并按需授权；资源 ACL 错误需要目标访问权，不试换身份。脚本固定以 user 身份执行。运行失败后的文档或部分块保留现场，核对后再重试。

## 完成条件

选定票据与基础依赖已逐篇验证，索引验证通过，未解决项逐条报告。只报告实际成功；上传不意味着票据任务已执行，不修改 Status/Blocked by。
