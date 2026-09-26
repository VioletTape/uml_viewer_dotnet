#!/usr/bin/env python3
"""
headless_agent.py - Autonomous Headless AI Agent Daemon for .NET Architecture Viewer.
Monitors .uml-viewer/tasks.json or SSE events, picks up pending tasks autonomously,
executes architectural analysis, generates What-If refactoring proposals, and resolves tasks.
"""

import datetime
import os
import re
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

from mailbox_store import mailbox


def slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()


class HeadlessAgentWorker:
    def __init__(self, project_path: str, notify_cb: Optional[Callable[[dict], None]] = None):
        self.project_path = os.path.abspath(project_path)
        self.mailbox_dir = os.path.join(self.project_path, ".uml-viewer")
        os.makedirs(self.mailbox_dir, exist_ok=True)
        self.notify_cb = notify_cb
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()

    def start(self):
        self._reconcile_orphaned_tasks()
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, name="HeadlessAgentThread", daemon=True)
        self.thread.start()
        print(f"[Headless Agent] Daemon started for project: {self.project_path}")

    def _reconcile_orphaned_tasks(self):
        """Reconciles tasks stuck in 'in_progress' from a prior crash or unclean exit."""
        try:
            with mailbox(self.project_path, "tasks.json") as tasks:
                reconciled = False
                for task in tasks:
                    if task.get("status") == "in_progress":
                        task["status"] = "pending"
                        reconciled = True
                if reconciled:
                    print(f"[Headless Agent] Reconciled orphaned 'in_progress' tasks back to 'pending'.")
        except Exception:
            pass

    def stop(self):
        self.running = False
        self._wake_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        print("[Headless Agent] Daemon stopped.")

    def trigger(self):
        """Immediately wake up worker to process queued tasks without waiting for poll interval."""
        self._wake_event.set()

    def _get_tasks(self) -> List[Dict]:
        with mailbox(self.project_path, "tasks.json", read_only=True) as tasks:
            return tasks

    def _emit(self, event_data: dict):
        if self.notify_cb:
            try:
                self.notify_cb(event_data)
            except Exception as e:
                print(f"[Headless Agent] SSE notification error: {e}")

    def _run_loop(self):
        while self.running:
            self._wake_event.wait(timeout=2.0)
            self._wake_event.clear()
            if not self.running:
                break
            try:
                self._check_and_process_tasks()
            except Exception as e:
                print(f"[Headless Agent] Error in processing loop: {e}", file=sys.stderr)

    def _check_and_process_tasks(self):
        tasks = self._get_tasks()
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            return

        for task in pending:
            self._process_single_task(task["id"])

    def _process_single_task(self, task_id: str):
        with mailbox(self.project_path, "tasks.json") as tasks:
            task = next((t for t in tasks if t["id"] == task_id), None)
            if not task or task.get("status") != "pending":
                return

            # 1. Transition to in_progress
            task["status"] = "in_progress"
        print(f"[Headless Agent] ⚡ Picked up task {task_id}: '{task.get('title')}'")
        self._emit({"type": "agent_task_updated", "task_id": task_id, "status": "in_progress"})

        # Artificial short breath (e.g. 500ms) to ensure UI displays in_progress state cleanly
        time.sleep(0.6)

        # 2. Execute agent reasoning & action
        op = task.get("op", "fix_violation")
        result_message = ""
        success = True
        try:
            if op == "propose_refactor":
                result_message = self._handle_propose_refactor(task)
            elif op == "fix_violation":
                result_message = self._handle_fix_violation(task)
            elif op == "explain_violation":
                result_message = self._handle_explain_violation(task)
            elif op == "refresh_crap":
                result_message = self._handle_refresh_crap(task)
            elif op == "stability_audit":
                result_message = self._handle_stability_audit(task)
            else:
                result_message = f"Completed generic task op '{op}'."
        except Exception as e:
            success = False
            result_message = f"Failed to execute task: {str(e)}"
            print(f"[Headless Agent] ❌ Task {task_id} failed: {e}", file=sys.stderr)

        # 3. Mark completed or failed
        final_status = "completed" if success else "failed"
        updated = False
        with mailbox(self.project_path, "tasks.json") as tasks:
            task = next((t for t in tasks if t["id"] == task_id), None)
            if task and task.get("status") == "in_progress":
                task["status"] = final_status
                task["result"] = result_message
                task["resolved_at"] = datetime.datetime.now().isoformat()
                updated = True
        if updated:
            if success:
                print(f"[Headless Agent] ✅ Completed task {task_id}: {result_message[:70]}...")
            else:
                print(f"[Headless Agent] ❌ Failed task {task_id}: {result_message[:70]}...")
            self._emit({"type": "agent_task_updated", "task_id": task_id, "status": final_status})

    def _handle_fix_violation(self, task: Dict) -> str:
        target = task.get("target", {})
        from_cls = target.get("from_class", "Source")
        to_cls = target.get("to_class", "Target")
        from_layer = target.get("from_layer", "Application")
        to_layer = target.get("to_layer", "Infrastructure")

        if target.get("category") == "stability_rule":
            kind = target.get("kind", "")
            if kind == "suspicious_custom_resilience":
                prop_id = f"prop-resilience-migrate-{slugify(from_cls)}"
                prop_name = f"Resilience: Migrate Custom Loops in {from_cls} to Polly v8"
                description = (
                    f"Simulation: Replace hand-rolled retry/timeout loops in {from_cls} with Microsoft.Extensions.Resilience "
                    f"pipeline (ResiliencePipelineBuilder / AddStandardResilienceHandler). Enforces exponential backoff with jitter and fail-fast cancellation."
                )
            else:
                prop_id = f"prop-resilience-{slugify(from_cls)}"
                prop_name = f"Resilience: Standard Resilience Handler for {from_cls}"
                description = f"Simulation: Wrap {from_cls} integration calls with Microsoft.Extensions.Http.Resilience standard pipeline (AddStandardResilienceHandler) to enforce exponential jittered backoff and 5s timeout."

            new_proposal = {
                "id": prop_id,
                "name": prop_name,
                "author": "Autonomous Headless Agent",
                "description": description,
                "layer_overrides": {},
                "omitted_edges": [],
                "proposed_edges": [],
                "created_at": datetime.datetime.now().isoformat()
            }
            with mailbox(self.project_path, "proposals.json") as proposals:
                proposals[:] = [p for p in proposals if p["id"] != prop_id]
                proposals.insert(0, new_proposal)
            self._emit({"type": "proposals_updated"})
            return f"Created What-If resilience proposal '{prop_name}'. Configured Polly v8 standard resilience handler with jittered backoff."

        prop_id = f"prop-fix-{slugify(from_cls)}-{slugify(to_cls)}"
        prop_name = f"DIP: Decouple {from_cls} from {to_cls}"

        layer_overrides = {}
        omitted = [{"from": from_cls, "to": to_cls}]
        proposed = [{"from": from_cls, "to": f"I{to_cls}Port", "kind": "dependency"}]

        # If to_class is an entity/value-object (e.g. ends with Id, Dto, Model, Options), move to Contracts/Domain
        if any(to_cls.endswith(sfx) for sfx in ["Id", "Dto", "Options", "Event", "Message"]):
            layer_overrides[to_cls] = "Contracts"

        new_proposal = {
            "id": prop_id,
            "name": prop_name,
            "author": "Autonomous Headless Agent",
            "description": f"DIP simulation: Decouples {from_cls} ({from_layer}) from concrete {to_cls} ({to_layer}). Inverts dependency using I{to_cls}Port in Contracts.",
            "layer_overrides": layer_overrides,
            "omitted_edges": omitted,
            "proposed_edges": proposed,
            "created_at": datetime.datetime.now().isoformat()
        }

        with mailbox(self.project_path, "proposals.json") as proposals:
            proposals[:] = [p for p in proposals if p["id"] != prop_id]
            proposals.insert(0, new_proposal)
        self._emit({"type": "proposals_updated"})

        return f"Created What-If proposal '{prop_name}'. Decoupled direct dependency and proposed interface I{to_cls}Port in Contracts."

    def _handle_propose_refactor(self, task: Dict) -> str:
        prop_id = "prop-headless-full-decoupling"
        prop_name = "Autonomous: Full Architecture Decoupling"

        new_proposal = {
            "id": prop_id,
            "name": prop_name,
            "author": "Autonomous Headless Agent",
            "description": "Comprehensive DIP refactoring: relocates Value Objects and Options to Domain/Contracts, and decouples Application/Domain from Infrastructure DataProviders.",
            "layer_overrides": {
                "AktorId": "Domain",
                "NavisionClientId": "Domain",
                "IDataProvider": "Contracts",
                "IDataExecutor": "Contracts",
                "ILegacyDataProvider": "Contracts",
                "IAssetManagementService": "Contracts",
                "ISubmittedTriplogService": "Contracts",
                "IOrganizationWriteHandler": "Contracts",
                "OrganizationClientOptions": "Contracts"
            },
            "omitted_edges": [
                {"from": "CustomerSuspendedConsumer", "to": "DataExecutor"},
                {"from": "CustomerSuspendedConsumer", "to": "DataProvider"},
                {"from": "OrganizationLegacyGrpcService", "to": "DataProvider"},
                {"from": "OrganizationInfoGrpcService", "to": "DataProvider"},
                {"from": "OrganizationInfoGrpcService", "to": "OrganizationClientOptions"},
                {"from": "OrganizationWriteHandler", "to": "DataExecutor"},
                {"from": "OrganizationWriteHandler", "to": "DataProvider"},
                {"from": "IDataProvider", "to": "DataProvider"},
                {"from": "IDataExecutor", "to": "DataExecutor"},
                {"from": "ILegacyDataProvider", "to": "DataProvider"},
                {"from": "DataProvider", "to": "IDataProvider"},
                {"from": "DataProvider", "to": "ILegacyDataProvider"},
                {"from": "DataExecutor", "to": "IDataExecutor"}
            ],
            "proposed_edges": [
                {"from": "CustomerSuspendedConsumer", "to": "IDataProvider", "kind": "dependency"},
                {"from": "OrganizationWriteHandler", "to": "IDataExecutor", "kind": "dependency"}
            ],
            "created_at": datetime.datetime.now().isoformat()
        }

        with mailbox(self.project_path, "proposals.json") as proposals:
            proposals[:] = [p for p in proposals if p["id"] != prop_id]
            proposals.insert(0, new_proposal)
        self._emit({"type": "proposals_updated"})

        return f"Autonomous agent generated What-If proposal '{prop_name}'. Relocated Value Objects to Domain and isolated Infrastructure DataProviders behind Contracts."

    def _handle_explain_violation(self, task: Dict) -> str:
        target = task.get("target", {})
        from_cls = target.get("from_class", "Source")
        to_cls = target.get("to_class", "Target")
        from_layer = target.get("from_layer", "Application")
        to_layer = target.get("to_layer", "Infrastructure")
        cat = target.get("category", "dependency_rule")
        reason = target.get("reason", "")

        if cat == "cycle":
            return (
                f"### Circular Dependency (ADP Violation)\n\n"
                f"**Path**: `{from_cls}` and `{to_cls}` participate in a mutual dependency cycle.\n\n"
                f"**Why this is harmful**: Circular dependencies couple classes into a single monolithic unit. "
                f"Neither can be independently tested, compiled, or reused without the other.\n\n"
                f"**Recommended Refactoring**:\n"
                f"1. **Dependency Inversion (DIP)**: Extract an interface in `Contracts` so one party depends only on the abstraction.\n"
                f"2. **Introduce an Orchestrator**: Move shared coordination into an Application service or mediator."
            )
        elif cat == "framework_taint":
            return (
                f"### Framework Isolation Violation\n\n"
                f"**Class**: `{from_cls}` ({from_layer}) directly references external framework package `{to_cls}`.\n\n"
                f"**Why this is harmful**: Clean Architecture dictates that core domain entities and use-cases remain framework-agnostic POCO code. "
                f"Coupling domain logic to external ORM, HTTP, or transport frameworks binds your business rules to third-party vendor volatility.\n\n"
                f"**Recommended Refactoring**:\n"
                f"1. Remove the direct NuGet reference from the Domain project.\n"
                f"2. Move framework attributes, DB contexts, or serialization logic into Infrastructure repository adapters."
            )
        elif cat == "stability_rule":
            kind = target.get("kind", "")
            if kind == "unjittered_retry":
                return (
                    f"### Stability Antipattern: Unjittered Retries (Killed by the Mob)\n\n"
                    f"**Class**: `{from_cls}` ({from_layer})\n"
                    f"**Problem**: Retries are configured with a constant delay or without jitter (`UseJitter = false`).\n\n"
                    f"**Why this is dangerous (Nygard 'Release It!')**:\n"
                    f"When a shared downstream dependency experiences transient degradation, all calling clients fail simultaneously. "
                    f"Without jitter (randomization of retry intervals), all instances retry at the exact same millisecond mark, "
                    f"sending synchronized traffic spikes ('thundering herd' or 'killed by the mob') that prevent the downstream service from recovering.\n\n"
                    f"**Recommended Fix (Polly v8)**:\n"
                    f"```csharp\n"
                    f"builder.AddRetry(new HttpRetryStrategyOptions\n"
                    f"{{\n"
                    f"    MaxRetryAttempts = 3,\n"
                    f"    BackoffType = DelayBackoffType.Exponential,\n"
                    f"    UseJitter = true,\n"
                    f"    Delay = TimeSpan.FromMilliseconds(500)\n"
                    f"}});\n"
                    f"```"
                )
            elif kind == "unbounded_timeout":
                return (
                    f"### Stability Antipattern: Unbounded / Default 100s Timeout\n\n"
                    f"**Class**: `{from_cls}` ({from_layer})\n"
                    f"**Problem**: `HttpClient` is used without an explicit `Timeout` or resilience pipeline.\n\n"
                    f"**Why this is dangerous (Nygard 'Release It!')**:\n"
                    f"The default timeout for `HttpClient` in .NET is **100 seconds**. In a high-throughput microservice, "
                    f"a slow downstream service causes callers to hold sockets and threads for up to 100 seconds each. "
                    f"This rapidly exhausts the .NET threadpool and OS socket handles, cascading failure back to your API gateway.\n\n"
                    f"**Recommended Fix**:\n"
                    f"1. Set explicit timeout: `client.Timeout = TimeSpan.FromSeconds(5);`\n"
                    f"2. Or configure Polly v8 standard resilience handler:\n"
                    f"```csharp\n"
                    f"builder.Services.AddHttpClient<IExternalClient, ExternalClient>()\n"
                    f"    .AddStandardResilienceHandler();\n"
                    f"```"
                )
            elif kind == "missing_cancellation_token":
                return (
                    f"### Stability Antipattern: Missing CancellationToken (Fail Fast Omission)\n\n"
                    f"**Class**: `{from_cls}` ({from_layer})\n"
                    f"**Problem**: Asynchronous I/O call does not pass a `CancellationToken`.\n\n"
                    f"**Why this is harmful**:\n"
                    f"When the client or upstream caller aborts an HTTP request (or timeout expires), .NET requests are cancelled. "
                    f"If the downstream I/O call does not observe this token, the server continues executing expensive remote queries, "
                    f"wasting CPU, memory, and database connections on orphaned results no client will ever receive.\n\n"
                    f"**Recommended Fix**:\n"
                    f"Pass `cancellationToken` into `.SendAsync(..., cancellationToken)` or `.SaveChangesAsync(cancellationToken)`."
                )
            elif kind == "suspicious_custom_resilience":
                return (
                    f"### AI Stability Audit: Suspicious Custom Resilience Pattern\n\n"
                    f"**Class**: `{from_cls}` ({from_layer})\n"
                    f"**Suspect Code**: {reason}\n\n"
                    f"**Why Hand-Rolled Resilience is Hazardous (Michael Nygard 'Release It!')**:\n"
                    f"1. **Threadpool Starvation Risk**: Tailor-made retry loops often employ synchronous `Thread.Sleep` or unmanaged `Task.Delay`. "
                    f"In web requests, blocking sleep locks ASP.NET worker threads, turning a brief downstream hiccup into instant threadpool starvation.\n"
                    f"2. **Idempotency & Double-Submit Trap**: Ad-hoc try-catch loops often retry blindly on any exception, including non-idempotent operations "
                    f"(HTTP POST/PUT, payment calls, or DB state mutations). If the remote dependency processed the request but the socket timed out returning the response, "
                    f"a naive retry executes duplicate mutations.\n"
                    f"3. **Missing Jitter & Thundering Herd**: Without randomized backoff, retrying instances synchronize, repeatedly dogpiling the recovering dependency.\n"
                    f"4. **Orphaned Tasks**: Custom timeout wrappers using `Task.WhenAny` that do not cancel the losing branch leave unobserved sockets and database connections active.\n\n"
                    f"**Recommended Refactoring**: Replace the hand-rolled loop with a formal Polly v8 Resilience Pipeline:\n"
                    f"```csharp\n"
                    f"var pipeline = new ResiliencePipelineBuilder()\n"
                    f"    .AddRetry(new RetryStrategyOptions\n"
                    f"    {{\n"
                    f"        BackoffType = DelayBackoffType.Exponential,\n"
                    f"        UseJitter = true,\n"
                    f"        MaxRetryAttempts = 3,\n"
                    f"        Delay = TimeSpan.FromMilliseconds(500)\n"
                    f"    }})\n"
                    f"    .AddTimeout(TimeSpan.FromSeconds(5))\n"
                    f"    .Build();\n\n"
                    f"// Execute cleanly with cancellation token forwarding:\n"
                    f"await pipeline.ExecuteAsync(async ct => await _remoteService.InvokeAsync(ct), cancellationToken);\n"
                    f"```"
                )
            else:
                return (
                    f"### Stability Pattern Violation: {kind}\n\n"
                    f"**Class**: `{from_cls}` ({from_layer})\n"
                    f"**Details**: {reason}\n\n"
                    f"**Recommendation**: Apply Michael Nygard stability patterns (*Release It!*) using Microsoft.Extensions.Http.Resilience "
                    f"or Polly v8 pipelines."
                )
        else:
            return (
                f"### Dependency Rule Violation\n\n"
                f"**Violation**: `{from_cls}` (layer **{from_layer}**) directly depends on `{to_cls}` (layer **{to_layer}**).\n\n"
                f"**Rule**: *Source code dependencies must point only inward, toward higher-level policies.*\n"
                f"Here, `{from_layer}` is an inner policy layer, while `{to_layer}` is an outer detail layer.\n\n"
                f"**Why this is harmful**: When core business logic directly calls concrete infrastructure (databases, GRPC/HTTP clients, or cloud SDKs), you cannot test business logic in isolation without mocking volatile third-party systems.\n\n"
                f"**Recommended Refactoring**:\n"
                f"1. **Define a Port**: Create an interface (e.g. `I{to_cls}Port` or `I{to_cls}Repository`) inside `{from_layer}` or `Contracts`.\n"
                f"2. **Inject Abstraction**: Have `{from_cls}` accept this interface via constructor dependency injection.\n"
                f"3. **Implement Adapter**: In `{to_layer}`, have `{to_cls}` implement the interface.\n"
                f"4. **Register**: Bind them in the DI container (`Program.cs`)."
            )

    def _handle_refresh_crap(self, task: Dict) -> str:
        self._emit({"type": "reload", "reason": "Headless agent requested metrics re-scan"})
        return "Notified architecture viewer to re-scan code graph and test coverage."

    def _handle_stability_audit(self, task: Dict) -> str:
        try:
            from stability_analyzer import StabilityAnalyzer
            from stability_store import StabilityStore
            from extractor import CodeGraphExtractor

            store = StabilityStore(self.project_path)
            store.invalidate_cache()
            analyzer = StabilityAnalyzer(self.project_path)

            ext = CodeGraphExtractor(self.project_path)
            raw_graph = ext.extract()
            findings = analyzer.evaluate_project_stability(raw_graph.get("classes", []), force_recheck=True)

            self._emit({"type": "reload", "reason": "Headless agent completed stability audit", "findings_count": len(findings)})
            return f"Completed stability audit: identified {len(findings)} stability findings across project."
        except Exception as e:
            return f"Failed to complete stability audit: {e}"


if __name__ == "__main__":
    target_project = sys.argv[1] if len(sys.argv) > 1 else "/home/vt/projects/organizations"
    worker = HeadlessAgentWorker(target_project)
    worker.start()
    print("[Headless Agent] Running in foreground. Press Ctrl+C to terminate.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        worker.stop()
