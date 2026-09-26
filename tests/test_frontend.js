// Run with node test_frontend.js; no browser or dependencies required.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const elements = [];
const byId = new Map();
function element() {
  const e = { innerHTML: "", dataset: {}, classList: { add() {}, remove() {} },
    appendChild() {}, addEventListener() {}, setAttribute() {}, replaceChildren() {} };
  elements.push(e);
  return e;
}
const context = {
  document: {
    getElementById(id) {
      if (!byId.has(id)) byId.set(id, element());
      return byId.get(id);
    },
    createElement: element,
    querySelectorAll: () => [],
    querySelector: () => null,
  },
  window: { addEventListener() {} }, console,
};
vm.createContext(context);
const appJsPath = path.resolve(__dirname, "..", "frontend", "app.js");
vm.runInContext(fs.readFileSync(appJsPath, "utf8"), context);

const payload = `<img src=x onerror="globalThis.injected=1">`;
context.payload = payload;
vm.runInContext(`
  graphData = {classes: [{id: 'c', name: 'C', namespace: 'App.Domain',
    layer: payload, members: [], file_path: 'C.cs'}], violations: []};
  renderLayers();
  availableProposals = [{id: payload, name: payload}];
  renderProposalSelector();
  graphData.violations = [{from_class: 'C', to_class: 'Other',
    from_layer: payload, to_layer: payload, reason: payload}];
  renderViolationsList();
  renderStandaloneViolationInspector(graphData.violations[0]);
`, context);
assert(elements.some(e => e.innerHTML.includes("&lt;img")));
assert(elements.every(e => !e.innerHTML.includes("<img")));

// After HTML attribute decoding, the handler must still receive the exact path
// as data, even with quotes, ampersands, backslashes, and code-shaped text.
context.sourcePath = `src/it's-"quoted"-&-\\-');globalThis.injected=1;//C.cs`;
vm.runInContext(`
  graphData.violations = [];
  selectClass({...graphData.classes[0], file_path: sourcePath});
`, context);
const html = byId.get("inspect-details").innerHTML;
const attribute = html.match(/onclick="(openInEditor\([^"]+)"/)[1];
const entities = { "&quot;": '"', "&#39;": "'", "&lt;": "<", "&gt;": ">", "&amp;": "&" };
const handler = attribute.replace(/&quot;|&#39;|&lt;|&gt;|&amp;/g, match => entities[match]);
let opened;
vm.runInNewContext(handler, { openInEditor(path) { opened = path; }, event: null });
assert.equal(opened, context.sourcePath);
assert.equal(context.injected, undefined);

vm.runInContext(`graphData.classes[0].layer = '__proto__'; renderLayers()`, context);
console.log("Frontend regression checks passed: escaped HTML, quoted paths, and layer names.");

// Nested namespaces collapse into cards, with cross-boundary dependencies kept.
const graph = {
  classes: [
    {id: 'a', name: 'A', namespace: 'App.Domain', layer: 'Domain'},
    {id: 'b', name: 'B', namespace: 'App.Domain.Services', layer: 'Domain'},
    {id: 'c', name: 'C', namespace: 'App.Domain.Services', layer: 'Domain'},
    {id: 'd', name: 'D', namespace: 'App.Infrastructure', layer: 'Infrastructure'},
    {id: 'e', name: 'E', namespace: 'App.DomainExtra', layer: 'Domain'},
  ],
  edges: [
    {from: 'b', to: 'd', kind: 'dependency'},
    {from: 'c', to: 'd', kind: 'dependency', violating: true},
    {from: 'a', to: 'b', kind: 'dependency'},
    {from: 'b', to: 'd', kind: 'dependency', is_omitted: true},
  ],
  violations: [{from_namespace: 'App.Domain.Services', from_class: 'C'}],
};
assert.equal(context.namespaceRoot(graph.classes), 'App');
assert.equal(context.inNamespace('App.DomainExtra', 'App.Domain'), false);
const overview = context.buildNamespaceView(graph, 'App');
assert.equal(overview.classes.length, 3);
const domain = overview.classes.find(c => c.child_namespace === 'App.Domain');
assert.equal(domain.class_count, 3);
assert.equal(domain.has_violations, true);
assert.equal(overview.edges.length, 2); // Omitted and real edges remain separate.
assert.equal(overview.edges.find(e => !e.is_omitted).count, 2);
assert.equal(overview.edges.find(e => !e.is_omitted).violating, true);
const nested = context.buildNamespaceView(graph, 'App.Domain');
assert(nested.classes.some(c => c.id === 'a'));
assert(nested.classes.some(c => c.child_namespace === 'App.Domain.Services'));
assert(nested.classes.some(c => c.child_namespace === 'App.Infrastructure' && c.external));
assert(!nested.classes.some(c => c.child_namespace === 'App.DomainExtra'));
const leaf = context.buildNamespaceView(graph, 'App.Domain.Services');
assert(leaf.classes.some(c => c.id === 'b') && leaf.classes.some(c => c.id === 'c'));
assert.equal(context.buildNamespaceView({classes: [], edges: []}, '').classes.length, 0);
assert.equal(context.namespaceRoot([{namespace: ''}, {namespace: 'App'}]), '');
console.log("Namespace regression checks passed: grouping, drill-down, boundaries, and bundled edges.");

vm.runInContext(`
  graphData.external_packages = [{package: 'Dapper', version: '',
    versions: ['2.0.0', '2.1.79'],
    version_projects: {'2.0.0': ['Other.csproj'], '2.1.79': ['App.csproj', payload]},
    types: [], referenced_by_count: 1}];
  renderNuGetList();
`, context);
const packagesHtml = byId.get('nuget-list').innerHTML;
assert(packagesHtml.includes('Consolidate versions: 2.0.0, 2.1.79'));
assert(packagesHtml.includes('2.0.0: Other.csproj'));
assert(packagesHtml.includes('2.1.79: App.csproj'));
assert(packagesHtml.includes('&lt;img'));
assert(!packagesHtml.includes('<img'));
vm.runInContext(`
  graphData.external_packages[0].versions = ['2.1.79'];
  graphData.external_packages[0].version = '2.1.79';
  renderNuGetList();
`, context);
assert(!byId.get('nuget-list').innerHTML.includes('Consolidate versions'));
assert(byId.get('nuget-list').innerHTML.includes('v2.1.79'));
console.log('NuGet regression checks passed: version conflicts, project paths, and escaped HTML.');
