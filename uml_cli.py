#!/usr/bin/env python3
"""
uml - Global Command Line Interface for .NET Clean Architecture & UML Viewer.

Enables running from any folder:
  uml start [path] [prefix] [--port 5050]   Start background daemon
  uml stop                                 Stop running server
  uml status                               Show current status & URL
  uml restart [path] [prefix]              Restart server
  uml open                                 Open UI in web browser
  uml logs [-f]                            View/tail daemon logs
  uml index [path]                         Build/rebuild CodeGraph index
"""

import argparse
import datetime
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Dict, Optional, Tuple

APP_DIR = os.path.dirname(os.path.realpath(__file__))
SERVER_PY = os.path.join(APP_DIR, "server.py")
DAEMON_DIR = os.path.expanduser("~/.uml-viewer")
DAEMON_FILE = os.path.join(DAEMON_DIR, "daemon.json")
LOG_FILE = os.path.join(DAEMON_DIR, "server.log")


def ensure_daemon_dir():
    os.makedirs(DAEMON_DIR, exist_ok=True)


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_daemon_state() -> Optional[Dict]:
    if not os.path.isfile(DAEMON_FILE):
        return None
    try:
        with open(DAEMON_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        pid = state.get("pid")
        if pid and is_process_alive(pid):
            return state
        # Process is dead, remove stale file
        try:
            os.remove(DAEMON_FILE)
        except OSError:
            pass
        return None
    except Exception:
        return None


def save_daemon_state(state: Dict):
    ensure_daemon_dir()
    with open(DAEMON_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def remove_daemon_state():
    if os.path.isfile(DAEMON_FILE):
        try:
            os.remove(DAEMON_FILE)
        except OSError:
            pass


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def auto_detect_prefix(project_path: str) -> str:
    """Attempts to auto-detect root namespace prefix for a .NET project."""
    project_path = os.path.abspath(project_path)
    if not os.path.isdir(project_path):
        return ""

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

    sln_files = [f for f in os.listdir(project_path) if f.endswith(".sln")]
    if sln_files:
        return os.path.splitext(sln_files[0])[0]

    return ""


def check_and_build_index(project_path: str):
    """Ensures .codegraph/codegraph.db exists, building it if needed."""
    db_path = os.path.join(project_path, ".codegraph", "codegraph.db")
    if os.path.isfile(db_path):
        return True

    codegraph_bin = shutil.which("codegraph")
    if not codegraph_bin:
        print(f"⚠️  No CodeGraph index found at {db_path}, and 'codegraph' CLI is not found in PATH.")
        print(f"   Please initialize the index first: codegraph init {project_path}")
        return False

    print(f"⚡ Building initial CodeGraph index for: {project_path} ...")
    try:
        subprocess.run([codegraph_bin, "init", project_path], check=True)
        return os.path.isfile(db_path)
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to build codegraph index: {e}")
        return False


def cmd_start(args):
    project_path = os.path.abspath(args.path or os.getcwd())
    if not os.path.isdir(project_path):
        print(f"❌ Error: '{project_path}' is not a valid directory.")
        sys.exit(1)

    # Check for existing daemon
    active = get_daemon_state()
    if active:
        active_pid = active.get("pid")
        active_proj = active.get("project_path")
        active_port = active.get("port", 5050)
        active_url = active.get("url", f"http://localhost:{active_port}")

        if os.path.abspath(active_proj) == project_path:
            print(f"● UML Viewer is already running for this project!")
            print(f"  Project: {active_proj}")
            print(f"  URL:     {active_url}")
            print(f"  PID:     {active_pid}")
            print(f"\nType 'uml open' to view in browser, or 'uml restart' to restart.")
            return
        else:
            print(f"ℹ UML Viewer is currently running for another project: {active_proj} (PID {active_pid}).")
            print(f"  Switching daemon to {project_path}...")
            cmd_stop(argparse.Namespace(quiet=True))

    port = args.port or 5050
    if is_port_in_use(port):
        print(f"❌ Port {port} is already in use by another application.")
        print(f"   Specify an alternative port with: uml start --port <PORT>")
        sys.exit(1)

    # Check CodeGraph database
    check_and_build_index(project_path)

    # Auto-detect prefix
    prefix = args.prefix or auto_detect_prefix(project_path)

    ensure_daemon_dir()
    cmd = [
        sys.executable,
        SERVER_PY,
        project_path,
        prefix,
        "--port",
        str(port),
    ]

    if getattr(args, "foreground", False):
        print(f"Starting UML Viewer in foreground for {project_path}...")
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            pass
        return

    # Start background process
    log_fp = open(LOG_FILE, "a", encoding="utf-8")
    log_fp.write(f"\n--- UML Viewer daemon started at {datetime.datetime.now().isoformat()} ---\n")
    log_fp.flush()

    proc = subprocess.Popen(
        cmd,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=APP_DIR
    )

    # Allow a brief moment to verify startup
    time.sleep(0.6)
    if proc.poll() is not None:
        print(f"❌ Failed to start server daemon (process exited with code {proc.returncode}).")
        print(f"   Check logs: {LOG_FILE}")
        sys.exit(1)

    url = f"http://localhost:{port}"
    state = {
        "pid": proc.pid,
        "project_path": project_path,
        "prefix": prefix,
        "port": port,
        "url": url,
        "started_at": datetime.datetime.now().isoformat()
    }
    save_daemon_state(state)

    print("\n=======================================================")
    print(" 🚀 UML Viewer Daemon Started!")
    print("=======================================================")
    print(f" Project:   {project_path}")
    print(f" Prefix:    {prefix or '(all)'}")
    print(f" URL:       {url}")
    print(f" PID:       {proc.pid}")
    print(f" Log File:  {LOG_FILE}")
    print("=======================================================")
    print(" Commands:")
    print("   uml open     - Open dashboard in browser")
    print("   uml status   - Check daemon status")
    print("   uml logs -f  - Follow server logs")
    print("   uml stop     - Stop the server daemon\n")


def cmd_stop(args=None):
    quiet = getattr(args, "quiet", False) if args else False
    state = get_daemon_state()

    if not state:
        # Check if any orphan server.py is running
        try:
            out = subprocess.check_output(["pgrep", "-f", "server.py"], text=True)
            pids = [int(p) for p in out.strip().split() if p.isdigit()]
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            if pids and not quiet:
                print(f"✔ Stopped orphan UML Viewer processes (PIDs: {', '.join(map(str, pids))}).")
                remove_daemon_state()
                return
        except Exception:
            pass

        if not quiet:
            print("○ UML Viewer is not currently running.")
        return

    pid = state.get("pid")
    port = state.get("port", 5050)

    if not quiet:
        print(f"Stopping UML Viewer (PID {pid})...", end="", flush=True)

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not is_process_alive(pid):
                break
            time.sleep(0.1)
        else:
            # Force kill if SIGTERM timed out
            os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass

    remove_daemon_state()

    if not quiet:
        print(f" ✔ Stopped.")


def cmd_status(args):
    state = get_daemon_state()
    if not state:
        print("○ UML Viewer is stopped.")
        print("  Type 'uml start' to launch in the current directory.")
        return

    pid = state.get("pid")
    project = state.get("project_path")
    prefix = state.get("prefix")
    url = state.get("url")
    started = state.get("started_at")

    print("● UML Viewer is RUNNING")
    print(f"  PID:      {pid}")
    print(f"  Project:  {project}")
    print(f"  Prefix:   {prefix or '(all)'}")
    print(f"  URL:      {url}")
    print(f"  Started:  {started}")
    print(f"  Logs:     {LOG_FILE}")


def cmd_restart(args):
    state = get_daemon_state()
    path = args.path or (state.get("project_path") if state else os.getcwd())
    prefix = args.prefix or (state.get("prefix") if state else "")
    port = args.port or (state.get("port") if state else 5050)

    cmd_stop(argparse.Namespace(quiet=True))
    time.sleep(0.3)
    cmd_start(argparse.Namespace(path=path, prefix=prefix, port=port, foreground=False))


def cmd_open(args):
    state = get_daemon_state()
    url = state.get("url") if state else "http://localhost:5050"

    if not state:
        print("⚠️  UML Viewer is not running. Starting it now in current directory...")
        cmd_start(argparse.Namespace(path=os.getcwd(), prefix="", port=5050, foreground=False))
        state = get_daemon_state()
        url = state.get("url") if state else "http://localhost:5050"

    print(f"Opening {url} ...")
    # Try WSL cmd.exe start first if in WSL
    opened = False
    if os.path.exists("/mnt/c/WINDOWS/system32/cmd.exe"):
        try:
            subprocess.run(["cmd.exe", "/c", "start", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            opened = True
        except Exception:
            pass

    if not opened and shutil.which("xdg-open"):
        try:
            subprocess.run(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            opened = True
        except Exception:
            pass

    if not opened:
        import webbrowser
        webbrowser.open(url)


def cmd_logs(args):
    if not os.path.isfile(LOG_FILE):
        print(f"No log file found at {LOG_FILE}.")
        return

    follow = getattr(args, "follow", False)
    lines = getattr(args, "lines", 40)

    if follow:
        try:
            subprocess.run(["tail", "-f", "-n", str(lines), LOG_FILE])
        except KeyboardInterrupt:
            pass
    else:
        try:
            subprocess.run(["tail", "-n", str(lines), LOG_FILE])
        except Exception:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                content = f.readlines()
                print("".join(content[-lines:]))


def cmd_index(args):
    project_path = os.path.abspath(args.path or os.getcwd())
    codegraph_bin = shutil.which("codegraph")
    if not codegraph_bin:
        print("❌ 'codegraph' CLI was not found in PATH.")
        sys.exit(1)

    print(f"Building/rebuilding index for {project_path} ...")
    subprocess.run([codegraph_bin, "index", project_path])


def main():
    parser = argparse.ArgumentParser(
        prog="uml",
        description=".NET Clean Architecture & UML Viewer CLI"
    )
    subparsers = parser.add_subparsers(dest="command", help="Sub-command to run")

    # start
    p_start = subparsers.add_parser("start", help="Start the UML Viewer daemon")
    p_start.add_argument("path", nargs="?", default="", help="Project root folder (default: current directory)")
    p_start.add_argument("prefix", nargs="?", default="", help="Root namespace prefix (default: auto-detected)")
    p_start.add_argument("--port", type=int, default=5050, help="Port to listen on (default: 5050)")
    p_start.add_argument("-f", "--foreground", action="store_true", help="Run in foreground instead of background daemon")

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop the running UML Viewer daemon")

    # status
    p_status = subparsers.add_parser("status", help="Show daemon status")

    # restart
    p_restart = subparsers.add_parser("restart", help="Restart the UML Viewer daemon")
    p_restart.add_argument("path", nargs="?", default="", help="Project root folder")
    p_restart.add_argument("prefix", nargs="?", default="", help="Root namespace prefix")
    p_restart.add_argument("--port", type=int, default=5050, help="Port to listen on")

    # open
    p_open = subparsers.add_parser("open", help="Open UI dashboard in browser")

    # logs
    p_logs = subparsers.add_parser("logs", help="View server logs")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow log stream (tail -f)")
    p_logs.add_argument("-n", "--lines", type=int, default=40, help="Number of lines to show (default: 40)")

    # index
    p_index = subparsers.add_parser("index", help="Run codegraph indexing")
    p_index.add_argument("path", nargs="?", default="", help="Project root folder")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "restart": cmd_restart,
        "open": cmd_open,
        "logs": cmd_logs,
        "index": cmd_index,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
