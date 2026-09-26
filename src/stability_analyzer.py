"""
stability_analyzer.py - 3-Tier Stability Analyzer for .NET microservices.
Enforces Nygard stability patterns from 'Release It!':
  - Integration Points (HttpClient, DbContext, Redis, MessageBus)
  - Timeouts (Unbounded / Default 100s HttpClient timeout)
  - Retries & Jitter ("Killed by the Mob" antipattern with multi-instance awareness)
  - Cancellation Token Propagation
  - Circuit Breakers & Resilience Pipelines (Polly v7/v8)

Pipeline:
  Tier 1: CodeGraph (.codegraph/codegraph.db) - Candidate classes & package references
  Tier 2: Roslyn (tools/RoslynStabilityAnalyzer / C# syntax analysis) - AST builder & option extraction
  Tier 3: AI Agent / Infra Correlation (override.yaml, workload.yaml) - Replica blast radius & budget alignment
"""

import json
import os
import re
import sqlite3
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple

from infra_scanner import InfraProfile
from path_utils import is_test_path
from stability_store import StabilityStore


INTEGRATION_INTERFACES = {
    "HttpClient", "HttpMessageInvoker", "IHttpClientFactory",
    "DbContext", "IDbConnection", "DbConnection", "ISqlConnection",
    "IConnectionMultiplexer", "IDatabase",
    "IBus", "IPublishEndpoint", "ISendEndpoint", "IModel", "IChannel"
}

INTEGRATION_NAMESPACES = {
    "System.Net.Http", "Microsoft.EntityFrameworkCore", "System.Data",
    "Dapper", "StackExchange.Redis", "MassTransit", "RabbitMQ.Client"
}


class StabilityAnalyzer:
    def __init__(self, project_path: str, roslyn_bin: Optional[str] = None):
        self.project_path = os.path.abspath(project_path)
        self.store = StabilityStore(self.project_path)
        self.infra = InfraProfile(self.project_path)
        self.roslyn_bin = roslyn_bin or self._find_roslyn_tool()

    def _find_roslyn_tool(self) -> Optional[str]:
        search_dirs = [
            os.path.dirname(__file__),
            os.path.dirname(os.path.dirname(__file__)),
            getattr(self, "project_path", ""),
        ]
        for base in search_dirs:
            if not base:
                continue
            # Check compiled binary first
            candidate_bin = os.path.join(
                base, "tools", "RoslynStabilityAnalyzer", "bin", "Release", "net10.0", "RoslynStabilityAnalyzer"
            )
            if os.path.isfile(candidate_bin) and os.access(candidate_bin, os.X_OK):
                return candidate_bin
            
            # Check csproj for dotnet run
            candidate_csproj = os.path.join(
                base, "tools", "RoslynStabilityAnalyzer", "RoslynStabilityAnalyzer.csproj"
            )
            if os.path.isfile(candidate_csproj):
                return candidate_csproj
        return None

    # =========================================================================
    # Tier 1: CodeGraph Candidate Discovery
    # =========================================================================
    def discover_candidates_from_codegraph(self) -> Dict[str, Any]:
        """
        Queries .codegraph/codegraph.db for types that reference integration packages or classes.
        """
        db_path = os.path.join(self.project_path, ".codegraph", "codegraph.db")
        candidates = []
        if not os.path.isfile(db_path):
            return {"candidates": [], "has_codegraph": False}

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Find classes whose files or dependencies reference HTTP, DB, Redis, or Polly
            cursor.execute("""
                SELECT DISTINCT n.id, n.name, n.qualified_name, n.file_path, n.start_line, n.end_line
                FROM nodes n
                WHERE n.kind IN ('class', 'struct')
            """)
            rows = cursor.fetchall()

            for r in rows:
                candidates.append({
                    "id": r["id"],
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "file_path": r["file_path"],
                    "line": r["start_line"]
                })
            conn.close()
            return {"candidates": candidates, "has_codegraph": True}
        except Exception as e:
            print(f"[StabilityAnalyzer] CodeGraph discovery error: {e}")
            return {"candidates": [], "has_codegraph": False}

    # =========================================================================
    # Tier 2: Roslyn Analysis & Deterministic Syntax Walker
    # =========================================================================
    def run_roslyn_tool(self, file_paths: List[str]) -> Optional[Dict[str, Any]]:
        """Invokes the Roslyn C# tool if available."""
        if not self.roslyn_bin or not file_paths:
            return None

        try:
            if self.roslyn_bin.endswith(".csproj"):
                cmd = ["dotnet", "run", "--project", self.roslyn_bin, "--", "--files"] + file_paths
            else:
                cmd = [self.roslyn_bin, "--files"] + file_paths

            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            if proc.returncode == 0 and proc.stdout:
                return json.loads(proc.stdout)
        except Exception as e:
            print(f"[StabilityAnalyzer] Roslyn invocation error: {e}")
        return None

    def analyze_csharp_syntax(self, file_path: str, code_content: str) -> List[Dict[str, Any]]:
        """
        Fast deterministic C# AST/Syntax inspection.
        Detects Nygard anti-patterns:
          1. Default HttpClient timeout (no .Timeout set)
          2. Unjittered retry (fixed TimeSpan or UseJitter = false)
          3. Missing CancellationToken on async integration calls
          4. Generic Exception catch in retries
        """
        violations = []
        lines = code_content.splitlines()

        # Track class context
        current_class = "Unknown"
        class_match = re.search(r"\bclass\s+([A-Za-z0-9_]+)", code_content)
        if class_match:
            current_class = class_match.group(1)

        # 1. Detect HttpClient instantiation or injection without explicit timeout
        has_http_client = bool(re.search(r"\b(HttpClient|IHttpClientFactory)\b", code_content))
        has_explicit_timeout = bool(re.search(r"\.Timeout\s*=\s*TimeSpan\.", code_content) or
                                   re.search(r"AddHttpClient[^\;]+Timeout\s*=", code_content) or
                                   re.search(r"AddTimeout\(", code_content))

        if has_http_client and not has_explicit_timeout:
            # Locate first HttpClient reference line
            line_no = 1
            for idx, line in enumerate(lines, 1):
                if "HttpClient" in line:
                    line_no = idx
                    break

            violations.append({
                "kind": "unbounded_timeout",
                "class_name": current_class,
                "file_path": file_path,
                "line": line_no,
                "severity": "warning",
                "message": (
                    "Unbounded / Default Timeout Risk: HttpClient is used without an explicit Timeout "
                    "(defaults to 100 seconds in .NET). Configure an explicit deadline or Polly timeout."
                )
            })

        # 2. Detect Retries without Jitter ("Killed by the Mob" risk)
        # Patterns:
        #   UseJitter = false
        #   DelayBackoffType.Constant
        #   WaitAndRetry(n, _ => TimeSpan.FromSeconds(...)) without Jitter
        #   RetryStrategyOptions without UseJitter = true
        for idx, line in enumerate(lines, 1):
            # Check UseJitter = false
            if re.search(r"UseJitter\s*=\s*false", line, re.IGNORECASE):
                violations.append({
                    "kind": "unjittered_retry",
                    "class_name": current_class,
                    "file_path": file_path,
                    "line": idx,
                    "severity": "error" if self.infra.is_multi_instance() else "warning",
                    "message": (
                        f"Unjittered Retry ({'Killed by the Mob Risk' if self.infra.is_multi_instance() else 'Warning'}): "
                        f"Retry explicitly disables jitter (UseJitter = false). "
                        f"{'Service runs ' + str(self.infra.max_replicas) + ' replicas (per override.yaml); ' if self.infra.is_multi_instance() else ''}"
                        f"Retries across instances will synchronize and overwhelm recovering downstreams."
                    )
                })

            # Check fixed TimeSpan delay in Polly retry
            if re.search(r"(WaitAndRetry|AddRetry)\b", line):
                # Check surrounding window for jitter
                window = "\n".join(lines[max(0, idx - 1): min(len(lines), idx + 8)])
                if "TimeSpan.From" in window and not ("Jitter" in window or "Decorrelated" in window or "UseJitter" in window):
                    violations.append({
                        "kind": "unjittered_retry",
                        "class_name": current_class,
                        "file_path": file_path,
                        "line": idx,
                        "severity": "error" if self.infra.is_multi_instance() else "warning",
                        "message": (
                            f"Unjittered Fixed Retry ({'Killed by the Mob Risk' if self.infra.is_multi_instance() else 'Warning'}): "
                            f"Retry policy uses constant or unjittered delay. "
                            f"{'Across ' + str(self.infra.max_replicas) + ' replicas (override.yaml), ' if self.infra.is_multi_instance() else ''}"
                            f"Use exponential backoff with decorrelated jitter (e.g. UseJitter = true)."
                        )
                    })

            # 3. Check generic Exception catch in retry handlers
            if re.search(r"Policy\.Handle<Exception>", line) or re.search(r"ShouldHandle\s*=\s*.*=>\s*true", line):
                violations.append({
                    "kind": "generic_exception_retry",
                    "class_name": current_class,
                    "file_path": file_path,
                    "line": idx,
                    "severity": "error",
                    "message": (
                        "Catch-All Retry Antipattern: Retry policy catches all Exceptions indiscriminately. "
                        "Retries should only be applied to transient faults (503, 429, timeouts, network loss), "
                        "never non-transient 4xx client errors or domain validation exceptions."
                    )
                })

        # 4. Check missing CancellationToken propagation on async integration calls
        for idx, line in enumerate(lines, 1):
            async_call = re.search(r"\.(GetAsync|PostAsync|PutAsync|DeleteAsync|SendAsync|SaveChangesAsync)\s*\((.*?)\)", line)
            if async_call:
                args = async_call.group(2).strip()
                # If no arguments or last argument doesn't look like a cancellation token
                has_ct = any(token in args for token in ("cancellationToken", "ct", "token", "RequestAborted"))
                if not has_ct and not ("HttpCompletionOption" in args and len(args.split(",")) > 1 and "ct" in args):
                    violations.append({
                        "kind": "missing_cancellation_token",
                        "class_name": current_class,
                        "file_path": file_path,
                        "line": idx,
                        "severity": "warning",
                        "message": (
                            f"Fail-Fast / Cancellation Omission: Async call '{async_call.group(1)}' does not pass "
                            f"a CancellationToken. Cancelled client requests will continue executing orphaned downstream I/O."
                        )
                    })

        # 5. Detect Unbounded Channels & Boundary Queues (STAB006)
        norm_path = file_path.replace("\\", "/")
        is_telemetry = (
            any(term in norm_path for term in ("/Logging/", "/Telemetry/", "/Metrics/", "/Diagnostics/", "/Serilog/")) or
            current_class.endswith(("Logger", "Logging", "LogSink", "TelemetrySink", "MetricsSink", "BatchSink")) or
            "Telemetry" in current_class or "Metric" in current_class
        )
        if not is_telemetry:
            is_guarded = "SemaphoreSlim" in code_content or "RateLimiter" in code_content
            for idx, line in enumerate(lines, 1):
                # Channel.CreateUnbounded
                if "Channel.CreateUnbounded" in line:
                    is_field = any(modifier in line for modifier in ("private ", "public ", "protected ", "internal ", "readonly ", "static ")) or re.search(r"Channel<[^>]+>\s+[_A-Za-z0-9]+\s*=", line)
                    if is_field or "AddSingleton" in line or "AddScoped" in line or "AddTransient" in line:
                        violations.append({
                            "kind": "unbounded_boundary_channel",
                            "class_name": current_class,
                            "file_path": file_path,
                            "line": idx,
                            "severity": "warning" if is_guarded else "error",
                            "message": (
                                "Unbounded channel at architectural boundary creates a buffer without backpressure. "
                                "Under downstream latency or stalls, memory grows monotonically until process termination by the Linux cgroup OOM killer. "
                                "Migrate to Channel.CreateBounded<T>(capacity) with BoundedChannelFullMode.Wait."
                            )
                        })

                # Channel.CreateBounded or BoundedChannelOptions with excessive capacity or int.MaxValue
                if "Channel.CreateBounded" in line or "BoundedChannelOptions" in line:
                    if "int.MaxValue" in line:
                        violations.append({
                            "kind": "unbounded_boundary_channel",
                            "class_name": current_class,
                            "file_path": file_path,
                            "line": idx,
                            "severity": "error",
                            "message": (
                                "Bounded channel configured with int.MaxValue is effectively unbounded, providing no backpressure. "
                                "Use a sized capacity budgeted against container memory limits."
                            )
                        })
                    else:
                        cap_match = re.search(r"(?:CreateBounded<[^>]+>|BoundedChannelOptions)\s*\(\s*(\d+)", line)
                        if cap_match and int(cap_match.group(1)) > 50000:
                            cap_val = int(cap_match.group(1))
                            violations.append({
                                "kind": "unbounded_boundary_channel",
                                "class_name": current_class,
                                "file_path": file_path,
                                "line": idx,
                                "severity": "warning",
                                "message": (
                                    f"Bounded channel capacity ({cap_val:,}) exceeds safe memory envelope for typical container limits (>50,000 items). "
                                    f"Under latency, retained memory may cause excessive Gen 2 GC pressure or OOM. Tune capacity to expected processing rate and memory budget."
                                )
                            })

                # ConcurrentQueue<T> as boundary field
                if "ConcurrentQueue<" in line:
                    if not any(p in line for p in ("Pool", "FreeList")):
                        is_field = any(modifier in line for modifier in ("private ", "public ", "protected ", "internal ", "readonly ")) or re.search(r"ConcurrentQueue<[^>]+>\s+[_A-Za-z0-9]+", line)
                        if is_field:
                            violations.append({
                                "kind": "unbounded_boundary_channel",
                                "class_name": current_class,
                                "file_path": file_path,
                                "line": idx,
                                "severity": "warning" if is_guarded else "error",
                                "message": (
                                    "ConcurrentQueue<T> used as shared boundary state provides no backpressure API. "
                                    "Under worker lag or downstream stalls, queue depth grows unconstrained, risking Gen 2 GC compaction spikes and Linux OOM-kill. "
                                    "Migrate to Channel.CreateBounded<T>(capacity) with BoundedChannelFullMode.Wait."
                                )
                            })

                # BlockingCollection without capacity or excessive capacity
                if "BlockingCollection<" in line:
                    if "int.MaxValue" in line or (("new()" in line or "new BlockingCollection" in line) and not re.search(r"new\s+BlockingCollection<[^>]+>\s*\(\s*\d+\s*\)", line)):
                        violations.append({
                            "kind": "unbounded_boundary_channel",
                            "class_name": current_class,
                            "file_path": file_path,
                            "line": idx,
                            "severity": "warning" if is_guarded else "error",
                            "message": (
                                "BlockingCollection<T> instantiated with default unbounded capacity (int.MaxValue). "
                                "Under worker lag or downstream stalls, queue depth grows unconstrained, risking Gen 2 GC compaction spikes and Linux OOM-kill. "
                                "Migrate to a bounded capacity (e.g. new BlockingCollection<T>(capacity)) or Channel.CreateBounded<T>(capacity)."
                            )
                        })
                    else:
                        cap_match = re.search(r"new\s+BlockingCollection<[^>]+>\s*\(\s*(\d+)\s*\)", line)
                        if cap_match and int(cap_match.group(1)) > 50000:
                            cap_val = int(cap_match.group(1))
                            violations.append({
                                "kind": "unbounded_boundary_channel",
                                "class_name": current_class,
                                "file_path": file_path,
                                "line": idx,
                                "severity": "warning",
                                "message": (
                                    f"BlockingCollection<T> capacity ({cap_val:,}) exceeds safe memory envelope for typical container limits (>50,000 items). "
                                    f"Under latency, retained memory may cause excessive Gen 2 GC pressure or OOM. Tune capacity to expected processing rate and memory budget."
                                )
                            })

        return violations

    # =========================================================================
    # Tier 3 & Full Evaluation: Combine with Graph & Cache
    # =========================================================================
    def evaluate_project_stability(self, graph_classes: List[Dict[str, Any]], force_recheck: bool = False) -> List[Dict[str, Any]]:
        """
        Evaluates stability rules for all classes in the project.
        Uses cached findings if files are unchanged and force_recheck is False.
        """
        if not force_recheck and self.store.is_cache_valid():
            return self.store.get_findings()

        all_findings: List[Dict[str, Any]] = []
        files_to_scan: Set[str] = set()

        class_by_file: Dict[str, Dict[str, Any]] = {}
        for c in graph_classes:
            fpath = c.get("file_path")
            if fpath and os.path.isfile(fpath):
                if is_test_path(fpath, self.project_path):
                    continue
                files_to_scan.add(fpath)
                class_by_file[fpath] = c

        # Scan all .cs files in project if not covered by graph
        excluded_dirs = {"bin", "obj", ".git", "node_modules", ".vs", "TestResults", ".codegraph", ".uml-viewer"}
        for root, dirs, files in os.walk(self.project_path):
            dirs[:] = [d for d in dirs if d not in excluded_dirs and not d.startswith(".")]
            if is_test_path(root, self.project_path):
                dirs[:] = []
                continue
            for f in files:
                if f.endswith(".cs"):
                    fpath = os.path.join(root, f)
                    if not is_test_path(fpath, self.project_path):
                        files_to_scan.add(fpath)

        file_list = sorted(list(files_to_scan))

        # Try Tier 2 Roslyn CLI first if available
        roslyn_output = self.run_roslyn_tool(file_list)
        if roslyn_output and "violations" in roslyn_output:
            for v in roslyn_output["violations"]:
                fpath = v.get("file")
                c_info = class_by_file.get(fpath, {})
                raw_kind = v.get("kind", "stability_issue")

                # Normalize kind identifiers between Roslyn analyzer and Python rules
                if raw_kind in ("default_timeout", "unbounded_timeout"):
                    kind = "unbounded_timeout"
                elif raw_kind in ("catch_all_exception_retry", "generic_exception_retry"):
                    kind = "generic_exception_retry"
                else:
                    kind = raw_kind

                severity = v.get("severity", "warning")
                reason = v.get("message", "")

                # Correlate unjittered retry with multi-instance deployment (override.yaml)
                if kind == "unjittered_retry" and self.infra.is_multi_instance():
                    severity = "error"
                    if "Killed by the Mob" not in reason:
                        reason = f"Unjittered Retry (Killed by the Mob Risk across {self.infra.max_replicas} replicas per override.yaml): {reason}"
                    elif f"{self.infra.max_replicas} replicas" not in reason:
                        reason += f" (Runs {self.infra.max_replicas} replicas per override.yaml)"
                elif kind == "suspicious_custom_resilience" and self.infra.is_multi_instance():
                    if f"{self.infra.max_replicas} replicas" not in reason:
                        reason += f" (Multi-instance: {self.infra.max_replicas} replicas amplify dogpiling storms when custom retries lack decorrelated jitter)"

                to_class = "Boundary Buffer / Queue" if kind == "unbounded_boundary_channel" else "External Service / Integration Point"
                to_layer = "In-Memory Boundary" if kind == "unbounded_boundary_channel" else "External Infrastructure"

                all_findings.append({
                    "category": "stability_rule",
                    "severity": severity,
                    "from_class": v.get("class_name") or c_info.get("name", "Unknown"),
                    "from_namespace": c_info.get("namespace", "Unknown"),
                    "from_layer": c_info.get("layer", "Infrastructure"),
                    "to_class": to_class,
                    "to_namespace": "",
                    "to_layer": to_layer,
                    "kind": kind,
                    "reason": reason,
                    "file_path": fpath,
                    "line": v.get("line", 1),
                    "tier": "roslyn"
                })
        else:
            # Fallback to high-speed deterministic syntax parser
            for fpath in file_list:
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    raw_violations = self.analyze_csharp_syntax(fpath, content)
                    c_info = class_by_file.get(fpath, {})

                    for v in raw_violations:
                        all_findings.append({
                            "category": "stability_rule",
                            "severity": v["severity"],
                            "from_class": v["class_name"] or c_info.get("name", "Unknown"),
                            "from_namespace": c_info.get("namespace", "Unknown"),
                            "from_layer": c_info.get("layer", "Infrastructure"),
                            "to_class": "Boundary Buffer / Queue" if v["kind"] == "unbounded_boundary_channel" else "External Service / Integration Point",
                            "to_namespace": "",
                            "to_layer": "In-Memory Boundary" if v["kind"] == "unbounded_boundary_channel" else "External Infrastructure",
                            "kind": v["kind"],
                            "reason": v["message"],
                            "file_path": v["file_path"],
                            "line": v["line"],
                            "tier": "static"
                        })
                except Exception as e:
                    print(f"[StabilityAnalyzer] Error reading {fpath}: {e}")

        # Check for completely unprotected integration points in Infrastructure layer
        for c in graph_classes:
            fpath = c.get("file_path")
            if is_test_path(fpath, self.project_path):
                continue
            if c.get("layer") == "Infrastructure":
                ext_refs = c.get("external_refs", [])
                has_integration = any(any(ig in r for ig in ("HttpClient", "DbContext", "Dapper", "Redis", "Bus")) for r in ext_refs)
                has_polly = any("Polly" in r or "Resilience" in r for r in ext_refs)
                if has_integration and not has_polly:
                    all_findings.append({
                        "category": "stability_rule",
                        "severity": "warning",
                        "from_class": c["name"],
                        "from_namespace": c.get("namespace", "Unknown"),
                        "from_layer": "Infrastructure",
                        "to_class": "Remote Dependency",
                        "to_namespace": "",
                        "to_layer": "External Infrastructure",
                        "kind": "unprotected_integration_point",
                        "reason": (
                            f"Unprotected Integration Point: Class '{c['name']}' performs external I/O but does not "
                            f"reference any Polly or Microsoft.Extensions.Resilience policies. "
                            f"Wrap calls in a Circuit Breaker or Retry with Jitter (Nygard Integration Point pattern)."
                        ),
                        "file_path": c.get("file_path"),
                        "line": c.get("start_line", 1),
                        "tier": "codegraph"
                    })

        # Exclude any findings located in test folders (case variations: test, tests, *.Tests, etc.)
        all_findings = [f for f in all_findings if not is_test_path(f.get("file_path"), self.project_path)]

        # Consolidate stability findings by Type (Class) and File Path to maximize signal-to-noise ratio
        consolidated_findings = self._consolidate_findings_by_class(all_findings)

        # Save to cache store
        self.store.save_findings(consolidated_findings)
        return consolidated_findings

    def _consolidate_findings_by_class(self, findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Consolidates stability findings by class (type) and file path to boost noise/signal ratio.
        Instead of reporting every individual method call site in a class (e.g. 15 Dapper queries in DBAccess),
        the class is marked once as having unprotected calls, with call sites and occurrences aggregated.
        """
        if not findings:
            return []

        from collections import defaultdict, Counter

        # Group findings by (from_class, file_path)
        grouped = defaultdict(list)
        for f in findings:
            cls_name = f.get("from_class") or "Unknown"
            fpath = f.get("file_path") or ""
            key = (cls_name, fpath)
            grouped[key].append(f)

        consolidated = []
        for (cls_name, fpath), group in grouped.items():
            if len(group) == 1:
                item = dict(group[0])
                item["occurrences"] = 1
                consolidated.append(item)
                continue

            # Multiple findings for the same class/type
            severity = "error" if any(f.get("severity") == "error" for f in group) else "warning"

            # Determine unique lines and primary line
            lines = sorted(list({int(f.get("line", 1)) for f in group if f.get("line") is not None}))
            primary_line = lines[0] if lines else group[0].get("line", 1)

            if len(lines) <= 4:
                lines_preview = ", ".join(str(l) for l in lines)
            else:
                lines_preview = ", ".join(str(l) for l in lines[:3]) + f", ... (+{len(lines) - 3} more)"

            # Analyze kinds present
            kind_counts = Counter(f.get("kind", "stability_issue") for f in group)

            # Determine primary kind and consolidated reason
            if len(kind_counts) == 1:
                primary_kind = list(kind_counts.keys())[0]
                if primary_kind == "missing_cancellation_token":
                    reason = (
                        f"Class '{cls_name}' makes {len(group)} calls missing CancellationToken (lines {lines_preview}): "
                        f"Async integration calls do not pass CancellationToken, risking orphaned background execution."
                    )
                elif primary_kind == "unbounded_timeout":
                    reason = (
                        f"Class '{cls_name}' makes {len(group)} calls without explicit Timeout (lines {lines_preview}): "
                        f"Default .NET timeout (100 seconds) risks socket pool exhaustion under remote latency."
                    )
                elif primary_kind == "unjittered_retry":
                    reason = (
                        f"Class '{cls_name}' configures {len(group)} retry policies without jitter (lines {lines_preview}): "
                        f"Risk of synchronized retry storms ('Killed by the Mob') across instances."
                    )
                elif primary_kind == "generic_exception_retry":
                    reason = (
                        f"Class '{cls_name}' configures {len(group)} retry policies catching generic System.Exception (lines {lines_preview}): "
                        f"Retrying non-transient failures can cause cascading outages and duplicate side-effects."
                    )
                elif primary_kind == "suspicious_custom_resilience":
                    reason = (
                        f"Class '{cls_name}' has {len(group)} suspicious custom resilience loops (lines {lines_preview}): "
                        f"Hand-rolled retry/timeout wrappers risk threadpool starvation and unobserved background leaks."
                    )
                elif primary_kind == "unbounded_boundary_channel":
                    reason = (
                        f"Class '{cls_name}' contains {len(group)} unbounded queue/channel boundary buffers (lines {lines_preview}): "
                        f"Unbounded buffers create memory accumulation risks without backpressure under worker lag or downstream stalls."
                    )
                else:
                    reason = f"Class '{cls_name}' has {len(group)} stability issues of type '{primary_kind}' (lines {lines_preview})."
            else:
                # Mixed issues for the class (e.g. unprotected_integration_point + missing_cancellation_token + unbounded_timeout)
                if "unprotected_integration_point" in kind_counts:
                    primary_kind = "unprotected_integration_point"
                else:
                    primary_kind = kind_counts.most_common(1)[0][0]

                breakdown_parts = [f"{cnt} {k.replace('_', ' ')}" for k, cnt in kind_counts.most_common()]
                breakdown = ", ".join(breakdown_parts)
                reason = (
                    f"Class '{cls_name}' performs {len(group)} unprotected calls without timeout or circuit breaker resilience "
                    f"({breakdown}) across lines {lines_preview}."
                )

            first = group[0]
            item = {
                "category": "stability_rule",
                "severity": severity,
                "from_class": cls_name,
                "from_namespace": first.get("from_namespace", "Unknown"),
                "from_layer": first.get("from_layer", "Infrastructure"),
                "to_class": first.get("to_class") or "External Service / Integration Point",
                "to_namespace": first.get("to_namespace", ""),
                "to_layer": first.get("to_layer", "External Infrastructure"),
                "kind": primary_kind,
                "reason": reason,
                "file_path": fpath,
                "line": primary_line,
                "tier": first.get("tier", "roslyn"),
                "occurrences": len(group),
                "lines": lines,
                "call_sites": [f"line {f.get('line')}: {f.get('reason')}" for f in group],
                "details": [
                    {
                        "line": f.get("line"),
                        "kind": f.get("kind"),
                        "reason": f.get("reason"),
                        "severity": f.get("severity")
                    }
                    for f in group
                ]
            }
            consolidated.append(item)

        return consolidated
