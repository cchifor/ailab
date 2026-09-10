// A dsh credentials provider that resolves references from OpenBao, per operation.
//
// WHY THIS EXISTS. Credentials for this estate live in OpenBao and nowhere else, so that rotating
// one -- or ADDING one -- reaches every agent on every machine with no per-machine step. dsh was
// the exception: each credential arrived as its own ExternalSecret projected into its own env var,
// so a new credential needed a manifest edit, a commit and a reconcile before dsh could see it.
// This closes that gap without moving the source of truth.
//
// WHY NOT AN OPENBAO AGENT IN THIS POD. openbao-eso.yaml has said "no" since 2026-09-07 and the
// answer got STRONGER under review, not weaker. The obvious design is a bao agent sidecar with a
// listener on 127.0.0.1 and `use_auto_auth_token`, so the pod holds no sink file. Cross-review
// (codex, 2026-09-10) showed that argument is a story I was telling myself:
//   * containers in a pod SHARE localhost, so model-authored code in the dsh container reaches
//     that listener exactly as this plugin would; and
//   * the listener will attach the agent's token to `GET /v1/auth/token/lookup-self`, whose
//     documented response carries the token itself at `data.id` (`renew-self` returns it at
//     `auth.client_token`). "No sink file" therefore does NOT mean "no token available to dsh".
// Denying those endpoints to the identity is not a fix either -- the agent renews with the same
// token, so denying renewal breaks the agent. The pod would end up holding a credential-minting
// oracle, which is the exact regression the ESO decision was made to avoid.
//
// WHAT THIS DOES INSTEAD. External Secrets stays the transport: it authenticates in its OWN
// namespace, mints its own token, and writes only resolved VALUES. `dataFrom.extract` over a
// single KV-v2 document means every field of that document becomes a key of one Secret, so a
// credential added in OpenBao appears here with no manifest change -- which is the whole
// requirement -- while the OpenBao policy stays `read` on ONE path. The pod gains no OpenBao
// identity, no token, and no egress exception. See openbao-eso.yaml.
//
// WHY A PLUGIN AT ALL, GIVEN THAT. Because a mounted file is not a credential seam. Without this
// the Secret's keys would have to be wired into env vars one by one in the Deployment -- the
// per-credential manifest edit this change exists to remove. dsh resolves every credential through
// the `credentials` service, so teaching THAT service to read the mount is what makes a newly
// added credential usable without touching this repo.
//
// SUBCLASS, NOT REIMPLEMENTATION. `CredentialProvider` is a cordis Service constructed as
// `super(ctx, "credentials")` -- a SINGLETON name, so this replaces dsh-base's row rather than
// adding a second. It extends the shipped LocalCredentialProvider (exported by
// @deepseek-ai/dsh-credentials-local@0.1.5-alpha.2, verified) and overrides only the REFERENCE
// half. That is deliberate and load-bearing: the RECORD half stores
// `client-connection/browser-session`, the browser cookie SIGNING SECRET that dsh writes itself
// and that survives restarts. Reimplementing record persistence would mean re-implementing the
// cross-process lock, the atomic replacement, the watcher and the read-modify-write contract --
// and `modifyRecord`'s callback returning `undefined` means LEAVE UNCHANGED, not delete, which
// browser auth relies on at every startup. Inheriting all of it means none of that can be got
// wrong here.
//
// LAYERING IS THE DOCUMENTED ONE, WITH OPENBAO INSERTED:
//     inherited process environment   (read-only, WINS -- so `-e` still overrides for debugging)
//   > OPENBAO, via the ESO mount      (this file)
//   > $DSH_HOME/.credentials.yaml     (provider-managed refs, and ALL records)
//   > <cwd>/.env > $DSH_HOME/.env     (read-only fallbacks)
// The env layer is resolved by `super`, NOT by reading process.env: upstream resolves it through
// an immutable launch-time snapshot, and reading the live environment would silently change the
// semantics.
import { LocalCredentialProvider } from '@deepseek-ai/dsh-credentials-local';
import z from '@deepseek-ai/schemastery';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

/** Where the ESO-managed Secret is mounted. One file per credential, named by its reference. */
const DEFAULT_DIR = '/dsh-credentials';
/** The JSON field carrying the secret when a mounted document holds JSON rather than a bare value. */
const DEFAULT_FIELD = 'value';
/** Reported as `source` when a value came from OpenBao. */
const SOURCE = 'openbao';

/**
 * The reference grammar, restated from @deepseek-ai/dsh-credentials' own REF_PATTERN.
 *
 * This is the containment check that matters for THIS file: a reference becomes a path segment
 * under the mount, so anything outside the grammar is REFUSED rather than escaped or normalised.
 * `credentialRef` would already have rejected it upstream; re-checking here means a future caller
 * that skips that constructor still cannot walk out of the directory.
 */
const REF_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Absence, distinguished from failure. `undefined` means "OpenBao does not have this". */
const ABSENT = undefined;

/**
 * Read one credential from the mount.
 *
 * @param dir - the mount directory.
 * @param field - the JSON field carrying the value when the document is JSON.
 * @param ref - the reference, already grammar-checked.
 * @returns the value, or {@link ABSENT} when the mount confirms it has no such credential.
 * @throws when the mount is unreadable or malformed -- a FAILURE, which must NOT be reported as
 *   absence: "unconfigured" and "I could not tell" are different answers, and only the first may
 *   fall through to a lower layer. Falling through on a failure would silently resolve a stale
 *   local value while OpenBao held a rotated one.
 */
async function readFromMount(dir, field, ref) {
  let text;
  try {
    // Reopened by pathname every time, never a retained descriptor: kubelet updates a Secret
    // volume by swapping an atomic symlink, so a held fd would pin the value at mount time and a
    // rotated credential would read as unchanged forever.
    text = await readFile(join(dir, ref), 'utf8');
  } catch (error) {
    // ENOENT: no such credential. ENOTDIR: the mount is absent entirely, which is the normal
    // state before the ExternalSecret first syncs and must not be an error.
    if (error && (error.code === 'ENOENT' || error.code === 'ENOTDIR')) return ABSENT;
    throw error;
  }
  const trimmed = text.trim();
  if (trimmed.length === 0) return ABSENT;
  // `dataFrom.extract` writes each field of the KV document as its own Secret key, so the file is
  // normally the bare value. It is JSON only when the KV field itself holds JSON.
  if (trimmed.startsWith('{')) {
    let parsed;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return trimmed;
    }
    if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const value = parsed[field];
      if (typeof value === 'string' && value.length > 0) return value;
      throw new Error(
        `openbao-credentials: "${ref}" is a JSON document without a non-empty "${field}" field`,
      );
    }
  }
  return trimmed;
}

/**
 * The provider. Only the REFERENCE half is overridden; every record method, the watcher, the
 * document lock and the launch-environment layering come from the base class unchanged.
 */
export class OpenBaoCredentialProvider extends LocalCredentialProvider {
  // The base class's own schema plus this file's two keys. It has to be RESTATED rather than
  // extended: schemastery has no inheritance here, and a subclass whose `static Config` omits
  // `path`/`dshHome`/`watch`/`debounceMs` would drop them before resolveSpec() ever sees them --
  // silently relocating the credentials document and disabling the watcher.
  static Config = z.object({
    path: z.string(),
    dshHome: z.string(),
    watch: z.boolean().default(true),
    debounceMs: z.number().min(0).default(100),
    dir: z.string().default(DEFAULT_DIR),
    field: z.string().default(DEFAULT_FIELD),
  });

  /** Mount directory, resolved once. */
  #dir;
  /** JSON field name, resolved once. */
  #field;

  constructor(ctx, config = {}) {
    super(ctx, config);
    this.#dir = typeof config.dir === 'string' && config.dir !== '' ? config.dir : DEFAULT_DIR;
    this.#field =
      typeof config.field === 'string' && config.field !== '' ? config.field : DEFAULT_FIELD;
  }

  /**
   * Whether the inherited environment supplies this reference.
   *
   * Asked through `super.describe`, which resolves the launch-time SNAPSHOT rather than the live
   * `process.env`, and which reads an in-memory map rather than touching disk. The base class's
   * own layer name is the test, so the documented precedence is preserved by construction instead
   * of being restated -- and `inherited()` itself is private, so this is also the supported way in.
   */
  async #suppliedByEnvironment(ref) {
    const info = await super.describe(ref);
    return info.configured && info.source === 'env';
  }

  /** Whether OpenBao should be consulted for this reference at all. */
  async #openbao(ref) {
    if (!REF_PATTERN.test(ref)) return ABSENT;
    if (await this.#suppliedByEnvironment(ref)) return ABSENT;
    return readFromMount(this.#dir, this.#field, ref);
  }

  async resolve(ref) {
    const value = await this.#openbao(ref);
    if (value !== ABSENT) return { value, source: SOURCE };
    return super.resolve(ref);
  }

  async describe(ref) {
    const value = await this.#openbao(ref);
    // writable: false, and it is not a courtesy. The Settings UI reads this to decide whether to
    // offer an edit box; accepting a write that OpenBao then shadows on the next resolve is the
    // exact "appears to succeed while resolution keeps returning the shadowing value" failure the
    // seam's contract forbids.
    if (value !== ABSENT) return { configured: true, source: SOURCE, writable: false };
    return super.describe(ref);
  }

  async set(ref, value) {
    // Checked, not assumed: a reference OpenBao does not currently hold is still writable to the
    // local document, and refusing it wholesale would break a legitimate local override.
    if ((await this.#openbao(ref)) !== ABSENT) {
      throw new Error(
        `openbao-credentials: "${ref}" is supplied by OpenBao and is read-only here; ` +
          'change it in OpenBao instead',
      );
    }
    return super.set(ref, value);
  }

  async unset(ref) {
    if ((await this.#openbao(ref)) !== ABSENT) {
      throw new Error(
        `openbao-credentials: "${ref}" is supplied by OpenBao and cannot be unset here; ` +
          'remove it in OpenBao instead',
      );
    }
    return super.unset(ref);
  }
}

export default OpenBaoCredentialProvider;
