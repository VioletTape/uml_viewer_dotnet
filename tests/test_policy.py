#!/usr/bin/env python3
"""
test_policy.py - Unit tests for Clean Architecture policy evaluation.
Covers:
  1. The Dependency Rule (inward dependencies).
  2. Framework Isolation (Domain cannot touch DB/Web frameworks).
  3. Acyclic Dependencies Principle (ADP - cycle detection).
  4. Layer Placement Completeness (no orphaned types).
  5. Configurable Allowed & Forbidden Whitelists/Blacklists.
"""

import unittest
from policy import ArchitecturePolicy


class TestArchitecturePolicy(unittest.TestCase):
    def setUp(self):
        self.policy = ArchitecturePolicy({
            "name": "Test Clean Architecture",
            "prefix": "MyApp",
            "layers": [
                {"name": "Domain", "pattern": r"(^|\.)Domain(\.|$)", "rank": 0},
                {"name": "Contracts", "pattern": r"(^|\.)Contracts(\.|$)", "rank": 1},
                {"name": "Application", "pattern": r"(^|\.)Application(\.|$)", "rank": 2},
                {"name": "Infrastructure", "pattern": r"(^|\.)Infrastructure(\.|$)", "rank": 3},
                {"name": "Api", "pattern": r"(^|\.)Api(\.|$)", "rank": 4}
            ],
            "allowed_dependencies": {},
            "forbidden_dependencies": {
                "Domain": ["Infrastructure", "Api"]
            },
            "forbidden_external": {
                "Domain": [r"^Microsoft\.AspNetCore", r"^Microsoft\.EntityFrameworkCore", r"^Dapper"]
            }
        })

    def test_layer_assignment(self):
        self.assertEqual(self.policy.assign_layer("MyApp.Domain.Entities"), "Domain")
        self.assertEqual(self.policy.assign_layer("MyApp.Application.UseCases"), "Application")
        self.assertEqual(self.policy.assign_layer("MyApp.Infrastructure.Data"), "Infrastructure")
        self.assertEqual(self.policy.assign_layer("MyApp.Api.Controllers"), "Api")
        self.assertIsNone(self.policy.assign_layer("Some.External.Namespace"))

    def test_dependency_rule_validation(self):
        # Outer layer depending on inner layer is allowed
        valid, reason = self.policy.validate_dependency("Application", "Domain")
        self.assertTrue(valid)
        self.assertIsNone(reason)

        # Same layer is allowed
        valid, reason = self.policy.validate_dependency("Domain", "Domain")
        self.assertTrue(valid)

        # Explicit forbidden blacklist rule
        valid, reason = self.policy.validate_dependency("Domain", "Infrastructure")
        self.assertFalse(valid)
        self.assertIn("Explicit Forbidden Dependency", reason or "")

        # Rank-based Dependency Rule violation (Contracts rank 1 -> Infrastructure rank 3)
        valid, reason = self.policy.validate_dependency("Contracts", "Infrastructure")
        self.assertFalse(valid)
        self.assertIn("Dependency Rule Violation", reason or "")

    def test_framework_isolation_violation(self):
        # Domain class referencing EntityFramework or AspNetCore
        tainted_class = {
            "name": "OrderEntity",
            "namespace": "MyApp.Domain.Entities",
            "external_refs": ["Microsoft.EntityFrameworkCore", "System.Guid"],
            "file_path": "src/Domain/Order.cs",
            "start_line": 10
        }
        violations = self.policy.check_framework_isolation(tainted_class, "Domain")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["category"], "framework_taint")
        self.assertEqual(violations[0]["to_class"], "Microsoft.EntityFrameworkCore")

    def test_acyclic_dependencies_cycle_detection(self):
        # A -> B -> A cycle
        classes = [
            {"id": "c1", "name": "ClassA", "namespace": "MyApp.Domain"},
            {"id": "c2", "name": "ClassB", "namespace": "MyApp.Domain"},
            {"id": "c3", "name": "ClassC", "namespace": "MyApp.Domain"}
        ]
        edges = [
            {"from": "c1", "to": "c2", "kind": "dependency"},
            {"from": "c2", "to": "c3", "kind": "dependency"},
            {"from": "c3", "to": "c1", "kind": "dependency"}
        ]
        cycles = self.policy.detect_dependency_cycles(classes, edges)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0], ["c1", "c2", "c3", "c1"])

    def test_evaluate_graph_comprehensive(self):
        graph_data = {
            "classes": [
                {
                    "id": "c_order",
                    "name": "Order",
                    "namespace": "MyApp.Domain.Entities",
                    "external_refs": ["Microsoft.AspNetCore.Http"],
                    "file_path": "src/Domain/Order.cs",
                    "start_line": 5
                },
                {
                    "id": "c_repo",
                    "name": "OrderRepository",
                    "namespace": "MyApp.Infrastructure.Data",
                    "external_refs": [],
                    "file_path": "src/Infrastructure/OrderRepository.cs",
                    "start_line": 8
                },
                {
                    "id": "c_orphan",
                    "name": "OrphanHelper",
                    "namespace": "Utilities.Random",
                    "external_refs": [],
                    "file_path": "src/Common/Orphan.cs",
                    "start_line": 1
                }
            ],
            "edges": [
                {
                    "from": "c_order",
                    "to": "c_repo",
                    "kind": "dependency",
                    "line": 12
                }
            ]
        }
        result = self.policy.evaluate_graph(graph_data)
        violations = result["violations"]
        categories = [v["category"] for v in violations]

        # 1. Dependency Rule: Domain -> Infrastructure
        self.assertIn("dependency_rule", categories)
        # 2. Framework Taint: Domain referencing Microsoft.AspNetCore.Http
        self.assertIn("framework_taint", categories)
        # 3. Layer Completeness: OrphanHelper has no layer
        self.assertIn("unassigned_layer", categories)

    def test_evaluate_graph_with_what_if_proposal(self):
        graph_data = {
            "classes": [
                {
                    "id": "c_order",
                    "name": "Order",
                    "namespace": "MyApp.Domain.Entities",
                    "external_refs": [],
                    "file_path": "src/Domain/Order.cs",
                    "start_line": 5
                },
                {
                    "id": "c_repo",
                    "name": "OrderRepository",
                    "namespace": "MyApp.Infrastructure.Data",
                    "external_refs": [],
                    "file_path": "src/Infrastructure/OrderRepository.cs",
                    "start_line": 8
                }
            ],
            "edges": [
                {
                    "from": "c_order",
                    "to": "c_repo",
                    "kind": "dependency",
                    "line": 12
                }
            ]
        }
        # Baseline without proposal has 1 violation
        res_baseline = self.policy.evaluate_graph(graph_data)
        self.assertEqual(len(res_baseline["violations"]), 1)

        # What-If proposal decoupling the two classes
        proposal = {
            "id": "prop-1",
            "name": "Decouple Order from Repo",
            "layer_overrides": {},
            "omitted_edges": [{"from": "Order", "to": "OrderRepository"}],
            "proposed_edges": []
        }
        res_proposed = self.policy.evaluate_graph(graph_data, proposal=proposal)
        # Violation is resolved!
        self.assertEqual(len(res_proposed["violations"]), 0)
        self.assertTrue(res_proposed["edges"][0]["is_omitted"])
        self.assertEqual(res_proposed["active_proposal"]["id"], "prop-1")


if __name__ == "__main__":
    unittest.main()
