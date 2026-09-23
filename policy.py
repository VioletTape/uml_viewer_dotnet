"""
policy.py - Validates architecture against Clean Architecture layer rules.
Enforces:
  1. The Dependency Rule (Inner layers cannot depend on outer layers).
  2. Framework Isolation (Core domain layers cannot depend on UI/DB frameworks).
  3. Acyclic Dependencies Principle (ADP - no circular dependency cycles).
  4. Layer Placement Completeness (All project types must belong to an architectural layer).
  5. Configurable Allowed / Forbidden Dependency Whitelists & Blacklists.
"""

import json
import os
import re
from typing import Dict, List, Optional, Set, Tuple


class ArchitecturePolicy:
    def __init__(self, policy_data: Optional[Dict] = None):
        self.data = policy_data or self._default_policy()
        self.prefix = self.data.get("prefix", "")
        self.layers = self.data.get("layers", [])
        self.layer_ranks = {l["name"]: l.get("rank", i) for i, l in enumerate(self.layers)}
        self.allowed_deps = self.data.get("allowed_dependencies", {})
        self.forbidden_deps = self.data.get("forbidden_dependencies", {})
        self.forbidden_external = self.data.get("forbidden_external", self._default_forbidden_external())
        self.strict_completeness = self.data.get("strict_completeness", False)

    @classmethod
    def load_from_file(cls, path: str) -> "ArchitecturePolicy":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data)

    def _default_policy(self) -> Dict:
        """Sensible Clean Architecture default layers for .NET."""
        return {
            "name": "Clean Architecture Default",
            "prefix": "",
            "layers": [
                {
                    "name": "Domain",
                    "pattern": r"(^|\.)Domain(\.|$)",
                    "rank": 0,
                    "description": "Enterprise Business Rules (Entities, Value Objects, Domain Events)"
                },
                {
                    "name": "Contracts",
                    "pattern": r"(^|\.)Contracts(\.|$)",
                    "rank": 1,
                    "description": "Shared DTOs, Event contracts, and Port Interfaces"
                },
                {
                    "name": "Application",
                    "pattern": r"(^|\.)(Application|Services)(\.|$)",
                    "rank": 2,
                    "description": "Application Business Rules (Use Cases, Commands, Queries, Handlers)"
                },
                {
                    "name": "Infrastructure",
                    "pattern": r"(^|\.)Infrastructure(\.|$)",
                    "rank": 3,
                    "description": "Frameworks & Drivers (Databases, Message Brokers, External Services)"
                },
                {
                    "name": "Api",
                    "pattern": r"(^|\.)(Api|Controllers|Endpoints|Web)(\.|$)",
                    "rank": 4,
                    "description": "Interface Adapters (HTTP APIs, gRPC, Controllers, Host)"
                },
                {
                    "name": "Client",
                    "pattern": r"(^|\.)Client(\.|$)",
                    "rank": 5,
                    "description": "Client SDKs and consumer proxies"
                }
            ],
            "allowed_dependencies": {},
            "forbidden_dependencies": {
                "Domain": ["Infrastructure", "Api", "Client"]
            },
            "forbidden_external": self._default_forbidden_external()
        }

    def _default_forbidden_external(self) -> Dict[str, List[str]]:
        """Clean Architecture framework isolation boundaries."""
        return {
            "Domain": [
                r"(^|\.)Microsoft\.AspNetCore",
                r"(^|\.)Microsoft\.EntityFrameworkCore",
                r"(^|\.)Microsoft\.Data\.SqlClient",
                r"(^|\.)System\.Data",
                r"(^|\.)Dapper",
                r"(^|\.)Grpc",
                r"(^|\.)System\.Web"
            ],
            "Application": [
                r"(^|\.)Microsoft\.AspNetCore\.Mvc",
                r"(^|\.)Microsoft\.AspNetCore\.Hosting",
                r"(^|\.)Swashbuckle"
            ]
        }

    def assign_layer(self, namespace: str) -> Optional[str]:
        """Maps a namespace to a configured architectural layer."""
        for layer in self.layers:
            pattern = layer.get("pattern")
            if pattern and re.search(pattern, namespace):
                return layer["name"]
        return None

    def validate_dependency(self, from_layer: Optional[str], to_layer: Optional[str]) -> Tuple[bool, Optional[str]]:
        """
        Validates if from_layer is allowed to depend on to_layer according to Clean Architecture rules.
        Returns (is_valid, violation_reason).
        """
        if not from_layer or not to_layer:
            # Unassigned layers are handled by layer completeness checks
            return True, None

        if from_layer == to_layer:
            # Same layer dependencies are permitted
            return True, None

        # 1. Check explicit forbidden blacklist first
        if self.forbidden_deps and from_layer in self.forbidden_deps:
            forbidden = self.forbidden_deps[from_layer]
            if to_layer in forbidden:
                return False, f"Explicit Forbidden Dependency: Layer '{from_layer}' is forbidden from depending on '{to_layer}'"

        # 2. Check explicit whitelist if provided
        if self.allowed_deps and from_layer in self.allowed_deps:
            allowed = self.allowed_deps[from_layer]
            if to_layer in allowed:
                return True, None
            return False, f"Layer '{from_layer}' is not allowed to depend on '{to_layer}' (Explicit policy whitelist)"

        # 3. Apply Robert C. Martin's Dependency Rule:
        # Inner layers (lower rank) cannot depend on Outer layers (higher rank).
        from_rank = self.layer_ranks.get(from_layer, 999)
        to_rank = self.layer_ranks.get(to_layer, 999)

        if from_rank < to_rank:
            return False, (
                f"Dependency Rule Violation: Inner layer '{from_layer}' (rank {from_rank}) "
                f"cannot depend on outer layer '{to_layer}' (rank {to_rank})"
            )

        return True, None

    def check_framework_isolation(self, class_node: Dict, layer: str) -> List[Dict]:
        """
        Checks if an internal class in a protected layer (e.g. Domain) references forbidden external frameworks.
        """
        forbidden_rules = self.forbidden_external.get(layer, [])
        if not forbidden_rules:
            return []

        violations = []
        ext_refs = class_node.get("external_refs", [])
        for ref in ext_refs:
            for pattern in forbidden_rules:
                if pattern == ref or re.search(pattern, ref):
                    violations.append({
                        "category": "framework_taint",
                        "severity": "error",
                        "from_class": class_node.get("name", "Unknown"),
                        "from_namespace": class_node.get("namespace", "Unknown"),
                        "from_layer": layer,
                        "to_class": ref,
                        "to_namespace": ref,
                        "to_layer": "External Framework",
                        "kind": "framework_dependency",
                        "reason": (
                            f"Framework Isolation Violation: Core layer '{layer}' must not depend on "
                            f"external framework '{ref}' (Clean Architecture Rule: Independent of Frameworks)"
                        ),
                        "file_path": class_node.get("file_path"),
                        "line": class_node.get("start_line", 1)
                    })
                    break
        return violations

    def detect_dependency_cycles(self, classes: List[Dict], edges: List[Dict]) -> List[List[str]]:
        """
        Detects circular dependency cycles among classes (violating the Acyclic Dependencies Principle - ADP).
        Returns a list of cycles, each represented as a list of class IDs.
        """
        adj: Dict[str, Set[str]] = {c["id"]: set() for c in classes}
        for e in edges:
            u, v = e.get("from"), e.get("to")
            if u in adj and v in adj and u != v:
                adj[u].add(v)

        visited: Dict[str, int] = {}  # 0: unvisited, 1: visiting, 2: visited
        cycles: List[List[str]] = []
        path: List[str] = []
        seen_cycle_sets: Set[Tuple[str, ...]] = set()

        def dfs(node: str):
            visited[node] = 1
            path.append(node)
            for neighbor in adj.get(node, set()):
                st = visited.get(neighbor, 0)
                if st == 1:
                    idx = path.index(neighbor)
                    cycle = path[idx:] + [neighbor]
                    cycle_key = tuple(sorted(set(cycle)))
                    if cycle_key not in seen_cycle_sets:
                        seen_cycle_sets.add(cycle_key)
                        cycles.append(cycle)
                elif st == 0:
                    dfs(neighbor)
            path.pop()
            visited[node] = 2

        for c in classes:
            cid = c["id"]
            if visited.get(cid, 0) == 0:
                dfs(cid)

        return cycles

    def evaluate_graph(self, graph_data: Dict) -> Dict:
        """
        Assigns layers to all classes and validates all edges and Clean Architecture rules.
        Returns enriched graph data with violation flags and stats.
        """
        classes = graph_data.get("classes", [])
        edges = graph_data.get("edges", [])

        class_by_id = {}
        unassigned_classes = []

        for c in classes:
            layer = self.assign_layer(c["namespace"])
            c["layer"] = layer
            c["layer_rank"] = self.layer_ranks.get(layer) if layer else None
            class_by_id[c["id"]] = c
            if not layer:
                unassigned_classes.append(c)

        violations = []
        enriched_edges = []

        # 1. Dependency Rule validation across edges
        for e in edges:
            src = class_by_id.get(e["from"])
            tgt = class_by_id.get(e["to"])

            src_layer = src.get("layer") if src else None
            tgt_layer = tgt.get("layer") if tgt else None

            is_valid, reason = self.validate_dependency(src_layer, tgt_layer)

            edge_copy = dict(e)
            edge_copy["from_class"] = src["name"] if src else "Unknown"
            edge_copy["to_class"] = tgt["name"] if tgt else "Unknown"
            edge_copy["from_namespace"] = src["namespace"] if src else "Unknown"
            edge_copy["to_namespace"] = tgt["namespace"] if tgt else "Unknown"
            edge_copy["from_layer"] = src_layer
            edge_copy["to_layer"] = tgt_layer
            edge_copy["violating"] = not is_valid
            edge_copy["is_cycle"] = False

            if not is_valid:
                edge_copy["violation_reason"] = reason
                violations.append({
                    "category": "dependency_rule",
                    "severity": "error",
                    "from_class": src["name"] if src else "Unknown",
                    "to_class": tgt["name"] if tgt else "Unknown",
                    "from_namespace": src["namespace"] if src else "Unknown",
                    "to_namespace": tgt["namespace"] if tgt else "Unknown",
                    "from_layer": src_layer,
                    "to_layer": tgt_layer,
                    "kind": e["kind"],
                    "reason": reason,
                    "file_path": src.get("file_path") if src else None,
                    "line": e.get("line") or (src.get("start_line") if src else None)
                })

            enriched_edges.append(edge_copy)

        # 2. Framework Isolation validation (Domain cannot touch frameworks)
        for c in classes:
            layer = c.get("layer")
            if layer:
                taint_violations = self.check_framework_isolation(c, layer)
                violations.extend(taint_violations)

        # 3. Acyclic Dependencies Principle (ADP - Cycle detection)
        cycles = self.detect_dependency_cycles(classes, enriched_edges)
        for cycle in cycles:
            cycle_names = [class_by_id.get(cid, {}).get("name", cid) for cid in cycle]
            cycle_str = " -> ".join(cycle_names)
            first_class = class_by_id.get(cycle[0], {})

            # Mark edges involved in cycle
            for i in range(len(cycle) - 1):
                u, v = cycle[i], cycle[i + 1]
                for ee in enriched_edges:
                    if ee["from"] == u and ee["to"] == v:
                        ee["is_cycle"] = True

            violations.append({
                "category": "cycle",
                "severity": "error",
                "from_class": cycle_names[0],
                "to_class": cycle_names[1] if len(cycle_names) > 1 else cycle_names[0],
                "from_namespace": first_class.get("namespace", "Unknown"),
                "to_namespace": class_by_id.get(cycle[1], {}).get("namespace", "Unknown") if len(cycle) > 1 else "",
                "from_layer": first_class.get("layer"),
                "to_layer": class_by_id.get(cycle[1], {}).get("layer") if len(cycle) > 1 else None,
                "kind": "circular_dependency",
                "reason": f"Acyclic Dependencies Violation (ADP): Circular dependency cycle detected: {cycle_str}",
                "file_path": first_class.get("file_path"),
                "line": first_class.get("start_line", 1)
            })

        # 4. Layer Completeness validation (No unassigned types)
        for uc in unassigned_classes:
            violations.append({
                "category": "unassigned_layer",
                "severity": "error" if self.strict_completeness else "warning",
                "from_class": uc["name"],
                "to_class": "None",
                "from_namespace": uc["namespace"],
                "to_namespace": "",
                "from_layer": "Unassigned",
                "to_layer": None,
                "kind": "unplaced_type",
                "reason": f"Layer Completeness Warning: Type '{uc['name']}' in namespace '{uc['namespace']}' does not match any configured architectural layer",
                "file_path": uc.get("file_path"),
                "line": uc.get("start_line", 1)
            })

        # Summary statistics
        layers_summary = {}
        for c in classes:
            l = c.get("layer") or "Unassigned"
            layers_summary[l] = layers_summary.get(l, 0) + 1

        cat_summary = {}
        sev_summary = {"error": 0, "warning": 0}
        for v in violations:
            cat = v.get("category", "dependency_rule")
            sev = v.get("severity", "error")
            cat_summary[cat] = cat_summary.get(cat, 0) + 1
            sev_summary[sev] = sev_summary.get(sev, 0) + 1

        return {
            "project_path": graph_data.get("project_path"),
            "classes": classes,
            "edges": enriched_edges,
            "external_packages": graph_data.get("external_packages", []),
            "violations": violations,
            "unassigned_classes": [uc["name"] for uc in unassigned_classes],
            "stats": {
                "total_classes": len(classes),
                "total_edges": len(enriched_edges),
                "total_violations": len(violations),
                "violations_by_category": cat_summary,
                "violations_by_severity": sev_summary,
                "layers": layers_summary
            }
        }
