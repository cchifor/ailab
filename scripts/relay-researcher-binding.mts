/**
 * Rebuild the researcher installation's PodRuntimeBinding and the complete operator definition for
 * step I4 of docs/runbooks/relay-researcher-installation.md, with the pinned Relay checkout's own
 * code. Nothing here reimplements a Relay digest or seal: the runtime digest, the template seal,
 * the policy digest, the binding schema and digest, and the preparation self-check are Relay
 * functions imported from that checkout.
 *
 * Run it with that checkout's tsx (Node 22.21 or newer), from the ailab checkout:
 *
 *   <relay>/node_modules/.bin/tsx scripts/relay-researcher-binding.mts \
 *     --relay <relay> --assets <dir> [--agent-assets <dir>] \
 *     --installation-id <uuid> --installation-public-key <base64url> --out <new dir>
 *
 *   --assets  the assets researcher-binding-inputs.json `release` pins (the runtime's release).
 *   --agent-assets   required exactly when the inputs pin an `agentRelease` (an agent-only update,
 *             runbook "Agent-only update"), refused otherwise: the assets `agentRelease` pins.
 *   --installation-id / --installation-public-key   the identity Relay approved at step I3. They must
 *             equal the reviewed record (researcher-binding-inputs.json `installation`), whose
 *             fingerprint Relay's publicKeyFingerprint must reproduce; a typo is refused, not built.
 *   --fence-object-uid <uuid>   step I10 only: adds identity.fenceObjectUid (the live fence ConfigMap UID)
 *
 * The committed docs/relay-workers/researcher-i4/ is this command's output for the recorded identity:
 * the operator's run must reproduce it byte for byte.
 *
 * Agent-only update (owner decision 2026-11-17): an optional `agentRelease` in the inputs pins a LATER
 * Relay release from which ONLY the infrastructure-agent image is taken (definition.agentImage, which
 * Relay's preparation names by digest only). Everything else still comes from `release` alone, as
 * without it: the checkout (HEAD = release.sourceCommit, whose code seals the binding), the runtime
 * manifest, runtime receipt and image, definition.sourceCommit (Relay's preparation requires it to equal
 * the runtime manifest's: installation-preparation.ts "runtime-source"), the template and the binding.
 * So binding.json, template-preparation-input.json and the binding digest are byte-identical with or
 * without it; definition.json differs only in agentImage, and summary.json also records agentRelease.
 * The agent receipt must then come from the agentRelease assets (pinned sha256s), name
 * agentRelease.sourceCommit and be an infrastructure-agent receipt whose digest-only publication.image
 * is agentRelease.agentImage (in release.agentImage's repository); that release's manifest.json must
 * name its sourceCommit and tag; and its tag must be newer than release's. Without `agentRelease` the
 * agent is release's, exactly as before.
 *
 * Refuses: a Relay checkout whose HEAD is not the pinned sourceCommit, or that has local changes;
 * a tsx that is not that checkout's; Node older than 22.21; any release asset whose sha256 is not
 * the pinned one; receipts that do not join the manifest; --agent-assets without an `agentRelease`,
 * or an `agentRelease` without --agent-assets, or one that fails the checks above; a runtime manifest
 * without the G3 fields the template requires (a BLOCKER: only a rebuilt image can fix it); a
 * template with OPERATOR-FILLS leaves other than the seven this fills; an identity other than the I3
 * record, or a key Relay's Ed25519 check refuses; any definition Relay's preparePodInstallation refuses.
 *
 * Writes a NEW directory (never overwrites) with canonical JSON (Relay canonicalJson + "\n"):
 *   binding.json                     the PodRuntimeBinding
 *   definition.json                  the I4 definition: the template, `_comment` removed, every
 *                                    OPERATOR-FILLS leaf filled; input of prepare-pod-installation.ts
 *   template-preparation-input.json  the input of Relay scripts/prepare-worker-templates.ts that
 *                                    seals the same researcher template (for an independent rerun)
 *   summary.json                     the pins, digests and output sha256s (and `agentRelease`, with its
 *                                    tag, Gitea release, sourceCommit and assets, when the inputs pin one)
 * Deterministic: identical inputs give byte-identical files. Reads no secret, contacts nothing.
 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const AILAB = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const INPUTS = join(AILAB, "docs/relay-workers/researcher-binding-inputs.json");
const TEMPLATE = join(AILAB, "docs/relay-workers/researcher-installation-definition.json");
const RUNTIME_CA = join(AILAB, "docs/relay-workers/researcher-runtime-ca.crt");
const PLACEHOLDER = "OPERATOR-FILLS";
/** The template's OPERATOR-FILLS leaves; a template with any other is refused (review this tool). */
const FILLED = [
  "/sourceCommit",
  "/agentImage",
  "/identity/installationId",
  "/identity/installationPublicKey",
  "/installationPolicy/allowedImageDigests",
  "/installationPolicy/allowedHarnessAdapters",
  "/binding",
];
const MIN_NODE = [22, 21];
/** The release tags an agent-only update compares: vMAJOR.MINOR.PATCH, nothing else. */
const TAG = /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const AGENT_RELEASE_KEYS = ["agentImage", "agentReceipt", "assets", "giteaReleaseId", "sourceCommit", "tag"];

class Refusal extends Error {}
const refuse = (message: string): never => {
  throw new Refusal(message);
};
const sha256 = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex");
const readJson = (path: string) => JSON.parse(readFileSync(path, "utf8"));

function parseArgs(argv: string[]) {
  const out: Record<string, string> = {};
  const values = new Set(["--relay", "--assets", "--installation-id", "--installation-public-key", "--out",
    "--fence-object-uid", "--agent-assets"]);
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]!;
    if (values.has(a) && i + 1 < argv.length && !(a in out)) out[a] = argv[++i]!;
    else refuse(`unknown, repeated or incomplete argument ${JSON.stringify(a)}`);
  }
  for (const required of ["--relay", "--assets", "--installation-id", "--installation-public-key", "--out"])
    if (typeof out[required] !== "string") refuse(`${required} is required`);
  return out as {
    "--relay": string;
    "--assets": string;
    "--out": string;
    "--installation-id": string;
    "--installation-public-key": string;
    "--fence-object-uid"?: string;
    "--agent-assets"?: string;
  };
}

function git(relay: string, ...args: string[]) {
  return execFileSync("git", ["-C", relay, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}

/** The checkout is the pinned commit, clean, and the one whose tsx runs this file. */
function verifyCheckout(relayArg: string, sourceCommit: string) {
  const [major, minor] = process.versions.node.split(".").map(Number) as [number, number];
  if (major < MIN_NODE[0]! || (major === MIN_NODE[0] && minor < MIN_NODE[1]!))
    refuse(`Node ${process.versions.node} is older than the ${MIN_NODE.join(".")} Relay requires`);
  const relay = realpathSync(relayArg);
  const head = git(relay, "rev-parse", "HEAD");
  if (head !== sourceCommit) refuse(`Relay checkout HEAD is ${head}, the release pins ${sourceCommit}`);
  if (git(relay, "status", "--porcelain")) refuse("the Relay checkout has local changes or untracked files");
  const loader = join(relay, "node_modules/tsx/");
  if (!process.execArgv.some((a) => a.includes(loader) || a.includes(pathToFileURL(loader).href)))
    refuse(`run this with ${join(relay, "node_modules/.bin/tsx")}, not another tsx`);
  return { relay, head };
}

/** Every pinned asset present, with exactly its pinned sha256; SHA256SUMS agrees where it lists one. */
function verifyAssets(dir: string, pinned: Record<string, string>) {
  const bytes: Record<string, Buffer> = {};
  for (const [name, digest] of Object.entries(pinned)) {
    const path = join(dir, name);
    if (!existsSync(path)) refuse(`release asset ${name} is missing from ${dir}`);
    const body = readFileSync(path);
    if (sha256(body) !== digest) refuse(`release asset ${name} has sha256 ${sha256(body)}, pinned ${digest}`);
    bytes[name] = body;
  }
  const sums = new Map(
    bytes["SHA256SUMS"]!
      .toString("utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => {
        const m = /^([0-9a-f]{64}) [ *]?(.+)$/.exec(line);
        if (!m) refuse(`SHA256SUMS line is not '<sha256>  <name>': ${JSON.stringify(line)}`);
        return [m![2]!, m![1]!] as const;
      }),
  );
  for (const [name, body] of Object.entries(bytes))
    if (sums.has(name) && sums.get(name) !== sha256(body)) refuse(`${name} differs from SHA256SUMS`);
  return bytes;
}

/** A newer vMAJOR.MINOR.PATCH than `than` (both already matched TAG). */
function newerTag(tag: string, than: string) {
  const [a, b] = [tag, than].map((t) => TAG.exec(t)!.slice(1).map(Number));
  for (let i = 0; i < 3; i++) if (a![i] !== b![i]) return a![i]! > b![i]!;
  return false;
}

/**
 * The inputs' `agentRelease` (agent-only update), checked before anything reads its assets: exactly
 * its six fields, a vMAJOR.MINOR.PATCH tag newer than release's, a 40-hex sourceCommit, pinned sha256s
 * that include SHA256SUMS, manifest.json and the agent receipt, and a digest-only image in the same
 * repository as release.agentImage. It never carries a runtime field: nothing else is taken from it.
 */
function checkAgentRelease(agent: any, release: any) {
  if (!agent || typeof agent !== "object" || Array.isArray(agent) ||
      JSON.stringify(Object.keys(agent).sort()) !== JSON.stringify(AGENT_RELEASE_KEYS))
    refuse(`agentRelease must be an object with exactly ${AGENT_RELEASE_KEYS.join(", ")}`);
  if (typeof agent.tag !== "string" || !TAG.test(agent.tag))
    refuse(`agentRelease.tag ${JSON.stringify(agent.tag)} is not a vMAJOR.MINOR.PATCH release tag`);
  if (!TAG.test(release.tag)) refuse(`release.tag ${JSON.stringify(release.tag)} is not a vMAJOR.MINOR.PATCH release tag`);
  if (!newerTag(agent.tag, release.tag))
    refuse(`agentRelease ${agent.tag} is not newer than release ${release.tag}: an agent-only update takes a LATER agent`);
  if (!Number.isSafeInteger(agent.giteaReleaseId) || agent.giteaReleaseId <= 0)
    refuse("agentRelease.giteaReleaseId is not a Gitea release ID");
  if (typeof agent.sourceCommit !== "string" || !/^[0-9a-f]{40}$/.test(agent.sourceCommit))
    refuse("agentRelease.sourceCommit is not a 40-hex commit");
  const assets = agent.assets;
  if (!assets || typeof assets !== "object" || Array.isArray(assets) ||
      Object.entries(assets).some(([n, d]) => !/^[A-Za-z0-9_-][A-Za-z0-9._-]*$/.test(n) ||
        typeof d !== "string" || !/^[0-9a-f]{64}$/.test(d)))
    refuse("agentRelease.assets is not a map of plain asset file name to sha256");
  for (const name of ["SHA256SUMS", "manifest.json", agent.agentReceipt])
    if (typeof name !== "string" || !Object.hasOwn(assets, name)) refuse(`agentRelease.assets does not pin ${name}`);
  const repository = String(release.agentImage).split("@")[0];
  if (typeof agent.agentImage !== "string" || agent.agentImage.split("@")[0] !== repository ||
      !/^[^@]+@sha256:[0-9a-f]{64}$/.test(agent.agentImage))
    refuse(`agentRelease.agentImage is not a digest-only ${repository} image`);
}

function placeholderPaths(value: unknown, path = ""): string[] {
  if (typeof value === "string") return value.startsWith(PLACEHOLDER) ? [path] : [];
  if (Array.isArray(value)) return value.flatMap((v, i) => placeholderPaths(v, `${path}/${i}`));
  if (value && typeof value === "object")
    return Object.entries(value).flatMap(([k, v]) => placeholderPaths(v, `${path}/${k}`));
  return [];
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const inputs = readJson(INPUTS);
  const { release, agentRelease, derived, decisions, installation: approved } = inputs;
  // Agent-only update: --agent-assets exactly when the inputs pin an agentRelease.
  const agentOnly = Object.hasOwn(inputs, "agentRelease");
  if (agentOnly && args["--agent-assets"] === undefined)
    refuse("researcher-binding-inputs.json pins an agentRelease: --agent-assets <its asset directory> is required");
  if (!agentOnly && args["--agent-assets"] !== undefined)
    refuse("--agent-assets is refused: researcher-binding-inputs.json pins no agentRelease (the agent comes from release)");
  if (agentOnly) checkAgentRelease(agentRelease, release);
  const { relay, head } = verifyCheckout(args["--relay"], release.sourceCommit);
  const relayModule = (path: string) => import(pathToFileURL(join(relay, path)).href);
  const [bindingTs, manifestTs, digestTs, configTs, primitivesTs, templateTs, rolesTs, preparationTs,
    installationAuthTs] = await Promise.all([
      relayModule("packages/worker-runtime/binding.ts"),
      relayModule("packages/worker-runtime/manifest.ts"),
      relayModule("packages/infrastructure-protocol/digest.ts"),
      relayModule("packages/infrastructure-agent/config.ts"),
      relayModule("packages/infrastructure-protocol/primitives.ts"),
      relayModule("packages/infrastructure-protocol/template.ts"),
      relayModule("packages/worker-roles/preparation.ts"),
      relayModule("packages/infrastructure-agent/installation-preparation.ts"),
      relayModule("packages/infrastructure-protocol/installation-auth.ts"),
    ]);
  const { PodRuntimeBinding, bindingDigest, POD_RUNTIME_IDENTITIES, runtimeOriginHosts } = bindingTs;
  const { WorkerRuntimeManifest, workerRuntimeDigest, artifact, harnessExecutable } = manifestTs;
  const { canonicalJson, contentDigest } = digestTs;
  const { InstallationTemplatePolicy } = templateTs;
  const canonical = (value: unknown) => canonicalJson(value) + "\n";

  // Identity: exactly the one Relay approved at step I3, as reviewed. The key must pass Relay's own
  // canonical, non-weak Ed25519 check (the hub's at approval), and reproduce the approval receipt's
  // fingerprint as Relay's installation view computes it (server/workers/installations/management.ts,
  // module-private there: sha256 over the base64url key text, first 16 hex).
  const installationId = args["--installation-id"];
  const installationPublicKey = args["--installation-public-key"];
  if (installationId !== approved.installationId || installationPublicKey !== approved.installationPublicKey)
    refuse("the installation ID or public key is not the one recorded at step I3 (researcher-binding-inputs.json)");
  try {
    installationAuthTs.ed25519PublicKey(installationPublicKey);
  } catch {
    refuse("the installation public key is not a canonical Ed25519 public key");
  }
  const publicKeyFingerprint = createHash("sha256").update(installationPublicKey).digest("hex").slice(0, 16);
  if (publicKeyFingerprint !== approved.publicKeyFingerprint)
    refuse(`the public key's fingerprint is ${publicKeyFingerprint}, the approval recorded ${approved.publicKeyFingerprint}`);

  // Release assets: exact bytes, then the joins between them.
  const assets = verifyAssets(resolve(args["--assets"]), release.assets);
  const releaseManifest = JSON.parse(assets["manifest.json"]!.toString("utf8"));
  if (releaseManifest.sourceCommit !== release.sourceCommit || releaseManifest.version !== release.tag)
    refuse("manifest.json is not the pinned release");
  const manifestBytes = assets[release.runtimeManifest]!;
  const publishedManifest = JSON.parse(manifestBytes.toString("utf8"));
  const manifest = WorkerRuntimeManifest.parse(publishedManifest);
  if (canonicalJson(manifest) !== canonicalJson(publishedManifest))
    refuse("Relay's manifest schema would normalize the published runtime manifest");
  const receipt = (from: Record<string, Buffer>, name: string, component: string, sourceCommit: string) => {
    const r = JSON.parse(from[name]!.toString("utf8"));
    const image = r.publication?.image;
    if (r.kind !== "relay-worker-image" || r.version !== 1 || r.component !== component ||
        r.sourceCommit !== sourceCommit || r.platform !== "linux/amd64" ||
        typeof image !== "string" || image !== `${image.split("@")[0]}@sha256:${r.publication.manifestSha256}`)
      refuse(`${name} is not the ${component} publish receipt of the pinned commit`);
    return r;
  };
  const runtimeReceipt = receipt(assets, release.runtimeReceipt, "worker-runtime", release.sourceCommit);
  const agentReceipt = receipt(assets, release.agentReceipt, "infrastructure-agent", release.sourceCommit);
  const runtimeImage: string = runtimeReceipt.publication.image;
  const releaseAgentImage: string = agentReceipt.publication.image;
  const runtimeDigest: string = workerRuntimeDigest(manifest);
  if (runtimeImage !== release.runtimeImage || releaseAgentImage !== release.agentImage)
    refuse("the receipts' images are not the pinned images");
  if (runtimeReceipt.runtime?.manifestSha256 !== sha256(manifestBytes) ||
      runtimeReceipt.runtime?.runtimeDigest !== runtimeDigest || runtimeDigest !== release.runtimeDigest ||
      runtimeReceipt.runtime?.holderProfilesSha256 !== artifact(manifest, "holder-profiles")?.sha256)
    refuse("the runtime receipt does not name this runtime manifest");
  if (manifest.sourceCommit !== release.sourceCommit || manifest.version !== release.tag.replace(/^v/, ""))
    refuse("the runtime manifest is not built from the pinned release");

  // The agent image: release's, or (agent-only update) agentRelease's, verified from ITS assets. Only
  // the image is taken from agentRelease; every runtime value above came from release alone.
  let agentImage = releaseAgentImage;
  if (agentOnly) {
    const agentAssets = verifyAssets(resolve(args["--agent-assets"]!), agentRelease.assets);
    const agentManifest = JSON.parse(agentAssets["manifest.json"]!.toString("utf8"));
    if (agentManifest.sourceCommit !== agentRelease.sourceCommit || agentManifest.version !== agentRelease.tag)
      refuse("the agent release's manifest.json is not the pinned agentRelease (sourceCommit and tag)");
    const agentOnlyReceipt = receipt(agentAssets, agentRelease.agentReceipt, "infrastructure-agent",
      agentRelease.sourceCommit);
    if (agentOnlyReceipt.publication.image !== agentRelease.agentImage)
      refuse("the agent release's receipt image is not agentRelease.agentImage");
    agentImage = agentOnlyReceipt.publication.image;
  }

  // G3 in the published runtime (template binding leaf). Only a rebuilt image can change these.
  const template = readJson(TEMPLATE);
  delete template._comment;
  const caSha = sha256(readFileSync(RUNTIME_CA));
  const blockers: string[] = [];
  const sc = manifest.supervisorConfig;
  if (sc.hubOrigin !== template.hub.origin) blockers.push(`supervisorConfig.hubOrigin is ${sc.hubOrigin}`);
  if (sc.hubCaPath !== derived.hubCaPath) blockers.push(`supervisorConfig.hubCaPath is ${sc.hubCaPath}`);
  if (sc.hubCaSha256 !== caSha) blockers.push(`supervisorConfig.hubCaSha256 is ${sc.hubCaSha256}, the ailab root CA is ${caSha}`);
  const hubCa = manifest.artifacts.filter((a: any) => a.role === "hub-ca");
  if (hubCa.length !== 1 || hubCa[0].sha256 !== caSha || hubCa[0].installPath !== derived.hubCaPath)
    blockers.push("the hub-ca artifact is not the ailab root CA at the hub CA path");
  const packaged = new Set(manifest.artifacts.filter((a: any) => a.sha256 === caSha).map((a: any) => a.installPath));
  const workerTemplates = manifest.holderProfiles.workerTemplates ?? [];
  if (manifest.holderProfiles.holderMode !== "worker-template" || !workerTemplates.length)
    blockers.push("the runtime has no worker templates");
  workerTemplates.forEach((t: any, i: number) => {
    for (const [client, origin] of [["router", derived.routerOrigin], ["communication", template.hub.origin]]) {
      const p = t.definition.holder[client!] ?? {};
      if (p.origin !== origin || p.caSha256 !== caSha || !packaged.has(p.caFile))
        blockers.push(`workerTemplates[${i}].holder.${client} is not ${origin} pinned to a packaged ailab root CA`);
    }
  });
  const hosts = runtimeOriginHosts(manifest);
  if (typeof hosts === "string") blockers.push(`host aliases are not renderable: ${hosts}`);
  if (blockers.length)
    refuse(`BLOCKER: the published runtime manifest does not satisfy the template's binding requirements ` +
      `(the image must be rebuilt): ${blockers.join("; ")}`);

  // The researcher template, sealed by Relay's own preparation (prepare-worker-templates.ts).
  const t = decisions.template;
  const templateInput = {
    version: 1,
    selections: [{
      role: "researcher",
      pins: {
        templateId: t.templateId, revision: t.revision, name: t.name, skills: t.skills, toolsets: t.toolsets,
        harness: {
          adapter: manifest.adapterName,
          adapterDigest: artifact(manifest, "bridge").sha256,
          executableDigest: harnessExecutable(manifest).sha256,
          modelPolicy: t.modelPolicy,
        },
        image: runtimeImage,
        egressDestinations: derived.templateEgressDestinations,
      },
    }],
  };
  const templateInputBytes = canonical(templateInput);
  const sealed = rolesTs.prepareWorkerTemplateBundle(templateInput, sha256(Buffer.from(templateInputBytes)));
  if (sealed.manifest.items.length !== 1) refuse("template preparation did not seal exactly one template");
  const sealedTemplate = JSON.parse(sealed.files[sealed.manifest.items[0].file]);

  // The installation policy the binding pins (its digest is the agent's localPolicyDigest).
  const installationPolicy = InstallationTemplatePolicy.parse({
    ...template.installationPolicy,
    allowedImageDigests: [primitivesTs.imageDigest(runtimeImage)],
    allowedHarnessAdapters: [sealedTemplate.definition.harness.adapter],
  });
  const policyDigest: string = contentDigest(configTs.POLICY_DIGEST_DOMAIN, installationPolicy);

  if (decisions.releaseApproval !== "absent")
    refuse("releaseApproval must be absent: Relay's candidate runtime refuses a binding that carries one");
  const raw = {
    version: 1,
    installationId,
    policyDigest,
    runtime: { manifest, image: runtimeImage },
    templates: [sealedTemplate],
    placements: [{ ...decisions.placement, ...derived.placement }],
    identities: { ...POD_RUNTIME_IDENTITIES },
    supervisor: decisions.supervisor,
    hostAliases: derived.hostAliases,
  };
  const binding = PodRuntimeBinding.parse(raw);
  if (canonicalJson(binding) !== canonicalJson(raw)) refuse("Relay's binding schema would normalize the binding");

  // The definition: the reviewed template with exactly its OPERATOR-FILLS leaves filled.
  const leaves = placeholderPaths(template);
  if (canonicalJson([...leaves].sort()) !== canonicalJson([...FILLED].sort()))
    refuse(`the template's OPERATOR-FILLS leaves are ${leaves.join(", ")}; this generator fills ${FILLED.join(", ")}`);
  const definition = structuredClone(template);
  definition.sourceCommit = release.sourceCommit;
  definition.agentImage = agentImage;
  definition.identity.installationId = installationId;
  definition.identity.installationPublicKey = installationPublicKey;
  if (args["--fence-object-uid"] !== undefined) {
    definition.identity.fenceObjectUid = args["--fence-object-uid"];
  }
  definition.installationPolicy.allowedImageDigests = installationPolicy.allowedImageDigests;
  definition.installationPolicy.allowedHarnessAdapters = installationPolicy.allowedHarnessAdapters;
  definition.binding = binding;
  if (placeholderPaths(definition).length) refuse("an OPERATOR-FILLS leaf is still unfilled");

  // Relay's own preparation must accept it (pure, in memory): policy digest, template fit, joins.
  let prepared: any;
  try {
    prepared = preparationTs.preparePodInstallation(definition);
  } catch (error) {
    refuse(`Relay preparePodInstallation refuses the definition: ${(error as Error).message.slice(0, 2000)}`);
  }
  const report = JSON.parse(prepared.files["report.json"]);
  const digest: string = bindingDigest(binding);
  if (report.identity.bindingDigest !== digest || report.identity.policyDigest !== policyDigest ||
      report.identity.runtimeDigest !== runtimeDigest || report.status !== "enrollment-only-worker-creation-disabled")
    refuse("Relay's preparation report disagrees with the generated binding");

  const files: Record<string, string> = {
    "binding.json": canonical(binding),
    "definition.json": canonical(definition),
    "template-preparation-input.json": templateInputBytes,
  };
  const summary = {
    kind: "ailab.relay-researcher-binding-summary",
    version: 1,
    release: { tag: release.tag, sourceCommit: release.sourceCommit, relayCheckoutHead: head, assets: release.assets },
    // Only for an agent-only update, so a summary without one is byte-identical to before.
    ...(agentOnly ? {
      agentRelease: {
        tag: agentRelease.tag,
        giteaReleaseId: agentRelease.giteaReleaseId,
        sourceCommit: agentRelease.sourceCommit,
        assets: agentRelease.assets,
      },
    } : {}),
    installationId,
    publicKeyFingerprint,
    runtimeImage,
    runtimeDigest,
    agentImage,
    template: {
      templateId: sealedTemplate.definition.templateId,
      revision: sealedTemplate.definition.revision,
      digest: sealedTemplate.digest,
    },
    policyDigest,
    bindingDigest: digest,
    files: Object.fromEntries(Object.entries(files).map(([name, body]) => [name, sha256(Buffer.from(body))])),
  };
  files["summary.json"] = canonical(summary);

  const out = resolve(args["--out"]);
  if (existsSync(out)) refuse(`--out ${out} must be a new directory`);
  mkdirSync(out, { mode: 0o755 });
  for (const [name, body] of Object.entries(files)) writeFileSync(join(out, name), body, { flag: "wx", mode: 0o644 });
  process.stdout.write(JSON.stringify(summary, null, 2) + "\n");
}

main().catch((error) => {
  process.stderr.write(`relay-researcher-binding: ${error instanceof Refusal ? error.message : (error as Error).stack}\n`);
  process.exitCode = 1;
});
