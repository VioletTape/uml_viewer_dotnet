# Work in Progress: Python Codebase Performance, Concurrency & Architectural Optimization

## 1. Initiative Overview

A comprehensive, multi-perspective code review conducted across the Python codebase in `/home/vt/exp/dotnet-uml-viewer` by three specialized review agents (Performance & Algorithmic Specialist, Systems & Concurrency Specialist, Architecture & Quality Specialist) identified critical silent bugs, database query storms (N+1 queries), quadratic algorithmic bottlenecks, disk I/O rehashing, and concurrency hazards.

This WIP documents the prioritized, step-by-step implementation plan. Each step is implemented, verified against test suites, evaluated by specialized subagents, and committed before proceeding to the next step.

---

## 2. Inventory of Issues & Impact

| Step | Area | Files Affected | Issue Description | Impact / Target Speedup | Status |
|:---:|:---|:---|:---|:---|:---:|
| **1** | Critical Bugs | `infra_scanner.py`, `stability_store.py`, `metrics.py` | `NameError: Tuple` in YAML fallback; `"obj" in root` substring false-positive folder skips; Cobertura XML namespace ignore | Eliminates silent manifest & coverage parsing failures | **Completed** |
| **2** | Database (SQLite) | `extractor.py` | N+1 queries in `_resolve_to_class` (firing up to 30,000 queries per extraction) | **50x – 200x** faster graph extraction | In Progress |
| **3** | Algorithmic Complexity | `metrics.py`, `policy.py` | $O(C \times L)$ coverage dict scan; $O(\text{Cycles} \times E)$ cycle edge tagging; 15 uncompiled regexes in method complexity | **100x – 1000x** metrics lookup; **8x – 15x** complexity calc | Pending |
| **4** | File I/O & Hashing | `stability_store.py`, `stability_analyzer.py` | Full-file SHA256 reads on cache check; unpruned `os.walk` descending into `.git` & `node_modules` | **100x – 500x** cache checks; **10x – 50x** disk scan reduction | Pending |
| **5** | Server-Side Caching | `server.py` | Zero caching on `/api/graph` and `/api/violations`; full extraction & metrics recalculated on every request | Sub-millisecond API responses (vs 2–5s) | Pending |
| **6** | Systems & Concurrency | `server.py`, `uml_cli.py`, `mailbox_store.py`, `headless_agent.py` | 15s SSE sleep causing 3s SIGKILL escalation; double JSON serialization in mailbox; failed tasks marked "completed" | Graceful shutdown, zero serialization waste, reliable task state | Pending |
| **7** | DRY, Portability & Polish | `path_utils.py`, `server.py`, `uml_cli.py`, `policy.py` | Duplicated `auto_detect_prefix`; Windows backslash handling in WSL; unmemoized `is_test_path` and `assign_layer` | Clean modularity, cross-platform WSL/Linux compatibility | Pending |

---

## 3. Step-by-Step Implementation Roadmap

### Step 1: Critical Bug Fixes & Scanner Hardening
- [x] Fix missing `Tuple` import in `infra_scanner.py` (`NameError: name 'Tuple' is not defined`).
- [x] Fix substring directory skipping (`any(p in root for p in ("bin", "obj", ...))`) in `stability_store.py` and `metrics.py` so projects containing "obj" or "bin" are not skipped.
- [x] Fix Cobertura XML namespace stripping in `metrics.py` (`root.tag == "coverage"` ignoring standard XML namespaces).
- [x] Run full test suite (`python3 -m unittest discover` - 42 tests passing).
- [x] Subagent evaluation of Step 1 (`44c5d15c` - confirmed READY TO COMMIT).
- [x] Commit Step 1.

### Step 2: Database N+1 Query Elimination in `extractor.py`
- [ ] In `CodeGraphExtractor.extract()`, preload all `kind = 'contains'` edges into an in-memory parent map `contains_parent_map: Dict[str, str]`.
- [ ] Refactor `_resolve_to_class()` to resolve member nodes via `contains_parent_map` without executing per-node SQL queries.
- [ ] Eliminate per-reference SQL queries in `_extract_nuget_dependencies`.
- [ ] Wrap SQLite connections in `try...finally` to eliminate connection descriptor leaks.
- [ ] Run full test suite (`python3 -m unittest discover`).
- [ ] Subagent evaluation of Step 2.
- [ ] Commit Step 2.

### Step 3: Algorithmic & Data Structure Inefficiencies in `metrics.py` and `policy.py`
- [ ] In `metrics.py`, restructure coverage storage into `self.file_coverage: Dict[str, Dict[int, int]]` to eliminate $O(C \times L)$ full table iterations.
- [ ] In `metrics.py`, precompile regexes for `calculate_method_complexity()` and use fast C-level `str.count()` for operators (`&&`, `||`, `??`).
- [ ] In `policy.py`, index `enriched_edges` by `(from, to)` to eliminate nested linear scans during cycle edge tagging.
- [ ] In `policy.py`, precompile layer matching regexes and forbidden external dependency patterns.
- [ ] Run full test suite (`python3 -m unittest discover`).
- [ ] Subagent evaluation of Step 3.
- [ ] Commit Step 3.

### Step 4: File I/O, Hashing & Directory Traversal Optimization
- [ ] In `stability_store.py`, replace whole-file content SHA256 reads with `os.stat` (`mtime_ns` + `size`) fingerprinting in `compute_project_signature()`.
- [ ] Prune `dirs[:]` in-place during `os.walk` in `stability_store.py` and `stability_analyzer.py` to prevent descending into `.git`, `node_modules`, `bin`, `obj`.
- [ ] Eliminate redundant second signature calculation in `StabilityStore.save_findings()`.
- [ ] Atomically write `stability_cache.json` using temp file replace to prevent 0-byte truncation on concurrent reads.
- [ ] Run full test suite (`python3 -m unittest discover`).
- [ ] Subagent evaluation of Step 4.
- [ ] Commit Step 4.

### Step 5: Server-Side In-Memory Graph Caching
- [ ] Implement thread-safe `GraphCacheManager` in `server.py` caching extracted, evaluated, and enriched graphs.
- [ ] Wire `file_watcher_loop` to invalidate the cache when `codegraph.db` or `policy.json` changes.
- [ ] Serve `GET /api/graph` and `GET /api/violations` from cache.
- [ ] Run full test suite (`python3 -m unittest discover`).
- [ ] Subagent evaluation of Step 5.
- [ ] Commit Step 5.

### Step 6: Systems, Concurrency & Lifecycle Hardening
- [ ] Set `ThreadingHTTPServer.daemon_threads = True` and broadcast `None` sentinel on server shutdown to unblock SSE worker threads immediately and avoid 3-second SIGKILL escalation in `uml_cli.py`.
- [ ] Add bounded capacity and slow consumer eviction to SSE client queues (`maxsize=128`).
- [ ] Refactor `mailbox_store.py` to eliminate double JSON dump on read-only access.
- [ ] Fix task lifecycle in `headless_agent.py`: record `"failed"` instead of `"completed"` on exceptions; reconcile orphaned `"in_progress"` tasks on startup.
- [ ] Add 5MB file size limit and explicit `Content-Length` header in `server.py:_handle_get_file`.
- [ ] Run full test suite (`python3 -m unittest discover`).
- [ ] Subagent evaluation of Step 6.
- [ ] Commit Step 6.

### Step 7: DRY, Portability & Polish
- [ ] Consolidate `auto_detect_prefix()` into `path_utils.py` and import it in `server.py` and `uml_cli.py`.
- [ ] Add Windows backslash normalization in `server.py:_source_path`.
- [ ] Add `@functools.lru_cache` to `is_test_path()` in `path_utils.py` and `assign_layer()` in `policy.py`.
- [ ] Run full test suite (`python3 -m unittest discover`).
- [ ] Subagent evaluation of Step 7.
- [ ] Commit Step 7.
