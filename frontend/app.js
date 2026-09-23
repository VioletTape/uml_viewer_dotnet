/**
 * app.js - Interactive architecture visualization, violation inspector & quality metrics overlay
 */

let graphData = null;
let displayMode = "violations"; // "violations" | "focus" | "all"
let showCrapOverlay = false;
let selectedClassId = null;
let hoveredClassId = null;
const collapsedNamespaces = new Set();

// Pan & Zoom state
let scale = 1.0;
let panX = 40;
let panY = 40;
let isDragging = false;
let startX = 0;
let startY = 0;

const viewport = document.getElementById("viewport");
const canvas = document.getElementById("canvas");
const svgEdges = document.getElementById("svg-edges");
const layersContainer = document.getElementById("layers-container");

let activeProposalId = "";
let availableProposals = [];
let agentTasks = [];

async function init() {
  setupPanZoom();
  setupTabs();
  setupToolbar();
  setupLiveSync();
  await fetchProposals();
  await loadGraph();
}

function setupLiveSync() {
  const badge = document.getElementById("live-badge");
  let eventSource = null;

  function connect() {
    eventSource = new EventSource("/api/events");

    eventSource.onopen = () => {
      if (badge) {
        badge.classList.remove("disconnected");
        badge.querySelector(".live-text").textContent = "Live Sync";
      }
    };

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "reload") {
          showToast(data.reason || "Architecture updated");
          loadGraph();
        } else if (data.type === "agent_task_queued") {
          showToast(`🤖 Task queued: ${data.task.title}`);
          fetchAgentTasks();
        } else if (data.type === "agent_task_updated") {
          showToast(`🤖 Task updated: ${data.status}`);
          fetchAgentTasks();
        } else if (data.type === "proposals_updated") {
          showToast("📐 Proposals updated");
          fetchProposals();
          loadGraph();
        }
      } catch (e) {
        console.error("SSE parse error", e);
      }
    };

    eventSource.onerror = () => {
      if (badge) {
        badge.classList.add("disconnected");
        badge.querySelector(".live-text").textContent = "Reconnecting...";
      }
    };
  }

  connect();
}

function showToast(msg) {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = `⚡ ${msg}`;
  toast.classList.add("show");
  setTimeout(() => {
    toast.classList.remove("show");
  }, 3000);
}

async function openInEditor(filePath, line = 1, event = null) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  if (!filePath) return;

  showToast(`Opening in VS Code...`);
  try {
    const res = await fetch("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_path: filePath, line: parseInt(line, 10) || 1 })
    });
    const data = await res.json();
    if (res.ok && data.status === "ok") {
      showToast(`⚡ ${data.message || 'Opened in VS Code'}`);
    } else if (data.wsl_url) {
      window.location.href = data.wsl_url;
      showToast(`Redirecting to VS Code via WSL URI...`);
    } else {
      showToast(`⚠ ${data.error || 'Could not launch VS Code'}`);
    }
  } catch (err) {
    console.error("Open in editor failed:", err);
    showToast(`Error connecting to editor service: ${err.message}`);
  }
}

async function loadGraph() {
  try {
    const url = activeProposalId 
      ? `/api/graph?proposal_id=${encodeURIComponent(activeProposalId)}`
      : `/api/graph`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    graphData = await res.json();
    renderAll();
    await fetchAgentTasks();
  } catch (err) {
    console.error("Failed to load architecture graph:", err);
  }
}

async function fetchProposals() {
  try {
    const res = await fetch("/api/agent/proposals");
    if (!res.ok) return;
    availableProposals = await res.json();
    renderProposalSelector();
  } catch (err) {
    console.error("Failed to load proposals:", err);
  }
}

function renderProposalSelector() {
  const select = document.getElementById("proposal-select");
  if (!select) return;

  const currentVal = activeProposalId;
  select.innerHTML = `
    <option value="">Real Codebase</option>
    ${availableProposals.map(p => `
      <option value="${escapeHtml(p.id)}" ${p.id === currentVal ? "selected" : ""}>
        Simulate: ${escapeHtml(p.name)}
      </option>
    `).join("")}
  `;

  select.onchange = async (e) => {
    activeProposalId = e.target.value;
    if (activeProposalId) {
      const p = availableProposals.find(item => item.id === activeProposalId);
      showToast(`Switched to Proposal: ${p ? p.name : activeProposalId}`);
    } else {
      showToast("Viewing Real Codebase");
    }
    await loadGraph();
  };
}

async function fetchAgentTasks() {
  try {
    const res = await fetch("/api/agent/tasks");
    if (!res.ok) return;
    agentTasks = await res.json();
    renderCopilotTasks(agentTasks);
  } catch (err) {
    console.error("Failed to load agent tasks:", err);
  }
}

function renderCopilotTasks(tasks) {
  const container = document.getElementById("copilot-tasks-list");
  const badge = document.getElementById("tab-copilot-count");
  if (!container) return;

  const pendingCount = tasks.filter(t => t.status === "pending").length;
  if (badge) badge.textContent = pendingCount;

  if (tasks.length === 0) {
    container.innerHTML = '<div class="empty-state">No agent tasks queued. Click <strong>"Fix with Agent"</strong> on any violation card!</div>';
    return;
  }

  container.innerHTML = tasks.map(t => {
    let statusBadge = `<span class="badge badge-pending" style="background: rgba(210, 153, 34, 0.2); color: #d29922; border: 1px solid rgba(210, 153, 34, 0.4); padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600;">PENDING</span>`;
    if (t.status === "completed") {
      statusBadge = `<span class="badge badge-completed" style="background: rgba(46, 160, 67, 0.2); color: #3fb950; border: 1px solid rgba(46, 160, 67, 0.4); padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600;">COMPLETED</span>`;
    } else if (t.status === "in_progress") {
      statusBadge = `<span class="badge badge-progress" style="background: rgba(88, 166, 255, 0.2); color: #58a6ff; border: 1px solid rgba(88, 166, 255, 0.4); padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600;">IN PROGRESS</span>`;
    }

    const timeStr = t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : "";

    return `
      <div class="agent-task-card" style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin-bottom: 8px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
          <strong style="font-size: 12px; color: #e6edf3;">${escapeHtml(t.title)}</strong>
          <div style="display: flex; align-items: center; gap: 6px;">
            <span style="font-size: 10px; color: #8b949e;">${timeStr}</span>
            ${statusBadge}
          </div>
        </div>
        <p style="font-size: 11px; color: #8b949e; margin: 4px 0 8px 0; line-height: 1.4;">${escapeHtml(t.prompt)}</p>
        ${t.result ? `<div style="background: rgba(46, 160, 67, 0.1); border-left: 2px solid #3fb950; padding: 6px 8px; font-size: 11px; color: #7ee787; margin-bottom: 6px;">${escapeHtml(t.result)}</div>` : ''}
        <div style="display: flex; justify-content: space-between; align-items: center; font-size: 10px; color: #8b949e;">
          <span>ID: <code>${escapeHtml(t.id)}</code></span>
          ${t.status === 'pending' ? `<button class="btn btn-sm" onclick="markTaskResolved(${htmlJs(t.id)})" style="font-size: 9px; padding: 2px 6px;">Mark Done</button>` : ''}
        </div>
      </div>
    `;
  }).join("");
}

async function markTaskResolved(taskId) {
  try {
    await fetch("/api/agent/tasks/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: taskId, status: "completed", result: "Completed by agent" })
    });
    fetchAgentTasks();
  } catch (err) {
    console.error(err);
  }
}

function renderAll() {
  if (!graphData) return;

  // 1. Update stats & header
  const stats = graphData.stats || {};
  document.getElementById("stat-classes").querySelector(".stat-val").textContent = stats.total_classes || 0;
  document.getElementById("stat-edges").querySelector(".stat-val").textContent = stats.total_edges || 0;

  const violCount = stats.total_violations || 0;
  const violBadge = document.getElementById("stat-violations");
  violBadge.querySelector(".stat-val").textContent = violCount;
  document.getElementById("tab-viol-count").textContent = violCount;

  if (violCount > 0) {
    violBadge.classList.add("alert");
  } else {
    violBadge.classList.remove("alert");
  }

  // 2. Render Layer Columns with Namespace Accordions
  renderLayers();

  // 3. Render Violations Side Panel
  renderViolationsList();

  // 4. Render Quality & CRAP Tab
  renderQualityTab();

  // 5. Render External NuGet List
  renderNuGetList();

  // 6. Draw SVG Edges after DOM layout updates
  setTimeout(drawEdges, 60);
}

function renderLayers() {
  layersContainer.innerHTML = "";
  const classes = graphData.classes || [];

  // Group classes by layer, then by namespace
  const layerGroups = Object.create(null);
  classes.forEach(c => {
    const layer = c.layer || "Unassigned";
    if (!layerGroups[layer]) layerGroups[layer] = Object.create(null);
    const ns = c.namespace || "(Root)";
    if (!layerGroups[layer][ns]) layerGroups[layer][ns] = [];
    layerGroups[layer][ns].push(c);
  });

  const order = ["Domain", "Contracts", "Application", "Infrastructure", "Api", "Client", "Unassigned"];
  const sortedLayers = Object.keys(layerGroups).sort((a, b) => {
    let idxA = order.indexOf(a);
    let idxB = order.indexOf(b);
    if (idxA === -1) idxA = 99;
    if (idxB === -1) idxB = 99;
    return idxA - idxB;
  });

  const violations = graphData.violations || [];
  const violatingClassNames = new Set(violations.map(v => v.from_class));

  sortedLayers.forEach(layerName => {
    const column = document.createElement("div");
    column.className = `layer-column layer-${layerName}`;

    const nsMap = layerGroups[layerName];
    let totalClassesInLayer = 0;
    Object.values(nsMap).forEach(arr => totalClassesInLayer += arr.length);

    const header = document.createElement("div");
    header.className = "layer-header";
    header.innerHTML = `<span>${escapeHtml(layerName)}</span> <span class="ns-count">${totalClassesInLayer}</span>`;
    column.appendChild(header);

    const classesDiv = document.createElement("div");
    classesDiv.className = "layer-classes";

    // Sort namespaces alphabetically
    const sortedNamespaces = Object.keys(nsMap).sort();

    sortedNamespaces.forEach(ns => {
      const classList = nsMap[ns];
      const nsId = `ns-${layerName}-${ns.replace(/[^a-zA-Z0-9]/g, "_")}`;
      const isCollapsed = collapsedNamespaces.has(nsId);

      const groupDiv = document.createElement("div");
      groupDiv.className = `ns-group ${isCollapsed ? "collapsed" : ""}`;
      groupDiv.id = groupDiv.dataset.nsid = nsId;

      // Extract short name for namespace
      const nsParts = ns.split(".");
      const shortNs = nsParts.length > 2 ? nsParts.slice(-2).join(".") : ns;

      const nsHeader = document.createElement("div");
      nsHeader.className = "ns-header";
      nsHeader.title = ns;
      nsHeader.innerHTML = `
        <div class="ns-title">
          <span class="ns-toggle-icon">▼</span>
          <span>📁 ${escapeHtml(shortNs)}</span>
        </div>
        <span class="ns-count">${classList.length}</span>
      `;

      nsHeader.addEventListener("click", () => {
        if (collapsedNamespaces.has(nsId)) {
          collapsedNamespaces.delete(nsId);
          groupDiv.classList.remove("collapsed");
        } else {
          collapsedNamespaces.add(nsId);
          groupDiv.classList.add("collapsed");
        }
        setTimeout(drawEdges, 40);
      });

      groupDiv.appendChild(nsHeader);

      const nsClassesDiv = document.createElement("div");
      nsClassesDiv.className = "ns-classes";

      classList.forEach(cls => {
        const card = document.createElement("div");
        card.className = "class-card";
        card.id = `card-${cls.id}`;
        card.dataset.id = cls.id;

        if (violatingClassNames.has(cls.name)) {
          card.classList.add("has-violation");
        }

        if (cls.is_proposed) {
          card.classList.add("proposed-class");
        }

        const stereotype = cls.stereotype ? `&lt;&lt;${escapeHtml(cls.stereotype)}&gt;&gt;` : "";
        const risk = cls.risk || "green";
        const crap = cls.crap || 1;
        const proposedBadge = cls.is_proposed 
          ? `<span style="font-size: 9px; background: rgba(56, 139, 253, 0.25); color: #58a6ff; border: 1px solid rgba(56, 139, 253, 0.5); padding: 1px 4px; border-radius: 3px; font-weight: 600;" title="Proposed layer move from ${escapeHtml(cls.original_layer || 'original')}">PROPOSED</span>`
          : "";

        card.innerHTML = `
          <div class="card-top">
            <div style="display: flex; align-items: center; gap: 4px;">
              <span class="card-stereotype">${stereotype}</span>
              ${proposedBadge}
            </div>
            <div style="display: flex; align-items: center; gap: 5px;">
              <span class="crap-pill ${risk}">CRAP ${crap}</span>
              <span class="risk-dot ${risk}" title="Risk: ${risk} (CRAP ${crap})"></span>
              <span style="font-size: 10px; color: #8b949e">${escapeHtml(cls.visibility)}</span>
            </div>
          </div>
          <div class="card-name">${escapeHtml(cls.name)}</div>
          <div class="card-stats">
            <span>⚙ ${cls.members.length} members</span>
            <span>⚡ Comp ${cls.complexity || 0}</span>
            ${cls.coverage_pct ? `<span>🛡 ${cls.coverage_pct}% cov</span>` : ''}
          </div>
        `;

        card.addEventListener("click", (e) => {
          e.stopPropagation();
          selectClass(cls);
        });

        card.addEventListener("mouseenter", () => {
          hoveredClassId = cls.id;
          if (displayMode === "focus" || displayMode === "all") {
            highlightClassEdges(cls.id);
          }
        });

        card.addEventListener("mouseleave", () => {
          hoveredClassId = null;
          if (displayMode === "focus" || displayMode === "all") {
            resetEdgeHighlights();
          }
        });

        nsClassesDiv.appendChild(card);
      });

      groupDiv.appendChild(nsClassesDiv);
      classesDiv.appendChild(groupDiv);
    });

    column.appendChild(classesDiv);
    layersContainer.appendChild(column);
  });
}

function getElementAnchor(elementId) {
  let el = document.getElementById(`card-${elementId}`);
  if (!el) return null;

  // If card is inside a collapsed namespace group, anchor to the group header
  const group = el.closest(".ns-group.collapsed");
  if (group) {
    el = group.querySelector(".ns-header");
  }

  const canvasRect = canvas.getBoundingClientRect();
  const r = el.getBoundingClientRect();

  return {
    left: (r.left - canvasRect.left) / scale,
    right: (r.right - canvasRect.left) / scale,
    top: (r.top - canvasRect.top) / scale,
    bottom: (r.bottom - canvasRect.top) / scale,
    centerX: (r.left + r.width / 2 - canvasRect.left) / scale,
    centerY: (r.top + r.height / 2 - canvasRect.top) / scale,
    width: r.width / scale,
    height: r.height / scale
  };
}

function drawEdges() {
  const defs = svgEdges.querySelector("defs");
  svgEdges.innerHTML = "";
  if (defs) svgEdges.appendChild(defs);

  if (!graphData) return;

  const edges = graphData.edges || [];
  const focusId = hoveredClassId || selectedClassId;

  edges.forEach(e => {
    // Mode filtering
    if (displayMode === "violations" && !e.violating) {
      return;
    }
    if (displayMode === "focus" && !e.violating) {
      if (e.from !== focusId && e.to !== focusId) {
        return;
      }
    }

    const a1 = getElementAnchor(e.from);
    const a2 = getElementAnchor(e.to);
    if (!a1 || !a2) return;

    let x1, y1, x2, y2, c1x, c1y, c2x, c2y;

    // Case 1: Normal flow left to right
    if (a1.right < a2.left - 20) {
      x1 = a1.right;
      y1 = a1.centerY;
      x2 = a2.left;
      y2 = a2.centerY;
      const dx = Math.min(100, Math.max(30, (x2 - x1) * 0.45));
      c1x = x1 + dx;
      c1y = y1;
      c2x = x2 - dx;
      c2y = y2;
    }
    // Case 2: Reverse flow right to left (VIOLATION)
    else if (a1.left > a2.right + 20) {
      x1 = a1.left;
      y1 = a1.centerY;
      x2 = a2.right;
      y2 = a2.centerY;
      const dx = Math.min(100, Math.max(30, (x1 - x2) * 0.45));
      c1x = x1 - dx;
      c1y = y1;
      c2x = x2 + dx;
      c2y = y2;
    }
    // Case 3: Same column dependency
    else {
      x1 = a1.right;
      y1 = a1.centerY;
      x2 = a2.right;
      y2 = a2.centerY;
      const dx = Math.min(70, Math.max(30, Math.abs(y2 - y1) * 0.35));
      c1x = x1 + dx;
      c1y = y1;
      c2x = x2 + dx;
      c2y = y2;
    }

    const pathD = `M ${x1} ${y1} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${x2} ${y2}`;

    if (e.is_omitted) {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", pathD);
      path.setAttribute("class", "edge-line omitted");
      path.setAttribute("style", "stroke: #3fb950; opacity: 0.45; stroke-dasharray: 4 4;");
      path.dataset.from = e.from;
      path.dataset.to = e.to;
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `SIMULATED RESOLUTION: Decoupled ${e.from_class} -> ${e.to_class}`;
      path.appendChild(title);
      svgEdges.appendChild(path);
      return;
    }

    if (e.is_proposed) {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", pathD);
      path.setAttribute("class", "edge-line proposed");
      path.setAttribute("style", "stroke: #58a6ff; opacity: 0.85; stroke-dasharray: 6 3;");
      path.setAttribute("marker-end", "url(#arrow-highlight)");
      path.dataset.from = e.from;
      path.dataset.to = e.to;
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `PROPOSED INTERFACE: ${e.from_class} -> ${e.to_class}`;
      path.appendChild(title);
      svgEdges.appendChild(path);
      return;
    }

    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathD);
    path.setAttribute("class", `edge-line ${e.violating ? "violating" : ""}`);
    path.dataset.from = e.from;
    path.dataset.to = e.to;

    if (e.violating) {
      path.setAttribute("marker-end", "url(#arrow-viol)");
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `VIOLATION: ${e.violation_reason}`;
      path.appendChild(title);
    } else {
      path.setAttribute("marker-end", "url(#arrow-default)");
    }

    svgEdges.appendChild(path);
  });
}

function highlightClassEdges(classId) {
  document.querySelectorAll("path.edge-line").forEach(p => {
    const isFrom = p.dataset.from === classId;
    const isTo = p.dataset.to === classId;
    if (isFrom || isTo) {
      p.classList.add("highlight");
      p.classList.remove("dimmed");
      p.setAttribute("marker-end", "url(#arrow-highlight)");
    } else {
      p.classList.remove("highlight");
      p.classList.add("dimmed");
      if (!p.classList.contains("violating")) {
        p.setAttribute("marker-end", "url(#arrow-default)");
      }
    }
  });
}

function resetEdgeHighlights() {
  document.querySelectorAll("path.edge-line").forEach(p => {
    p.classList.remove("highlight", "dimmed");
    if (p.classList.contains("violating")) {
      p.setAttribute("marker-end", "url(#arrow-viol)");
    } else {
      p.setAttribute("marker-end", "url(#arrow-default)");
    }
  });
}

function selectClass(cls, activeViolation = null) {
  selectedClassId = cls.id;
  document.querySelectorAll(".class-card").forEach(c => c.classList.remove("selected"));
  const card = document.getElementById(`card-${cls.id}`);
  if (card) card.classList.add("selected");

  if (displayMode === "focus") {
    drawEdges();
  }

  // Switch to Inspector tab
  switchTab("tab-inspector");

  const inspectTitle = document.getElementById("inspect-title");
  const inspectSub = document.getElementById("inspect-subtitle");
  const details = document.getElementById("inspect-details");

  inspectTitle.textContent = cls.name;
  inspectSub.textContent = cls.namespace;

  const riskClass = cls.risk || "green";
  const crapScore = cls.crap || 1;
  const comp = cls.complexity || 0;
  const cov = cls.coverage_pct || 0;

  // 1. Gather all violations involving this class
  const classViolations = (graphData.violations || []).filter(
    v => v.from_class === cls.name || v.to_class === cls.name
  );

  let violationsHtml = "";
  if (classViolations.length > 0) {
    violationsHtml = `
      <div class="inspect-section">
        <h4 style="color: #ff7b72; display: flex; justify-content: space-between; align-items: center; border-color: rgba(248, 81, 73, 0.4);">
          <span>⚠ Clean Architecture Violations (${classViolations.length})</span>
        </h4>
        <div style="display: flex; flex-direction: column; gap: 8px;">
          ${classViolations.map((v) => {
            const isHighlighted = activeViolation && (
              activeViolation === v ||
              (activeViolation.from_class === v.from_class && activeViolation.to_class === v.to_class)
            );
            const cat = v.category || "dependency_rule";
            let catBadge = "DEPENDENCY RULE";
            let catColor = "#ff7b72";
            if (cat === "cycle") {
              catBadge = "CIRCULAR DEPENDENCY (ADP)";
              catColor = "#d2a8ff";
            } else if (cat === "framework_taint") {
              catBadge = "FRAMEWORK TAINT";
              catColor = "#f0883e";
            } else if (cat === "unassigned_layer") {
              catBadge = "UNASSIGNED TYPE";
              catColor = "#d29922";
            }

            const violSlug = `${slugify(v.from_class)}-${slugify(v.to_class || 'none')}`;
            const isOutgoing = v.from_class === cls.name;

            return `
              <div class="inspect-violation-card ${isHighlighted ? 'active-highlight' : ''}" id="inspect-viol-${violSlug}">
                <div class="viol-title" style="display: flex; justify-content: space-between; align-items: center;">
                  <span style="color: ${catColor}; font-weight: 700; font-size: 11px;">⚠ ${catBadge}</span>
                  <span style="font-size: 10px; padding: 1px 6px; border-radius: 4px; background: rgba(255,255,255,0.06); color: #8b949e;">${isOutgoing ? 'OUTGOING' : 'INCOMING'}</span>
                </div>
                <div class="viol-path" style="margin: 6px 0; font-size: 11px;">
                  <strong>${escapeHtml(v.from_class)}</strong> (${escapeHtml(v.from_layer || 'None')}) &rarr; <strong>${escapeHtml(v.to_class || 'None')}</strong> (${escapeHtml(v.to_layer || 'None')})
                </div>
                <div class="viol-reason" style="font-size: 11px; margin-bottom: 6px; word-break: break-word; overflow-wrap: anywhere;">${escapeHtml(v.reason)}</div>
                ${v.file_path ? `<div class="viol-loc" style="font-size: 10px; margin-bottom: 8px; word-break: break-all; overflow-wrap: anywhere; line-height: 1.4;">📍 ${escapeHtml(v.file_path)}:${v.line || 1}</div>` : ''}
                
                <div style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px;">
                  <button class="btn btn-sm" onclick="askAgentToFixForViolation(${htmlJs(v.from_class)}, ${htmlJs(v.to_class)}, event)" style="font-size: 11px; padding: 3px 10px; background: #238636; border-color: #2ea043; color: #fff;" title="Autonomous AI Agent generates What-If DIP decoupling proposal">
                    🤖 Fix with AI
                  </button>
                  <button class="btn btn-sm" onclick="askAgentToExplainViolation(${htmlJs(v.from_class)}, ${htmlJs(v.to_class)}, event)" style="font-size: 11px; padding: 3px 10px; background: rgba(163, 113, 247, 0.15); border-color: rgba(163, 113, 247, 0.4); color: #d2a8ff;" title="AI explains Clean Architecture principles & solution">
                    💡 Explain with AI
                  </button>
                  ${v.file_path ? `
                  <button class="btn btn-sm" onclick="openInEditor(${htmlJs(v.file_path)}, ${v.line || 1}, event)" style="font-size: 11px; padding: 3px 10px; background: rgba(56, 139, 253, 0.15); border-color: rgba(56, 139, 253, 0.4); color: #58a6ff;" title="Open in VS Code">
                    ✎ Open in VS Code
                  </button>` : ''}
                </div>

                <div id="ai-explain-box-${violSlug}" class="ai-explanation-container"></div>
              </div>
            `;
          }).join("")}
        </div>
      </div>
    `;
  } else {
    violationsHtml = `
      <div style="background: rgba(46, 160, 67, 0.08); border: 1px solid rgba(46, 160, 67, 0.3); border-radius: 6px; padding: 8px 12px; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; font-size: 11px; color: #7ee787;">
        <span>✔</span> <span>No architecture violations detected for this class.</span>
      </div>
    `;
  }

  let membersHtml = "";
  if (cls.members && cls.members.length > 0) {
    membersHtml = cls.members.map(m => `
      <div class="member-item" onclick="openInEditor(${htmlJs(cls.file_path)}, ${m.start_line || m.line || 1}, event)" style="cursor: pointer; display: flex; justify-content: space-between; align-items: center;" title="Click to open in VS Code">
        <div>
          <span style="color: #58a6ff">${escapeHtml(m.kind)}</span> 
          <strong>${escapeHtml(m.name)}</strong>
          <span style="color: #8b949e">:${m.start_line || m.line}</span>
        </div>
        <div style="display: flex; align-items: center; gap: 6px;">
          <span style="font-size: 10px; color: #8b949e">Comp ${m.complexity || 1}</span>
          <span class="crap-pill ${m.risk || 'green'}" style="display: inline-block;">CRAP ${m.crap || 1}</span>
        </div>
      </div>
    `).join("");
  } else {
    membersHtml = "<div class='empty-state'>No methods or properties recorded</div>";
  }

  details.innerHTML = `
    ${violationsHtml}

    <div class="inspect-section">
      <h4>Location</h4>
      <p style="font-family: 'JetBrains Mono', monospace; font-size: 11px; word-break: break-all; color: #e6edf3">
        ${escapeHtml(cls.file_path)}:${cls.start_line}-${cls.end_line}
      </p>
      <button onclick="openInEditor(${htmlJs(cls.file_path)}, ${cls.start_line || 1}, event)" class="vscode-btn">
        <span>✎ Open in VS Code (Line ${cls.start_line})</span>
      </button>
    </div>

    <div class="inspect-section">
      <h4>Change Risk & Quality (CRAP)</h4>
      <div style="display: flex; gap: 8px; margin-top: 6px;">
        <div class="qstat-box ${riskClass}" style="flex: 1;">
          <div class="qstat-val">${crapScore}</div>
          <div class="qstat-lbl">CRAP Score</div>
        </div>
        <div class="qstat-box" style="flex: 1;">
          <div class="qstat-val">${comp}</div>
          <div class="qstat-lbl">Complexity</div>
        </div>
        <div class="qstat-box" style="flex: 1;">
          <div class="qstat-val">${cov}%</div>
          <div class="qstat-lbl">Coverage</div>
        </div>
      </div>
    </div>

    <div class="inspect-section">
      <h4>Members (${cls.members.length})</h4>
      <div class="member-list">
        ${membersHtml}
      </div>
    </div>
  `;

  if (activeViolation) {
    const violSlug = `${slugify(activeViolation.from_class)}-${slugify(activeViolation.to_class || 'none')}`;
    const cardEl = document.getElementById(`inspect-viol-${violSlug}`);
    if (cardEl) {
      cardEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
      const vp = document.getElementById("viewport");
      if (vp) { vp.scrollTop = 0; vp.scrollLeft = 0; }
      window.scrollTo(0, 0);
    }
  }
}

function renderQualityTab() {
  const container = document.getElementById("quality-details");
  if (!container || !graphData) return;

  const qs = graphData.quality_summary || {};
  const classes = graphData.classes || [];

  // Find top 10 most complex / riskiest methods across all classes
  const allMethods = [];
  classes.forEach(cls => {
    (cls.members || []).forEach(m => {
      if (m.kind === "method" || m.kind === "constructor") {
        allMethods.push({
          ...m,
          class_name: cls.name,
          layer: cls.layer,
          file_path: cls.file_path
        });
      }
    });
  });

  allMethods.sort((a, b) => (b.crap || 0) - (a.crap || 0) || (b.complexity || 0) - (a.complexity || 0));
  const topMethods = allMethods.slice(0, 10);

  const coverageMsg = qs.has_coverage_data 
    ? `Coverage loaded from: <code>${escapeHtml(qs.coverage_file)}</code>`
    : `Coverage report not found. Complexity analyzed directly from C# syntax trees (0% coverage baseline).`;

  container.innerHTML = `
    <div style="margin-bottom: 16px;">
      <div style="font-size: 11px; color: #8b949e; margin-bottom: 8px;">${coverageMsg}</div>

      <div class="quality-meter">
        <div class="meter-green" style="width: ${qs.green_pct || 100}%" title="Low Risk: ${qs.green_pct}%"></div>
        <div class="meter-yellow" style="width: ${qs.yellow_pct || 0}%" title="Moderate Risk: ${qs.yellow_pct}%"></div>
        <div class="meter-red" style="width: ${qs.red_pct || 0}%" title="High Risk: ${qs.red_pct}%"></div>
      </div>

      <div class="quality-stats-grid">
        <div class="qstat-box green">
          <div class="qstat-val">${qs.green_count || 0}</div>
          <div class="qstat-lbl">Low Risk (&le;15)</div>
        </div>
        <div class="qstat-box yellow">
          <div class="qstat-val">${qs.yellow_count || 0}</div>
          <div class="qstat-lbl">Moderate (15-30)</div>
        </div>
        <div class="qstat-box red">
          <div class="qstat-val">${qs.red_count || 0}</div>
          <div class="qstat-lbl">CRAPpy (&gt;30)</div>
        </div>
      </div>

      <div style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border); border-radius: 6px; padding: 10px; font-size: 11px; margin-bottom: 16px;">
        <div><strong>Alberto Savoia CRAP Formula:</strong></div>
        <code style="display: block; margin-top: 4px; color: #79c0ff;">CRAP(m) = comp² × (1 - cov)³ + comp</code>
        <div style="color: #8b949e; margin-top: 6px;">Average Project CRAP: <strong>${qs.avg_crap || 0}</strong></div>
      </div>
    </div>

    <h4>Top Change-Risk Methods Needing Tests</h4>
    <div style="margin-top: 8px;">
      ${topMethods.map(m => {
        const vscodeUri = `vscode://file${m.file_path.startsWith('/') ? '' : '/'}${m.file_path}:${m.start_line || m.line || 1}`;
        return `
          <div class="leaderboard-item">
            <div class="leaderboard-title">
              <span><strong>${escapeHtml(m.class_name)}</strong>.${escapeHtml(m.name)}</span>
              <span class="crap-pill ${m.risk || 'green'}" style="display: inline-block;">CRAP ${m.crap || 1}</span>
            </div>
            <div class="leaderboard-meta">
              <span>Layer: ${escapeHtml(m.layer)}</span>
              <span>Comp: ${m.complexity || 1}</span>
              <span>Cov: ${m.coverage_pct || 0}%</span>
              <button onclick="openInEditor(${htmlJs(m.file_path)}, ${m.start_line || m.line || 1}, event)" style="background: none; border: none; color: #58a6ff; cursor: pointer; font-size: 11px; margin-left: auto;">✎ Edit</button>
            </div>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderViolationsList() {
  const container = document.getElementById("violations-list");
  const violations = graphData.violations || [];

  if (violations.length === 0) {
    container.innerHTML = '<div class="empty-state">No violations detected! Architecture is clean.</div>';
    return;
  }

  container.innerHTML = violations.map((v, i) => {
    const cat = v.category || "dependency_rule";
    let catBadge = "DEPENDENCY RULE";
    let catColor = "#ff7b72";
    if (cat === "cycle") {
      catBadge = "CIRCULAR DEPENDENCY (ADP)";
      catColor = "#d2a8ff";
    } else if (cat === "framework_taint") {
      catBadge = "FRAMEWORK TAINT";
      catColor = "#f0883e";
    } else if (cat === "unassigned_layer") {
      catBadge = "UNASSIGNED TYPE";
      catColor = "#d29922";
    }

    return `
    <div class="violation-card" onclick="inspectViolation(${i})" style="cursor: pointer;" title="Click to view details, fix or explain with AI in Inspector">
      <div class="viol-title">
        <span style="color: ${catColor}; font-weight: 600;">⚠ ${catBadge}</span>
        <span style="font-size: 10px; color: #8b949e">#${i + 1}</span>
      </div>
      <div class="viol-path">
        <strong>${escapeHtml(v.from_class)}</strong> (${escapeHtml(v.from_layer || 'None')}) → <strong>${escapeHtml(v.to_class || 'None')}</strong> (${escapeHtml(v.to_layer || 'None')})
      </div>
      <div class="viol-reason">${escapeHtml(v.reason)}</div>
      <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 8px;">
        <div class="viol-loc">📍 ${escapeHtml(v.file_path || "Unknown")}:${v.line || 1}</div>
        <div style="display: flex; align-items: center; gap: 4px; font-size: 11px; color: #58a6ff; font-weight: 500;">
          <span>Inspect & Fix</span>
          <span>&rarr;</span>
        </div>
      </div>
    </div>
  `;
  }).join("");
}

function inspectViolation(violationIndex) {
  const v = (graphData.violations || [])[violationIndex];
  if (!v) return;

  const cls = (graphData.classes || []).find(c => c.name === v.from_class);
  if (cls) {
    const card = document.getElementById(`card-${cls.id}`);
    if (card) {
      const group = card.closest(".ns-group.collapsed");
      if (group) {
        group.classList.remove("collapsed");
        collapsedNamespaces.delete(group.dataset.nsid);
        drawEdges();
      }
      centerOnElement(card);
    }
    selectClass(cls, v);
  } else {
    switchTab("tab-inspector");
    renderStandaloneViolationInspector(v);
  }
}

function renderStandaloneViolationInspector(v) {
  const inspectTitle = document.getElementById("inspect-title");
  const inspectSub = document.getElementById("inspect-subtitle");
  const details = document.getElementById("inspect-details");

  inspectTitle.textContent = v.from_class;
  inspectSub.textContent = `Layer: ${v.from_layer || 'Unassigned'}`;

  const cat = v.category || "dependency_rule";
  let catBadge = "DEPENDENCY RULE";
  let catColor = "#ff7b72";
  if (cat === "cycle") { catBadge = "CIRCULAR DEPENDENCY (ADP)"; catColor = "#d2a8ff"; }
  else if (cat === "framework_taint") { catBadge = "FRAMEWORK TAINT"; catColor = "#f0883e"; }
  else if (cat === "unassigned_layer") { catBadge = "UNASSIGNED TYPE"; catColor = "#d29922"; }

  const violSlug = `${slugify(v.from_class)}-${slugify(v.to_class || 'none')}`;

  details.innerHTML = `
    <div class="inspect-section">
      <h4 style="color: #ff7b72;">⚠ Architecture Violation</h4>
      <div class="inspect-violation-card active-highlight">
        <div class="viol-title" style="display: flex; justify-content: space-between; align-items: center;">
          <span style="color: ${catColor}; font-weight: 700; font-size: 11px;">⚠ ${catBadge}</span>
        </div>
        <div class="viol-path" style="margin: 6px 0; font-size: 11px; word-break: break-word; overflow-wrap: anywhere;">
          <strong>${escapeHtml(v.from_class)}</strong> (${escapeHtml(v.from_layer || 'None')}) &rarr; <strong>${escapeHtml(v.to_class || 'None')}</strong> (${escapeHtml(v.to_layer || 'None')})
        </div>
        <div class="viol-reason" style="font-size: 11px; margin-bottom: 6px; word-break: break-word; overflow-wrap: anywhere;">${escapeHtml(v.reason)}</div>
        ${v.file_path ? `<div class="viol-loc" style="font-size: 10px; margin-bottom: 8px; word-break: break-all; overflow-wrap: anywhere; line-height: 1.4;">📍 ${escapeHtml(v.file_path)}:${v.line || 1}</div>` : ''}

        <div style="display: flex; gap: 6px; flex-wrap: wrap;">
          <button class="btn btn-sm" onclick="askAgentToFixForViolation(${htmlJs(v.from_class)}, ${htmlJs(v.to_class)}, event)" style="font-size: 11px; padding: 4px 10px; background: #238636; border-color: #2ea043; color: #fff;">
            🤖 Fix with AI
          </button>
          <button class="btn btn-sm" onclick="askAgentToExplainViolation(${htmlJs(v.from_class)}, ${htmlJs(v.to_class)}, event)" style="font-size: 11px; padding: 4px 10px; background: rgba(163, 113, 247, 0.15); border-color: rgba(163, 113, 247, 0.4); color: #d2a8ff;">
            💡 Explain with AI
          </button>
          ${v.file_path ? `
          <button class="btn btn-sm" onclick="openInEditor(${htmlJs(v.file_path)}, ${v.line || 1}, event)" style="font-size: 11px; padding: 4px 10px; background: rgba(56, 139, 253, 0.15); border-color: rgba(56, 139, 253, 0.4); color: #58a6ff;">
            ✎ Open in VS Code
          </button>` : ''}
        </div>

        <div id="ai-explain-box-${violSlug}" class="ai-explanation-container"></div>
      </div>
    </div>
  `;
}

async function askAgentToFixForViolation(fromClass, toClass, event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  const v = (graphData.violations || []).find(
    x => x.from_class === fromClass && (!toClass || x.to_class === toClass)
  );
  if (!v) return;

  const prompt = `Refactor '${v.from_class}' to eliminate the Clean Architecture violation with '${v.to_class}'. Apply the Dependency Inversion Principle (DIP) or introduce an interface/port abstraction in the Contracts/Application layer.`;

  showToast(`Queueing DIP fix for Autonomous Agent...`);
  try {
    const res = await fetch("/api/agent/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        op: "fix_violation",
        title: `Decouple ${v.from_class} from ${v.to_class}`,
        target: v,
        prompt: prompt
      })
    });
    const data = await res.json();
    if (data.status === "ok") {
      showToast(`🤖 Task queued for Headless Agent!`);
      await fetchAgentTasks();
    }
  } catch (err) {
    showToast(`Error queueing agent task: ${err.message}`);
  }
}

async function askAgentToExplainViolation(fromClass, toClass, event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  const v = (graphData.violations || []).find(
    x => x.from_class === fromClass && (!toClass || x.to_class === toClass)
  );
  if (!v) return;

  const violSlug = `${slugify(v.from_class)}-${slugify(v.to_class || 'none')}`;
  const box = document.getElementById(`ai-explain-box-${violSlug}`);
  if (box) {
    box.style.display = "block";
    box.innerHTML = `<div style="display: flex; align-items: center; gap: 8px; color: #d2a8ff;">
      <span class="live-dot" style="background: #a371f7;"></span>
      <span>AI Architecture Expert is analyzing violation...</span>
    </div>`;
  }

  showToast(`💡 Asking AI to explain architecture rule...`);
  try {
    const res = await fetch("/api/agent/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        op: "explain_violation",
        title: `Explain Clean Architecture rule for ${v.from_class} -> ${v.to_class}`,
        target: v,
        prompt: `Explain why ${v.from_class} depending on ${v.to_class} violates Clean Architecture and provide refactoring steps.`
      })
    });
    const data = await res.json();
    if (data.status === "ok") {
      const taskId = data.task.id;
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        const tasksRes = await fetch("/api/agent/tasks");
        const tasks = await tasksRes.json();
        const updated = tasks.find(t => t.id === taskId);
        if (updated && updated.status === "completed" && updated.result) {
          clearInterval(poll);
          if (box) {
            let html = escapeHtml(updated.result)
              .replace(/### (.*?)\n/g, '<h3 style="color: #d2a8ff; margin: 4px 0 8px 0; font-size: 12px;">$1</h3>')
              .replace(/\*\*(.*?)\*\*/g, '<strong style="color: #f0f6fc;">$1</strong>')
              .replace(/`([^`]+)`/g, '<code style="background: rgba(255,255,255,0.1); padding: 1px 4px; border-radius: 3px; font-family: monospace;">$1</code>')
              .replace(/\n\n/g, '<br><br>');
            box.innerHTML = html;
          }
          showToast(`💡 Explanation ready!`);
        } else if (attempts > 12) {
          clearInterval(poll);
        }
      }, 350);
    }
  } catch (err) {
    if (box) box.textContent = `Failed to get explanation: ${err.message}`;
  }
}

function focusViolation(className) {
  const card = Array.from(document.querySelectorAll(".class-card"))
    .find(c => c.querySelector(".card-name").textContent === className);

  if (card) {
    // If inside collapsed namespace, expand it
    const group = card.closest(".ns-group.collapsed");
    if (group) {
      group.classList.remove("collapsed");
      collapsedNamespaces.delete(group.dataset.nsid);
      drawEdges();
    }
    centerOnElement(card);
    card.click();
  }
}

function centerOnElement(el) {
  if (!el) return;
  const vp = document.getElementById("viewport");
  if (!vp) return;

  // Reset any browser native scroll so sticky or transformed elements never get displaced
  vp.scrollTop = 0;
  vp.scrollLeft = 0;
  window.scrollTo(0, 0);

  const elRect = el.getBoundingClientRect();
  const vpRect = vp.getBoundingClientRect();

  // Find centers in viewport screen coordinates
  const elCenterX = elRect.left + elRect.width / 2;
  const elCenterY = elRect.top + elRect.height / 2;

  const vpCenterX = vpRect.left + vpRect.width / 2;
  const vpCenterY = vpRect.top + vpRect.height / 2;

  // Delta to center el in viewport
  panX += (vpCenterX - elCenterX);
  panY += (vpCenterY - elCenterY);

  updateTransform();
  drawEdges();
}

function renderNuGetList() {
  const container = document.getElementById("nuget-list");
  const packages = graphData.external_packages || [];

  if (packages.length === 0) {
    container.innerHTML = '<div class="empty-state">No external packages found</div>';
    return;
  }

  container.innerHTML = packages.map(p => `
    <div class="violation-card" style="background: rgba(56, 139, 253, 0.08); border-color: rgba(56, 139, 253, 0.3)">
      <div style="font-weight: 700; color: #58a6ff; font-size: 13px">📦 ${escapeHtml(p.package)}</div>
      <div style="font-size: 11px; color: #8b949e; margin-top: 4px">
        Referenced by ${p.referenced_by_count} classes
      </div>
      ${p.types.length ? `<div style="font-size: 10px; color: #c9d1d9; margin-top: 6px; font-family: monospace">Sample types: ${p.types.map(escapeHtml).join(", ")}</div>` : ''}
    </div>
  `).join("");
}

function setupPanZoom() {
  updateTransform();

  viewport.addEventListener("scroll", () => {
    viewport.scrollTop = 0;
    viewport.scrollLeft = 0;
  });

  viewport.addEventListener("mousedown", e => {
    if (e.target.closest(".class-card") || e.target.closest(".ns-header") || e.target.closest("button")) return;
    isDragging = true;
    startX = e.clientX - panX;
    startY = e.clientY - panY;
  });

  window.addEventListener("mousemove", e => {
    if (!isDragging) return;
    panX = e.clientX - startX;
    panY = e.clientY - startY;
    updateTransform();
  });

  window.addEventListener("mouseup", () => {
    isDragging = false;
  });

  viewport.addEventListener("wheel", e => {
    e.preventDefault();
    const zoomFactor = e.deltaY < 0 ? 1.08 : 0.92;
    scale = Math.min(Math.max(0.2, scale * zoomFactor), 2.5);
    updateTransform();
  }, { passive: false });
}

function updateTransform() {
  canvas.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
}

function setupToolbar() {
  const btnViol = document.getElementById("btn-filter-violations");
  const btnFocus = document.getElementById("btn-filter-focus");
  const btnAll = document.getElementById("btn-filter-all");
  const btnCrap = document.getElementById("btn-toggle-crap");

  function setMode(mode) {
    displayMode = mode;
    btnViol.classList.toggle("active", mode === "violations");
    btnFocus.classList.toggle("active", mode === "focus");
    btnAll.classList.toggle("active", mode === "all");
    drawEdges();
  }

  btnViol.addEventListener("click", () => setMode("violations"));
  btnFocus.addEventListener("click", () => setMode("focus"));
  btnAll.addEventListener("click", () => setMode("all"));

  btnCrap.addEventListener("click", () => {
    showCrapOverlay = !showCrapOverlay;
    btnCrap.classList.toggle("active", showCrapOverlay);
    canvas.classList.toggle("show-crap", showCrapOverlay);
  });

  document.getElementById("btn-zoom-in").addEventListener("click", () => {
    scale = Math.min(2.5, scale * 1.2);
    updateTransform();
  });

  document.getElementById("btn-zoom-out").addEventListener("click", () => {
    scale = Math.max(0.2, scale / 1.2);
    updateTransform();
  });

  document.getElementById("btn-zoom-reset").addEventListener("click", () => {
    scale = 1.0;
    panX = 40;
    panY = 40;
    updateTransform();
  });

  document.getElementById("btn-refresh").addEventListener("click", loadGraph);

  const btnProposeAll = document.getElementById("btn-agent-propose-all");
  if (btnProposeAll) {
    btnProposeAll.addEventListener("click", async () => {
      showToast("Queuing AI Copilot architecture proposal...");
      await fetch("/api/agent/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          op: "propose_refactor",
          title: "Propose Clean Architecture Decoupling",
          prompt: "Analyze all current violations and create a What-If proposal decoupling the core Domain and Application layers from Infrastructure implementations."
        })
      });
      await fetchAgentTasks();
      switchTab("tab-copilot");
    });
  }

  const btnRefreshMetrics = document.getElementById("btn-agent-refresh-metrics");
  if (btnRefreshMetrics) {
    btnRefreshMetrics.addEventListener("click", async () => {
      showToast("Requesting metrics & test coverage refresh...");
      await fetch("/api/agent/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          op: "refresh_crap",
          title: "Re-run Coverage & Recalculate CRAP",
          prompt: "Execute dotnet test --collect:\"XPlat Code Coverage\" and recalculate cyclomatic complexity and CRAP scores."
        })
      });
      await fetchAgentTasks();
      switchTab("tab-copilot");
    });
  }
}

function setupTabs() {
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      switchTab(btn.dataset.tab);
    });
  });
}

function switchTab(tabId) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));

  const targetBtn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
  const targetContent = document.getElementById(tabId);

  if (targetBtn) targetBtn.classList.add("active");
  if (targetContent) targetContent.classList.add("active");
}

function escapeHtml(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function htmlJs(value) {
  return escapeHtml(JSON.stringify(value ?? ""));
}

function slugify(text) {
  return String(text || "").replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
}

window.addEventListener("DOMContentLoaded", init);
window.addEventListener("resize", drawEdges);
