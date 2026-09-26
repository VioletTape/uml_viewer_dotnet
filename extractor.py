"""
extractor.py - Extracts .NET code graph from .codegraph/codegraph.db
Filters out BCL noise and collapses NuGet dependencies ("Just My Code").
"""

import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

_RE_VALID_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$')


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

    def _is_ignored_external(self, name: str) -> bool:
        """Returns True if the external package/import should be ignored (internal code or BCL framework noise)."""
        if self.prefix and (name == self.prefix or name.startswith(self.prefix + ".")):
            return True
        if self.just_my_code:
            for ign in self.ignored_namespaces:
                if name == ign or name.startswith(ign + "."):
                    return True
        return False

    def extract(self) -> Dict:
        """Extracts nodes and edges from SQLite database."""
        conn = self._connect()
        try:
            return self._extract_internal(conn)
        finally:
            conn.close()

    def _extract_internal(self, conn: sqlite3.Connection) -> Dict:
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

        # Preload 'contains' edges so member -> class resolution is in-memory O(1) (eliminates N+1 queries)
        cursor.execute("SELECT target, source FROM edges WHERE kind = 'contains'")
        contains_parent_map: Dict[str, str] = {row["target"]: row["source"] for row in cursor.fetchall()}

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

            # Map method/member-level edges to parent class via in-memory contains_parent_map
            src_class_id = self._resolve_to_class(conn, src_id, nodes_by_id, contains_parent_map)
            tgt_class_id = self._resolve_to_class(conn, tgt_id, nodes_by_id, contains_parent_map)

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
        external_packages = self._extract_nuget_dependencies(conn, nodes_by_id, contains_parent_map)

        return {
            "project_path": self.project_path,
            "classes": classes,
            "edges": edges,
            "external_packages": external_packages
        }

    def _resolve_to_class(
        self,
        conn: Optional[sqlite3.Connection],
        node_id: str,
        nodes_by_id: Dict,
        contains_parent_map: Optional[Dict[str, str]] = None
    ) -> Optional[str]:
        """If node_id is already a class/interface, return it. Otherwise find its containing class."""
        if node_id in nodes_by_id:
            return node_id
        if contains_parent_map is not None:
            parent_id = contains_parent_map.get(node_id)
            if parent_id and parent_id in nodes_by_id:
                return parent_id
            return None
        # Fallback to database query if contains_parent_map is omitted
        if conn is not None:
            cursor = conn.cursor()
            cursor.execute("SELECT source FROM edges WHERE target = ? AND kind = 'contains'", (node_id,))
            row = cursor.fetchone()
            if row:
                parent_id = row[0]
                if parent_id in nodes_by_id:
                    return parent_id
        return None

    def _parse_csproj_packages(self) -> Dict[str, Dict]:
        """Scans project directory for .csproj files and extracts PackageReference entries."""
        packages = {}
        # Check for Central Package Management (CPM) Directory.Packages.props
        cpm_versions = {}
        props_path = os.path.join(self.project_path, "Directory.Packages.props")
        if os.path.isfile(props_path):
            try:
                tree = ET.parse(props_path)
                for pv in tree.findall(".//PackageVersion"):
                    pkg_name = pv.get("Include") or pv.get("Update")
                    ver = pv.get("Version")
                    if pkg_name and ver:
                        cpm_versions[pkg_name.lower()] = ver
            except Exception:
                pass

        pkg_name_map = {}
        for root, dirs, files in os.walk(self.project_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("bin", "obj", "node_modules")]
            for file in files:
                if file.endswith(".csproj"):
                    csproj_path = os.path.join(root, file)
                    try:
                        tree = ET.parse(csproj_path)
                        for pr in tree.findall(".//PackageReference"):
                            name = pr.get("Include") or pr.get("Update")
                            if not name:
                                continue
                            if self._is_ignored_external(name):
                                continue
                            version = pr.get("Version")
                            if not version:
                                ver_elem = pr.find("Version")
                                if ver_elem is not None and ver_elem.text:
                                    version = ver_elem.text
                            if not version and name.lower() in cpm_versions:
                                version = cpm_versions[name.lower()]
                            name_lower = name.lower()
                            if name_lower in pkg_name_map:
                                name = pkg_name_map[name_lower]
                            else:
                                pkg_name_map[name_lower] = name
                            if name not in packages:
                                packages[name] = {
                                    "package": name,
                                    "version_projects": defaultdict(set),
                                    "types": set(),
                                    "referencing_classes": set()
                                }
                            packages[name]["version_projects"][version or ""].add(
                                os.path.relpath(csproj_path, self.project_path)
                            )
                    except Exception:
                        pass
        return packages

    def _matches_package(self, pkg_name: str, import_name: str) -> bool:
        """Match a package's namespace and its children, never sibling packages."""
        # ponytail: namespace/package names are heuristic; assembly metadata is needed for aliases.
        pkg = pkg_name.lower()
        imp = import_name.lower()
        return imp == pkg or imp.startswith(pkg + ".")

    def _extract_nuget_dependencies(
        self,
        conn: sqlite3.Connection,
        nodes_by_id: Dict,
        contains_parent_map: Optional[Dict[str, str]] = None
    ) -> List[Dict]:
        """Extract external NuGet references aggregated by package name."""
        csproj_packages = self._parse_csproj_packages()

        # Build map of file_path -> class IDs
        file_to_classes = defaultdict(list)
        for class_id, node in nodes_by_id.items():
            fpath = node.get("file_path")
            if fpath:
                file_to_classes[os.path.normpath(fpath)].append(class_id)

        cursor = conn.cursor()
        cursor.execute("""
            SELECT reference_name, reference_kind, from_node_id, file_path
            FROM unresolved_refs
            WHERE reference_kind IN ('imports', 'extends', 'implements', 'references', 'calls', 'instantiates')
        """)

        # Sort packages descending by length once for fast prefix matching (longest subpackage takes precedence)
        sorted_packages = sorted(
            [(pkg.lower(), pkg) for pkg in csproj_packages],
            key=lambda x: len(x[0]),
            reverse=True
        ) if csproj_packages else []

        package_map = csproj_packages.copy()
        for row in cursor.fetchall():
            imp_name = row["reference_name"] or ""
            imp_name = imp_name.removeprefix("global::")
            if not _RE_VALID_IDENT.match(imp_name):
                continue
            if self._is_ignored_external(imp_name):
                continue
            is_import = row["reference_kind"] == "imports"
            if not is_import and (not csproj_packages or "." not in imp_name):
                continue

            if sorted_packages:
                imp_lower = imp_name.lower()
                pkg_name = None
                for pkg_low, orig_pkg in sorted_packages:
                    if imp_lower == pkg_low or imp_lower.startswith(pkg_low + "."):
                        pkg_name = orig_pkg
                        break
                if not pkg_name:
                    continue
            else:
                # Without manifests, only imports can supply inferred package names.
                parts = imp_name.split(".")
                pkg_name = ".".join(parts[:2]) if parts[0] in ("Microsoft", "System") else parts[0]
                if self._is_ignored_external(pkg_name):
                    continue
                package_map.setdefault(pkg_name, {
                    "version_projects": {}, "types": set(), "referencing_classes": set()
                })

            fpath = os.path.normpath(row["file_path"]) if row["file_path"] else ""
            class_ids = set(file_to_classes.get(fpath, [])) if is_import else set()
            src_class_id = self._resolve_to_class(conn, row["from_node_id"], nodes_by_id, contains_parent_map)
            if src_class_id:
                class_ids.add(src_class_id)
            data = package_map[pkg_name]
            data["types"].add(imp_name.split(".")[-1])
            data["referencing_classes"].update(class_ids)
            for cid in class_ids:
                if pkg_name not in nodes_by_id[cid]["external_refs"]:
                    nodes_by_id[cid]["external_refs"].append(pkg_name)

        result = []
        for pkg, data in sorted(package_map.items(), key=lambda x: (-len(x[1]["referencing_classes"]), x[0])):
            versions = sorted(v for v in data["version_projects"] if v)
            result.append({
                "package": pkg,
                "version": versions[0] if len(versions) == 1 else "",
                "versions": versions,
                "version_projects": {v: sorted(paths) for v, paths in sorted(data["version_projects"].items())},
                "types": sorted(data["types"])[:5],
                "referenced_by_count": len(data["referencing_classes"])
            })
        return result
