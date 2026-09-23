"""
extractor.py - Extracts .NET code graph from .codegraph/codegraph.db
Filters out BCL noise and collapses NuGet dependencies ("Just My Code").
"""

import os
import re
import sqlite3
from typing import Dict, List, Optional, Set, Tuple


class CodeGraphExtractor:
    def __init__(self, project_path: str, prefix: str = "", just_my_code: bool = True):
        self.project_path = os.path.abspath(project_path)
        self.prefix = prefix
        self.just_my_code = just_my_code
        self.db_path = os.path.join(self.project_path, ".codegraph", "codegraph.db")

        if not os.path.isfile(self.db_path):
            raise FileNotFoundError(
                f"No codegraph database found at {self.db_path}. Run 'codegraph init' first."
            )

        # Standard noise to filter out in "Just My Code" mode
        self.ignored_namespaces = {
            "System", "System.Collections", "System.Collections.Generic", "System.Linq",
            "System.Threading", "System.Threading.Tasks", "System.IO", "System.Text",
            "Microsoft.Extensions", "Microsoft.Extensions.Logging", "Microsoft.Extensions.Options",
            "Microsoft.Extensions.DependencyInjection", "Microsoft.AspNetCore"
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _parse_qualified_name(self, qname: str) -> Tuple[str, str]:
        """Splits 'Namespace::TypeName' into ('Namespace', 'TypeName')."""
        if "::" in qname:
            ns, name = qname.split("::", 1)
            return ns, name
        # If no '::', last dot separates namespace and type
        if "." in qname:
            parts = qname.rsplit(".", 1)
            return parts[0], parts[1]
        return "", qname

    def _is_just_my_code(self, namespace: str, qname: str) -> bool:
        if not self.just_my_code:
            return True
        if self.prefix:
            return namespace == self.prefix or namespace.startswith(self.prefix + ".")
        # If no prefix specified, check against common framework namespaces
        for ign in self.ignored_namespaces:
            if namespace == ign or namespace.startswith(ign + "."):
                return False
        return True

    def extract(self) -> Dict:
        """Extracts nodes and edges from SQLite database."""
        conn = self._connect()
        cursor = conn.cursor()

        # 1. Fetch internal types (class, interface, struct, enum)
        cursor.execute("""
            SELECT id, kind, name, qualified_name, file_path, start_line, end_line,
                   is_abstract, is_static, visibility, signature, docstring
            FROM nodes
            WHERE kind IN ('class', 'interface', 'struct', 'enum')
        """)
        raw_nodes = cursor.fetchall()

        nodes_by_id = {}
        classes = []

        for row in raw_nodes:
            qname = row["qualified_name"] or row["name"]
            namespace, type_name = self._parse_qualified_name(qname)

            if not self._is_just_my_code(namespace, qname):
                continue

            node_data = {
                "id": row["id"],
                "name": type_name,
                "namespace": namespace,
                "qualified_name": qname,
                "kind": row["kind"],
                "stereotype": row["kind"] if row["kind"] != "class" else ("abstract" if row["is_abstract"] else None),
                "file_path": row["file_path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "visibility": row["visibility"],
                "is_abstract": bool(row["is_abstract"]),
                "is_static": bool(row["is_static"]),
                "members": [],
                "external_refs": [],
                "foreign": False
            }
            nodes_by_id[row["id"]] = node_data
            classes.append(node_data)

        # 2. Fetch members (methods, properties, fields) for internal types
        if nodes_by_id:
            cursor.execute("""
                SELECT n.id, n.kind, n.name, n.signature, n.visibility, n.start_line, n.end_line, e.source AS parent_id
                FROM nodes n
                JOIN edges e ON e.target = n.id
                WHERE e.kind = 'contains' AND n.kind IN ('method', 'property', 'field')
            """)
            for row in cursor.fetchall():
                parent_id = row["parent_id"]
                if parent_id in nodes_by_id:
                    nodes_by_id[parent_id]["members"].append({
                        "id": row["id"],
                        "kind": row["kind"],
                        "name": row["name"],
                        "signature": row["signature"],
                        "visibility": row["visibility"],
                        "line": row["start_line"],
                        "end_line": row["end_line"]
                    })

        # 3. Extract relationships between internal types
        cursor.execute("""
            SELECT source, target, kind, line, col
            FROM edges
            WHERE kind IN ('extends', 'implements', 'references', 'calls', 'instantiates')
        """)
        raw_edges = cursor.fetchall()

        edges = []
        edge_set = set()

        for row in raw_edges:
            src_id = row["source"]
            tgt_id = row["target"]
            kind = row["kind"]

            # Map method/member-level edges to parent class if necessary
            # (In codegraph, calls often come from method nodes)
            src_class_id = self._resolve_to_class(conn, src_id, nodes_by_id)
            tgt_class_id = self._resolve_to_class(conn, tgt_id, nodes_by_id)

            if not src_class_id or not tgt_class_id:
                continue
            if src_class_id == tgt_class_id:
                continue

            mapped_kind = "inheritance" if kind == "extends" else (
                "implements" if kind == "implements" else "dependency"
            )

            edge_key = (src_class_id, tgt_class_id, mapped_kind)
            if edge_key not in edge_set:
                edge_set.add(edge_key)
                edges.append({
                    "from": src_class_id,
                    "to": tgt_class_id,
                    "kind": mapped_kind,
                    "raw_kind": kind,
                    "line": row["line"]
                })

        # 4. Extract external NuGet dependencies from unresolved_refs
        external_packages = self._extract_nuget_dependencies(conn, nodes_by_id)

        conn.close()

        return {
            "project_path": self.project_path,
            "classes": classes,
            "edges": edges,
            "external_packages": external_packages
        }

    def _resolve_to_class(self, conn: sqlite3.Connection, node_id: str, nodes_by_id: Dict) -> Optional[str]:
        """If node_id is already a class/interface, return it. Otherwise find its containing class."""
        if node_id in nodes_by_id:
            return node_id
        # Check if parent is in nodes_by_id
        cursor = conn.cursor()
        cursor.execute("SELECT source FROM edges WHERE target = ? AND kind = 'contains'", (node_id,))
        row = cursor.fetchone()
        if row:
            parent_id = row[0]
            if parent_id in nodes_by_id:
                return parent_id
        return None

    def _extract_nuget_dependencies(self, conn: sqlite3.Connection, nodes_by_id: Dict) -> List[Dict]:
        """Extract external NuGet references aggregated by package name."""
        cursor = conn.cursor()
        cursor.execute("""
            SELECT reference_name, reference_kind, from_node_id, file_path
            FROM unresolved_refs
            WHERE reference_kind IN ('imports', 'extends', 'implements', 'references', 'calls')
        """)
        raw_refs = cursor.fetchall()

        package_map = {}
        for row in raw_refs:
            target = row["reference_name"] or ""
            # Extract root package name (e.g. MassTransit from MassTransit.RabbitMq)
            parts = target.split(".")
            if not parts or not parts[0]:
                continue
            root_pkg = parts[0]
            if len(parts) > 1 and parts[0] in ("Microsoft", "System"):
                root_pkg = f"{parts[0]}.{parts[1]}"

            # Ignore internal and standard framework
            if root_pkg in self.ignored_namespaces or (self.prefix and root_pkg.startswith(self.prefix)):
                continue

            if root_pkg not in package_map:
                package_map[root_pkg] = {
                    "package": root_pkg,
                    "types": set(),
                    "referencing_classes": set()
                }

            if len(parts) > 1:
                package_map[root_pkg]["types"].add(parts[-1])

            src_class_id = self._resolve_to_class(conn, row["from_node_id"], nodes_by_id)
            if src_class_id:
                package_map[root_pkg]["referencing_classes"].add(src_class_id)
                if src_class_id in nodes_by_id:
                    if root_pkg not in nodes_by_id[src_class_id]["external_refs"]:
                        nodes_by_id[src_class_id]["external_refs"].append(root_pkg)


        return [
            {
                "package": pkg,
                "types": sorted(list(data["types"]))[:5],  # Top types
                "referenced_by_count": len(data["referencing_classes"])
            }
            for pkg, data in package_map.items()
        ]
