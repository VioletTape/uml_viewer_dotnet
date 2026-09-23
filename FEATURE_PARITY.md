# .NET Architecture & UML Viewer: Feature Parity & Implementation Roadmap

This document tracks feature parity and implementation progress for the modern .NET/Web reimplementation of Uncle Bob's `uml-viewer`.

---

## 1. Feature Parity Matrix

| Feature Domain | Original `uml-viewer` (Clojure/Quil) | New Architecture (.NET/Python/Web/codegraph) | Status |
|---|---|---|---|
| **Data Extraction** | Clojure AST reader (`clj-graph`) | Query `.codegraph/codegraph.db` (SQLite) directly | [x] Implemented (`extractor.py`) |
| **"Just My Code" Filter** | Manual foreign list in `.policy.edn` | Auto-filter BCL (`System.*`), collapse NuGets to package badges | [x] Implemented (`extractor.py`) |
| **Clean Architecture Rules** | Dependency Rule validator (`domain/policy.clj`) | Layer rank evaluator + hot-red violation arrow flagging | [x] Implemented (`policy.py`) |
| **Interactive Canvas** | Quil (Java Processing / AWT desktop) | Modern Web (Browser SVG/Canvas with Pan & Zoom) | [x] Implemented (`frontend/`) |
| **Hierarchical Drill-Down** | Namespace splitting & arrow collapsing | Layer columns & namespace containers with edge toggle | [x] Implemented (`frontend/`) |
| **Class Cards & Details** | Class cards with fields, methods, shapes | Interactive cards with stereotypes, methods, and details | [x] Implemented (`frontend/`) |
| **Source Code Drill-Down** | Swing code window with regex highlighter | WSL-aware VS Code opener (finds `.sln`/`.git` root + jumps to line) | [x] Implemented (`server.py`, `app.js`) |
| **Quality & Mutation Overlays** | CRAP score & clj-mutate EDN loaders | Coverlet coverage + McCabe complexity + CRAP scores | [x] Implemented (`metrics.py`) |
| **What-If Proposals** | Proposal layers in policy/IR | Simulating proposed layer reorganizations via policy config | [ ] Planned |
| **Agent / Hot Reload Loop** | Spawns `tmux` + AppleScript + EDN mailbox | Local HTTP REST API + live reload (no tmux, no Java) | [x] Implemented (`server.py`) |

---

## 2. Implementation Phases

### Phase 1: Core Extractor & Policy Engine
- [x] Connect to `.codegraph/codegraph.db` in a target project.
- [x] Extract internal types: classes, interfaces, records, structs, enums.
- [x] Extract relations: inheritance (`extends`), interface implementation (`implements`), dependencies (`references`, `calls`, `instantiates`).
- [x] Apply "Just My Code" rules:
  - Filter out BCL noise (`System.*`, `Microsoft.Extensions.*`).
  - Collapse external NuGet dependencies into high-level package boxes or badges.
- [x] Implement Clean Architecture policy checker:
  - Parse `policy.json` (layers, ordering, allowed dependencies).
  - Flag dependency rule violations (`violating: true`, severity, reason).
- [x] Export standard `architecture.json` document.

### Phase 2: Interactive Web Viewer (Frontend)
- [x] Lightweight standalone HTML/CSS/JS frontend (`frontend/index.html`, `style.css`, `app.js`).
- [x] Visual styling:
  - Clean architecture layer columns (`Domain`, `Contracts`, `Infrastructure`, `Api`, `Client`).
  - Stereotypes (`<<interface>>`, `<<abstract>>`, `<<record>>`).
  - Hot-red glowing arrows for dependency violations with tooltips.
- [x] Interactive features:
  - Pan & Zoom canvas (mouse drag + wheel zoom + 1:1 reset).
  - "Violations Only" filter toggle to instantly de-clutter large graphs.
  - Click on class to view methods/properties in side inspector.
  - "Open in VS Code" button using `vscode://file/{path}:{line}`.

### Phase 3: Live Server & Agent Pairing
- [x] Lightweight local server in WSL (`server.py` serving web frontend + REST API).
- [x] REST endpoints: `/api/graph`, `/api/violations`, `/api/file`.
- [x] Server-Sent Events (SSE) `/api/events` for instant live auto-reload.
- [x] Background file watcher monitoring `.codegraph/codegraph.db`, WAL, and `policy.json`.
- [x] Agent-friendly JSON triggers (`POST /api/reload`, `GET /api/violations`).

### Phase 4: Quality & Mutation Testing Overlays
- [x] Coverlet JSON (`coverage.json`), Cobertura (`*.cobertura.xml`), and OpenCover (`*.opencover.xml`) parser.
- [x] McCabe Cyclomatic Complexity analyzer computed from C# syntax trees.
- [x] Savoia & Evans' CRAP score calculation: $\text{CRAP} = \text{comp}^2 \times (1 - \text{cov})^3 + \text{comp}$.
- [x] Traffic light indicators (🟢 🟡 🔴) and `🎯 CRAP Overlay` toggle on canvas.
- [x] Quality & CRAP inspection tab with top change-risk method leaderboard.
- [ ] Stryker.NET `mutation-report.json` parser (deferred for later).
