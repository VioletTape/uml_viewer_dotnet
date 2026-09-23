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
from typing import Dict, Optional, Set

from extractor import CodeGraphExtractor
from policy import ArchitecturePolicy
from metrics import QualityMetricsEngine
from headless_agent import HeadlessAgentWorker

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
            notify_all({"type": "reload", "reason": f"Change detected in {changed_file}"})


class ArchitectureHandler(http.server.SimpleHTTPRequestHandler):
    project_path = ""
    prefix = ""
    policy_path = ""
    agent_worker: Optional[HeadlessAgentWorker] = None

    def __init__(self, *args, **kwargs):
        frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
        super().__init__(*args, directory=frontend_dir, **kwargs)

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
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/reload":
            print("[LiveSync] Manual reload triggered via POST /api/reload")
            notify_all({"type": "reload", "reason": "Manual API trigger"})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
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

            abs_file = file_path if os.path.isabs(file_path) else os.path.join(self.project_path, file_path)
            ws_root = find_workspace_root(abs_file, self.project_path)
            distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu-24.04")
            wsl_url = f"vscode://vscode-remote/wsl+{distro}{abs_file}:{line}"

            code_bin = find_vs_code_binary()
            if not code_bin:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
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
            self.send_header("Access-Control-Allow-Origin", "*")
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
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def _get_mailbox_dir(self):
        d = os.path.join(self.project_path, ".uml-viewer")
        os.makedirs(d, exist_ok=True)
        return d

    def _get_agent_tasks(self):
        fpath = os.path.join(self._get_mailbox_dir(), "tasks.json")
        if os.path.isfile(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_agent_tasks(self, tasks):
        fpath = os.path.join(self._get_mailbox_dir(), "tasks.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(tasks, f, indent=2)

    def _get_proposals(self):
        fpath = os.path.join(self._get_mailbox_dir(), "proposals.json")
        if os.path.isfile(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        default_proposals = [{
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
        }]
        self._save_proposals(default_proposals)
        return default_proposals

    def _save_proposals(self, proposals):
        fpath = os.path.join(self._get_mailbox_dir(), "proposals.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(proposals, f, indent=2)

    def _handle_get_agent_tasks(self):
        tasks = self._get_agent_tasks()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(tasks, indent=2).encode("utf-8"))

    def _handle_post_agent_task(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body) if body else {}

            task = {
                "id": f"task-{int(time.time()*1000)}",
                "timestamp": datetime.datetime.now().isoformat(),
                "op": data.get("op", "fix_violation"),
                "title": data.get("title", "Architecture Copilot Task"),
                "prompt": data.get("prompt", ""),
                "target": data.get("target", {}),
                "status": "pending",
                "result": None
            }
            tasks = self._get_agent_tasks()
            tasks.insert(0, task)
            self._save_agent_tasks(tasks)

            print(f"[Copilot] New task queued: {task['id']} - {task['title']}")
            notify_all({"type": "agent_task_queued", "task": task})

            if ArchitectureHandler.agent_worker:
                ArchitectureHandler.agent_worker.trigger()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
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

            tasks = self._get_agent_tasks()
            found = False
            for t in tasks:
                if t.get("id") == task_id:
                    t["status"] = status
                    t["result"] = result
                    t["resolved_at"] = datetime.datetime.now().isoformat()
                    found = True
                    break

            if found:
                self._save_agent_tasks(tasks)
                notify_all({"type": "agent_task_updated", "task_id": task_id, "status": status})

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(proposals, indent=2).encode("utf-8"))

    def _handle_post_proposal(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            prop = json.loads(body) if body else {}

            if not prop.get("id"):
                prop["id"] = f"prop-{int(time.time()*1000)}"
            if not prop.get("created_at"):
                prop["created_at"] = datetime.datetime.now().isoformat()

            proposals = self._get_proposals()
            idx = next((i for i, p in enumerate(proposals) if p.get("id") == prop.get("id")), -1)
            if idx >= 0:
                proposals[idx] = prop
            else:
                proposals.append(prop)

            self._save_proposals(proposals)
            notify_all({"type": "proposals_updated", "proposal": prop})

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "proposal": prop}).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def _handle_get_graph(self, query=None):
        try:
            proposal_id = query.get("proposal_id", [""])[0] if query else ""
            selected_proposal = None
            if proposal_id:
                proposals = self._get_proposals()
                selected_proposal = next((p for p in proposals if p.get("id") == proposal_id), None)

            ext = CodeGraphExtractor(self.project_path, prefix=self.prefix)
            raw_graph = ext.extract()

            if self.policy_path and os.path.isfile(self.policy_path):
                policy = ArchitecturePolicy.load_from_file(self.policy_path)
            else:
                policy = ArchitecturePolicy()

            evaluated = policy.evaluate_graph(raw_graph, proposal=selected_proposal)

            metrics_engine = QualityMetricsEngine(self.project_path)
            enriched = metrics_engine.enrich_graph_with_metrics(evaluated)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(enriched, indent=2).encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def _handle_get_violations(self):
        try:
            ext = CodeGraphExtractor(self.project_path, prefix=self.prefix)
            raw_graph = ext.extract()
            policy = ArchitecturePolicy()
            evaluated = policy.evaluate_graph(raw_graph)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(evaluated.get("violations", []), indent=2).encode("utf-8"))
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

        abs_path = file_param if os.path.isabs(file_param) else os.path.join(self.project_path, file_param)

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
            self.send_header("Access-Control-Allow-Origin", "*")
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
    with http.server.ThreadingHTTPServer(("", port), ArchitectureHandler) as httpd:
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
