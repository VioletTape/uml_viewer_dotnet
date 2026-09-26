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

    def test_nuget_extraction_uses_csproj_and_ignores_garbage_expressions(self):
        # 1. Write a real .csproj file with package references
        csproj = self.project / "App.csproj"
        csproj.write_text("""<Project Sdk="Microsoft.NET.Sdk">
          <ItemGroup>
            <PackageReference Include="MassTransit.RabbitMQ" Version="8.5.9" />
            <PackageReference Include="Dapper" Version="2.1.79" />
            <PackageReference Include="Google.Protobuf" Version="3.0.0" />
            <PackageReference Include="Google.Cloud.Storage.V1" Version="4.0.0" />
            <PackageReference Include="Example" Version="1.0.0" />
            <PackageReference Include="Example.Transport" Version="1.0.0" />
          </ItemGroup>
        </Project>""")

        # 2. Setup SQLite database with valid nodes and unresolved_refs containing garbage
        (self.project / "C.cs").write_text("class C {}")
        (self.project / ".codegraph").mkdir(exist_ok=True)
        with sqlite3.connect(self.project / ".codegraph" / "codegraph.db") as db:
            db.executescript("""
                CREATE TABLE nodes (id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
                    file_path TEXT, start_line INT, end_line INT, is_abstract INT,
                    is_static INT, visibility TEXT, signature TEXT, docstring TEXT);
                CREATE TABLE edges (source TEXT, target TEXT, kind TEXT, line INT, col INT);
                CREATE TABLE unresolved_refs (reference_name TEXT, reference_kind TEXT,
                    from_node_id TEXT, file_path TEXT);
                INSERT INTO nodes VALUES ('c','class','C','App.Domain::C','C.cs',1,8,0,0,'public','','');
                -- Valid import corresponding to MassTransit package
                INSERT INTO unresolved_refs VALUES ('MassTransit.RabbitMQ', 'imports', 'c', 'C.cs');
                INSERT INTO unresolved_refs VALUES ('MassTransit.Testing', 'imports', 'c', 'C.cs');
                -- Fully qualified dependency, with no Dapper using directive
                INSERT INTO unresolved_refs VALUES ('global::Dapper.SqlMapper', 'references', 'c', 'C.cs');
                INSERT INTO unresolved_refs VALUES ('Google.Protobuf.Collections', 'imports', 'file', 'C.cs');
                INSERT INTO unresolved_refs VALUES ('Example.Transport.Options', 'imports', 'c', 'C.cs');
                INSERT INTO unresolved_refs VALUES ('Example.TransportExtra', 'imports', 'c', 'C.cs');
                -- A reference from C must not taint another class in the same file
                INSERT INTO nodes VALUES ('d','class','D','App.Domain::D','C.cs',9,10,0,0,'public','','');
                -- Garbage entries that codegraph generates
                INSERT INTO unresolved_refs VALUES ('services.AddMassTransit', 'calls', 'c', 'C.cs');
                INSERT INTO unresolved_refs VALUES ('((string)childAktor', 'calls', 'c', 'C.cs');
                INSERT INTO unresolved_refs VALUES ('(await LoadCompanyRow())', 'calls', 'c', 'C.cs');
                INSERT INTO unresolved_refs VALUES ('IServiceCollection', 'references', 'c', 'C.cs');
                INSERT INTO unresolved_refs VALUES ('Exception', 'references', 'c', 'C.cs');
            """)

        extractor = CodeGraphExtractor(str(self.project), prefix="App")
        graph = extractor.extract()

        pkgs = {p["package"]: p for p in graph["external_packages"]}
        # Real packages are extracted with their versions and referencing count
        self.assertIn("MassTransit.RabbitMQ", pkgs)
        self.assertEqual(pkgs["MassTransit.RabbitMQ"]["version"], "8.5.9")
        self.assertEqual(pkgs["MassTransit.RabbitMQ"]["referenced_by_count"], 2)
        self.assertNotIn("Testing", pkgs["MassTransit.RabbitMQ"]["types"])

        self.assertIn("Dapper", pkgs)
        self.assertEqual(pkgs["Dapper"]["version"], "2.1.79")
        self.assertEqual(pkgs["Dapper"]["referenced_by_count"], 1)
        self.assertEqual(pkgs["Google.Protobuf"]["referenced_by_count"], 2)
        self.assertEqual(pkgs["Google.Cloud.Storage.V1"]["referenced_by_count"], 0)
        self.assertEqual(pkgs["Example"]["types"], ["TransportExtra"])
        self.assertEqual(pkgs["Example.Transport"]["types"], ["Options"])
        classes = {c["id"]: c for c in graph["classes"]}
        self.assertIn("Dapper", classes["c"]["external_refs"])
        self.assertNotIn("Dapper", classes["d"]["external_refs"])
        policy = ArchitecturePolicy()
        policy.forbidden_external = {"Domain": ["Dapper"]}
        self.assertEqual(len(policy.check_framework_isolation(classes["c"], "Domain")), 1)

        # None of the AST call/variable rubbish is present as packages
        self.assertNotIn("services", pkgs)
        self.assertNotIn("((string)childAktor", pkgs)
        self.assertNotIn("(await LoadCompanyRow())", pkgs)
        self.assertNotIn("IServiceCollection", pkgs)
        self.assertNotIn("Exception", pkgs)

        # Package identity is case-insensitive; retain every version and its projects.
        (self.project / "Other.csproj").write_text('''<Project><ItemGroup>
          <PackageReference Include="dapper" Version="2.0.0" />
        </ItemGroup></Project>''')
        pkgs = {p["package"].lower(): p for p in extractor.extract()["external_packages"]}
        self.assertEqual(pkgs["dapper"]["version"], "")
        self.assertEqual(pkgs["dapper"]["versions"], ["2.0.0", "2.1.79"])
        self.assertEqual(pkgs["dapper"]["version_projects"], {
            "2.0.0": ["Other.csproj"], "2.1.79": ["App.csproj"]
        })

        # Fully qualified calls, base types and interfaces also retain the dependency.
        for kind in ("calls", "extends", "implements", "instantiates"):
            with self.subTest(kind=kind):
                with sqlite3.connect(extractor.db_path) as db:
                    db.execute("UPDATE unresolved_refs SET reference_kind = ? WHERE reference_name = ?",
                               (kind, "global::Dapper.SqlMapper"))
                pkgs = {p["package"].lower(): p for p in extractor.extract()["external_packages"]}
                self.assertEqual(pkgs["dapper"]["referenced_by_count"], 1)

    def test_infra_scanner_simple_yaml_parser_without_error(self):
        from infra_scanner import _parse_simple_yaml
        yaml_content = """
application:
  name: order-service
replicaCount: 3
environments:
  - production
"""
        parsed = _parse_simple_yaml(yaml_content)
        self.assertEqual(parsed.get("replicaCount"), 3)
        self.assertEqual(parsed.get("application", {}).get("name"), "order-service")

    def test_cobertura_xml_with_namespace(self):
        cobertura_xml = '''<?xml version="1.0" encoding="utf-8"?>
<coverage line-rate="0.85" branch-rate="0.75" version="1.9" xmlns="http://cobertura.sourceforge.net/xml/coverage-04.dtd">
  <packages>
    <package name="OrderService">
      <classes>
        <class name="OrderService.Order" filename="Order.cs" line-rate="0.85">
          <lines>
            <line number="10" hits="5" />
            <line number="11" hits="0" />
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>'''
        cov_file = self.project / "coverage.cobertura.xml"
        cov_file.write_text(cobertura_xml)
        engine = QualityMetricsEngine(str(self.project))
        self.assertIn("OrderService.Order", engine.class_coverage)
        self.assertEqual(engine.class_coverage["OrderService.Order"]["rate"], 0.85)

    def test_obj_substring_in_directory_name_not_pruned(self):
        from stability_store import StabilityStore
        obj_dir = self.project / "my-object-service"
        obj_dir.mkdir()
        (obj_dir / "Model.cs").write_text("class Model {}")

        # Actual build obj directory should be pruned
        build_obj = self.project / "obj"
        build_obj.mkdir()
        (build_obj / "Generated.cs").write_text("class Generated {}")

        store = StabilityStore(str(self.project))
        files = store._get_relevant_files()
        file_names = [Path(f).name for f in files]
        self.assertIn("Model.cs", file_names)
        self.assertNotIn("Generated.cs", file_names)

    def test_extractor_n_plus_one_elimination_and_connection_cleanup(self):
        # Create minimal codegraph db
        cg_dir = self.project / ".codegraph"
        cg_dir.mkdir(parents=True, exist_ok=True)
        db_path = cg_dir / "codegraph.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, is_abstract INT, is_static INT, visibility TEXT, signature TEXT, docstring TEXT)")
            conn.execute("CREATE TABLE edges (source TEXT, target TEXT, kind TEXT, line INT, col INT)")
            conn.execute("CREATE TABLE unresolved_refs (reference_name TEXT, reference_kind TEXT, from_node_id TEXT, file_path TEXT)")

            conn.execute("INSERT INTO nodes VALUES ('cls1', 'class', 'Order', 'Shop.Order', 'Order.cs', 1, 50, 0, 0, 'public', '', '')")
            conn.execute("INSERT INTO nodes VALUES ('cls2', 'class', 'Customer', 'Shop.Customer', 'Customer.cs', 1, 50, 0, 0, 'public', '', '')")
            conn.execute("INSERT INTO nodes VALUES ('m1', 'method', 'GetTotal', 'Shop.Order.GetTotal', 'Order.cs', 10, 20, 0, 0, 'public', 'int GetTotal()', '')")
            conn.execute("INSERT INTO edges VALUES ('cls1', 'm1', 'contains', 10, 0)")
            conn.execute("INSERT INTO edges VALUES ('m1', 'cls2', 'calls', 15, 0)")

        extractor = CodeGraphExtractor(str(self.project), prefix="Shop")
        # Direct test of _resolve_to_class using in-memory map without needing DB queries
        parent = extractor._resolve_to_class(None, "m1", {"cls1": {}}, {"m1": "cls1"})
        self.assertEqual(parent, "cls1")

        graph = extractor.extract()
        self.assertEqual(len(graph["classes"]), 2)
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["edges"][0]["from"], "cls1")
        self.assertEqual(graph["edges"][0]["to"], "cls2")

    def test_step3_metrics_and_policy_optimizations(self):
        # 1. Test QualityMetricsEngine complexity and file_coverage index
        cs_file = self.project / "ComplexService.cs"
        cs_content = """
public class ComplexService {
    public int Process(int x) {
        // Comment with if and while keywords
        /* Multi-line comment
           catch (Exception) {} */
        string str = "literal if && || ??";
        if (x > 0 && x < 100 || x == -1) {
            while (x < 10) x++;
            for (int i = 0; i < 5; i++) {}
            foreach (var item in new int[] {1}) {}
        }
        try {
            int val = x ?? 0;
            return val > 5 ? 1 : 0;
        } catch (Exception) {
            return -1;
        }
    }
}
"""
        cs_file.write_text(cs_content)
        engine = QualityMetricsEngine(str(self.project))
        comp = engine.calculate_method_complexity(str(cs_file), 3, 20)
        # Expected complexity:
        # Base: 1
        # if (1), && (1), || (1) -> +3
        # while (1) -> +1
        # for (1) -> +1
        # foreach (1) -> +1
        # ?? (1) -> +1
        # ? : (1) -> +1
        # catch (1) -> +1
        # Total = 1 + 3 + 1 + 1 + 1 + 1 + 1 + 1 = 10
        self.assertEqual(comp, 10)

        # 2. Test ArchitecturePolicy precompiled layer and cycle tagging
        policy = ArchitecturePolicy()
        self.assertEqual(policy.assign_layer("MyApp.Domain.Orders"), "Domain")
        self.assertEqual(policy.assign_layer("MyApp.Infrastructure.Data"), "Infrastructure")

        graph_data = {
            "project_path": str(self.project),
            "classes": [
                {"id": "c1", "name": "Order", "namespace": "MyApp.Domain.Orders", "file_path": "Order.cs"},
                {"id": "c2", "name": "Repo", "namespace": "MyApp.Infrastructure.Data", "file_path": "Repo.cs"}
            ],
            "edges": [
                {"from": "c1", "to": "c2", "kind": "dependency"},
                {"from": "c2", "to": "c1", "kind": "dependency"}
            ]
        }
        res = policy.evaluate_graph(graph_data)
        cycle_edges = [e for e in res["edges"] if e.get("is_cycle")]
        self.assertEqual(len(cycle_edges), 2)
        cycle_violations = [v for v in res["violations"] if v.get("category") == "cycle"]
        self.assertEqual(len(cycle_violations), 1)


if __name__ == "__main__":
    unittest.main()


