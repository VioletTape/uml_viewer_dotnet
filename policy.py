"""
policy.py - Validates architecture against Clean Architecture layer rules.
Flags dependency rule violations (inner layers depending on outer layers).
"""

import json
import os
import re
from typing import Dict, List, Optional, Tuple


class ArchitecturePolicy:
    def __init__(self, policy_data: Optional[Dict] = None):
        """
        Policy schema:
        {
            "name": "OrgStructure Architecture",
            "prefix": "OrgStructure",
            "layers": [
                {"name": "Domain", "pattern": r"^.*\.Domain(\..*)?$", "rank": 0},
                {"name": "Contracts", "pattern": r"^.*\.Contracts(\..*)?$", "rank": 1},
                {"name": "Infrastructure", "pattern": r"^.*\.Infrastructure(\..*)?$", "rank": 2},
                {"name": "Api", "pattern": r"^.*\.Api(\..*)?$", "rank": 3},
                {"name": "Client", "pattern": r"^.*\.Client(\..*)?$", "rank": 4}
            ],
            "allowed_dependencies": {
                # Optional explicit whitelist overriding rank order
            }
        }
        """
        self.data = policy_data or self._default_policy()
        self.prefix = self.data.get("prefix", "")
        self.layers = self.data.get("layers", [])
        self.layer_ranks = {l["name"]: l.get("rank", i) for i, l in enumerate(self.layers)}
        self.allowed_deps = self.data.get("allowed_dependencies", {})

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
                {"name": "Domain", "pattern": r"(^|\.)Domain(\.|$)", "rank": 0},
                {"name": "Contracts", "pattern": r"(^|\.)Contracts(\.|$)", "rank": 1},
                {"name": "Application", "pattern": r"(^|\.)(Application|Services)(\.|$)", "rank": 2},
                {"name": "Infrastructure", "pattern": r"(^|\.)Infrastructure(\.|$)", "rank": 3},
                {"name": "Api", "pattern": r"(^|\.)(Api|Controllers|Endpoints|Web)(\.|$)", "rank": 4},
                {"name": "Client", "pattern": r"(^|\.)Client(\.|$)", "rank": 5}
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
        Validates if from_layer is allowed to depend on to_layer.
        Returns (is_valid, violation_reason).
        """
        if not from_layer or not to_layer:
            # Unassigned layers are not checked for strict violations
            return True, None

        if from_layer == to_layer:
            # Same layer dependencies are always permitted
            return True, None

        # Check explicit whitelist if provided
        if self.allowed_deps and from_layer in self.allowed_deps:
            allowed = self.allowed_deps[from_layer]
            if to_layer in allowed:
                return True, None
            return False, f"Layer '{from_layer}' is not allowed to depend on '{to_layer}' (Explicit policy whitelist)"

        # Otherwise apply Clean Architecture rank rule:
        # Inner layers (lower rank) cannot depend on Outer layers (higher rank).
        from_rank = self.layer_ranks.get(from_layer, 999)
        to_rank = self.layer_ranks.get(to_layer, 999)

        if from_rank < to_rank:
            return False, (
                f"Dependency Rule Violation: Inner layer '{from_layer}' (rank {from_rank}) "
                f"cannot depend on outer layer '{to_layer}' (rank {to_rank})"
            )

        return True, None

    def evaluate_graph(self, graph_data: Dict) -> Dict:
        """
        Assigns layers to all classes and validates all edges.
        Returns enriched graph data with violation flags and stats.
        """
        classes = graph_data.get("classes", [])
        edges = graph_data.get("edges", [])

        class_by_id = {}
        for c in classes:
            layer = self.assign_layer(c["namespace"])
            c["layer"] = layer
            c["layer_rank"] = self.layer_ranks.get(layer) if layer else None
            class_by_id[c["id"]] = c

        violations = []
        enriched_edges = []

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

            if not is_valid:
                edge_copy["violation_reason"] = reason
                violations.append({
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

        # Summary statistics
        layers_summary = {}
        for c in classes:
            l = c.get("layer") or "Unassigned"
            layers_summary[l] = layers_summary.get(l, 0) + 1

        return {
            "project_path": graph_data.get("project_path"),
            "classes": classes,
            "edges": enriched_edges,
            "external_packages": graph_data.get("external_packages", []),
            "violations": violations,
            "stats": {
                "total_classes": len(classes),
                "total_edges": len(enriched_edges),
                "total_violations": len(violations),
                "layers": layers_summary
            }
        }
