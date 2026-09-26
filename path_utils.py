"""
path_utils.py - Path filtering utilities for Clean Architecture & Stability analysis.
Excludes test projects and test folders from architectural and stability violations.
"""

import functools
import os
from typing import Optional


def _is_test_folder_name(name: str) -> bool:
    """
    Checks if a directory name signifies a test project or test directory.
    Matches 'test', 'tests' (with any case variations like Test, Tests, TEST, TESTS)
    and typical .NET conventions like 'SomeProject.Tests', 'SomeProject.Test',
    'Tests.Unit', 'UnitTests', 'IntegrationTests'.
    """
    n = name.lower().strip()
    if not n:
        return False
    if n in ("test", "tests", "unittest", "unittests", "integrationtest", "integrationtests"):
        return True
    # Dotted namespaces/folders e.g. "Billing.Tests", "Tests.Unit", "Order.Test"
    parts = n.split(".")
    if any(p in ("test", "tests", "unittest", "unittests", "integrationtest", "integrationtests") for p in parts):
        return True
    if n.startswith(("test-", "test_", "tests-", "tests_")):
        return True
    return False


@functools.lru_cache(maxsize=4096)
def is_test_path(path: Optional[str], project_path: Optional[str] = None) -> bool:
    """
    Determines if a given file or directory path is located inside a test folder
    (e.g., 'test', 'tests' with any case variations).
    
    If project_path is provided, inspects relative path segments within the project
    to avoid false positives if an upstream parent folder happens to contain 'test'.
    """
    if not path:
        return False

    normalized = os.path.normpath(str(path)).replace("\\", "/")

    # If project_path is supplied, evaluate relative directory components first
    if project_path:
        try:
            norm_proj = os.path.normpath(str(project_path)).replace("\\", "/")
            if os.path.isabs(normalized) and os.path.isabs(norm_proj):
                rel = os.path.relpath(normalized, norm_proj).replace("\\", "/")
                if not rel.startswith(".."):
                    rel_parts = [p for p in rel.split("/") if p and p != "."]
                    # If it has a file extension, inspect parent directory segments; otherwise inspect all segments
                    dir_parts = rel_parts[:-1] if os.path.splitext(normalized)[1] else rel_parts
                    for part in dir_parts:
                        if _is_test_folder_name(part):
                            return True
                    return False
        except Exception:
            pass

    # Fallback: check directory components of the path
    parts = [p for p in normalized.split("/") if p]
    dir_parts = parts[:-1] if os.path.splitext(normalized)[1] else parts

    # Check the last 4 directory levels above the file/folder
    for part in dir_parts[-4:]:
        if _is_test_folder_name(part):
            return True

    return False


def auto_detect_prefix(project_path: str) -> str:
    """Attempts to auto-detect root namespace prefix for a .NET project."""
    project_path = os.path.abspath(project_path)
    if not os.path.isdir(project_path):
        return ""

    # 1. Scan for .csproj files
    csproj_names = []
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in ("bin", "obj", ".git", "node_modules", ".vs", "TestResults", ".codegraph", ".uml-viewer")]
        for f in files:
            if f.endswith(".csproj") and not ("Test" in f or "test" in f):
                csproj_names.append(os.path.splitext(f)[0])

    if csproj_names:
        parts_list = [name.split(".") for name in csproj_names]
        if len(parts_list) == 1:
            return parts_list[0][0]
        common_parts = []
        for i, part in enumerate(parts_list[0]):
            if all(len(p) > i and p[i] == part for p in parts_list):
                common_parts.append(part)
            else:
                break
        if common_parts:
            return ".".join(common_parts)

    # 2. Scan for .sln
    try:
        sln_files = [f for f in os.listdir(project_path) if f.endswith(".sln")]
        if sln_files:
            return os.path.splitext(sln_files[0])[0]
    except OSError:
        pass

    # 3. Check SQLite db if exists
    db_path = os.path.join(project_path, ".codegraph", "codegraph.db")
    if os.path.isfile(db_path):
        try:
            import sqlite3
            from collections import Counter
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.cursor()
                cur.execute("SELECT namespace FROM symbols WHERE kind='class' AND namespace != '' LIMIT 100")
                rows = [r[0] for r in cur.fetchall() if r[0]]
            finally:
                conn.close()
            if rows:
                top_levels = [r.split(".")[0] for r in rows if not r.startswith("System") and not r.startswith("Microsoft")]
                if top_levels:
                    return Counter(top_levels).most_common(1)[0][0]
        except Exception:
            pass

    return ""
