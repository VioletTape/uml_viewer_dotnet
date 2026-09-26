#!/usr/bin/env python3
"""
test_stability.py - Comprehensive test suite for Nygard stability patterns.
Validates:
  1. Infra manifest scanner (override.yaml, workload.yaml, replicas, multi-instance).
  2. Stability cache mailbox (hashing, invalidation, zero-token persistence).
  3. Static & Roslyn stability pattern rules:
     - Unjittered retry ("Killed by the Mob" risk on multi-instance).
     - Unbounded / default 100s HttpClient timeout.
     - Missing CancellationToken on async integration calls.
     - Generic exception catch in retries.
  4. Integration into ArchitecturePolicy.evaluate_graph.
  5. Server API POST /api/stability/recheck.
  6. Headless Agent stability tasks and explanations.
  7. Roslyn C# tool standalone integration on SamplePatterns.cs.
"""

import io
import json
import os
import shutil
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from infra_scanner import InfraProfile, parse_yaml_file
from stability_store import StabilityStore, compute_file_hash
from stability_analyzer import StabilityAnalyzer
from policy import ArchitecturePolicy
from headless_agent import HeadlessAgentWorker
from mailbox_store import mailbox
from server import ArchitectureHandler


class TestInfraScanner(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_single_instance_default(self):
        profile = InfraProfile(self.test_dir)
        self.assertFalse(profile.is_multi_instance())
        self.assertEqual(profile.max_replicas, 1)

    def test_multi_instance_override_yaml(self):
        override_path = os.path.join(self.test_dir, "override.yaml")
        with open(override_path, "w", encoding="utf-8") as f:
            f.write("replicaCount: 3\nresources:\n  requests:\n    cpu: 0.5\n")

        profile = InfraProfile(self.test_dir)
        self.assertTrue(profile.is_multi_instance())
        self.assertEqual(profile.max_replicas, 3)
        self.assertEqual(profile.replica_by_env.get("default"), 3)

    def test_env_variation_staging_and_workload(self):
        with open(os.path.join(self.test_dir, "override.staging.yaml"), "w", encoding="utf-8") as f:
            f.write("replicaCount: 5\n")
        with open(os.path.join(self.test_dir, "workload.yaml"), "w", encoding="utf-8") as f:
            f.write("""application:
  name: test-svc
deployments:
  kubernetes:
    test-svc:
      environments:
        - staging
        - production
      healthcheck:
        timeoutSeconds: 4
""")

        profile = InfraProfile(self.test_dir)
        self.assertTrue(profile.is_multi_instance())
        self.assertEqual(profile.max_replicas, 5)
        self.assertIn("staging", profile.environments)
        self.assertIn("production", profile.environments)
        self.assertEqual(profile.healthcheck_timeout_seconds, 4)


class TestStabilityStore(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store = StabilityStore(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cache_miss_initially(self):
        self.assertFalse(self.store.is_cache_valid())
        self.assertEqual(self.store.get_findings(), [])

    def test_cache_valid_after_save(self):
        cs_file = os.path.join(self.test_dir, "Service.cs")
        with open(cs_file, "w", encoding="utf-8") as f:
            f.write("public class Service {}")

        findings = [{
            "category": "stability_rule",
            "kind": "unbounded_timeout",
            "from_class": "Service",
            "file_path": cs_file,
            "line": 1
        }]
        self.store.save_findings(findings)

        self.assertTrue(self.store.is_cache_valid())
        retrieved = self.store.get_findings()
        self.assertEqual(len(retrieved), 1)
        self.assertEqual(retrieved[0]["kind"], "unbounded_timeout")

    def test_cache_invalidates_on_file_change(self):
        cs_file = os.path.join(self.test_dir, "Service.cs")
        with open(cs_file, "w", encoding="utf-8") as f:
            f.write("public class Service {}")

        self.store.save_findings([{"kind": "test"}])
        self.assertTrue(self.store.is_cache_valid())

        # Modify file
        with open(cs_file, "a", encoding="utf-8") as f:
            f.write("\n// modified")

        self.assertFalse(self.store.is_cache_valid())


class TestRoslynStabilityAnalyzer(unittest.TestCase):
    def setUp(self):
        self.project_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.sample_cs = os.path.join(
            self.project_path, "tools", "RoslynStabilityAnalyzer", "Tests", "SamplePatterns.cs"
        )
        self.analyzer = StabilityAnalyzer(self.project_path)

    def test_find_roslyn_tool(self):
        roslyn_bin = self.analyzer._find_roslyn_tool()
        self.assertIsNotNone(roslyn_bin, "Roslyn tool binary or csproj should be found")
        self.assertTrue(os.path.exists(roslyn_bin), f"Roslyn tool path does not exist: {roslyn_bin}")

    def test_run_roslyn_tool_on_sample(self):
        output = self.analyzer.run_roslyn_tool([self.sample_cs])
        self.assertIsNotNone(output, "Roslyn analyzer output should not be None")
        self.assertIn("integration_points", output)
        self.assertIn("resilience_pipelines", output)
        self.assertIn("violations", output)

        # 1. Verify Integration Points
        integration_points = output["integration_points"]
        kinds = {ip["kind"] for ip in integration_points}
        self.assertIn("HttpClient", kinds)
        self.assertIn("DbContext", kinds)
        self.assertIn("DbConnection", kinds)
        self.assertIn("Redis", kinds)
        self.assertIn("MessageBus", kinds)

        # Verify explicit timeout detection on HttpClient
        unconfigured = next(ip for ip in integration_points if ip["name"] == "UnconfiguredClient")
        self.assertFalse(unconfigured["has_explicit_timeout"])

        configured = next(ip for ip in integration_points if ip["name"] == "TimeoutConfiguredClient")
        self.assertTrue(configured["has_explicit_timeout"])

        # 2. Verify Resilience Pipelines
        resilience = output["resilience_pipelines"]
        strategy_types = {rp["strategy_type"] for rp in resilience}
        self.assertIn("StandardResilience", strategy_types)
        self.assertIn("Retry", strategy_types)
        self.assertIn("Timeout", strategy_types)
        self.assertIn("ResiliencePipeline", strategy_types)

        # 3. Verify Violations
        violations = output["violations"]
        violation_kinds = {v["kind"] for v in violations}
        self.assertIn("missing_cancellation_token", violation_kinds)
        self.assertIn("default_timeout", violation_kinds)
        self.assertIn("catch_all_exception_retry", violation_kinds)
        self.assertIn("unjittered_retry", violation_kinds)

    def test_scout_suspicious_custom_resilience_loop(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cs", delete=False) as f:
            f.write("""
public class BespokeService
{
    private System.Net.Http.HttpClient _client = new System.Net.Http.HttpClient();

    public async System.Threading.Tasks.Task<string> SendWithNaiveRetry()
    {
        for (int attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                var response = await _client.PostAsync("https://api.example.com", null);
                return await response.Content.ReadAsStringAsync();
            }
            catch (System.Exception ex)
            {
                if (attempt == 2) throw;
                System.Threading.Thread.Sleep(1000);
            }
        }
        return null;
    }
}
""")
            temp_path = f.name

        try:
            output = self.analyzer.run_roslyn_tool([temp_path])
            self.assertIsNotNone(output)
            violations = output.get("violations", [])
            kinds = [v["kind"] for v in violations]
            self.assertIn("suspicious_custom_resilience", kinds)
            scouted = next(v for v in violations if v["kind"] == "suspicious_custom_resilience")
            self.assertIn("PostAsync", scouted["message"])
            self.assertIn("Thread.Sleep", scouted["message"])
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_scout_ignores_pagination_loop_without_delay_or_retry(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cs", delete=False) as f:
            f.write("""
public class PaginationService
{
    private System.Data.IDbConnection _db;

    public async System.Threading.Tasks.Task ReadAllPages()
    {
        int page = 0;
        while (page < 10)
        {
            var rows = await _db.ExecuteReaderAsync();
            page++;
        }
    }
}
""")
            temp_path = f.name

        try:
            output = self.analyzer.run_roslyn_tool([temp_path])
            self.assertIsNotNone(output)
            violations = output.get("violations", [])
            kinds = [v["kind"] for v in violations]
            self.assertNotIn("suspicious_custom_resilience", kinds)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


class TestStabilityAnalyzer(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        # Add override.yaml with 3 replicas to test "Killed by the Mob" detection
        with open(os.path.join(self.test_dir, "override.yaml"), "w", encoding="utf-8") as f:
            f.write("replicaCount: 3\n")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_unjittered_retry_detected_with_mob_risk(self):
        code = """
        public class PaymentClient
        {
            public void Configure()
            {
                var options = new RetryStrategyOptions
                {
                    MaxRetryAttempts = 3,
                    UseJitter = false
                };
            }
        }
        """
        cs_path = os.path.join(self.test_dir, "PaymentClient.cs")
        with open(cs_path, "w", encoding="utf-8") as f:
            f.write(code)

        analyzer = StabilityAnalyzer(self.test_dir)
        findings = analyzer.evaluate_project_stability([], force_recheck=True)

        retry_violations = [f for f in findings if f["kind"] == "unjittered_retry"]
        self.assertTrue(len(retry_violations) >= 1)
        v = retry_violations[0]
        self.assertEqual(v["severity"], "error")
        self.assertIn("Killed by the Mob", v["reason"])
        self.assertIn("3 replicas", v["reason"])

    def test_unbounded_http_client_timeout_detected(self):
        code = """
        public class WeatherClient
        {
            public void FetchData()
            {
                var client = new HttpClient();
            }
        }
        """
        cs_path = os.path.join(self.test_dir, "WeatherClient.cs")
        with open(cs_path, "w", encoding="utf-8") as f:
            f.write(code)

        analyzer = StabilityAnalyzer(self.test_dir)
        findings = analyzer.evaluate_project_stability([], force_recheck=True)

        timeout_violations = [f for f in findings if f["kind"] == "unbounded_timeout"]
        self.assertTrue(len(timeout_violations) >= 1)
        self.assertIn("100 seconds", timeout_violations[0]["reason"])

    def test_missing_cancellation_token_detected(self):
        code = """
        public class OrderRepository
        {
            public async Task SaveAsync(Order order)
            {
                await _context.SaveChangesAsync();
            }
        }
        """
        cs_path = os.path.join(self.test_dir, "OrderRepository.cs")
        with open(cs_path, "w", encoding="utf-8") as f:
            f.write(code)

        analyzer = StabilityAnalyzer(self.test_dir)
        findings = analyzer.evaluate_project_stability([], force_recheck=True)

        ct_violations = [f for f in findings if f["kind"] == "missing_cancellation_token"]
        self.assertTrue(len(ct_violations) >= 1)
        self.assertIn("CancellationToken", ct_violations[0]["reason"])

    def test_generic_exception_retry_detected(self):
        code = """
        public class NotificationService
        {
            public void Setup()
            {
                Policy.Handle<Exception>()
                      .WaitAndRetry(3, _ => TimeSpan.FromSeconds(2));
            }
        }
        """
        cs_path = os.path.join(self.test_dir, "NotificationService.cs")
        with open(cs_path, "w", encoding="utf-8") as f:
            f.write(code)

        analyzer = StabilityAnalyzer(self.test_dir)
        findings = analyzer.evaluate_project_stability([], force_recheck=True)

        exc_violations = [f for f in findings if f["kind"] == "generic_exception_retry"]
        self.assertTrue(len(exc_violations) >= 1)
        self.assertIn(exc_violations[0]["severity"], ("error", "warning"))


class TestPolicyIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        # Create a sample C# class with unbounded timeout
        with open(os.path.join(self.test_dir, "Client.cs"), "w", encoding="utf-8") as f:
            f.write("public class Client { public void M() { var client = new System.Net.Http.HttpClient(); } }")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_evaluate_graph_includes_stability_violations(self):
        policy = ArchitecturePolicy()
        raw_graph = {
            "project_path": self.test_dir,
            "classes": [{
                "id": "c1",
                "name": "Client",
                "namespace": "MyApp.Infrastructure",
                "file_path": os.path.join(self.test_dir, "Client.cs"),
                "start_line": 1
            }],
            "edges": [],
            "external_packages": []
        }

        evaluated = policy.evaluate_graph(raw_graph)
        self.assertIn("violations", evaluated)
        self.assertIn("stability_rule", evaluated["stats"]["violations_by_category"])


class TestServerStabilityEndpoint(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()

        # Write sample CS file in project
        with open(self.project / "Sample.cs", "w", encoding="utf-8") as f:
            f.write("public class Sample { public void Run() { var c = new System.Net.Http.HttpClient(); } }")

        self.handler = type("TestHandler", (ArchitectureHandler,), {
            "project_path": str(self.project),
            "policy_path": "",
            "log_message": lambda *args: None,
        })

    def request(self, path, body=None, headers=None):
        payload = json.dumps(body).encode() if body is not None else b""
        fields = {
            "Host": "localhost:5050",
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            **(headers or {})
        }
        method = "POST" if body is not None else "GET"
        req = f"{method} {path} HTTP/1.0\r\n"
        req += "".join(f"{key}: {value}\r\n" for key, value in fields.items())
        response = io.BytesIO()
        connection = SimpleNamespace(
            makefile=lambda *args: io.BytesIO(req.encode() + b"\r\n" + payload),
            sendall=response.write,
        )
        self.handler(connection, ("127.0.0.1", 12345), SimpleNamespace(server_port=5050))
        head, _, data = response.getvalue().partition(b"\r\n\r\n")
        return int(head.split()[1]), head, data

    def test_stability_recheck_endpoint(self):
        status, head, data = self.request("/api/stability/recheck", body={})
        self.assertEqual(status, 200, f"Response: {data.decode('utf-8', errors='ignore')}")
        res = json.loads(data.decode("utf-8"))
        self.assertEqual(res.get("status"), "ok")
        self.assertIn("findings_count", res)
        self.assertIn("findings", res)


class TestHeadlessAgentStabilityTasks(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.worker = HeadlessAgentWorker(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_explain_stability_violation(self):
        task = {
            "target": {
                "category": "stability_rule",
                "kind": "unjittered_retry",
                "from_class": "PaymentService",
                "from_layer": "Infrastructure",
                "reason": "Retry without jitter"
            }
        }
        explanation = self.worker._handle_explain_violation(task)
        self.assertIn("Killed by the Mob", explanation)
        self.assertIn("UseJitter = true", explanation)

    def test_fix_stability_violation_proposal(self):
        task = {
            "target": {
                "category": "stability_rule",
                "kind": "unbounded_timeout",
                "from_class": "InvoiceClient",
                "from_layer": "Infrastructure"
            }
        }
        res = self.worker._handle_fix_violation(task)
        self.assertIn("Resilience", res)
        # Verify proposal was created in mailbox
        with mailbox(self.test_dir, "proposals.json") as proposals:
            self.assertTrue(any("invoiceclient" in p["id"].lower() for p in proposals))

    def test_explain_suspicious_custom_resilience(self):
        task = {
            "target": {
                "category": "stability_rule",
                "kind": "suspicious_custom_resilience",
                "from_class": "LegacyPaymentAdapter",
                "from_layer": "Infrastructure",
                "reason": "contains blocking Thread.Sleep"
            }
        }
        explanation = self.worker._handle_explain_violation(task)
        self.assertIn("Threadpool Starvation Risk", explanation)
        self.assertIn("Polly v8", explanation)
        self.assertIn("Idempotency", explanation)

    def test_fix_suspicious_custom_resilience_proposal(self):
        task = {
            "target": {
                "category": "stability_rule",
                "kind": "suspicious_custom_resilience",
                "from_class": "LegacyPaymentAdapter",
                "from_layer": "Infrastructure"
            }
        }
        res = self.worker._handle_fix_violation(task)
        self.assertIn("Migrate Custom Loops", res)
        with mailbox(self.test_dir, "proposals.json") as proposals:
            self.assertTrue(any("legacypaymentadapter" in p["id"].lower() for p in proposals))


class TestExclusionOfTestFolders(unittest.TestCase):
    """
    Validates that test projects and folders ('test', 'tests', with any case variations,
    or *.Tests, *.Test) are completely excluded from stability and architecture violations.
    """
    def setUp(self):
        self.project_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.project_dir, ignore_errors=True)

    def test_is_test_path_case_variations(self):
        from path_utils import is_test_path

        # Case variations of 'test' and 'tests'
        for folder in ["test", "tests", "Test", "Tests", "TEST", "TESTS", "tEsTs"]:
            p = os.path.join(self.project_dir, folder, "PaymentServiceTests.cs")
            self.assertTrue(is_test_path(p, self.project_dir), f"Failed for {folder}")

        # Typical .NET test project conventions
        for proj_folder in ["Billing.Tests", "Billing.Test", "Billing.UnitTests", "Tests.Integration"]:
            p = os.path.join(self.project_dir, "src", proj_folder, "UnitTest1.cs")
            self.assertTrue(is_test_path(p, self.project_dir), f"Failed for {proj_folder}")

        # Production files should NEVER be marked as test paths
        for prod_folder in ["src/Domain", "src/Application", "src/Infrastructure", "src/Api"]:
            p = os.path.join(self.project_dir, prod_folder, "PaymentService.cs")
            self.assertFalse(is_test_path(p, self.project_dir), f"Failed for {prod_folder}")

    def test_parent_folder_named_test_does_not_cause_false_positive(self):
        from path_utils import is_test_path
        # If the project is checked out in a user directory like /home/test/projects/myproj
        fake_proj = "/home/test/projects/myproj"
        prod_file = "/home/test/projects/myproj/src/Domain/Order.cs"
        test_file = "/home/test/projects/myproj/tests/OrderTests.cs"

        self.assertFalse(is_test_path(prod_file, fake_proj))
        self.assertTrue(is_test_path(test_file, fake_proj))

    def test_stability_analyzer_ignores_test_folders(self):
        # Create a production file with an issue
        prod_dir = os.path.join(self.project_dir, "src", "Billing")
        os.makedirs(prod_dir, exist_ok=True)
        with open(os.path.join(prod_dir, "InvoiceService.cs"), "w", encoding="utf-8") as f:
            f.write("public class InvoiceService { public void Call() { var c = new System.Net.Http.HttpClient(); } }")

        # Create test files with issues across various test directory names
        for t_dir in ["tests", "Tests", "TEST", "MyProject.Tests"]:
            full_t_dir = os.path.join(self.project_dir, t_dir)
            os.makedirs(full_t_dir, exist_ok=True)
            with open(os.path.join(full_t_dir, "ServiceTests.cs"), "w", encoding="utf-8") as f:
                f.write("""
                public class ServiceTests {
                    public void TestLoop() {
                        var c = new System.Net.Http.HttpClient();
                        for (int i = 0; i < 3; i++) {
                            try { c.GetAsync("http://foo"); }
                            catch { System.Threading.Thread.Sleep(100); }
                        }
                    }
                }
                """)

        analyzer = StabilityAnalyzer(self.project_dir)
        findings = analyzer.evaluate_project_stability([], force_recheck=True)

        # There should only be findings for the production file, none for test folders
        self.assertTrue(len(findings) >= 1)
        for finding in findings:
            fpath = finding.get("file_path", "")
            self.assertNotIn("/tests/", fpath.replace("\\", "/"))
            self.assertNotIn("/Tests/", fpath.replace("\\", "/"))
            self.assertNotIn("/TEST/", fpath.replace("\\", "/"))
            self.assertNotIn("MyProject.Tests", fpath.replace("\\", "/"))
            self.assertIn("InvoiceService.cs", fpath)

    def test_policy_evaluator_ignores_test_folders(self):
        policy = ArchitecturePolicy()
        raw_graph = {
            "project_path": self.project_dir,
            "classes": [
                {
                    "id": "c_prod",
                    "name": "InvoiceService",
                    "namespace": "MyApp.Infrastructure",
                    "file_path": os.path.join(self.project_dir, "src", "Infrastructure", "InvoiceService.cs"),
                    "start_line": 1
                },
                {
                    "id": "c_test",
                    "name": "InvoiceServiceTests",
                    "namespace": "MyApp.Tests",
                    "file_path": os.path.join(self.project_dir, "tests", "InvoiceServiceTests.cs"),
                    "start_line": 1
                }
            ],
            "edges": [
                {
                    "from": "c_test",
                    "to": "c_prod",
                    "kind": "dependency",
                    "line": 5
                }
            ],
            "external_packages": []
        }

        evaluated = policy.evaluate_graph(raw_graph)
        violations = evaluated.get("violations", [])

        # The test class MyApp.Tests does not belong to any layer, but should NOT produce an unassigned_layer violation
        unassigned_violations = [v for v in violations if v.get("category") == "unassigned_layer"]
        self.assertEqual(len(unassigned_violations), 0)

        # The test dependency edge to Infrastructure should NOT produce a dependency_rule violation
        dep_violations = [v for v in violations if v.get("category") == "dependency_rule"]
        self.assertEqual(len(dep_violations), 0)


class TestClassLevelConsolidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.temp_dir, "MyProject")
        os.makedirs(os.path.join(self.project_dir, "src", "Infrastructure"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_dbaccess_methods_consolidated_into_single_class_violation(self):
        # Create a DBAccess class with multiple methods missing cancellation token
        db_path = os.path.join(self.project_dir, "src", "Infrastructure", "DBAccess.cs")
        with open(db_path, "w", encoding="utf-8") as f:
            f.write("""
            using System.Threading.Tasks;
            using System.Net.Http;

            namespace MyApp.Infrastructure
            {
                public class DBAccess
                {
                    private HttpClient _http = new HttpClient();

                    public async Task MethodA() {
                        await _http.GetAsync("http://service/a");
                    }

                    public async Task MethodB() {
                        await _http.GetAsync("http://service/b");
                    }

                    public async Task MethodC() {
                        await _http.PostAsync("http://service/c", null);
                    }
                }
            }
            """)

        graph_classes = [
            {
                "id": "c_db",
                "name": "DBAccess",
                "namespace": "MyApp.Infrastructure",
                "layer": "Infrastructure",
                "file_path": db_path,
                "start_line": 7,
                "external_refs": ["HttpClient"]
            }
        ]

        analyzer = StabilityAnalyzer(self.project_dir)
        findings = analyzer.evaluate_project_stability(graph_classes, force_recheck=True)

        # DBAccess should be consolidated into exactly ONE finding instead of 3-4 separate violations
        db_findings = [f for f in findings if f["from_class"] == "DBAccess"]
        self.assertEqual(len(db_findings), 1)

        finding = db_findings[0]
        self.assertEqual(finding["from_class"], "DBAccess")
        self.assertTrue(finding["occurrences"] >= 3)
        self.assertTrue(len(finding["details"]) >= 3)
        self.assertIn("DBAccess", finding["reason"])


if __name__ == "__main__":
    unittest.main()
