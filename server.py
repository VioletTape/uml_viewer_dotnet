"""
server.py - Local HTTP server for .NET Architecture & UML Viewer.
Serves the web dashboard, REST API, and provides instant live-reload via Server-Sent Events (SSE).
"""

import argparse
import datetime
import glob
import http.server
import json
import os
import queue
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from extractor import CodeGraphExtractor
from policy import ArchitecturePolicy
from metrics import QualityMetricsEngine
from headless_agent import HeadlessAgentWorker
from mailbox_store import mailbox

DEFAULT_PORT = 5050


def find_workspace_root(file_path: str, fallback_root: str) -> str:
    """
    Finds the root workspace folder for VS Code by searching upward
    from the file for a solution (*.sln), repository (.git), or fallback_root.
    """
    if not os.path.isabs(file_path):
        file_path = os.path.join(fallback_root, file_path)

    curr = os.path.dirname(os.path.abspath(file_path))
    solution_folder = None
    git_folder = None

    while curr and curr != "/" and curr != os.path.dirname(curr):
        try:
            entries = os.listdir(curr)
            if any(e.endswith(".sln") for e in entries) and not solution_folder:
                solution_folder = curr
            if ".git" in entries and not git_folder:
                git_folder = curr
        except PermissionError:
            break
        curr = os.path.dirname(curr)

    return solution_folder or git_folder or fallback_root


def find_vs_code_binary() -> Optional[str]:
    """Finds VS Code executable on PATH or via standard WSL Windows mounts."""
    code_path = shutil.which("code")
    if code_path:
        return code_path

    candidates = [
        "/mnt/c/Users/agord/AppData/Local/Programs/Microsoft VS Code/bin/code",
    ]
    try:
        candidates.extend(glob.glob("/mnt/c/Users/*/AppData/Local/Programs/Microsoft VS Code/bin/code"))
        candidates.extend(glob.glob("/mnt/c/Program Files/Microsoft VS Code/bin/code"))
    except Exception:
        pass

    for c in candidates:
        if os.path.exists(c):
            return c
    return None

# Global SSE subscriber queues and file-watch state
subscribers: Set[queue.Queue] = set()
subscribers_lock = threading.Lock()
server_stopping = False


def notify_all(event_data: Dict):
    """Pushes an SSE event payload to all connected browser tabs."""
    with subscribers_lock:
        for q in list(subscribers):
            try:
                q.put_nowait(event_data)
            except Exception:
                pass



class GraphCacheManager:
    """Thread-safe in-memory cache for extracted graphs, evaluations, and metrics."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache_key: Optional[Tuple[str, str, str]] = None
        self._db_mtime: float = 0.0
        self._policy_mtime: float = 0.0
        self._raw_graph: Optional[Dict[str, Any]] = None
        self._base_evaluated: Optional[Dict[str, Any]] = None
        self._base_enriched_json: Optional[bytes] = None
        self._base_violations_json: Optional[bytes] = None
        self._proposal_cache: Dict[str, bytes] = {}

    def invalidate(self):
        """Explicitly clear all cached graph data."""
        with self._lock:
            self._cache_key = None
            self._raw_graph = None
            self._base_evaluated = None
            self._base_enriched_json = None
            self._base_violations_json = None
            self._proposal_cache.clear()

    @staticmethod
    def _safe_mtime(path: str) -> float:
        try:
            return os.path.getmtime(path) if path and os.path.isfile(path) else 0.0
        except Exception:
            return 0.0

    def _sync_mtimes_locked(self, project_path: str, prefix: str, policy_path: str) -> bool:
        """
        Checks if project_path/prefix/policy_path or on-disk mtimes have changed.
        Returns True if cache was invalidated.
        Must be called while holding self._lock.
        """
        current_key = (project_path, prefix, policy_path)
        db_path = os.path.join(project_path, ".codegraph", "codegraph.db")
        wal_path = os.path.join(project_path, ".codegraph", "codegraph.db-wal")
        current_db_mtime = max(self._safe_mtime(db_path), self._safe_mtime(wal_path))
        current_policy_mtime = self._safe_mtime(policy_path)

        if self._cache_key != current_key or self._db_mtime != current_db_mtime:
            self._cache_key = current_key
            self._db_mtime = current_db_mtime
            self._policy_mtime = current_policy_mtime
            self._raw_graph = None
            self._base_evaluated = None
            self._base_enriched_json = None
            self._base_violations_json = None
            self._proposal_cache.clear()
            return True
        elif self._policy_mtime != current_policy_mtime:
            self._policy_mtime = current_policy_mtime
            self._base_evaluated = None
            self._base_enriched_json = None
            self._base_violations_json = None
            self._proposal_cache.clear()
            return True
        return False

    def get_enriched_graph_json(
        self,
        project_path: str,
        prefix: str,
        policy_path: str,
        policy: ArchitecturePolicy,
        proposal: Optional[Dict] = None
    ) -> bytes:
        """Returns JSON bytes for the enriched graph (base or proposal-modified)."""
        prop_id = proposal.get("id") if proposal else ""
        with self._lock:
            self._sync_mtimes_locked(project_path, prefix, policy_path)

            if not prop_id and self._base_enriched_json is not None:
                return self._base_enriched_json
            if prop_id and prop_id in self._proposal_cache:
                return self._proposal_cache[prop_id]

            if self._raw_graph is None:
                ext = CodeGraphExtractor(project_path, prefix=prefix)
                self._raw_graph = ext.extract()

            if not prop_id:
                if self._base_evaluated is None:
                    self._base_evaluated = policy.evaluate_graph(self._raw_graph, proposal=None)
                if self._base_enriched_json is None:
                    metrics_engine = QualityMetricsEngine(project_path)
                    enriched = metrics_engine.enrich_graph_with_metrics(self._base_evaluated)
                    self._base_enriched_json = json.dumps(enriched, indent=2).encode("utf-8")
                    self._base_violations_json = json.dumps(self._base_evaluated.get("violations", []), indent=2).encode("utf-8")
                return self._base_enriched_json
            else:
                evaluated = policy.evaluate_graph(self._raw_graph, proposal=proposal)
                metrics_engine = QualityMetricsEngine(project_path)
                enriched = metrics_engine.enrich_graph_with_metrics(evaluated)
                prop_json = json.dumps(enriched, indent=2).encode("utf-8")
                self._proposal_cache[prop_id] = prop_json
                return prop_json

    def get_violations_json(
        self,
        project_path: str,
        prefix: str,
        policy_path: str,
        policy: ArchitecturePolicy
    ) -> bytes:
        """Returns JSON bytes for the list of violations."""
        with self._lock:
            self._sync_mtimes_locked(project_path, prefix, policy_path)

            if self._base_violations_json is not None:
                return self._base_violations_json

            if self._raw_graph is None:
                ext = CodeGraphExtractor(project_path, prefix=prefix)
                self._raw_graph = ext.extract()

            if self._base_evaluated is None:
                self._base_evaluated = policy.evaluate_graph(self._raw_graph, proposal=None)

            self._base_violations_json = json.dumps(self._base_evaluated.get("violations", []), indent=2).encode("utf-8")
            return self._base_violations_json


def file_watcher_loop(project_path: str, policy_path: str):
    """Background thread watching .codegraph/codegraph.db and policy.json for changes."""
    db_path = os.path.join(project_path, ".codegraph", "codegraph.db")
    wal_path = os.path.join(project_path, ".codegraph", "codegraph.db-wal")

    last_mtimes = {}

    def get_mtime(fpath):
        try:
            return os.path.getmtime(fpath) if os.path.isfile(fpath) else None
        except Exception:
            return None

    # Initial mtimes
    for p in (db_path, wal_path, policy_path):
        if p:
            last_mtimes[p] = get_mtime(p)

    while not server_stopping:
        time.sleep(1.0)
        changed = False
        changed_file = ""

        for p in (db_path, wal_path, policy_path):
            if not p:
                continue
            cur = get_mtime(p)
            prev = last_mtimes.get(p)
            if cur is not None and prev is not None and cur > prev:
                changed = True
                changed_file = os.path.basename(p)
                last_mtimes[p] = cur
            elif cur is not None and prev is None:
                last_mtimes[p] = cur

        if changed:
            # Short debounce to let file write settle
            time.sleep(0.3)
            # Re-read mtime
            for p in (db_path, wal_path, policy_path):
                if p:
                    last_mtimes[p] = get_mtime(p)
            print(f"[LiveSync] Detected change in {changed_file}. Pushing reload to connected browsers...")
            ArchitectureHandler.graph_cache.invalidate()
            notify_all({"type": "reload", "reason": f"Change detected in {changed_file}"})


class ArchitectureHandler(http.server.SimpleHTTPRequestHandler):
    project_path = ""
    prefix = ""
    policy_path = ""
    agent_worker: Optional[HeadlessAgentWorker] = None
    graph_cache = GraphCacheManager()

    def __init__(self, *args, **kwargs):
        frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
        super().__init__(*args, directory=frontend_dir, **kwargs)

    def parse_request(self):
        if not super().parse_request():
            return False
        port = self.server.server_port
        suffix = f":{port}" if port != 80 else ""
        hosts = {f"localhost{suffix}", f"127.0.0.1{suffix}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if (host not in hosts or
                (origin is not None and origin != f"http://{host}") or
                self.headers.get("Sec-Fetch-Site") == "cross-site"):
            self.send_error(403, "Only requests from the local viewer are allowed")
            return False
        return True

    def _source_path(self, file_path):
        root = Path(self.project_path).resolve()
        path = (root / file_path).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = None
        if (relative is None or path.suffix.lower() not in {".cs", ".csx"} or
                any(part.startswith(".") for part in relative.parts)):
            self.send_error(403, "Only C# source files inside the project are allowed")
            return None
        return str(path)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/graph":
            self._handle_get_graph(query)
        elif path == "/api/events":
            self._handle_sse_stream()
        elif path == "/api/file":
            self._handle_get_file(query)
        elif path == "/api/violations":
            self._handle_get_violations()
        elif path == "/api/agent/tasks":
            self._handle_get_agent_tasks()
        elif path == "/api/agent/proposals":
            self._handle_get_proposals()
        else:
            super().do_GET()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if not 0 <= length <= 1024 * 1024:
            self.send_error(413, "Request body too large")
            return
        if length and self.headers.get_content_type() != "application/json":
            self.send_error(415, "Expected application/json")
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/reload":
            print("[LiveSync] Manual reload triggered via POST /api/reload")
            self.graph_cache.invalidate()
            notify_all({"type": "reload", "reason": "Manual API trigger"})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok", "message": "Reload event broadcasted"}\n')
        elif parsed.path == "/api/open":
            self._handle_open_editor()
        elif parsed.path == "/api/agent/tasks":
            self._handle_post_agent_task()
        elif parsed.path == "/api/agent/tasks/resolve":
            self._handle_resolve_agent_task()
        elif parsed.path == "/api/agent/proposals":
            self._handle_post_proposal()
        elif parsed.path == "/api/stability/recheck":
            self._handle_stability_recheck()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_open_editor(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body) if body else {}

            file_path = data.get("file_path", "")
            line = data.get("line", 1)

            if not file_path:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Missing file_path"}\n')
                return

            abs_file = self._source_path(file_path)
            if abs_file is None:
                return
            ws_root = find_workspace_root(abs_file, self.project_path)
            distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu-24.04")
            wsl_url = f"vscode://vscode-remote/wsl+{distro}{abs_file}:{line}"

            code_bin = find_vs_code_binary()
            if not code_bin:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "fallback",
                    "wsl_url": wsl_url,
                    "error": "VS Code 'code' command not found. Use direct WSL URL."
                }).encode("utf-8"))
                return

            # Open folder and goto file:line
            cmd = [code_bin, "-r", ws_root, "-g", f"{abs_file}:{line}"]
            print(f"[Editor] Launching VS Code: {' '.join(cmd)}")
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "workspace_root": ws_root,
                "file_path": abs_file,
                "line": line,
                "wsl_url": wsl_url,
                "message": f"Opened {os.path.basename(abs_file)}:{line} (Workspace: {os.path.basename(ws_root)})"
            }).encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def _handle_sse_stream(self):
        """Streams Server-Sent Events to keep browser tabs live-updated."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # Initial handshake
        self.wfile.write(b"data: {\"type\": \"connected\", \"message\": \"Live-sync active\"}\n\n")
        self.wfile.flush()

        client_queue = queue.Queue()
        with subscribers_lock:
            subscribers.add(client_queue)

        try:
            while not server_stopping:
                try:
                    payload = client_queue.get(timeout=15.0)
                    line = f"data: {json.dumps(payload)}\n\n".encode("utf-8")
                    self.wfile.write(line)
                    self.wfile.flush()
                except queue.Empty:
                    # Keep-alive heartbeat comment
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with subscribers_lock:
                subscribers.discard(client_queue)

    def _get_agent_tasks(self):
        with mailbox(self.project_path, "tasks.json") as tasks:
            return tasks

    def _get_proposals(self):
        with mailbox(self.project_path, "proposals.json") as proposals:
            if not (Path(self.project_path) / ".uml-viewer" / "proposals.json").exists():
                proposals.extend([{
                    "id": "prop-invert-dataprovider",
                    "name": "Simulate: Decouple DataProvider",
                    "author": "AI Copilot",
                    "description": "Simulate introducing abstractions to decouple Domain interfaces from concrete Infrastructure DataProvider.",
                    "layer_overrides": {
                        "SubmittedTriplogService": "Infrastructure"
                    },
                    "omitted_edges": [
                        {"from": "IDataProvider", "to": "DataProvider"},
                        {"from": "IDataExecutor", "to": "DataExecutor"},
                        {"from": "ILegacyDataProvider", "to": "DataProvider"}
                    ],
                    "proposed_edges": [],
                    "created_at": datetime.datetime.now().isoformat()
                }])
            return proposals

    def _handle_get_agent_tasks(self):
        tasks = self._get_agent_tasks()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(tasks, indent=2).encode("utf-8"))

    def _handle_post_agent_task(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body) if body else {}

            task = {
                "id": f"task-{uuid.uuid4().hex}",
                "timestamp": datetime.datetime.now().isoformat(),
                "op": data.get("op", "fix_violation"),
                "title": data.get("title", "Architecture Copilot Task"),
                "prompt": data.get("prompt", ""),
                "target": data.get("target", {}),
                "status": "pending",
                "result": None
            }
            with mailbox(self.project_path, "tasks.json") as tasks:
                tasks.insert(0, task)

            print(f"[Copilot] New task queued: {task['id']} - {task['title']}")
            notify_all({"type": "agent_task_queued", "task": task})

            if ArchitectureHandler.agent_worker:
                ArchitectureHandler.agent_worker.trigger()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "task": task}).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def _handle_resolve_agent_task(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body) if body else {}

            task_id = data.get("task_id")
            status = data.get("status", "completed")
            result = data.get("result", "Task resolved by agent.")

            with mailbox(self.project_path, "tasks.json") as tasks:
                found = False
                for t in tasks:
                    if t.get("id") == task_id:
                        t["status"] = status
                        t["result"] = result
                        t["resolved_at"] = datetime.datetime.now().isoformat()
                        found = True
                        break

            if found:
                notify_all({"type": "agent_task_updated", "task_id": task_id, "status": status})

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok" if found else "not_found"}).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def _handle_get_proposals(self):
        proposals = self._get_proposals()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(proposals, indent=2).encode("utf-8"))

    def _handle_post_proposal(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            prop = json.loads(body) if body else {}

            if not prop.get("id"):
                prop["id"] = f"prop-{uuid.uuid4().hex}"
            if not prop.get("created_at"):
                prop["created_at"] = datetime.datetime.now().isoformat()

            with mailbox(self.project_path, "proposals.json") as proposals:
                idx = next((i for i, p in enumerate(proposals) if p.get("id") == prop.get("id")), -1)
                if idx >= 0:
                    proposals[idx] = prop
                else:
                    proposals.append(prop)

            self.graph_cache.invalidate()
            notify_all({"type": "proposals_updated", "proposal": prop})

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "proposal": prop}).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def _handle_stability_recheck(self):
        try:
            from stability_analyzer import StabilityAnalyzer
            from stability_store import StabilityStore

            StabilityStore(self.project_path).invalidate_cache()
            self.graph_cache.invalidate()
            analyzer = StabilityAnalyzer(self.project_path)

            prefix = getattr(self, "prefix", "") or ""
            classes = []
            try:
                ext = CodeGraphExtractor(self.project_path, prefix=prefix)
                raw_graph = ext.extract()
                classes = raw_graph.get("classes", [])
            except Exception:
                pass

            findings = analyzer.evaluate_project_stability(classes, force_recheck=True)

            notify_all({"type": "reload", "reason": "Stability recheck complete", "findings_count": len(findings)})

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "message": "Stability recheck complete",
                "findings_count": len(findings),
                "findings": findings
            }).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def _load_policy(self):
        if self.policy_path and os.path.isfile(self.policy_path):
            return ArchitecturePolicy.load_from_file(self.policy_path)
        return ArchitecturePolicy()

    def _handle_get_graph(self, query=None):
        try:
            proposal_id = query.get("proposal_id", [""])[0] if query else ""
            selected_proposal = None
            if proposal_id:
                proposals = self._get_proposals()
                selected_proposal = next((p for p in proposals if p.get("id") == proposal_id), None)

            policy = self._load_policy()
            graph_json_bytes = self.graph_cache.get_enriched_graph_json(
                self.project_path,
                self.prefix,
                self.policy_path,
                policy,
                proposal=selected_proposal
            )

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(graph_json_bytes)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def _handle_get_violations(self):
        try:
            policy = self._load_policy()
            violations_json_bytes = self.graph_cache.get_violations_json(
                self.project_path,
                self.prefix,
                self.policy_path,
                policy
            )

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(violations_json_bytes)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def _handle_get_file(self, query):
        file_param = query.get("path", [""])[0]
        if not file_param:
            self.send_response(400)
            self.end_headers()
            return

        abs_path = self._source_path(file_param)
        if abs_path is None:
            return

        if not os.path.isfile(abs_path):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"File not found: {abs_path}"}).encode("utf-8"))
            return

        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "path": abs_path,
                "content": content
            }).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


def auto_detect_prefix(project_path: str) -> str:
    """Attempts to auto-detect root namespace prefix for a .NET project."""
    project_path = os.path.abspath(project_path)
    if not os.path.isdir(project_path):
        return ""

    # 1. Scan for .csproj files
    csproj_names = []
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in ("bin", "obj", ".git", "node_modules", ".vs", "TestResults")]
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
    sln_files = [f for f in os.listdir(project_path) if f.endswith(".sln")]
    if sln_files:
        return os.path.splitext(sln_files[0])[0]

    # 3. Check SQLite db if exists
    db_path = os.path.join(project_path, ".codegraph", "codegraph.db")
    if os.path.isfile(db_path):
        try:
            import sqlite3
            from collections import Counter
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT namespace FROM symbols WHERE kind='class' AND namespace != '' LIMIT 100")
            rows = [r[0] for r in cur.fetchall() if r[0]]
            conn.close()
            if rows:
                top_levels = [r.split(".")[0] for r in rows if not r.startswith("System") and not r.startswith("Microsoft")]
                if top_levels:
                    return Counter(top_levels).most_common(1)[0][0]
        except Exception:
            pass

    return ""


def run_server(project_path: str, prefix: str = "", policy_path: str = "", port: int = DEFAULT_PORT):
    global server_stopping
    ArchitectureHandler.project_path = os.path.abspath(project_path)
    ArchitectureHandler.prefix = prefix or auto_detect_prefix(project_path)
    ArchitectureHandler.policy_path = os.path.abspath(policy_path) if policy_path else ""

    # Start file-watcher daemon thread
    watcher_thread = threading.Thread(
        target=file_watcher_loop,
        args=(ArchitectureHandler.project_path, ArchitectureHandler.policy_path),
        daemon=True
    )
    watcher_thread.start()

    # Start autonomous headless agent daemon
    ArchitectureHandler.agent_worker = HeadlessAgentWorker(
        ArchitectureHandler.project_path,
        notify_cb=notify_all
    )
    ArchitectureHandler.agent_worker.start()

    print(f"\n=======================================================")
    print(f" .NET Clean Architecture & UML Viewer")
    print(f"=======================================================")
    print(f" Project:   {ArchitectureHandler.project_path}")
    print(f" Prefix:    {ArchitectureHandler.prefix or '(all)'}")
    print(f" URL:       http://localhost:{port}")
    print(f" LiveSync:  Watching .codegraph/codegraph.db & policy")
    print(f" Agent:     Autonomous Headless Daemon Active")
    print(f"=======================================================\n")
    sys.stdout.flush()

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), ArchitectureHandler) as httpd:
        def shutdown_sig(sig, frame):
            nonlocal httpd
            global server_stopping
            print(f"\nReceived signal {sig}. Shutting down server...")
            server_stopping = True
            if ArchitectureHandler.agent_worker:
                ArchitectureHandler.agent_worker.stop()
            threading.Thread(target=httpd.shutdown).start()

        signal.signal(signal.SIGTERM, shutdown_sig)
        signal.signal(signal.SIGINT, shutdown_sig)

        try:
            httpd.serve_forever()
        except Exception:
            pass
        finally:
            server_stopping = True
            if ArchitectureHandler.agent_worker:
                ArchitectureHandler.agent_worker.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=".NET Clean Architecture & UML Viewer Server")
    parser.add_argument("project", nargs="?", default=".", help="Target project root directory (default: current directory)")
    parser.add_argument("prefix", nargs="?", default="", help="Namespace prefix (default: auto-detected)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--policy", default="", help="Path to policy.json file")

    args = parser.parse_args()
    proj = os.path.abspath(args.project)
    pfx = args.prefix or auto_detect_prefix(proj)
    run_server(proj, prefix=pfx, policy_path=args.policy, port=args.port)
