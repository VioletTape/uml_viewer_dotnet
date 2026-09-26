# Operational Resilience & Stability Patterns Guide

> **"Most outages are caused by software that reacts poorly to unexpected conditions in the environment, rather than outright hardware failures."**  
> — Michael Nygard, *Release It! Design and Deploy Production-Ready Software*

The **.NET Clean Architecture & UML Viewer** extends beyond classical structural checks (such as Clean Architecture layer boundaries) to provide an **operational resilience radar**. It automatically analyzes your C# codebase and Kubernetes/Helm deployment manifests to uncover stability antipatterns before they manifest as production incidents.

---

## 1. Why Stability Patterns Matter

In modern distributed .NET architectures, satisfying the **Dependency Inversion Principle (DIP)**—having an interface in `Application` and its implementation in `Infrastructure`—is necessary, but not sufficient.

```
┌─────────────────┐       ┌────────────────────┐       ┌──────────────────────┐
│   Application   │ ───►  │   Infrastructure   │ ───►  │  Remote Dependency   │
│   (Domain/Use)  │       │ (HttpClient/Polly) │       │ (Payment / Partner)  │
└─────────────────┘       └────────────────────┘       └──────────────────────┘
   Clean Architecture        Operational Boundary          Source of Latency
   Inward Dependency ✓       Timeouts & Jitter?             Hanging or Flapping
```

If an `Infrastructure` adapter communicates with an external HTTP service, message broker, or database without resilience safeguards:
- A brief network pause can exhaust socket pools.
- An unjittered retry policy can synchronize across multiple Kubernetes pods and crush recovering downstreams (**"Killed by the Mob"**).
- A missing `CancellationToken` keeps expensive database connections and CPU threads tied up doing work for requests that were already cancelled by the user.

By evaluating both **spatial boundaries** (Clean Architecture) and **temporal boundaries** (Stability Patterns), you get continuous visibility into both code design and operational health.

---

## 2. Daily Workflow & Practical Usage

### Zero-Friction Inspection
You do not need to trigger scans or remember special flags. When you start the viewer:
```bash
uml start
uml open
```
1. **Automatic Discovery**: Stability findings are computed and integrated directly into the **Violations** panel under the `STABILITY (NYGARD)` badge (`#f778ba`).
2. **Zero Token Overhead**: Initial checks and reloads consume **0 LLM tokens**. Findings are validated against source signatures and cached locally in `.uml-viewer/stability_findings.json`. Loading the dashboard takes milliseconds.
3. **Live Sync**: Whenever source files or deployment manifests change, the viewer updates automatically over Server-Sent Events (SSE).

### Instant Re-Check
If you modify retry options or timeouts in your code or tweak replica counts in your infrastructure manifests, you can force an instant re-scan at any time:
- Click the **Re-check Stability** button in the dashboard, or
- Trigger the endpoint via CLI:
  ```bash
  curl -X POST http://localhost:5050/api/stability/recheck
  ```

### Automatic Test Project & Folder Exclusion
Unit tests and integration tests frequently mock remote adapters, use synchronous test delays (`Thread.Sleep`), omit cancellation tokens, or instantiate lightweight test `HttpClient` instances. Additionally, test projects depend across multiple layers and do not follow production architectural boundaries.

To eliminate noise and prevent false positives:
- Any file or class located within folders named `test` or `tests` (**case-insensitive**: `test`, `tests`, `Test`, `Tests`, `TEST`, `TESTS`), or project folders ending in `.Tests` / `.Test` (e.g. `Billing.Tests`, `IntegrationTests`), is **automatically excluded** from architectural violations, layer completeness warnings, and stability pattern findings.
- Upstream parent directories (such as running inside `/home/test/...`) are resolved safely without accidentally suppressing real production code.

### Class-Level (Type) Consolidation for High Signal-to-Noise Ratio
In real-world applications, a database repository or API client (such as `DBAccess` or `PaymentGatewayClient`) may execute dozens of queries or remote requests. Reporting a separate violation card for every single method invocation clutters the UI and obscures other architectural defects.

To maintain a high signal-to-noise ratio:
- Violations are **consolidated by class (type)** and file path.
- The class is flagged once with an aggregated occurrences count (`x calls`), an overview of all affected lines, and a breakdown of issue types.
- Selecting the class or clicking **Inspect & Fix** reveals the full list of individual call sites and line numbers directly in the Inspector drawer with one-click editor navigation.

### Collapsible Category Accordion with Finding Counts
The **Violations** tab groups findings by architectural categories:
- **Stability Patterns (Nygard)** (`stability_rule`)
- **Dependency Rules** (`dependency_rule`)
- **Circular Dependencies (ADP)** (`cycle`)
- **Framework Isolation** (`framework_taint`)
- **Layer Completeness** (`unassigned_layer`)

**User Experience Highlights**:
- **Collapsed by Default**: Opening the Violations tab displays a clean, non-overwhelming summary where all categories start collapsed.
- **Header Counts**: Each category header displays `<topic> (count)` (e.g. `Stability Patterns (Nygard) (21)` and `Dependency Rules (5)`) along with error/warning badges.
- **Convenience Controls**: Easily expand or collapse individual topics with a click, or use the **Expand All** / **Collapse All** toolbar buttons.
- **Inspector Deep-Linking**: Clicking a violation on the canvas or selecting a class automatically expands the relevant category.

---

## 3. How to Interpret Results

Stability findings display with a severity rating (`ERROR` or `WARNING`), the offending class, the integration point, and the specific failure mode.

### Severity Guidelines
- **`ERROR` (Critical Operational Risk)**: The issue is compounded by deployment topology (e.g. multiple replicas amplifying a retry storm) or represents an immediate risk of threadpool starvation or cascading failure.
- **`WARNING` (Resilience Defect)**: The code relies on fragile defaults, catches generic exceptions, or omits cancellation propagation that could leak resources under load.

---

### Key Pattern Interpretations

#### 1. Unbounded / Default Timeout (`unbounded_timeout`)
* **What the Radar Found**: An `HttpClient` is instantiated or registered without an explicit `Timeout` configuration.
* **Why It Is Dangerous**: In .NET, the default `HttpClient.Timeout` is **100 seconds**. If a remote microservice or 3rd-party API begins hanging, client sockets and ASP.NET threadpool threads stay locked for over a minute and a half per request. Under moderate traffic, this exhausts connection pools and causes the entire host application to become unresponsive.
* **How to Resolve**: Configure an explicit, aggressive timeout matching your SLA:
  ```csharp
  // Via HttpClientFactory
  services.AddHttpClient<IWeatherService, WeatherService>(client =>
  {
      client.Timeout = TimeSpan.FromSeconds(5);
  });
  
  // Or via Microsoft.Extensions.Resilience (Polly v8)
  services.AddHttpClient<IWeatherService, WeatherService>()
          .AddStandardResilienceHandler(options =>
          {
              options.TotalRequestTimeout.Timeout = TimeSpan.FromSeconds(5);
          });
  ```

---

#### 2. Unjittered Retries & "Killed by the Mob" (`unjittered_retry`)
* **What the Radar Found**: A Polly retry strategy uses a fixed interval (`DelayBackoffType.Constant`) or explicitly disables jitter (`UseJitter = false`).
* **Why It Is Dangerous**: When your service runs across multiple instances (detected automatically from `override.yaml` or `workload.yaml` where `replicaCount > 1`), any outage at a downstream dependency will cause **all pods to retry simultaneously**. 
  
  When the downstream service attempts to recover, the synchronized wave of retries hits it at the exact same millisecond interval, driving it back down. This is Michael Nygard's classic **"Killed by the Mob" (dogpiling)** antipattern. The viewer escalates this to an **`ERROR`** when multiple replicas are active.
* **How to Resolve**: Enable exponential backoff with decorrelated jitter:
  ```csharp
  // Polly v8
  builder.AddRetry(new HttpRetryStrategyOptions
  {
      BackoffType = DelayBackoffType.Exponential,
      UseJitter = true,
      MaxRetryAttempts = 3,
      Delay = TimeSpan.FromMilliseconds(500)
  });
  ```

---

#### 3. Missing Cancellation Tokens (`missing_cancellation_token`)
* **What the Radar Found**: Asynchronous integration calls (`GetAsync`, `SendAsync`, `SaveChangesAsync`, `ExecuteAsync`) do not pass a `CancellationToken`, or a method receives a `cancellationToken` parameter but fails to forward it.
* **Why It Is Dangerous**: If an incoming web request is aborted (e.g. the user closes the browser or an upstream API gateway times out), the cancellation signal stops at the boundary. Your server continues executing database queries or remote HTTP requests to completion, pointlessly consuming database connections, network bandwidth, and memory.
* **How to Resolve**: Always pass the `CancellationToken` through to all async APIs:
  ```csharp
  public async Task<Order> GetOrderAsync(int id, CancellationToken cancellationToken)
  {
      // Pass cancellationToken forward
      var response = await _httpClient.GetAsync($"/api/orders/{id}", cancellationToken);
      return await response.Content.ReadFromJsonAsync<Order>(cancellationToken: cancellationToken);
  }
  ```

---

#### 4. Catch-All Exception Retries (`generic_exception_retry`)
* **What the Radar Found**: A retry policy catches generic `System.Exception` or unconstrained error filters instead of transient network/HTTP exceptions.
* **Why It Is Dangerous**: Non-transient errors (e.g. `400 Bad Request`, `401 Unauthorized`, database constraint violations, or `NullReferenceException`) will never succeed on a retry. Retrying them wastes compute, delays error feedback to users, clutters audit logs, and risks executing duplicate unintended side-effects on non-idempotent operations.
* **How to Resolve**: Filter strictly for transient failures:
  ```csharp
// Only retry transient network failures and 5xx / 429 status codes
  builder.AddRetry(new HttpRetryStrategyOptions
  {
      ShouldHandle = new PredicateBuilder<HttpResponseMessage>()
          .Handle<HttpRequestException>()
          .HandleResult(res => res.StatusCode == HttpStatusCode.RequestTimeout ||
                               res.StatusCode == HttpStatusCode.TooManyRequests ||
                               (int)res.StatusCode >= 500)
  });
  ```

---

#### 5. Suspicious Custom Resilience Loops (`suspicious_custom_resilience`)
* **What the Radar Found**: A hand-rolled `for`/`while` loop wrapping an external I/O anchor (`HttpClient`, `DbContext`, Dapper, Redis, or Message Broker) with `Thread.Sleep`, `Task.Delay`, or retry counters; OR a naive `Task.WhenAny` timeout wrapper without linked cancellation.
* **Why Hand-Rolled Resilience is Hazardous**:
  1. **Threadpool Starvation**: Tailor-made retry loops frequently use blocking `Thread.Sleep(1000)`. In ASP.NET Core, synchronous sleep starves the worker threadpool, converting a minor downstream latency bump into immediate process-wide unresponsive outages.
  2. **Idempotency & Duplicate Mutations**: Hand-rolled retry loops rarely evaluate HTTP method semantics or state mutation idempotency. If a mutating call (POST/PUT/payment charge) timed out on the return trip, a naive retry loop executes duplicate charges.
  3. **"Killed by the Mob" Thundering Herds**: Hand-rolled delays lack randomized decorrelated jitter, guaranteeing that multiple pods will retry in unison.
  4. **Orphaned Background Leaks**: Naive timeouts using `Task.WhenAny(remoteTask, Task.Delay(5000))` without explicitly cancelling `remoteTask` leave background network sockets and database queries running silently until completion.
* **How It Works (2-Step Scout + AI Audit Workflow)**:
  1. **Step 1: Hardened Scout (Local AST / 0 Tokens)**: The Roslyn analyzer evaluates the **Triad Filter** (Remote I/O Anchor + Loop/Catch + Delay/Counter) to filter out benign pagination loops or consumer pumps with near-zero false positives. It flags the site with the amber badge `⚠ SCOUTED (CUSTOM RESILIENCE)`.
  2. **Step 2: On-Demand AI Audit**: Click **`🤖 Audit with AI`** directly in the Inspector. The AI agent inspects the method semantics, checks mutation idempotency and threadpool blocking risk, and generates a What-If refactoring proposal migrating the custom loop to a standard Polly v8 `ResiliencePipeline`.

## 4. When and How AI is Used

The system is deliberately designed with a **deterministic-first, AI-on-demand** philosophy to prevent unnecessary token consumption and latency.

```
┌────────────────────────────────────────────────────────┐
│  Continuous Analysis (Zero Tokens / Local Only)       │
│  • CodeGraph SQLite index queries                      │
│  • High-speed Roslyn syntax & AST analyzer             │
│  • Kubernetes override.yaml / workload.yaml correlation │
│  • Hash-based mailbox cache (zero-cost reloads)        │
└────────────────────────────────────────────────────────┘
                           │
                           ▼ (User Action or Arch Simulation)
┌────────────────────────────────────────────────────────┐
│  Autonomous AI Agent (Token Budgeted / On-Demand)      │
│  • 💡 Explain with AI: Contextual blast radius         │
│  • 🤖 Fix with AI: What-If Resilience Proposals        │
│  • Multi-service timeout budget hierarchy alignment    │
│  • Idempotency analysis for stateful endpoints         │
└────────────────────────────────────────────────────────┘
```

### When AI is NOT Used
- **Background scanning and graph loading never query an LLM.**
- Pattern detection, timeout checks, cancellation token analysis, and multi-instance correlation run via local C# Roslyn analysis and Python rule evaluation in milliseconds.

### When AI IS Used
AI is invoked only when human context, semantic synthesis, or generative refactoring is needed:

1. **`💡 Explain with AI`**:
   - Clicking this on a stability violation asks the Headless AI Agent to analyze the operational impact in the context of your specific class and upstream callers.
   - It explains *why* this particular dependency is vulnerable to failure and provides immediate mitigation steps.

2. **`🤖 Fix with AI` (What-If Resilience Proposals)**:
   - Clicking this prompts the AI to generate a **Resilience What-If Proposal** (e.g. proposing an injection of Polly v8 `AddStandardResilienceHandler`).
   - The proposal appears in the **What-If Simulations** tray (`📐 Arch Mode`) where you can preview the architectural changes and review code diffs before applying anything to disk.

3. **Distributed Timeout Budget Reasoning**:
   - When evaluating multi-hop chains (e.g., Ingress Gateway -> API -> Adapter -> Database), the agent validates that timeout budgets are monotonically decreasing:
     $$\text{Ingress Timeout} > \text{Service Request Timeout} > \text{Dependency Socket Timeout}$$
   - If a downstream timeout exceeds an upstream deadline, the AI flags the budget misalignment.

---

## 5. Summary Cheat Sheet

| Stability Finding | Primary Risk | Deployment Impact | Standard Remedy |
| :--- | :--- | :--- | :--- |
| **`unbounded_timeout`** | Socket pool exhaustion & hanging threads | Host lockup under remote degradation | Explicit `Timeout` or `AddStandardResilienceHandler` |
| **`unjittered_retry`** | "Killed by the Mob" retry storm | Downstream crushed across replicas | Exponential backoff with `UseJitter = true` |
| **`missing_cancellation_token`** | Orphaned compute & database connection leaks | Resource exhaustion on cancelled requests | Forward `CancellationToken` to all async calls |
| **`generic_exception_retry`** | Pointless retry delays & duplicate mutations | Masked bugs and log pollution | Constrain `ShouldHandle` to transient errors only |
| **`suspicious_custom_resilience`** | Threadpool starvation, non-idempotent duplicate writes | Dogpiling & unobserved socket leaks | On-Demand AI Audit & migrate to Polly v8 pipeline |
