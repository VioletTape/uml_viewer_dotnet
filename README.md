# .NET Clean Architecture & UML Viewer

A fast, interactive architecture radar and dependency visualizer for .NET projects, designed for **WSL**, **`codegraph`**, and modern web browsers.

Eliminates the Java 21 / Quil / Processing / Swing / tmux dependencies of the original prototype, replacing them with a local REST backend and an interactive SVG/HTML5 web UI.

---

## Features

- **Blazing Fast**: Extracts classes, interfaces, records, methods, and relationships from `.codegraph/codegraph.db` (SQLite) in **<15ms**.
- **Just My Code**: Automatically filters out framework noise (`System.*`, `Microsoft.Extensions.*`) and collapses external NuGet references into summary badges.
- **Clean Architecture Policy Enforcement**: Enforces dependency rules across layers (`Domain`, `Contracts`, `Application`, `Infrastructure`, `Api`, `Client`). Flags inner $\rightarrow$ outer dependency violations with glowing **hot-red arrows**.
- **Interactive Web UI**:
  - Smooth 60fps pan and zoom (drag + mouse wheel).
  - Filter toggle (`All Edges` vs `⚡ Violations Only`) to declutter large codebases.
  - Interactive class cards with stereotypes (`<<interface>>`, `<<abstract>>`, `<<record>>`) and member count.
  - Side inspector with member listing and **"Open in VS Code"** links (`vscode://file/...`) jumping straight to the exact line in WSL/Windows.
- **Zero Alien Dependencies**: Runs natively on Python 3 (guaranteed present in WSL) or .NET without needing Java, Clojure, X11, or tmux.

---

## Quick Start

### 1. Run the Viewer
From the `/home/vt/exp/dotnet-uml-viewer` directory, start the server pointing to any .NET project with `.codegraph`:

```bash
python3 server.py /path/to/your/project MyProject
```

Then open your browser to:
👉 **[http://localhost:5050](http://localhost:5050)**

### 2. Command Line Arguments
```bash
python3 server.py <project-path> [namespace-prefix] [policy-file-path]
```
- `<project-path>`: Path to project root containing `.codegraph/codegraph.db`.
- `[namespace-prefix]`: Optional root namespace prefix (e.g. `ACME`).
- `[policy-file-path]`: Optional path to a custom `policy.json` (defaults to standard Clean Architecture layers).

---

## REST Endpoints

- `GET /api/graph`: Complete architecture graph with layers, types, edges, violations, and external package summaries.
- `GET /api/violations`: List of detected Clean Architecture violations with line numbers and rationale.
- `GET /api/file?path=...`: Raw file contents for in-browser inspection.
