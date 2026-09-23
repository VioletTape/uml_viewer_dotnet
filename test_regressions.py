"""Run with python3 -m unittest discover; all data and processes are isolated."""

from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch
from urllib.parse import urlencode

from extractor import CodeGraphExtractor
from headless_agent import HeadlessAgentWorker
from mailbox_store import mailbox
from metrics import QualityMetricsEngine
from policy import ArchitecturePolicy
from server import ArchitectureHandler, run_server
import uml_cli


class RegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.handler = type("TestHandler", (ArchitectureHandler,), {
            "project_path": str(self.project), "policy_path": "",
            "log_message": lambda *args: None,
        })

    def request(self, path, body=None, headers=None):
        payload = json.dumps(body).encode() if body is not None else b""
        fields = {"Host": "localhost:5050", "Content-Type": "application/json",
                  "Content-Length": str(len(payload)), **(headers or {})}
        method = "POST" if body is not None else "GET"
        request = f"{method} {path} HTTP/1.0\r\n"
        request += "".join(f"{key}: {value}\r\n" for key, value in fields.items())
        response = io.BytesIO()
        connection = SimpleNamespace(
            makefile=lambda *args: io.BytesIO(request.encode() + b"\r\n" + payload),
            sendall=response.write,
        )
        self.handler(connection, ("127.0.0.1", 12345), SimpleNamespace(server_port=5050))
        head, _, data = response.getvalue().partition(b"\r\n\r\n")
        return int(head.split()[1]), head, data

    def test_local_requests_and_source_file_boundaries(self):
        (self.project / "C.cs").write_text("class C {}")
        (self.root / "outside.cs").write_text("dummy outside source")
        (self.project / "link.cs").symlink_to(self.root / "outside.cs")
        (self.project / ".env").write_text("dummy credential")
        for source in ("C.cs", str(self.project / "C.cs")):
            status, headers, data = self.request("/api/file?" + urlencode({"path": source}))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data)["content"], "class C {}")
            self.assertNotIn(b"Access-Control-Allow-Origin", headers)
        for source in ("../outside.cs", str(self.root / "outside.cs"), "link.cs", ".env"):
            self.assertEqual(self.request("/api/file?" + urlencode({"path": source}))[0], 403)
            with patch("server.subprocess.Popen") as launch:
                self.assertEqual(self.request("/api/open", {"file_path": source})[0], 403)
                launch.assert_not_called()
        for headers in ({"Origin": "https://foreign.example"}, {"Origin": "null"},
                        {"Host": "foreign.example:5050"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("/api/agent/tasks", headers=headers)[0], 403)
            self.assertEqual(self.request("/api/agent/tasks", {}, headers=headers)[0], 403)
        self.assertEqual(self.request("/api/agent/tasks", {},
                                     {"Origin": "http://localhost:5050"})[0], 200)
        self.assertEqual(self.request("/api/agent/tasks", {},
                                     {"Content-Type": "text/plain"})[0], 415)
        with patch("server.find_vs_code_binary", return_value=None):
            self.assertEqual(self.request("/api/open", {"file_path": "C.cs"})[0], 200)

    def test_server_binds_only_loopback(self):
        with patch("server.http.server.ThreadingHTTPServer") as httpd, \
                patch("server.threading.Thread"), patch("server.HeadlessAgentWorker"), \
                patch("server.signal.signal"), patch("server.ArchitectureHandler.agent_worker"), \
                patch("server.ArchitectureHandler.project_path"), \
                patch("server.ArchitectureHandler.prefix"), \
                patch("server.ArchitectureHandler.policy_path"), patch("server.server_stopping"):
            run_server(str(self.project), prefix="App")
            self.assertEqual(httpd.call_args.args[0], ("127.0.0.1", 5050))

    def test_stop_does_not_signal_unrelated_processes(self):
        state = self.root / "daemon.json"
        with patch.object(uml_cli, "DAEMON_FILE", str(state)), patch("uml_cli.os.kill") as kill:
            uml_cli.cmd_stop()
            kill.assert_not_called()
            state.write_text(json.dumps({"pid": 12345}))
            with patch.object(uml_cli, "is_viewer_process", return_value=False):
                uml_cli.cmd_stop()
            kill.assert_not_called()
        for script, expected in (("/other/server.py", False), (uml_cli.SERVER_PY, True)):
            cmdline = b"python3\0" + script.encode() + b"\0/project\0"
            with patch("builtins.open", mock_open(read_data=cmdline)):
                self.assertEqual(uml_cli.is_viewer_process(12345), expected)
        with patch.object(uml_cli, "get_daemon_state", return_value={"pid": 12345}), \
                patch.object(uml_cli, "is_viewer_process", return_value=False), \
                patch.object(uml_cli, "remove_daemon_state"), patch("uml_cli.os.kill") as kill:
            uml_cli.cmd_stop()
            kill.assert_called_once_with(12345, uml_cli.signal.SIGTERM)

    def test_queue_survives_concurrent_posts_and_worker_completion(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda i: self.request("/api/agent/tasks", {"title": str(i)}), range(24)))
        self.assertTrue(all(status == 200 for status, _, _ in responses))
        worker = HeadlessAgentWorker(str(self.project))
        tasks = worker._get_tasks()
        self.assertEqual(len({t["id"] for t in tasks}), 24)
        # A new request lands between the worker's claim and completion transactions.
        with patch("headless_agent.time.sleep", side_effect=lambda _: self.request("/api/agent/tasks", {})):
            worker._process_single_task(tasks[0]["id"])
        tasks = worker._get_tasks()
        self.assertEqual(len(tasks), 25)
        self.assertEqual(sum(t["status"] == "completed" for t in tasks), 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: self.request("/api/agent/proposals", {"id": str(i)}), range(24)))
        with mailbox(self.project, "proposals.json") as proposals:
            self.assertEqual(len(proposals), 25)  # Includes the worker's proposal.
        path = self.project / ".uml-viewer" / "tasks.json"
        path.write_text("broken JSON")
        with self.assertRaises(json.JSONDecodeError):
            with mailbox(self.project, "tasks.json") as tasks:
                tasks.append({"id": "must-not-overwrite"})
        self.assertEqual(path.read_text(), "broken JSON")

    def test_extraction_preserves_method_body_for_metrics(self):
        (self.project / "C.cs").write_text(
            "class C {\n void M() {\n if (a) {}\n if (b) {}\n if (c) {}\n if (d) {}\n }\n}\n")
        (self.project / ".codegraph").mkdir()
        with sqlite3.connect(self.project / ".codegraph" / "codegraph.db") as db:
            db.executescript("""
                CREATE TABLE nodes (id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
                    file_path TEXT, start_line INT, end_line INT, is_abstract INT,
                    is_static INT, visibility TEXT, signature TEXT, docstring TEXT);
                CREATE TABLE edges (source TEXT, target TEXT, kind TEXT, line INT, col INT);
                CREATE TABLE unresolved_refs (reference_name TEXT, reference_kind TEXT,
                    from_node_id TEXT, file_path TEXT);
                INSERT INTO nodes VALUES ('c','class','C','App.Domain::C','C.cs',1,8,0,0,'public','','');
                INSERT INTO nodes VALUES ('m','method','M','App.Domain::C.M','C.cs',2,7,0,0,'public','','');
                INSERT INTO edges VALUES ('c','m','contains',2,0);
            """)
        graph = CodeGraphExtractor(str(self.project)).extract()
        member = QualityMetricsEngine(str(self.project)).enrich_graph_with_metrics(graph)["classes"][0]["members"][0]
        self.assertEqual(member["complexity"], 5)
        self.assertEqual(member["crap"], 30)

    def test_omitted_edges_break_cycles(self):
        graph = {"classes": [{"id": n, "name": n, "namespace": "App.Domain"} for n in ("A", "B")],
                 "edges": [{"from": "A", "to": "B", "kind": "dependency"},
                           {"from": "B", "to": "A", "kind": "dependency"}]}
        policy = ArchitecturePolicy()
        self.assertTrue(policy.evaluate_graph(copy.deepcopy(graph))["violations"])
        result = policy.evaluate_graph(graph, {"omitted_edges": [{"from": "A", "to": "B"}]})
        self.assertEqual(result["violations"], [])
        self.assertFalse(any(e["is_cycle"] for e in result["edges"]))

    def test_both_endpoints_use_custom_policy(self):
        policy = self.project / "policy.json"
        policy.write_text(json.dumps({"layers": [{"name": "Everything", "pattern": ".*", "rank": 0}]}))
        self.handler.policy_path = str(policy)
        graph = {"classes": [{"id": "c", "name": "C", "namespace": "App.Other", "members": [], "file_path": "C.cs"}], "edges": []}
        with patch("server.CodeGraphExtractor") as extractor:
            extractor.return_value.extract.side_effect = lambda: copy.deepcopy(graph)
            graph_response = self.request("/api/graph")
            violations_response = self.request("/api/violations")
        self.assertEqual(graph_response[0], 200)
        self.assertEqual(violations_response[0], 200)
        self.assertEqual(json.loads(graph_response[2])["violations"], [])
        self.assertEqual(json.loads(violations_response[2]), [])


if __name__ == "__main__":
    unittest.main()
