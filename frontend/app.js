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

async function init() {
  setupPanZoom();
  setupTabs();
  setupToolbar();
  setupLiveSync();
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
    const res = await fetch("/api/graph");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    graphData = await res.json();
    renderAll();
  } catch (err) {
    console.error("Failed to load architecture graph:", err);
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
  const layerGroups = {};
  classes.forEach(c => {
    const layer = c.layer || "Unassigned";
    if (!layerGroups[layer]) layerGroups[layer] = {};
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
    header.innerHTML = `<span>${layerName}</span> <span class="ns-count">${totalClassesInLayer}</span>`;
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

        const stereotype = cls.stereotype ? `&lt;&lt;${cls.stereotype}&gt;&gt;` : "";
        const risk = cls.risk || "green";
        const crap = cls.crap || 1;

        card.innerHTML = `
          <div class="card-top">
            <span class="card-stereotype">${stereotype}</span>
            <div style="display: flex; align-items: center; gap: 5px;">
              <span class="crap-pill ${risk}">CRAP ${crap}</span>
              <span class="risk-dot ${risk}" title="Risk: ${risk} (CRAP ${crap})"></span>
              <span style="font-size: 10px; color: #8b949e">${cls.visibility || ""}</span>
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

function selectClass(cls) {
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

  const vscodeUri = `vscode://file${cls.file_path.startsWith('/') ? '' : '/'}${cls.file_path}:${cls.start_line || 1}`;
  const riskClass = cls.risk || "green";
  const crapScore = cls.crap || 1;
  const comp = cls.complexity || 0;
  const cov = cls.coverage_pct || 0;

  let membersHtml = "";
  if (cls.members && cls.members.length > 0) {
    membersHtml = cls.members.map(m => `
      <div class="member-item" onclick="openInEditor('${cls.file_path}', ${m.start_line || m.line || 1}, event)" style="cursor: pointer; display: flex; justify-content: space-between; align-items: center;" title="Click to open in VS Code">
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
    <div class="inspect-section">
      <h4>Location</h4>
      <p style="font-family: 'JetBrains Mono', monospace; font-size: 11px; word-break: break-all; color: #e6edf3">
        ${cls.file_path}:${cls.start_line}-${cls.end_line}
      </p>
      <button onclick="openInEditor('${cls.file_path}', ${cls.start_line || 1}, event)" class="vscode-btn">
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
              <button onclick="openInEditor('${m.file_path}', ${m.start_line || m.line || 1}, event)" style="background: none; border: none; color: #58a6ff; cursor: pointer; font-size: 11px; margin-left: auto;">✎ Edit</button>
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
    <div class="violation-card" onclick="focusViolation('${v.from_class}')">
      <div class="viol-title">
        <span style="color: ${catColor}; font-weight: 600;">⚠ ${catBadge}</span>
        <span style="font-size: 10px; color: #8b949e">#${i + 1}</span>
      </div>
      <div class="viol-path">
        <strong>${escapeHtml(v.from_class)}</strong> (${v.from_layer || 'None'}) → <strong>${escapeHtml(v.to_class || 'None')}</strong> (${v.to_layer || 'None'})
      </div>
      <div class="viol-reason">${escapeHtml(v.reason)}</div>
      <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px;">
        <div class="viol-loc">📍 ${escapeHtml(v.file_path || "Unknown")}:${v.line || 1}</div>
        ${v.file_path ? `<button class="btn btn-sm" onclick="openInEditor('${v.file_path}', ${v.line || 1}, event)" style="font-size: 10px; padding: 2px 8px; background: rgba(56, 139, 253, 0.15); border-color: rgba(56, 139, 253, 0.4); color: #58a6ff;" title="Open in VS Code">✎ Edit</button>` : ''}
      </div>
    </div>
  `;
  }).join("");
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
    card.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
    card.click();
  }
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
      ${p.types.length ? `<div style="font-size: 10px; color: #c9d1d9; margin-top: 6px; font-family: monospace">Sample types: ${p.types.join(", ")}</div>` : ''}
    </div>
  `).join("");
}

function setupPanZoom() {
  updateTransform();

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
  if (!str) return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

window.addEventListener("DOMContentLoaded", init);
window.addEventListener("resize", drawEdges);
