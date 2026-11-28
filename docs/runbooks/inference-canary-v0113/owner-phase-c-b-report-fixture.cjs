"use strict";

// Exact output of the qualified synthetic B host, including its actual fence
// controller. No report field is relabelled and no live result is claimed.
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { createHash } = require("node:crypto");
const sha = (v) => createHash("sha256").update(v).digest("hex");
const reportRaw = fs.readFileSync(
  path.join(__dirname, "fixtures/phase-b-fenced-report.json"),
  "utf8",
);
const reportSha256 = sha(reportRaw);
if (
  reportSha256 !==
  "d0532ad01ec6aea711d1aefae6b46329d88cb2170a8d6a1d4201c8cf5a848fd5"
)
  throw Error("Historical B report bytes changed");
const report = JSON.parse(reportRaw);
// Every relative JavaScript dependency uses this private module map. No module
// in the historical graph is inserted into or loaded from require.cache.
function historicalGraph(
  source = fs.readFileSync(path.join(__dirname, "owner-phase-b.js"), "utf8"),
) {
  const block = source.match(/const PINS = Object\.freeze\(\{[\s\S]*?\n\}\);/g);
  const pins =
    block?.length === 1 && block[0].match(/^  router: "([a-f0-9]{40})",$/gm);
  if (
    !pins ||
    pins.length !== 1 ||
    report.router !== "02d698236b6336daee0d5581547636f7cf2b36f3"
  )
    throw Error("Historical B source fixture differs");
  const oldPin = pins[0],
    newPin = `  router: "${report.router}",`;
  if (source.split(oldPin).length !== 2)
    throw Error("Historical B source pin is ambiguous");
  const qualified = source.replace(oldPin, newPin),
    modules = new Map();
  function load(filename) {
    if (modules.has(filename)) return modules.get(filename).exports;
    const loaded = new Module(filename, module);
    modules.set(filename, loaded);
    loaded.filename = filename;
    loaded.paths = Module._nodeModulePaths(__dirname);
    const original = loaded.require.bind(loaded);
    loaded.require = (request) => {
      if (!request.startsWith(".")) return original(request);
      const resolved = Module.createRequire(filename).resolve(request);
      if (path.dirname(resolved) !== __dirname)
        throw Error("Historical graph escaped runbook");
      return /\.(?:cjs|js)$/.test(resolved)
        ? load(resolved)
        : original(request);
    };
    loaded._compile(
      path.basename(filename) === "owner-phase-b.js"
        ? qualified
        : fs.readFileSync(filename, "utf8"),
      filename,
    );
    loaded.loaded = true;
    return loaded.exports;
  }
  const local = (name) => load(path.join(__dirname, name));
  const a = local("owner-phase-a.js"),
    b = local("owner-phase-b.js"),
    fence = local("owner-phase-b-fence.cjs"),
    c = local("owner-phase-c.cjs"),
    host = local("owner-phase-a-automation.cjs"),
    { runPhaseCCli } = local("owner-phase-c-cli.cjs");
  function assertIsolated() {
    if ([...modules.keys()].some((filename) => require.cache[filename]))
      throw Error("Historical graph mixed with global modules");
    return [...modules.keys()]
      .map((filename) => path.basename(filename))
      .sort();
  }
  const proof = Object.freeze({
    reportSha256,
    producerQualification: "pi-phase-b-fence-full-host-20261128-r2",
    producerQualificationSha256:
      "faf7248a48e59864d941fbbc285b13fb18fbd0a6b2f6be9e7ef3609a461a67fa",
    bSourceSha256: sha(source),
    testedBSourceSha256: sha(qualified),
    substitution: { before: oldPin, after: newPin },
    liveQualified: false,
  });
  assertIsolated();
  return { a, b, fence, c, host, runPhaseCCli, proof, assertIsolated };
}
module.exports = { ...historicalGraph(), historicalGraph, report, reportRaw };
