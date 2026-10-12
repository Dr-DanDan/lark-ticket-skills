import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/lark-push-tickets/scripts/ticket_sync.py"
spec = importlib.util.spec_from_file_location("ticket_sync", SCRIPT)
syncmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(syncmod)


class FakeLark:
    def __init__(self):
        self.docs = {"mother": {"content": "Project notes\n", "revision": 1}}
        self.nodes = {"root": {"node_token": "root", "obj_token": "mother", "obj_type": "docx", "space_id": "1"}}
        self.fetches, self.creates, self.writes = [], [], []
        self.fail = set()

    def node(self, token):
        return self.nodes["root"]

    def children(self, space, parent):
        return [{**n, "has_child": any(child.get("parent_node_token") == key for child in self.nodes.values())}
                for key, n in self.nodes.items() if n.get("parent_node_token") == parent]

    def revision(self, token):
        if token in self.fail:
            raise syncmod.SyncError("permission denied")
        return self.docs[token]["revision"]

    def fetch(self, token, revision):
        self.fetches.append(token)
        return self.docs[token]["content"]

    def blocks(self, token, revision):
        if not self.docs[token]["content"]:
            return []
        if token == "mother" and "feishu-project/v1" in self.docs[token]["content"]:
            return [{"block_id": "start", "heading2": {"elements": [{"text_run": {"content": "票据索引"}}]}},
                    {"block_id": "json"},
                    {"block_id": "end", "heading2": {"elements": [{"text_run": {"content": "票据索引结束"}}]}}]
        return [{"block_id": token + "-body"}]

    def create(self, parent, title):
        key = "n" + str(len(self.nodes))
        token = "d" + str(len(self.docs))
        node = {"node_token": key, "obj_token": token, "obj_type": "docx", "space_id": "1", "title": title, "parent_node_token": parent}
        self.nodes[key] = node
        self.docs[token] = {"content": "", "revision": 1}
        self.creates.append((parent, title))
        return node

    def write(self, token, text, revision, blocks=None):
        if self.docs[token]["revision"] != revision:
            raise syncmod.SyncError("revision mismatch")
        self.writes.append(token)
        self.docs[token]["content"] = text if blocks or not self.docs[token]["content"] else self.docs[token]["content"] + text
        self.docs[token]["revision"] += 1
        return self.docs[token]["revision"]

    def ticket(self, path, body="# Content\n", requires=()):
        node = self.create("root", Path(path).name)
        token = node["obj_token"]
        links = {p: "https://test.feishu.cn/wiki/ref" for p in requires}
        self.docs[token]["content"] = syncmod.envelope(path, requires, body, links)
        return node

    def index(self, records):
        content = {"schema": "feishu-project/v1", "entries": [{"path": p, **n} for p, n in records.items()]}
        self.docs["mother"]["content"] = "## 票据索引\n```json\n" + json.dumps(content) + "\n```\n## 票据索引结束\n"
        self.docs["mother"]["revision"] += 1


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="飞书 sync ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        folder = self.root / "docs/agents"
        folder.mkdir(parents=True)
        (folder / "domain.md").write_text("single-context", encoding="utf-8")
        (folder / "issue-tracker.md").write_text("local Markdown", encoding="utf-8")
        self.remote = FakeLark()
        self.sync = syncmod.Sync(self.root, self.remote)
        self.sync.bind("https://test.feishu.cn/wiki/root")

    def local(self, path, text):
        target = self.sync.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def apply(self, plan, choices=None):
        return self.sync.apply_pull(plan["plan"], choices or {})

    def test_selected_closure_only_and_zero_unchanged_body_fetches(self):
        spec_path = ".scratch/login/spec.md"
        first = ".scratch/login/issues/01-first.md"
        second = ".scratch/login/issues/02-second.md"
        records = {spec_path: self.remote.ticket(spec_path)}
        records[first] = self.remote.ticket(first, requires=[spec_path])
        records[second] = self.remote.ticket(second, requires=[spec_path])
        for i in range(1000):
            path = f".scratch/unselected/issues/{i:04}-unused.md"
            records[path] = self.remote.ticket(path)
        self.remote.index(records)
        plan = self.sync.plan_pull([first, second])
        self.assertEqual(set(plan["files"]), {first, second, spec_path})
        ticket_reads = [t for t in self.remote.fetches if t != "mother"]
        self.assertEqual(len(ticket_reads), 3)
        self.assertEqual(len(set(ticket_reads)), 3)
        result = self.apply(plan)
        self.assertEqual(result["results"][first]["status"], "written")
        self.remote.fetches.clear()
        plan = self.sync.plan_pull([first, second])
        self.apply(plan)
        self.assertEqual(self.remote.fetches, [])

    def test_dependency_failure_preserves_main_and_other_group_continues(self):
        path = ".scratch/a/issues/01-a.md"
        dep = ".scratch/a/spec.md"
        other = "docs/adr/0001-other.md"
        records = {path: self.remote.ticket(path, requires=[dep]), dep: self.remote.ticket(dep), other: self.remote.ticket(other)}
        self.remote.index(records)
        old = self.local(path, "old local\n")
        self.remote.fail.add(records[dep]["obj_token"])
        plan = self.sync.plan_pull([path, other])
        result = self.apply(plan)
        self.assertIn(path, plan["errors"])
        self.assertEqual(old.read_text(encoding="utf-8"), "old local\n")
        self.assertEqual(result["results"][other]["status"], "written")

    def test_conflict_needs_hitl_and_blocks_dependent_main(self):
        spec_path, issue = ".scratch/a/spec.md", ".scratch/a/issues/01-a.md"
        records = {spec_path: self.remote.ticket(spec_path, "remote spec\n"), issue: self.remote.ticket(issue, requires=[spec_path])}
        self.remote.index(records)
        self.local(spec_path, "local spec\n")
        plan = self.sync.plan_pull([issue])
        result = self.apply(plan)
        self.assertEqual(result["results"][issue]["status"], "blocked")
        self.assertFalse(self.sync.path(issue).exists())
        self.assertNotIn(spec_path, self.sync.state["files"])
        result = self.apply(plan, {spec_path: "remote"})
        self.assertEqual(result["results"][issue]["status"], "written")

    def test_local_or_remote_change_after_plan_is_not_overwritten(self):
        path = "TODO.md"
        node = self.remote.ticket(path, "remote\n")
        self.remote.index({path: node})
        plan = self.sync.plan_pull([path])
        self.local(path, "new local\n")
        result = self.apply(plan)
        self.assertEqual(result["results"][path]["status"], "blocked")
        self.assertEqual(self.sync.path(path).read_text(encoding="utf-8"), "new local\n")
        plan = self.sync.plan_pull([path])
        self.remote.docs[node["obj_token"]]["revision"] += 1
        result = self.apply(plan, {path: "remote"})
        self.assertEqual(result["results"][path]["status"], "blocked")

    def test_missing_local_dependency_is_repaired_even_at_same_revision(self):
        path, dep = ".scratch/a/issues/01-a.md", ".scratch/a/spec.md"
        records = {path: self.remote.ticket(path, requires=[dep]), dep: self.remote.ticket(dep)}
        self.remote.index(records)
        self.apply(self.sync.plan_pull([path]))
        old_dep = self.sync.path(dep)
        old_dep.rename(old_dep.with_suffix(".saved"))
        self.remote.fetches.clear()
        result = self.apply(self.sync.plan_pull([path]))
        self.assertEqual(result["results"][path]["status"], "reused")
        self.assertTrue(old_dep.exists())
        self.assertEqual([t for t in self.remote.fetches if t != "mother"], [records[dep]["obj_token"]])

    def test_path_validation_unicode_spaces_and_crlf(self):
        for invalid in ("../x.md", "C:/x.md", "//server/x.md", ".scratch/a/../../x.md", ".scratch/a/NUL.md", ".scratch/.feishu-sync/x.md", "docs/agents/x.md", "AGENTS.md", ".scratch/a/name. /x.md"):
            with self.subTest(invalid=invalid), self.assertRaises(syncmod.SyncError):
                self.sync.path(invalid)
        path = ".scratch/中文 feature/spec.md"
        file = self.local(path, "old\n")
        file.write_bytes(b"\xef\xbb\xbfold\r\n")
        syncmod.atomic_text(file, "new\n")
        self.assertEqual(file.read_bytes(), b"\xef\xbb\xbfnew\r\n")
        result = subprocess.run([sys_executable(), str(SCRIPT), "--root", str(self.root), "plan-pull"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--select", result.stderr)

    def test_frontmatter_code_checkboxes_and_unmarked_docs(self):
        body = "---\nstatus: ready\n---\n# Spec\n\n```python\nprint('<img>')\n```\n- [ ] done\n"
        payload = syncmod.envelope("TODO.md", [], body, {})
        _, restored = syncmod.parse_envelope(payload)
        self.assertEqual(restored, body)
        _, restored = syncmod.parse_envelope(payload.replace('```json', '```JSON').replace('```yaml', '```YAML'))
        self.assertEqual(restored, body)
        with self.assertRaises(syncmod.SyncError):
            syncmod.parse_envelope("# Ordinary note\n")
        with self.assertRaises(syncmod.SyncError):
            syncmod.envelope("TODO.md", [], "![image](file.png)", {})
        self.assertEqual(syncmod.restore_checkboxes('<checkbox done="true">Done</checkbox>'), "- [x] Done")

    def test_push_reuses_documents_and_blockers_and_does_not_recreate_missing(self):
        spec_path = ".scratch/a/spec.md"
        first = ".scratch/a/issues/01-first.md"
        second = ".scratch/a/issues/02-second.md"
        self.local(spec_path, "# Spec\n")
        self.local(first, "# First\n\n**Blocked by:** None — can start immediately\n")
        self.local(second, "# Second\n\n**Blocked by:** 01 — First\n")
        result = self.sync.push([second], {})
        self.assertEqual(result["results"][second]["status"], "uploaded")
        count = len(self.remote.creates)
        self.assertEqual(self.sync.state["files"][second]["requires"], [first, spec_path])
        again = self.sync.push([second], {})
        self.assertTrue(all(r["status"] == "skipped" for r in again["results"].values()))
        self.assertEqual(len(self.remote.creates), count)
        token = self.sync.state["files"][second]["node_token"]
        self.remote.nodes.pop(token)
        result = self.sync.push([second], {})
        self.assertEqual(result["results"][second]["status"], "blocked")
        self.assertEqual(len(self.remote.creates), count)

    def test_cycle_or_cross_project_dependency_does_not_write_main(self):
        a, b = "docs/adr/0001-a.md", "docs/adr/0002-b.md"
        nodes = {a: self.remote.ticket(a, requires=[b]), b: self.remote.ticket(b, requires=[a])}
        self.remote.index(nodes)
        plan = self.sync.plan_pull([a])
        self.assertIn(a, plan["errors"])
        self.assertFalse(self.sync.path(a).exists())
        self.remote.docs[nodes[a]["obj_token"]]["content"] = syncmod.envelope(a, ["docs/adr/9999-external.md"], "# A\n", {"docs/adr/9999-external.md": "https://test.feishu.cn/wiki/other"})
        plan = self.sync.plan_pull([a])
        self.assertIn(a, plan["errors"])

    def test_root_contexts_do_not_create_implicit_dependency_cycle(self):
        adr = "docs/adr/0001-a.md"
        records = {p: self.remote.ticket(p) for p in ("CONTEXT.md", "CONTEXT-MAP.md", adr)}
        self.remote.index(records)
        plan = self.sync.plan_pull([adr])
        self.assertEqual(plan["errors"], {})
        result = self.apply(plan)
        self.assertEqual(result["results"][adr]["status"], "written")

    def test_project_lock_and_last_moment_file_change(self):
        with syncmod.project_lock(self.root):
            with self.assertRaises(OSError):
                with syncmod.project_lock(self.root):
                    self.fail("Another worker acquired the same project lock")
        path = self.local("TODO.md", "edited\n")
        with self.assertRaises(syncmod.SyncError):
            syncmod.atomic_text(path, "replacement\n", expected_hash=syncmod.digest("old\n"))
        self.assertEqual(path.read_text(encoding="utf-8"), "edited\n")


def text_block(key, text):
    return {"block_id": key, "block_type": 2, "text": {"elements": [{"text_run": {"content": text}}]}}


class NativeLark(syncmod.Lark):
    def __init__(self, fail_insert=False):
        self.items = [text_block("notes", "outside notes"), text_block("meta", "same metadata"), text_block("body", "old body"), text_block("tail", "outside tail")]
        self.converted = [text_block("new-meta", "same metadata"), text_block("new-body", "new body")]
        self.version = 1
        self.operations = []
        self.fail_insert = fail_insert
        self.progress = lambda *a, **k: None

    def revision(self, token):
        return self.version

    def blocks(self, token, revision):
        return self.items[:]

    def call(self, *args, content=None):
        method, path = args[1], args[2]
        if path.endswith("/convert"):
            return {"blocks": self.converted, "first_level_block_ids": [b["block_id"] for b in self.converted]}
        if method == "GET":
            return {"items": self.items[:], "has_more": False}
        params = json.loads(args[args.index("--params") + 1])
        if params["document_revision_id"] != self.version:
            raise syncmod.SyncError("revision mismatch")
        data = json.loads(content)
        self.operations.append((method, data))
        if method == "POST":
            if self.fail_insert:
                raise syncmod.SyncError("insert failed")
            self.items[data["index"]:data["index"]] = [{**b, "block_id": "inserted-" + b["block_id"]} for b in data["descendants"]]
        elif method == "DELETE":
            del self.items[data["start_index"]:data["end_index"]]
        self.version += 1
        return {"document_revision_id": self.version}


class BlockUpdateTests(unittest.TestCase):
    def test_only_changed_blocks_replaced_and_outside_notes_preserved(self):
        remote = NativeLark()
        remote.write("doc", "new", 1, ["meta", "body"])
        self.assertEqual([b["block_id"] for b in remote.items], ["notes", "meta", "inserted-new-body", "tail"])
        self.assertEqual([op[0] for op in remote.operations], ["POST", "DELETE"])
        self.assertEqual(remote.operations[1][1], {"start_index": 3, "end_index": 4})

    def test_failed_insert_keeps_original_blocks(self):
        remote = NativeLark(fail_insert=True)
        before = remote.items[:]
        with self.assertRaises(syncmod.SyncError):
            remote.write("doc", "new", 1, ["meta", "body"])
        self.assertEqual(remote.items, before)
        self.assertEqual([op[0] for op in remote.operations], ["POST"])

    def test_equal_blocks_do_not_generate_mutations(self):
        remote = NativeLark()
        remote.converted[1] = text_block("new-body", "old body")
        version = remote.write("doc", "same", 1, ["meta", "body"])
        self.assertEqual(version, 1)
        self.assertEqual(remote.operations, [])


class CLIContractTests(unittest.TestCase):
    def client(self):
        client = syncmod.Lark.__new__(syncmod.Lark)
        client.binary = "lark-cli"
        client.root = Path.cwd()
        client.progress = lambda *a, **k: None
        return client

    def test_json_envelopes_and_unicode_arguments(self):
        client = self.client()
        payload = {"ok": True, "data": {"code": 0, "data": {"document": {"revision_id": 4}}}}
        with patch.object(syncmod.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")) as run:
            result = client.call("api", "GET", "/path", content="中文 $() [path]")
        self.assertEqual(result["document"]["revision_id"], 4)
        self.assertEqual(run.call_args.kwargs["input"], "中文 $() [path]")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_external_nonzero_and_nonobject_json_are_rejected(self):
        for result in (subprocess.CompletedProcess([], 5, "", "permission denied"), subprocess.CompletedProcess([], 0, "[]", "")):
            with self.subTest(result=result), patch.object(syncmod.subprocess, "run", return_value=result), self.assertRaises(syncmod.SyncError):
                self.client().call("api", "GET", "/path")


def sys_executable():
    import sys
    return sys.executable


if __name__ == "__main__":
    unittest.main()
