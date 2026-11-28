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
const source = fs.readFileSync(
  path.join(__dirname, "owner-phase-b.js"),
  "utf8",
);
const oldPin = 'router: "4e4bb34e2f968f9450aabe9cf228d3e70c8bab71"';
const newPin = 'router: "02d698236b6336daee0d5581547636f7cf2b36f3"';
if (
  source.split(oldPin).length !== 2 ||
  report.router !== "02d698236b6336daee0d5581547636f7cf2b36f3"
)
  throw Error("Historical B source fixture differs");
const qualified = source.replace(oldPin, newPin);
function compile(name, dependencies, bytes) {
  const filename = path.join(__dirname, name),
    loaded = new Module(filename, module);
  loaded.filename = filename;
  loaded.paths = Module._nodeModulePaths(__dirname);
  const original = loaded.require.bind(loaded);
  loaded.require = (request) => dependencies[request] || original(request);
  loaded._compile(bytes ?? fs.readFileSync(filename, "utf8"), filename);
  return loaded.exports;
}
const b = compile("owner-phase-b.js", {}, qualified);
const c = compile("owner-phase-c.cjs", { "./owner-phase-b.js": b });
const host = compile("owner-phase-a-automation.cjs", {
  "./owner-phase-c.cjs": c,
});
const { runPhaseCCli } = compile("owner-phase-c-cli.cjs", {
  "./owner-phase-a-automation.cjs": host,
});
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
module.exports = { c, host, runPhaseCCli, report, reportRaw, proof };
