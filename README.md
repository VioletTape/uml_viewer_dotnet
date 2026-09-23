# .NET Clean Architecture & UML Viewer

A fast, interactive architecture radar, code-coverage quality engine, and autonomous AI refactoring workbench for .NET projects. Designed for **WSL**, **`codegraph`**, and modern web browsers.

Eliminates the Java 21 / Quil / Processing / Swing / tmux dependencies of the original prototype, replacing them with a local REST backend, headless AI daemon, and an interactive SVG/HTML5 web UI.

The server listens only on `127.0.0.1`. Use `http://localhost:5050` or
`http://127.0.0.1:5050` (or your configured port). Browser requests must come
from the viewer's own origin; local CLI requests can omit `Origin`. File reads
and editor actions accept only `.cs` and `.csx` files inside the project,
including after resolving symlinks. Mailbox locking and daemon identification
use Linux/WSL facilities.

---

## 🚀 Global CLI: `uml`

Run UML Viewer from **any project folder** with a single command:

```bash
# Navigate to your .NET solution or project
cd ~/projects/organizations

# Start UML Viewer daemon in the background (auto-detects project & namespace)
uml start

# Open dashboard directly in your browser
uml open

# Check status and running PID
uml status

# View or follow live logs
uml logs -f

# Stop the server daemon
uml stop
```

### CLI Command Reference

| Command | Description |
|---|---|
| `uml start [path] [prefix] [--port 5050]` | Starts background server & headless AI daemon. Auto-detects namespace prefix and builds CodeGraph index if missing. |
| `uml stop` | Gracefully shuts down the background daemon. |
| `uml status` | Displays daemon state, watched project, PID, and URL. |
| `uml restart [path]` | Restarts the background daemon for the current or specified path. |
| `uml open` | Launches default browser directly to the dashboard (`http://localhost:5050`). |
| `uml logs [-f] [-n LINES]` | Views or follows live server logs. |
| `uml index [path]` | Builds or rebuilds the project's CodeGraph index. |

---

## Features

- **Global CLI Daemon**: Run `uml start` and `uml stop` anywhere without manual path configuration.
- **Autonomous Headless AI Agent**: Continuously watches `.uml-viewer/tasks.json` and evaluates What-If architecture proposals, DIP decoupling suggestions, and architectural rationales in the background.
- **Interactive What-If Simulation**: Preview architectural decoupling proposals (`📐 Arch Mode`) live on canvas before touching source code.
- **Code Coverage & Quality Radar**: Integrates Coverlet/Cobertura XML coverage with Cyclomatic Complexity to compute **CRAP** (Change Risk Analysis and Predictions) risk pills for every class and method.
- **Clean Architecture Policy Enforcement**: Flags Dependency Rule violations, Circular Dependencies (ADP), and Framework Taint.
- **Deep Inspector**:
  - Focuses selected classes with geometric auto-centering.
  - Interactive Clean Architecture violation cards with `🤖 Fix with AI`, `💡 Explain with AI`, and `✎ Open in VS Code`.
- **VS Code Deep-Linking**: One-click jump to file and line in VS Code inside WSL.

---

## Architecture Endpoints

- `GET /api/graph[?proposal_id=...]`: Complete graph with layers, classes, members, edges, CRAP scores, and violations.
- `GET /api/events`: Server-Sent Events (SSE) live-stream pushing graph reloads, proposal updates, and agent status.
- `GET /api/agent/tasks` & `POST /api/agent/tasks`: Queue and retrieve background AI agent tasks.
- `GET /api/agent/proposals`: Retrieve stored What-If simulation proposals.
- `POST /api/open`: Launches local VS Code to file path and line.

Run regression checks with `python3 -m unittest discover -v` and
`node test_frontend.js`.
