# Lark Ticket Skills

[English](README.md) | [简体中文](README.zh-CN.md)

两个配套的 Codex 技能，使用官方 [Lark CLI](https://github.com/larksuite/cli)，在本地工程项目与可编辑的飞书知识库文档之间上传和导入 Markdown 票据。

| 技能 | 用途 |
| --- | --- |
| [lark-push-tickets](skills/lark-push-tickets/SKILL.md) | 上传或更新票据，建立依赖链接，维护项目母文档索引。 |
| [lark-pull-tickets](skills/lark-pull-tickets/SKILL.md) | 将 HITL 选定的主票据及必要依赖导入已有本地目录。 |

HITL 指人工参与决策：用户选择主票据和处理冲突，共享 Python 脚本按固定规则完成路径校验、依赖补齐、版本检查、暂存和落地。

## 环境要求

- Codex 及其内置 skill-installer。
- Python 3.9 或以上，规则脚本只使用标准库。
- PATH 中可用的官方 `lark-cli`，以及目标知识库与文档的访问权限；脚本以 user 身份执行。
- 目标项目已使用 [setup-drdandan-skills](https://github.com/Dr-DanDan/setup-drdandan-skills) 初始化，包含 `docs/agents/domain.md` 与 `docs/agents/issue-tracker.md`。
- 后台启动器面向 Windows PowerShell 7。本地已使用 Python 3.9、Lark CLI 1.0.97 验证；尚未实测真实飞书往返。

## 配套安装

本仓库为私有仓库，安装帐号须有访问权，并已配置可用的 Git 认证。下面的命令通过 Git 凭据使用 Codex 内置安装器：

```powershell
$codexRootPath = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$skillInstallerPath = Join-Path $codexRootPath 'skills\.system\skill-installer\scripts\install-skill-from-github.py'
$installArgs = @(
    $skillInstallerPath, '--repo', 'Dr-DanDan/lark-ticket-skills',
    '--method', 'git', '--ref', 'main', '--path',
    'skills/lark-push-tickets', 'skills/lark-pull-tickets'
)
& python @installArgs
if ($LASTEXITCODE -ne 0) { throw '技能安装失败。' }
```

两个技能需一起安装：pull 复用 push 目录中的脚本和协议。安装目标已存在时安装器会停止，更新前应先核对已有文件。每个技能目录都包含 MIT 许可证，单独分发也保留许可声明。

## 在 Codex 中使用

先在目标项目运行 setup，首次同步时提供已有项目母文档的飞书 `/wiki/` 链接。

```text
$lark-push-tickets 把当前项目的 .scratch/login/ 上传到已绑定的飞书项目。
$lark-pull-tickets 把 .scratch/login/issues/01-login.md 及其必要依赖导入当前项目。
```

如果没有指定要导入哪些票据，技能先展示轻量目录或索引供 HITL 选择；不会先逐篇读取所有票据正文。

## 直接使用共享脚本

两个技能共用 push 目录的 `scripts/ticket_sync.py`。命令形式为 `python <脚本路径> --root <项目绝对路径> <命令> ...`。

| 命令 | 行为 |
| --- | --- |
| `bind --wiki-url URL` | 绑定已有母文档 Wiki 节点，写入本地配置。 |
| `catalog` | 列出项目节点和已知票据路径，供人工选择。 |
| `push --select 文件或目录 ...` | 上传指定范围及依赖；`.` 表示所有允许的项目 Markdown。 |
| `plan-pull --select 路径或节点或Wiki链接 ...` | 暂存选定票据及依赖，返回计划和冲突。 |
| `apply-pull --plan 文件` | 必要依赖就绪后应用脚本生成的计划。 |

`push` 和 `apply-pull` 支持 `--decisions 文件`，其中 JSON 按相对路径记录用户实际选择的 `local` 或 `remote`。push 的 `local` 允许用本地替换尚未拉取的远端改动；选择保留远端则跳过对应上传，按需导入。人工合并后重新生成计划。

### Windows 后台执行

将下面的示例值替换为已初始化项目及其母文档链接：

```powershell
$projectRoot = (Resolve-Path -LiteralPath 'D:\Projects\my-project').Path
$codexRootPath = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$ticketLauncher = Join-Path $codexRootPath 'skills\lark-push-tickets\scripts\launch.ps1'
$wikiUrl = 'https://your-team.feishu.cn/wiki/YOUR_PROJECT_NODE'
$workerArgs = @('--root', $projectRoot, 'bind', '--wiki-url', $wikiUrl)
& $ticketLauncher -WorkerArgs $workerArgs
```

导入已选择票据时使用 `@('--root', $projectRoot, 'plan-pull', '--select', '.scratch/login/issues/01-login.md')`。核对结果并处理冲突后，用 worker 返回的计划路径启动 `apply-pull`。

启动器返回 PID 和程序拥有的进度文件路径；启动成功不等于同步完成。后续只读取 `.scratch/.feishu-sync/last-run.json` 及其指向的日志或结果快照，两次检查之间结束 Codex 轮次。后台程序独立于聊天运行，自行保存时间戳、失败与结果。

退出码：`0` 完成，`1` 配置或运行失败，`2` 部分结果需要处理。已经成功的独立票据组保留。

## 文件、协议与依赖

- 项目票据包括执行标准、实施票据、grill 澄清产物、CONTEXT/CONTEXT-MAP、ADR 和 TODO。
- 默认位置为 `.scratch/<feature>/spec.md`、`.scratch/<feature>/issues/<NN>-<slug>.md`、根 CONTEXT/CONTEXT-MAP/TODO 与 `docs/adr/`。
- 本地 `docs/agents/` 和 AGENTS 由 setup 维护；附件作为独立参考资料存放。
- 一篇本地 Markdown 对应一篇在线子文档，母文档管理子文档及其本地相对路径。
- 子文档头部的 `feishu-ticket/v1` JSON 记录 `path` 和 `requires`；导入时去掉元信息、自动导航和传输分区标题。
- 实施票据依赖同功能 spec，已登记的根 CONTEXT/CONTEXT-MAP 是共享基础资料。编号型 `Blocked by` 和明确依赖链接按规则解析。
- 其它必要文件通过远端 `requires`，或本地 `## 基础依赖` 章节中的相对 Markdown 链接声明。普通参考链接不触发导入。
- 依赖去重且限定在绑定项目内。缺失、循环、无权限或冲突未解决的依赖会阻塞对应主票据。

配置保存在 `docs/agents/feishu-sync.md`，映射、基线、暂存与日志使用 `.scratch/.feishu-sync/`；这些已有标识继续兼容早期安装。

只对选定主票据及必要依赖读取正文。远端版本未变且本地基线校验通过时复用；缺失的本地依赖仍会修复。上传按文档块差异写入，保留未变化块的 ID，先插入新内容，再移除核对后的旧块。

自动操作仅新增和更新。标题改名保持身份；路径搬移与删除另行处理。导入票据不会执行任务，也不会改写 Status/Blocked by。

## 本地验证

在仓库根目录执行：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw '票据同步验证失败。' }
```

测试通过离线 fixture 覆盖选择边界、依赖就绪、基线复用、冲突、路径保护、外部命令失败和块替换；独立离线执行也核对了 CLI 流程。这些检查不代表真实飞书 API 已实测。

## 当前限制

- 在线文档转换可能改变 Markdown 转义、排版和字节。
- 内嵌附件、不支持的富文本/XML、含糊的依赖声明会阻断对应文件。
- 单个改动组超过嵌套插入的 1000-block 上限时安全停止。
- 真实认证、Markdown 往返、改动块的评论锚点和服务器并发更新仍需授权测试文档验证。多文档更新不保证原子性；部分失败保留现场以便核对。

详细流程见[共享协议](skills/lark-push-tickets/references/protocol.md)，设计依据见[架构决策](docs/adr/)。

## 许可证

[MIT](LICENSE)，Copyright 2026 Dr-DanDan。两个可分发技能目录也分别包含许可证。
