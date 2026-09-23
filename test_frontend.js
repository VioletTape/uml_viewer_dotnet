// Run with node test_frontend.js; no browser or dependencies required.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const elements = [];
const byId = new Map();
function element() {
  const e = { innerHTML: "", dataset: {}, classList: { add() {}, remove() {} },
    appendChild() {}, addEventListener() {} };
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
vm.runInContext(fs.readFileSync(`${__dirname}/frontend/app.js`, "utf8"), context);

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
