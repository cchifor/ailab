"use strict";
// Fixed reviewed entry: the same pure validator supplies both pre-grant scope and runtime inputs.
const fs = require("node:fs");
const path = require("node:path");
const { readPredecessor } = require("./owner-phase-a-cli.cjs");
const core = require("./owner-finalization.cjs");

function readInputs(directory) {
  if (!path.isAbsolute(directory) || fs.realpathSync(directory) !== directory)
    throw Error("input-directory");
  const manifest = JSON.parse(readPredecessor(path.join(directory, "manifest.json")));
  const names = core.reportArtifactNames(manifest).slice().sort();
  if (JSON.stringify(names) !== JSON.stringify(["a", "b", "bInputs", "c"]) &&
      JSON.stringify(names) !== JSON.stringify(["a", "b", "bInputs", "c", "d"]))
    throw Error("input-artifacts");
  const expected = [...names.map(n => n + ".json"), "decision.json", "manifest.json"].sort();
  if (JSON.stringify(fs.readdirSync(directory).sort()) !== JSON.stringify(expected))
    throw Error("input-files");
  const reports = Object.fromEntries(names.map(n => [n, readPredecessor(path.join(directory, n + ".json"))]));
  const decisionRaw = readPredecessor(path.join(directory, "decision.json"));
  if (core.sha(decisionRaw) !== manifest.decisionSha256) throw Error("decision-hash");
  return { reports, manifest };
}

async function runEntry(argv = process.argv.slice(2), env = process.env) {
  try {
    if (process.platform !== "linux" ||
        !(argv.length === 2 && argv[0] === "--validate-inputs" || argv.length === 3 && argv[0] === "--run"))
      throw Error("entry-arguments");
    const inputs = readInputs(argv[1]);
    const projection = core.validateFinalizationInputs(inputs); // compiled NULL qualification refuses here
    if (argv[0] === "--validate-inputs") {
      const raw = JSON.stringify(projection) + "\n";
      if (Buffer.byteLength(raw) > 256 * 1024) throw Error("projection-size");
      const bytes = Buffer.from(raw);
      for (let n = 0; n < bytes.length;) {
        const count = fs.writeSync(1, bytes, n, bytes.length - n);
        if (!(count > 0)) throw Error("output-failed");
        n += count;
      }
      return 0;
    }
    const { createFinalizationObserver } = require("../../../scripts/relay-finalization/client.cjs");
    const { runFinalizationCli } = require("./owner-finalization-cli.cjs");
    const observer = createFinalizationObserver({ socketPath: env.RELAY_OBSERVER_SOCKET,
      qualificationSha256: projection.sources.observerQualificationSha256 });
    const args = ["--manifest", path.join(argv[1], "manifest.json"),
      "--phase-a", path.join(argv[1], "a.json"), "--phase-b", path.join(argv[1], "b.json"),
      "--phase-b-inputs", path.join(argv[1], "bInputs.json"), "--phase-c", path.join(argv[1], "c.json")];
    if (Object.hasOwn(inputs.reports, "d")) args.push("--active-drain", path.join(argv[1], "d.json"));
    args.push("--output", argv[2]);
    return await runFinalizationCli({ argv: args, env, observer });
  } catch {
    try { fs.writeSync(2, "finalization-entry-refused\n"); } catch {}
    return 2;
  }
}
module.exports = { runEntry };
if (require.main === module) runEntry().then(code => { process.exitCode = code; });
