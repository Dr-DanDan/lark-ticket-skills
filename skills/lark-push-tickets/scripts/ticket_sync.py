"""Rule-based Feishu ticket transfer. Uses the standard library and lark-cli."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse


SCHEMA = "feishu-ticket/v1"
BODY = "票据正文"
CONFIG = "docs/agents/feishu-sync.md"
CACHE = ".scratch/.feishu-sync"
RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.I)
UNSET = object()


class SyncError(Exception):
    pass


def digest(text):
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def read_text(path):
    return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


def atomic_text(path, text, expected_hash=UNSET):
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_bytes() if path.is_file() else b""
    newline = "\r\n" if b"\r\n" in old else "\n"
    data = text.replace("\r\n", "\n").replace("\n", newline).encode("utf-8")
    if old.startswith(b"\xef\xbb\xbf"):
        data = b"\xef\xbb\xbf" + data
    fd, name = tempfile.mkstemp(prefix=".feishu-write-", dir=path.parent)
    # The single sibling temporary file is retained if replacement fails.
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if expected_hash is not UNSET:
        current = read_text(path) if path.is_file() else None
        if (digest(current) if current is not None else None) != expected_hash:
            raise SyncError("Local file changed before replacement")
    os.replace(name, path)


def dump(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def ensure_inside(root, location):
    for parent in [location, *location.parents]:
        if parent == root:
            break
        if parent.is_symlink() or (parent.exists() and getattr(parent.lstat(), "st_file_attributes", 0) & 1024):
            raise SyncError("Reparse/symlink locations are not allowed")
    if not location.resolve().is_relative_to(root):
        raise SyncError("Location escapes project")


@contextmanager
def project_lock(root):
    cache = root / CACHE
    ensure_inside(root, cache / "worker.lock")
    for required in ("docs/agents/domain.md", "docs/agents/issue-tracker.md"):
        if not (root / required).is_file():
            raise SyncError("Run setup-drdandan-skills first; missing " + required)
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "worker.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def json_block(text, schema):
    for match in re.finditer(r"(?m)^```json\s*\n(.*?)\n```\s*$", text, re.S | re.I):
        try:
            value = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("schema") == schema:
            return value, match.span()
    raise SyncError(f"Missing or invalid {schema} JSON block")


def outside_code(text):
    result, fence = [], None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
            continue
        if fence is None:
            result.append(re.sub(r"(`+).*?\1", "", line))
    return "\n".join(result)


def portable(text):
    if not text.strip():
        raise SyncError("Empty ticket body")
    visible = outside_code(text)
    if re.search(r"!\[[^\n]*\]\(|<(?:img|source|whiteboard|sheet|bitable|cite|synced_|grid|callout|button|time|task)\b", visible, re.I):
        raise SyncError("Embedded resources are outside ticket transfer; put them in separate reference documents")
    if re.search(r"</?[A-Za-z][^>]*>", visible):
        raise SyncError("Unsupported rich-text/XML body; convert it explicitly to portable Markdown first")


def restore_checkboxes(text):
    # Lark-flavored Markdown can export native checkboxes as XML.
    def replace(match):
        value = match.group(2)
        if "<" in value or "\n" in value:
            raise SyncError("Complex checkbox content needs an explicit Markdown conversion")
        return "- [" + ("x" if match.group(1) == "true" else " ") + "] " + value
    return re.sub(r'<checkbox done="(true|false)">(.*?)</checkbox>', replace, text)


def link_label(text):
    return re.sub(r"([\\`*_\[\]~$])", r"\\\1", text)


def envelope(path, requires, body, links):
    portable(body)
    front = re.match(r"\A---\n(.*?)\n---(?:\n|$)", body, re.S)
    meta = {"schema": SCHEMA, "path": path, "requires": sorted(set(requires))}
    if front:
        meta["frontmatter"] = True
        body = "```yaml\n" + front.group(1) + "\n```\n" + body[front.end():]
    navigation = "\n".join(f"- [{link_label(key)}]({links[key]})" for key in meta["requires"])
    return ("## 同步元信息\n\n```json\n" + json.dumps(meta, ensure_ascii=False, indent=2)
            + "\n```\n\n## 基础依赖导航\n\n" + (navigation or "无")
            + "\n\n## " + BODY + "\n\n" + body.rstrip() + "\n")


def parse_envelope(text):
    meta, span = json_block(text, SCHEMA)
    if not re.search(r"(?m)^## 同步元信息[ \t]*\n[ \t\n]*$", text[:span[0]]):
        raise SyncError("Ticket JSON must be in the leading sync metadata section")
    if not isinstance(meta.get("path"), str) or not isinstance(meta.get("requires"), list):
        raise SyncError("Ticket header needs a path and requires list")
    if any(not isinstance(item, str) for item in meta["requires"]):
        raise SyncError("Invalid requires item")
    meta["requires"] = sorted(set(meta["requires"]))
    match = re.search(r"(?m)^## " + BODY + r"\s*$", text[span[1]:])
    if not match:
        raise SyncError("Missing managed ticket body heading")
    body = text[span[1] + match.end():].lstrip("\n")
    if meta.get("frontmatter"):
        front = re.match(r"\A```(?:yaml|yml)\n(.*?)\n```(?:\n|$)", body, re.S | re.I)
        if not front:
            raise SyncError("Frontmatter code block missing; no lossy conversion allowed")
        body = "---\n" + front.group(1) + "\n---\n" + body[front.end():].lstrip("\n")
    body = restore_checkboxes(body)
    portable(body)
    return meta, body.rstrip() + "\n"


class Lark:
    def __init__(self, root, progress):
        self.root, self.progress = root, progress
        self.binary = shutil.which("lark-cli")
        if not self.binary:
            raise SyncError("lark-cli is not installed or not on PATH")

    def call(self, *args, content=None):
        self.progress("cli", command=list(args[:3]))
        result = subprocess.run([self.binary, *args, "--as", "user"], cwd=self.root,
                                input=content, capture_output=True, text=True,
                                encoding="utf-8", timeout=120)
        if result.returncode:
            raise SyncError(f"lark-cli exit {result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            raise SyncError("lark-cli did not return JSON") from error
        if not isinstance(value, dict):
            raise SyncError("lark-cli JSON response must be an object")
        if value.get("ok") is False or value.get("code", 0) != 0:
            raise SyncError(str(value.get("error", value.get("msg", "API failure"))))
        data = value.get("data", value)
        if not isinstance(data, dict):
            raise SyncError("lark-cli response data must be an object")
        if "code" in data:
            if data["code"] != 0:
                raise SyncError(str(data.get("msg", "API failure")))
            data = data.get("data", data)
        return data

    def node(self, token):
        data = self.call("wiki", "+node-get", "--node-token", token)
        return data.get("node", data)

    def children(self, space, parent):
        nodes, cursor, seen = [], "", set()
        while True:
            args = ["wiki", "+node-list", "--space-id", space, "--parent-node-token", parent]
            if cursor:
                args += ["--page-token", cursor]
            data = self.call(*args)
            nodes.extend(data["nodes"])
            if not data.get("has_more"):
                return nodes
            cursor = data.get("page_token")
            if not cursor or cursor in seen:
                raise SyncError("Incomplete or repeated Wiki pagination cursor")
            seen.add(cursor)

    def revision(self, token):
        data = self.call("api", "GET", "/open-apis/docx/v1/documents/" + token)
        return data["document"]["revision_id"]

    def fetch(self, token, revision):
        data = self.call("docs", "+fetch", "--doc", token, "--revision-id", str(revision),
                         "--doc-format", "markdown")
        doc = data["document"]
        if doc["revision_id"] != revision:
            raise SyncError("Fetched revision differs from requested revision")
        return doc["content"]

    def blocks(self, token, revision):
        items, cursor, seen = [], "", set()
        while True:
            params = {"page_size": 100, "document_revision_id": revision}
            if cursor:
                params["page_token"] = cursor
            data = self.call("api", "GET", f"/open-apis/docx/v1/documents/{token}/blocks/{token}/children",
                             "--params", json.dumps(params))
            items.extend(data["items"])
            if not data.get("has_more"):
                return items
            cursor = data.get("page_token")
            if not cursor or cursor in seen:
                raise SyncError("Incomplete document block pagination")
            seen.add(cursor)

    def create(self, parent, title):
        return self.call("wiki", "+node-create", "--parent-node-token", parent, "--title", title)

    def write(self, token, text, revision, blocks=None):
        converted = self.call("api", "POST", "/open-apis/docx/v1/documents/blocks/convert",
                              "--data", "-", content=json.dumps({"content_type": "markdown", "content": text}, ensure_ascii=False))
        new_map = {b["block_id"]: b for b in converted["blocks"]}
        new_ids = converted["first_level_block_ids"]
        roots = self.blocks(token, revision)
        root_ids = [b["block_id"] for b in roots]
        old_map = {b["block_id"]: b for b in roots}
        cursor, seen = "", set()
        while True:
            params = {"page_size": 500, "document_revision_id": revision}
            if cursor:
                params["page_token"] = cursor
            data = self.call("api", "GET", f"/open-apis/docx/v1/documents/{token}/blocks", "--params", json.dumps(params))
            old_map.update({b["block_id"]: b for b in data["items"]})
            if not data.get("has_more"):
                break
            cursor = data.get("page_token")
            if not cursor or cursor in seen:
                raise SyncError("Incomplete native block inventory")
            seen.add(cursor)
        if blocks:
            offset = root_ids.index(blocks[0])
            if root_ids[offset:offset + len(blocks)] != blocks:
                raise SyncError("Managed block range changed")
            old_ids = blocks
        else:
            offset, old_ids = len(root_ids), []

        def clean(value):
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items() if k not in ("block_id", "parent_id", "children", "merge_info") and v not in (None, False, "", [], {})}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value

        def signature(key, mapping, visiting=None):
            visiting = set() if visiting is None else visiting
            if key in visiting:
                raise SyncError("Cyclic document block tree")
            visiting.add(key)
            block = mapping[key]
            value = [clean(block), [signature(child, mapping, visiting) for child in block.get("children", [])]]
            visiting.remove(key)
            return json.dumps(value, ensure_ascii=False, sort_keys=True)

        old_signatures = [signature(key, old_map) for key in old_ids]
        new_signatures = [signature(key, new_map) for key in new_ids]
        edits = difflib.SequenceMatcher(None, old_signatures, new_signatures, autojunk=False).get_opcodes()
        for tag, i1, i2, j1, j2 in reversed(edits):
            if tag == "equal":
                continue
            if self.revision(token) != revision:
                raise SyncError("Concurrent edit before block update")
            inserted = new_ids[j1:j2]
            if inserted:
                needed = set()

                def collect(key):
                    if key in needed:
                        return
                    needed.add(key)
                    for child in new_map[key].get("children", []):
                        collect(child)

                for key in inserted:
                    collect(key)
                descendants = json.loads(json.dumps([new_map[key] for key in new_map if key in needed]))
                for block in descendants:
                    block.get("table", {}).pop("merge_info", None)
                if len(descendants) > 1000:
                    # shortcut: larger groups stop safely, batch nested insertion when large tickets require it.
                    raise SyncError("Changed block group exceeds Feishu's 1000-block insertion limit")
                operation = str(uuid.uuid4())
                self.progress("insert_blocks", operation=operation, count=len(descendants))
                data = self.call("api", "POST", f"/open-apis/docx/v1/documents/{token}/blocks/{token}/descendant",
                                 "--params", json.dumps({"document_revision_id": revision, "client_token": operation}),
                                 "--data", "-", content=json.dumps({"children_id": inserted, "descendants": descendants, "index": offset + i1}, ensure_ascii=False))
                revision = data["document_revision_id"]
            if i2 > i1:
                if self.revision(token) != revision:
                    raise SyncError("Concurrent edit after insert; original blocks retained")
                current_ids = [b["block_id"] for b in self.blocks(token, revision)]
                start = offset + i1 + len(inserted)
                if current_ids[start:start + i2 - i1] != old_ids[i1:i2]:
                    raise SyncError("Original block range moved; original blocks retained")
                operation = str(uuid.uuid4())
                self.progress("replace_old_blocks", operation=operation, count=i2 - i1)
                data = self.call("api", "DELETE", f"/open-apis/docx/v1/documents/{token}/blocks/{token}/children/batch_delete",
                                 "--params", json.dumps({"document_revision_id": revision, "client_token": operation}),
                                 "--data", "-", content=json.dumps({"start_index": start, "end_index": start + i2 - i1}))
                revision = data["document_revision_id"]
        return revision


class Sync:
    def __init__(self, root, remote=None):
        self.root = Path(root).resolve()
        for required in ("docs/agents/domain.md", "docs/agents/issue-tracker.md"):
            if not (self.root / required).is_file():
                raise SyncError("Run setup-drdandan-skills first; missing " + required)
        self.cache = self.root / CACHE
        self.config_path = self.root / CONFIG
        for location in (self.cache, self.config_path):
            ensure_inside(self.root, location)
        self.context_paths = []
        self.state_path = self.cache / "state.json"
        self.state = {"schema": "feishu-state/v1", "files": {}, "directories": {}, "pending": {}}
        if self.state_path.exists():
            try:
                self.state = json.loads(read_text(self.state_path))
                if self.state["schema"] != "feishu-state/v1":
                    raise ValueError("Unsupported state version")
                for key in ("files", "directories", "pending"):
                    if not isinstance(self.state[key], dict):
                        raise ValueError("Malformed state")
            except (ValueError, KeyError) as error:
                raise SyncError("Corrupt sync state; restore it or explicitly rebuild the selected baseline") from error
        if self.config_path.exists():
            self.config, _ = json_block(read_text(self.config_path), "feishu-sync/v1")
        else:
            self.config = {"schema": "feishu-sync/v1", "project_url": None, "context_paths": []}
        self.context_paths = self.config.get("context_paths", [])
        if not isinstance(self.context_paths, list) or any(not isinstance(p, str) for p in self.context_paths):
            raise SyncError("Invalid configured context paths")
        if any(not re.fullmatch(r"src/[^/.]+/CONTEXT\.md", p) for p in self.context_paths):
            raise SyncError("context_paths must name setup-declared src/<context>/CONTEXT.md files")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        self.run = self.cache / "runs" / stamp
        self.log = self.run / "progress.jsonl"
        self.remote = remote or Lark(self.root, self.progress)
        self.progress("start", pid=os.getpid())

    def progress(self, event, **fields):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}, ensure_ascii=False) + "\n")
        if event in ("start", "finished", "failed"):
            dump(self.cache / "last-run.json", {"pid": os.getpid(), "time": datetime.now(timezone.utc).isoformat(),
                                               "event": event, "progress": str(self.log), **fields})

    def save(self):
        dump(self.state_path, self.state)

    def path(self, name):
        if not isinstance(name, str) or "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            raise SyncError("Expected project-relative slash path: " + str(name))
        parts = name.split("/")
        if any(not p or p in (".", "..") or p.endswith((" ", ".")) or RESERVED.fullmatch(p)
               or re.search(r'[<>:"|?*\x00-\x1f]', p) for p in parts):
            raise SyncError("Invalid Windows/project path: " + name)
        allowed = (name in ("CONTEXT.md", "CONTEXT-MAP.md", "TODO.md")
                   or (name.startswith(".scratch/") and all(not p.startswith(".") for p in parts[1:]))
                   or name.startswith("docs/adr/") or name in self.context_paths
                   or any(p.endswith("/CONTEXT.md") and name.startswith(p[:-len("CONTEXT.md")] + "docs/adr/") for p in self.context_paths))
        if not allowed or not name.endswith(".md"):
            raise SyncError("Path is outside configured ticket scope: " + name)
        target = self.root.joinpath(*parts)
        ensure_inside(self.root, target)
        folder = self.root
        for part in parts:
            if folder.is_dir():
                matches = [entry.name for entry in folder.iterdir() if entry.name.casefold() == part.casefold()]
                if matches and matches != [part]:
                    raise SyncError("Windows case collision: " + name)
            folder = folder / part
        return target

    def bound(self):
        url = self.config.get("project_url")
        if not url:
            raise SyncError("Bind a Feishu project mother document first")
        node = self.remote.node(url)
        if node.get("obj_type") != "docx":
            raise SyncError("Mother document must be an online docx Wiki node")
        if node.get("node_type", "origin") != "origin":
            raise SyncError("Bind the original mother-document node, not a cross-project shortcut")
        key = node["node_token"]
        if self.state.get("root_node") not in (None, key):
            raise SyncError("State belongs to another Feishu project")
        self.state["root_node"] = key
        self.mother = node
        return node

    def bind(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.path.startswith("/wiki/"):
            raise SyncError("Expected an HTTPS Feishu/Lark Wiki URL")
        host = parsed.hostname or ""
        if not any(host == suffix or host.endswith("." + suffix) for suffix in ("feishu.cn", "larksuite.com", "larkoffice.com")):
            raise SyncError("Unsupported Wiki URL host")
        if self.config.get("project_url") and self.config["project_url"] != url:
            raise SyncError("Project already bound; use a separate local project for another mother document")
        self.config["project_url"] = url
        node = self.bound()
        if self.config_path.exists():
            text = read_text(self.config_path)
            _, span = json_block(text, "feishu-sync/v1")
            text = text[:span[0]] + "```json\n" + json.dumps(self.config, ensure_ascii=False, indent=2) + "\n```" + text[span[1]:]
        else:
            text = "# Feishu ticket sync\n\n```json\n" + json.dumps(self.config, ensure_ascii=False, indent=2) + "\n```\n\nUse lark-push-tickets / lark-pull-tickets. Select main tickets before pulling; required dependencies are included automatically.\n"
        atomic_text(self.config_path, text)
        ignore = self.root / ".gitignore"
        ensure_inside(self.root, ignore)
        text = read_text(ignore) if ignore.exists() else ""
        if CACHE + "/" not in text.splitlines():
            atomic_text(ignore, text.rstrip("\n") + "\n" + CACHE + "/\n")
        self.save()
        return {"root_node": node["node_token"], "config": str(self.config_path)}

    def catalog(self):
        mother = self.bound()
        nodes, pending, visited = {}, [mother["node_token"]], set()
        while pending:
            parent = pending.pop()
            if parent in visited:
                raise SyncError("Repeated Wiki node in project tree")
            visited.add(parent)
            for node in self.remote.children(str(mother["space_id"]), parent):
                if node.get("node_type", "origin") != "origin":
                    continue
                if node["node_token"] in nodes:
                    raise SyncError("Duplicate Wiki node")
                nodes[node["node_token"]] = node
                if node.get("has_child"):
                    pending.append(node["node_token"])
        self.nodes = nodes
        self.index = {}
        revision = self.remote.revision(mother["obj_token"])
        index_cache = self.cache / "index.json"
        if self.state.get("index_revision") == revision and index_cache.exists():
            self.index = json.loads(read_text(index_cache))
        else:
            text = self.remote.fetch(mother["obj_token"], revision)
            try:
                index, _ = json_block(text, "feishu-project/v1")
            except SyncError:
                if "feishu-project/" in text:
                    raise SyncError("Malformed or unsupported project index")
                index = {"entries": []}
            for item in index.get("entries", []):
                self.path(item["path"])
                key = item["path"]
                if any(old.casefold() == key.casefold() for old in self.index):
                    raise SyncError("Duplicate/case-colliding index path: " + key)
                if item["node_token"] in nodes and nodes[item["node_token"]].get("obj_token") == item["obj_token"]:
                    self.index[key] = item
            dump(index_cache, self.index)
            self.state["index_revision"] = revision
        for key, item in self.state["files"].items():
            self.path(key)
            if item["node_token"] in nodes and key not in self.index:
                self.index[key] = item
        self.save()
        known = {item["node_token"]: key for key, item in self.index.items()}
        return [{"node_token": key, "title": node.get("title", ""), "path": known.get(key),
                 "type": node.get("obj_type"), "edited_time": node.get("obj_edit_time"),
                 "parent": node.get("parent_node_token"), "url": self.node_url(key)} for key, node in nodes.items()]

    def node_url(self, token):
        return "https://" + urlparse(self.config["project_url"]).netloc + "/wiki/" + token

    def structural(self, path, available):
        requires = set()
        if path in ("CONTEXT.md", "CONTEXT-MAP.md", *self.context_paths):
            return requires
        for base in ("CONTEXT.md", "CONTEXT-MAP.md"):
            if base != path and base in available:
                requires.add(base)
        match = re.match(r"(\.scratch/[^/]+)/issues/[^/]+\.md$", path)
        if match:
            requires.add(match.group(1) + "/spec.md")
        for context in self.context_paths:
            if path.startswith(context[:-len("CONTEXT.md")] + "docs/adr/"):
                requires.add(context)
        return requires

    def local_dependencies(self, name, text, available):
        requires = self.structural(name, available)
        section = re.search(r"(?ms)^## 基础依赖\s*\n(.*?)(?=^## |\Z)", outside_code(text))
        if section:
            for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", section.group(1)):
                if "://" in link or "#" in link:
                    raise SyncError("Required dependencies must be plain project-relative Markdown paths")
                link = unquote(link[1:-1] if link.startswith("<") and link.endswith(">") else link)
                joined = PurePosixPath(name).parent / link
                normalized = []
                for part in joined.parts:
                    if part == "..":
                        if not normalized:
                            raise SyncError("Dependency escapes project")
                        normalized.pop()
                    elif part != ".":
                        normalized.append(part)
                requires.add("/".join(normalized))
        blockers = re.search(r"(?mi)^\*{0,2}Blocked by:\*{0,2}\s*(.+)$", outside_code(text))
        if blockers and not re.match(r"(?:none|无)\b", blockers.group(1), re.I):
            value = blockers.group(1).strip()
            numbers = []
            for edge in re.split(r"[,，;；]", value):
                match = re.match(r"^#?(\d+)(?:\s*[—–:-]|\s*$)", edge.strip())
                if not match:
                    raise SyncError("Blocked by is ambiguous; declare exact numbers or add explicit dependency links")
                numbers.append(match.group(1))
            for number in numbers:
                prefix = str(PurePosixPath(name).parent / (number.zfill(2) + "-"))
                matches = [p for p in available if p.startswith(prefix)]
                if len(matches) != 1:
                    raise SyncError("Cannot uniquely resolve blocker " + number)
                requires.add(matches[0])
        requires.update(self.state["files"].get(name, {}).get("requires", []))
        for item in requires:
            self.path(item)
            if item not in available:
                raise SyncError("Missing required file: " + item)
        return sorted(requires)

    def accept(self, name, node, revision, local_body, remote_body, requires):
        self.state["files"][name] = {"node_token": node["node_token"], "obj_token": node["obj_token"],
                                     "revision": revision, "local_hash": digest(local_body),
                                     "remote_hash": digest(remote_body), "requires": requires}
        self.state["pending"].pop(name, None)
        self.save()

    def plan_pull(self, selected):
        if not selected:
            raise SyncError("HITL selection is required")
        self.catalog()
        plan = {"schema": "feishu-pull-plan/v1", "project": str(self.root),
                "root_node": self.mother["node_token"], "main": [], "files": {}, "errors": {}}
        visiting = set()

        def visit(identifier):
            if identifier in self.index:
                node = self.nodes[self.index[identifier]["node_token"]]
                expected = identifier
            else:
                token = urlparse(identifier).path.rstrip("/").split("/")[-1] if "://" in identifier else identifier
                node = self.nodes.get(token)
                expected = None
                if node is None:
                    raise SyncError("Required/selected ticket not in project index: " + identifier)
            if node.get("obj_type") != "docx":
                raise SyncError("Selected node is not an online ticket document")
            token = node["obj_token"]
            if token in visiting:
                raise SyncError("Cyclic required dependencies")
            if expected in plan["files"]:
                return expected
            visiting.add(token)
            revision = self.remote.revision(token)
            cached = self.state["files"].get(expected, {})
            target = self.path(expected) if expected else None
            local = read_text(target) if target and target.is_file() else None
            if cached.get("revision") == revision and local is not None and digest(local) == cached.get("local_hash"):
                name, requires, body, action = expected, cached["requires"], None, "reuse"
            else:
                meta, body = parse_envelope(self.remote.fetch(token, revision))
                name, requires = meta["path"], list(meta["requires"])
                self.path(name)
                if expected and name != expected:
                    raise SyncError("Remote path changed; explicit relocation is required")
                if any(p.casefold() == name.casefold() and p != name for p in [*self.index, *plan["files"]]):
                    raise SyncError("Case-colliding path: " + name)
                if name in plan["files"] and plan["files"][name]["node"]["obj_token"] != token:
                    raise SyncError("Two documents claim the same local path")
                self.index.setdefault(name, {**node, "path": name})
                requires = sorted(set(requires) | self.structural(name, self.index))
                target = self.path(name)
                local = read_text(target) if target.is_file() else None
                cached = self.state["files"].get(name, {})
                action = "write"
                if local is not None and digest(local) != cached.get("local_hash") and digest(local) != digest(body):
                    action = "conflict"
                if local is not None and digest(local) == digest(body):
                    action = "reuse"
                stage = self.run / (hashlib.sha256(name.encode()).hexdigest() + ".md")
                atomic_text(stage, body)
            record = {"node": node, "revision": revision, "requires": requires,
                      "action": action, "local_hash": digest(local) if local is not None else None}
            if body is not None:
                record["stage"] = str(stage)
                record["remote_hash"] = digest(body)
                if action == "conflict":
                    diff = "".join(difflib.unified_diff((local or "").splitlines(True), body.splitlines(True),
                                                        fromfile=name + " (local)", tofile=name + " (Feishu)"))
                    diff_path = stage.with_suffix(".diff")
                    atomic_text(diff_path, diff)
                    record["diff"] = str(diff_path)
            plan["files"][name] = record
            for dependency in requires:
                self.path(dependency)
                visit(dependency)
            visiting.remove(token)
            return name

        for identifier in dict.fromkeys(selected):
            try:
                name = visit(identifier)
                plan["main"].append(name)
            except (SyncError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
                plan["errors"][identifier] = str(error)
                visiting.clear()
        plan_path = self.run / "pull-plan.json"
        dump(plan_path, plan)
        self.progress("planned", main=plan["main"], errors=plan["errors"])
        return {"plan": str(plan_path), "main": plan["main"], "files": plan["files"], "errors": plan["errors"]}

    def apply_pull(self, plan_path, decisions):
        plan_file = Path(plan_path).resolve()
        if not plan_file.is_relative_to(self.cache.resolve()):
            raise SyncError("Plan must be a script-generated file in the project sync cache")
        plan = json.loads(read_text(plan_file))
        self.catalog()
        if plan.get("schema") != "feishu-pull-plan/v1" or Path(plan["project"]).resolve() != self.root or plan["root_node"] != self.mother["node_token"]:
            raise SyncError("Plan belongs to another project")
        results, visiting = {}, set()

        def apply(name):
            if name in visiting:
                raise SyncError("Cyclic plan")
            if name in results:
                if results[name]["status"] not in ("written", "reused", "kept-local"):
                    raise SyncError("Dependency failed: " + name)
                return
            visiting.add(name)
            try:
                if name not in plan["files"]:
                    raise SyncError("Missing dependency in plan: " + name)
                record = plan["files"][name]
                target = self.path(name)
                actual = self.nodes.get(record["node"]["node_token"])
                if not actual or actual.get("obj_token") != record["node"]["obj_token"]:
                    raise SyncError("Planned document is no longer in the bound project")
                for dependency in record["requires"]:
                    apply(dependency)
                current = read_text(target) if target.is_file() else None
                if (digest(current) if current is not None else None) != record["local_hash"]:
                    raise SyncError("Local file changed after planning")
                if self.remote.revision(record["node"]["obj_token"]) != record["revision"]:
                    raise SyncError("Remote document changed after planning")
                action = record["action"]
                if action == "conflict":
                    choice = decisions.get(name)
                    if choice == "local" and current is not None:
                        results[name] = {"status": "kept-local", "note": "HITL retained local content; sync baseline not advanced"}
                        return
                    if choice != "remote":
                        raise SyncError("HITL conflict choice is required")
                if "stage" in record:
                    stage = Path(record["stage"]).resolve()
                    if not stage.is_relative_to(self.cache.resolve()):
                        raise SyncError("Stage path outside sync cache")
                    body = read_text(stage)
                    if digest(body) != record["remote_hash"]:
                        raise SyncError("Staged body changed")
                    portable(body)
                    if action != "reuse":
                        self.path(name)
                        atomic_text(target, body, expected_hash=record["local_hash"])
                    if digest(read_text(target)) != digest(body):
                        raise SyncError("Local write verification failed")
                    self.accept(name, record["node"], record["revision"], read_text(target), body, record["requires"])
                    results[name] = {"status": "reused" if action == "reuse" else "written"}
                else:
                    results[name] = {"status": "reused"}
            except (SyncError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
                results[name] = {"status": "blocked", "error": str(error)}
                raise SyncError(str(error)) from error
            finally:
                visiting.discard(name)

        for name in plan["main"]:
            try:
                apply(name)
            except SyncError:
                visiting.clear()
        self.progress("applied", results=results, errors=plan["errors"])
        return {"main": plan["main"], "results": results, "errors": plan["errors"]}

    def directory(self, relative):
        if relative in ("", "."):
            return self.mother["node_token"]
        if relative in self.state["directories"]:
            token = self.state["directories"][relative]
            if token not in self.nodes:
                raise SyncError("Previously created directory disappeared: " + relative)
            return token
        path = PurePosixPath(relative)
        parent = self.directory(str(path.parent))
        matches = [n for n in self.nodes.values() if n.get("parent_node_token") == parent and n.get("title") == path.name]
        if len(matches) > 1:
            raise SyncError("Ambiguous remote directory: " + relative)
        if matches:
            node = matches[0]
            if node.get("obj_type") != "docx":
                raise SyncError("Remote directory title is occupied by another resource")
        else:
            node = self.remote.create(parent, path.name)
            self.nodes[node["node_token"]] = {**node, "parent_node_token": parent}
        self.state["directories"][relative] = node["node_token"]
        self.save()
        return node["node_token"]

    def update_index(self):
        entries = {key: {"path": key, "node_token": value["node_token"], "obj_token": value["obj_token"],
                         "requires": value.get("requires", []), "revision": value.get("revision")}
                   for key, value in self.index.items()}
        for key, value in self.state["files"].items():
            if value["node_token"] in self.nodes:
                entries[key] = {"path": key, "node_token": value["node_token"], "obj_token": value["obj_token"],
                                "requires": value["requires"], "revision": value["revision"]}
        revision = self.remote.revision(self.mother["obj_token"])
        blocks = self.remote.blocks(self.mother["obj_token"], revision)
        start = end = None
        for i, block in enumerate(blocks):
            heading = "".join(e.get("text_run", {}).get("content", "") for e in block.get("heading2", {}).get("elements", []))
            if heading == "票据索引":
                if start is not None:
                    raise SyncError("Duplicate managed mother-document index")
                start = i
            elif heading == "票据索引结束":
                end = i
        if (start is None) != (end is None) or (start is not None and end <= start):
            raise SyncError("Malformed mother-document index boundary")
        text = "## 票据索引\n\n```json\n" + json.dumps({"schema": "feishu-project/v1", "entries": list(entries.values())}, ensure_ascii=False, indent=2) + "\n```\n\n## 票据链接\n\n"
        text += "\n".join(f"- [{link_label(key)}]({self.node_url(item['node_token'])})" for key, item in sorted(entries.items()))
        text += "\n\n## 票据索引结束\n"
        selected = [b["block_id"] for b in blocks[start:end + 1]] if start is not None else None
        new_revision = self.remote.write(self.mother["obj_token"], text, revision, selected)
        check = self.remote.fetch(self.mother["obj_token"], new_revision)
        actual, _ = json_block(check, "feishu-project/v1")
        if actual.get("entries") != list(entries.values()):
            raise SyncError("Mother-document index verification failed")
        self.state.pop("index_revision", None)
        self.save()

    def push(self, selected, decisions):
        if not selected:
            raise SyncError("An explicit local upload scope is required")
        self.catalog()
        available = set()
        prefixes = [self.root / ".scratch", self.root / "docs/adr"]
        prefixes.extend((self.root / p).parent / "docs/adr" for p in self.context_paths)
        for prefix in prefixes:
            if prefix.is_dir():
                for folder, directories, files in os.walk(prefix, followlinks=False):
                    directories[:] = [d for d in directories if not (Path(folder) / d).is_symlink()
                                      and not (getattr((Path(folder) / d).lstat(), "st_file_attributes", 0) & 1024)
                                      and not d.startswith(".")]
                    for filename in files:
                        if not filename.endswith(".md"):
                            continue
                        name = (Path(folder) / filename).relative_to(self.root).as_posix()
                        try:
                            self.path(name)
                        except SyncError:
                            continue
                        available.add(name)
        for name in ("CONTEXT.md", "CONTEXT-MAP.md", "TODO.md", *self.context_paths):
            if self.path(name).is_file():
                available.add(name)
        if len({p.casefold() for p in available}) != len(available):
            raise SyncError("Local ticket paths collide on Windows")
        main = []
        for choice in selected:
            choice = choice.replace("\\", "/")
            matches = list(available) if choice == "." else [p for p in available if p == choice or p.startswith(choice.rstrip("/") + "/")]
            if not matches:
                raise SyncError("No allowed Markdown files in selected scope: " + choice)
            main.extend(sorted(matches))
        results, visiting = {}, set()

        def upload(name):
            if name in visiting:
                raise SyncError("Cyclic required dependencies")
            if name in results:
                if results[name]["status"] not in ("uploaded", "skipped"):
                    raise SyncError("Upload dependency failed: " + name)
                return
            visiting.add(name)
            try:
                source = self.path(name)
                body = read_text(source)
                portable(body)
                requires = self.local_dependencies(name, body, available)
                for dependency in requires:
                    upload(dependency)
                node = self.index.get(name)
                cached = self.state["files"].get(name, {})
                if cached and not node:
                    raise SyncError("Previously synchronized document is missing; do not recreate automatically")
                if node and node["node_token"] not in self.nodes:
                    raise SyncError("Previously indexed document disappeared; do not recreate automatically")
                if node:
                    revision = self.remote.revision(node["obj_token"])
                    if revision == cached.get("revision") and digest(body) == cached.get("local_hash") and requires == cached.get("requires"):
                        results[name] = {"status": "skipped"}
                        return
                    remote_meta, remote_body = parse_envelope(self.remote.fetch(node["obj_token"], revision))
                    if remote_meta["path"] != name:
                        raise SyncError("Remote ticket path changed")
                    remote_changed = digest(remote_body) != cached.get("remote_hash") or remote_meta["requires"] != cached.get("requires")
                    if remote_changed and decisions.get(name) != "local":
                        diff = "".join(difflib.unified_diff(remote_body.splitlines(True), body.splitlines(True), fromfile=name + " (Feishu)", tofile=name + " (local)"))
                        diff_path = self.run / (hashlib.sha256(name.encode()).hexdigest() + ".diff")
                        atomic_text(diff_path, diff)
                        raise SyncError("Remote/unpaired changes require HITL local choice; diff: " + str(diff_path))
                    blocks = self.remote.blocks(node["obj_token"], revision)
                    ids = [b["block_id"] for b in blocks]
                else:
                    parent = self.directory(str(PurePosixPath(name).parent))
                    matches = [n for n in self.nodes.values() if n.get("parent_node_token") == parent and n.get("title") == PurePosixPath(name).name]
                    pending = self.state["pending"].get(name)
                    if pending and pending.get("node_token"):
                        node = pending
                    elif matches:
                        raise SyncError("Unindexed node occupies ticket title; use catalog/HITL to pair it before creating")
                    elif pending:
                        raise SyncError("Previous create outcome unknown; inspect Wiki before retrying")
                    else:
                        self.state["pending"][name] = {"status": "creating", "parent_node_token": parent}
                        self.save()
                        node = self.remote.create(parent, PurePosixPath(name).name)
                        self.state["pending"][name] = node
                        self.save()
                        self.nodes[node["node_token"]] = {**node, "parent_node_token": parent}
                    revision = self.remote.revision(node["obj_token"])
                    blocks = self.remote.blocks(node["obj_token"], revision)
                    if blocks:
                        raise SyncError("Pending document is not empty; inspect partial write instead of appending again")
                    ids = None
                links = {p: self.node_url(self.state["files"][p]["node_token"]) for p in requires}
                payload = envelope(name, requires, body, links)
                if digest(read_text(source)) != digest(body):
                    raise SyncError("Local file changed during upload")
                new_revision = self.remote.write(node["obj_token"], payload, revision, ids)
                meta, remote_body = parse_envelope(self.remote.fetch(node["obj_token"], new_revision))
                if meta["path"] != name or meta["requires"] != requires:
                    raise SyncError("Uploaded header verification failed")
                if self.remote.revision(node["obj_token"]) != new_revision:
                    raise SyncError("Document changed during upload verification")
                self.accept(name, node, new_revision, body, remote_body, requires)
                self.index[name] = self.state["files"][name]
                results[name] = {"status": "uploaded", "url": self.node_url(node["node_token"])}
            except (SyncError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
                results[name] = {"status": "blocked", "error": str(error)}
                raise SyncError(str(error)) from error
            finally:
                visiting.discard(name)

        for name in dict.fromkeys(main):
            try:
                upload(name)
            except SyncError:
                visiting.clear()
        index_error = None
        if any(r["status"] == "uploaded" for r in results.values()):
            try:
                self.update_index()
            except (SyncError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
                index_error = str(error)
        self.progress("pushed", results=results, index_error=index_error)
        return {"main": list(dict.fromkeys(main)), "results": results, "index_error": index_error}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Local project already initialized by setup")
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind")
    bind.add_argument("--wiki-url", required=True)
    sub.add_parser("catalog")
    plan = sub.add_parser("plan-pull")
    plan.add_argument("--select", nargs="+", required=True)
    apply = sub.add_parser("apply-pull")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--decisions", help="HITL choices JSON: relative path -> local or remote")
    push = sub.add_parser("push")
    push.add_argument("--select", nargs="+", required=True, help="Local files or feature directories chosen by user")
    push.add_argument("--decisions", help="HITL choices JSON: relative path -> local")
    args = parser.parse_args()
    sync = None
    try:
        with project_lock(Path(args.root).resolve()):
            sync = Sync(args.root)
            if args.command == "bind":
                result = sync.bind(args.wiki_url)
            elif args.command == "catalog":
                result = sync.catalog()
            elif args.command == "plan-pull":
                result = sync.plan_pull(args.select)
            else:
                choices = json.loads(read_text(Path(args.decisions))) if args.decisions else {}
                if not isinstance(choices, dict):
                    raise SyncError("HITL choices must be a path-to-choice JSON object")
                result = sync.apply_pull(args.plan, choices) if args.command == "apply-pull" else sync.push(args.select, choices)
            attention = isinstance(result, dict) and (result.get("errors") or result.get("index_error") or any(r.get("status") == "blocked" for r in result.get("results", {}).values()))
            code = 2 if attention else 0
            summary = {"ok": not bool(attention), "progress": str(sync.log), "data": result}
            result_file = sync.run / "result.json"
            dump(result_file, summary)
            sync.progress("finished", result=str(result_file), exit_code=code)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return code
    except (SyncError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        if sync:
            sync.progress("failed", error=str(error))
        else:
            try:
                root = Path(args.root).resolve()
                startup = root / CACHE / ("start-" + str(os.getpid()) + ".json")
                ensure_inside(root, startup)
                if (root / CACHE).is_dir():
                    dump(startup, {"pid": os.getpid(), "time": datetime.now(timezone.utc).isoformat(), "event": "failed", "error": str(error)})
            except (OSError, SyncError):
                pass
        print(json.dumps({"ok": False, "error": str(error), "progress": str(sync.log) if sync else None}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
