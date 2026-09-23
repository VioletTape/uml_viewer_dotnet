"""
server.py - Local HTTP server for .NET Architecture & UML Viewer.
Serves the web dashboard, REST API, and provides instant live-reload via Server-Sent Events (SSE).
"""

import glob
import http.server
import json
import os
import queue
import shutil
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

    def __init__(self, *args, **kwargs):
        frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
        super().__init__(*args, directory=frontend_dir, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/graph":
            self._handle_get_graph()
        elif path == "/api/events":
            self._handle_sse_stream()
        elif path == "/api/file":
            self._handle_get_file(query)
        elif path == "/api/violations":
            self._handle_get_violations()
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

    def _handle_get_graph(self):
        try:
            ext = CodeGraphExtractor(self.project_path, prefix=self.prefix)
            raw_graph = ext.extract()

            if self.policy_path and os.path.isfile(self.policy_path):
                policy = ArchitecturePolicy.load_from_file(self.policy_path)
            else:
                policy = ArchitecturePolicy()

            evaluated = policy.evaluate_graph(raw_graph)

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


def run_server(project_path: str, prefix: str = "", policy_path: str = "", port: int = DEFAULT_PORT):
    global server_stopping
    ArchitectureHandler.project_path = os.path.abspath(project_path)
    ArchitectureHandler.prefix = prefix
    ArchitectureHandler.policy_path = os.path.abspath(policy_path) if policy_path else ""

    # Start file-watcher daemon thread
    watcher_thread = threading.Thread(
        target=file_watcher_loop,
        args=(ArchitectureHandler.project_path, ArchitectureHandler.policy_path),
        daemon=True
    )
    watcher_thread.start()

    print(f"\n=======================================================")
    print(f" .NET Clean Architecture & UML Viewer")
    print(f"=======================================================")
    print(f" Project:   {ArchitectureHandler.project_path}")
    print(f" Prefix:    {prefix or '(auto-detect)'}")
    print(f" URL:       http://localhost:{port}")
    print(f" LiveSync:  Watching .codegraph/codegraph.db & policy")
    print(f"=======================================================\n")

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(("", port), ArchitectureHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")
            server_stopping = True


if __name__ == "__main__":
    proj = sys.argv[1] if len(sys.argv) > 1 else "/home/vt/projects/organizations"
    pfx = sys.argv[2] if len(sys.argv) > 2 else "OrgStructure"
    run_server(proj, prefix=pfx)
