# Lark Ticket Skills

[English](README.md) | [简体中文](README.zh-CN.md)

Two Codex skills for transferring project Markdown between a local engineering project and editable Feishu/Lark Wiki documents, using the official [Lark CLI](https://github.com/larksuite/cli).

| Skill | Purpose |
| --- | --- |
| [lark-push-tickets](skills/lark-push-tickets/SKILL.md) | Upload or update tickets, create dependency links, and maintain a project index document. |
| [lark-pull-tickets](skills/lark-pull-tickets/SKILL.md) | Import human-selected tickets and their required dependencies into the established local layout. |

HITL means human-in-the-loop: the user selects main tickets and resolves conflicts. The shared Python helper performs path checks, dependency expansion, revision checks, staging, and writes according to fixed rules.

## Requirements

- Codex with its bundled skill installer.
- Python 3.9 or newer; the helper uses the standard library.
- The official `lark-cli`, available on PATH, with access to the target Wiki and documents. Runtime commands use the user identity.
- A target project initialized by [setup-drdandan-skills](https://github.com/Dr-DanDan/setup-drdandan-skills), including `docs/agents/domain.md` and `docs/agents/issue-tracker.md`.
- PowerShell 7 on Windows for the bundled background launcher. Current local validation used Python 3.9 and Lark CLI 1.0.97; real Feishu round trips remain untested.

## Install both skills

This repository is private. Your GitHub account must have access, and Git authentication must already work. The following command uses the bundled installer with Git credentials:

```powershell
$codexRootPath = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$skillInstallerPath = Join-Path $codexRootPath 'skills\.system\skill-installer\scripts\install-skill-from-github.py'
$installArgs = @(
    $skillInstallerPath, '--repo', 'Dr-DanDan/lark-ticket-skills',
    '--method', 'git', '--ref', 'main', '--path',
    'skills/lark-push-tickets', 'skills/lark-pull-tickets'
)
& python @installArgs
if ($LASTEXITCODE -ne 0) { throw 'Skill installation failed.' }
```

Install the pair together: pull shares the helper and protocol located inside push. The installer stops when a destination folder already exists; review existing files before updating. Each skill directory includes its own MIT license.

## Use in Codex

Run setup in the target project, then provide its Feishu project mother-document Wiki URL on first transfer.

```text
$lark-push-tickets Upload .scratch/login/ from this project to the configured Feishu project.
$lark-pull-tickets Import .scratch/login/issues/01-login.md and its required dependencies into this project.
```

When an import request does not identify tickets, the skill first presents a lightweight directory/index for the user to choose from. It does not fetch every ticket body to make that choice.

## Direct helper usage

Both skills call `scripts/ticket_sync.py` inside the installed push skill. The common command shape is `python <helper> --root <absolute-project-path> <command> ...`.

| Command | Action |
| --- | --- |
| `bind --wiki-url URL` | Bind the local project to an existing mother-document Wiki node; write local configuration. |
| `catalog` | List project nodes and known ticket paths for selection. |
| `push --select FILE_OR_DIRECTORY ...` | Upload the selected scope and its dependencies; `.` selects all allowed project Markdown. |
| `plan-pull --select PATH_OR_NODE_OR_WIKI_URL ...` | Stage the selected ticket closure and return a plan plus any conflicts. |
| `apply-pull --plan FILE` | Apply a generated plan after required dependencies are ready. |

`push` and `apply-pull` accept `--decisions FILE` for actual HITL choices. The JSON maps relative paths to `local` or `remote`; push accepts `local` to authorize replacing unpulled remote changes. Keeping a remote version during push means skipping it and importing it when desired. Manual merges require a new plan.

### Background execution on Windows

Set the example values to your initialized project and existing Wiki URL:

```powershell
$projectRoot = (Resolve-Path -LiteralPath 'D:\Projects\my-project').Path
$codexRootPath = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$ticketLauncher = Join-Path $codexRootPath 'skills\lark-push-tickets\scripts\launch.ps1'
$wikiUrl = 'https://your-team.feishu.cn/wiki/YOUR_PROJECT_NODE'
$workerArgs = @('--root', $projectRoot, 'bind', '--wiki-url', $wikiUrl)
& $ticketLauncher -WorkerArgs $workerArgs
```

For a selected import, launch with `@('--root', $projectRoot, 'plan-pull', '--select', '.scratch/login/issues/01-login.md')`. After its result is verified and conflicts are resolved, launch `apply-pull` with the plan path returned by the worker.

The launcher returns a PID and a program-owned progress-file path. A successful start does not establish transfer success. Read `.scratch/.feishu-sync/last-run.json` and its referenced progress/result files as snapshots; finish the Codex turn between checks. The process runs independently of the chat and writes timestamps, failures, and results itself.

Exit codes: `0` completed, `1` configuration/runtime failure, `2` partial results need attention. Successful independent groups remain in place.

## Files, protocol, and dependencies

- Project tickets include specs, implementation issues, grill-derived design material, CONTEXT/CONTEXT-MAP, ADRs, and TODO.
- The default layout is `.scratch/<feature>/spec.md`, `.scratch/<feature>/issues/<NN>-<slug>.md`, root CONTEXT/CONTEXT-MAP/TODO, and `docs/adr/`.
- Setup owns local `docs/agents/` and AGENTS files. Attachments are kept as separate reference material.
- One local Markdown file maps to one online child document. A mother document indexes children and their relative paths.
- A leading `feishu-ticket/v1` JSON block stores `path` and `requires`. Generated dependency navigation and transport headings are removed on import.
- Issues require their feature spec. Existing registered root context documents are shared foundations. Numbered `Blocked by` references and explicitly declared dependency links are resolved deterministically.
- Other required files can be declared in the remote `requires` list or in a local `## 基础依赖` section with relative Markdown links. Ordinary reference links do not cause imports.
- Dependencies are deduplicated and limited to the bound project. Missing, cyclic, inaccessible, or unresolved dependencies block the corresponding main ticket.

Configuration is stored in `docs/agents/feishu-sync.md`; mappings, baselines, staging, and logs use `.scratch/.feishu-sync/`. These existing identifiers remain compatible with earlier installations.

Only selected tickets and their dependency closure are considered for body reads. An unchanged remote revision plus a verified local baseline permits reuse; a missing local dependency is repaired. Writes use a block diff, preserve unchanged block IDs, and insert new content before removing confirmed old blocks.

Only add/update operations are automatic. Title changes retain identity; path relocation and deletion require separate handling. Ticket import does not complete its task or change Status/Blocked by.

## Validate locally

From the repository root:

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Ticket transfer tests failed.' }
```

Tests use offline fixtures for selection boundaries, dependency readiness, baseline reuse, conflicts, path protection, external-command failures, and block replacement. An isolated offline execution check also exercised the CLI workflow. These checks do not verify live Feishu behavior.

## Current limits

- Online document conversion may change Markdown escaping, layout, and bytes.
- Embedded attachments, unsupported rich-text/XML, and ambiguous dependency declarations stop the affected file.
- A changed group exceeding the 1000-block nested-insertion limit stops safely.
- Real authentication, Markdown round trips, changed-block comment anchors, and concurrent server edits still need an authorized test document. Multi-document updates are not atomic; partial failures retain their state for inspection.

See the [shared protocol](skills/lark-push-tickets/references/protocol.md) for the full workflow and the [architecture decisions](docs/adr/) for its rationale.

## License

[MIT](LICENSE), copyright 2026 Dr-DanDan. The license is also included in each distributable skill directory.
