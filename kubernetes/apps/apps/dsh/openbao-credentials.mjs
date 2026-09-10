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
/** Reported as `source` when a value came from OpenBao. */
const SOURCE = 'openbao';

/**
 * The reference grammar, restated from @deepseek-ai/dsh-credentials' own REF_PATTERN.
 *
 * This is the containment check that matters for THIS file: a reference becomes a path segment
 * under the mount, so anything outside the grammar is REFUSED rather than escaped or normalised.
 * `credentialRef` would already have rejected it upstream; re-checking here means a future caller
 * that skips that constructor still cannot walk out of the directory. Deleting this check makes
 * the provider return the contents of an arbitrary file -- which is what a mutation test does.
 */
const REF_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Absence, distinguished from failure. `undefined` means "the mount does not carry this". */
const ABSENT = undefined;

/**
 * Read one credential from the mount.
 *
 * The value is returned VERBATIM. `dataFrom.extract` has already pulled the individual field out
 * of the KV document, so what lands in the file is the credential itself with no envelope left to
 * strip -- and trimming it, or reinterpreting a value that happens to begin with `{` as JSON,
 * would corrupt legitimate opaque secrets. Only the empty file is special, and it means absent.
 *
 * @param dir - the mount directory.
 * @param ref - the reference, already grammar-checked.
 * @returns the value, or {@link ABSENT} when the mount does not carry this credential.
 * @throws when the mount is unreadable -- a FAILURE, which must NOT be reported as absence:
 *   "not carried here" and "I could not tell" are different answers, and only the first may fall
 *   through to a lower layer. Falling through on a failure would silently resolve a stale local
 *   value while OpenBao held a rotated one.
 */
async function readFromMount(dir, ref) {
  let text;
  try {
    // Reopened by pathname every time, never a retained descriptor: kubelet updates a Secret
    // volume by swapping an atomic symlink, so a held fd would pin the value at mount time and a
    // rotated credential would read as unchanged forever.
    text = await readFile(join(dir, ref), 'utf8');
  } catch (error) {
    // ENOENT ONLY. An absent optional Secret still mounts as an EMPTY DIRECTORY, and a removed
    // volume leaves the directory itself missing, so both arrive here as ENOENT. ENOTDIR means a
    // path component is a regular file -- broken configuration, not absence -- and is propagated.
    if (error && error.code === 'ENOENT') return ABSENT;
    throw error;
  }
  return text.length === 0 ? ABSENT : text;
}

/**
 * The provider. Only the REFERENCE half is overridden; every record method, the watcher, the
 * document lock and the launch-environment layering come from the base class unchanged.
 *
 * NO NATIVE `#private` MEMBERS, AND THAT IS LOAD-BEARING. cordis hands a service call a SHADOW
 * receiver: `createShadowMethod` substitutes `thisArg` for a Proxy over the instance before
 * applying the method. Native private fields are branded to the instance, so `this.#anything`
 * inside a method reached through `ctx.credentials` throws
 *     TypeError: Cannot read private member #x from an object whose class did not declare it
 * -- which would make every resolution fail even when the value comes from the environment and
 * the mount is absent. Upstream's TypeScript `private` compiles to ordinary properties and does
 * not have this problem. Ordinary `openbao`-prefixed properties are used instead, prefixed
 * because the base class owns `config`, `spec`, `text`, `values`, `records` and `operations`.
 */
export class OpenBaoCredentialProvider extends LocalCredentialProvider {
  // The base class's schema plus this file's key. Restated rather than composed with
  // `z.intersect([LocalCredentialProvider.Config, ...])`, which would also work: one literal is
  // easier to read against the base than an intersection, and this is the surface an operator
  // checks when asking where a credential comes from. Restating means the base keys must stay in
  // step with dsh-credentials-local across upgrades.
  static Config = z.object({
    path: z.string(),
    dshHome: z.string(),
    watch: z.boolean().default(true),
    debounceMs: z.number().min(0).default(100),
    dir: z.string().default(DEFAULT_DIR),
  });

  constructor(ctx, config = {}) {
    super(ctx, config);
    this.openbaoDir = typeof config.dir === 'string' && config.dir !== '' ? config.dir : DEFAULT_DIR;
  }

  /**
   * The OpenBao value for a reference, or {@link ABSENT} when a lower layer should answer.
   *
   * The environment is checked through `super.describe`, which resolves the launch-time SNAPSHOT
   * rather than the live `process.env` and reads an in-memory map rather than touching disk. The
   * base class's own layer name is the test, so the documented precedence is preserved by
   * construction instead of being restated -- and `inherited()` is private upstream, so this is
   * also the supported way in.
   */
  async openbaoLookup(ref) {
    if (!REF_PATTERN.test(ref)) return ABSENT;
    const info = await super.describe(ref);
    if (info.configured && info.source === 'env') return ABSENT;
    return readFromMount(this.openbaoDir, ref);
  }

  async resolve(ref) {
    const value = await this.openbaoLookup(ref);
    if (value !== ABSENT) return { value, source: SOURCE };
    return super.resolve(ref);
  }

  async describe(ref) {
    const value = await this.openbaoLookup(ref);
    // writable: false, and it is not a courtesy. The Settings UI reads this to decide whether to
    // offer an edit box; accepting a write that OpenBao then shadows on the next resolve is the
    // exact "appears to succeed while resolution keeps returning the shadowing value" failure the
    // seam's contract forbids.
    if (value !== ABSENT) return { configured: true, source: SOURCE, writable: false };
    return super.describe(ref);
  }

  async set(ref, value) {
    // Checked, not assumed: a reference the mount does not carry is still writable to the local
    // document, and refusing it wholesale would break a legitimate local override.
    if ((await this.openbaoLookup(ref)) !== ABSENT) {
      throw new Error(
        `openbao-credentials: "${ref}" is supplied by OpenBao and is read-only here; ` +
          'change it in OpenBao instead',
      );
    }
    return super.set(ref, value);
  }

  async unset(ref) {
    if ((await this.openbaoLookup(ref)) !== ABSENT) {
      throw new Error(
        `openbao-credentials: "${ref}" is supplied by OpenBao and cannot be unset here; ` +
          'remove it in OpenBao instead',
      );
    }
    return super.unset(ref);
  }
}

export default OpenBaoCredentialProvider;
