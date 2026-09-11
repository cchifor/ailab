# Working notes for this deployment

Installed to `$DSH_HOME/AGENTS.md` by the seed-settings initContainer and picked up by the
`agent-instructions` row as the user-global instruction file.

Everything here is a fact about THIS deployment. General tool rules are already in the system
prompt and are deliberately not repeated — the read-before-write rule in particular is stated
there verbatim, so restating it buys nothing.

## Write generated artifacts under /workspace

`/workspace` is the working directory and starts empty each time. `$DSH_HOME` is a persistent
volume that carries files from **earlier sessions**, and it is where stray artifacts have been
accumulating: eight pelican SVGs and PNGs from previous runs were sitting there.

That matters because file observation is per-session and is not persisted. A filename you have
not used in *this* session may still exist on disk from a previous one, and writing to it fails
with:

    cannot modify "<path>": file has not been read — read the file, then retry

which is the read-before-overwrite interlock doing its job — it is refusing to blind-overwrite
something you have never seen. Glob or read before writing to a path in `$DSH_HOME`, or simply
put new artifacts in `/workspace`, where the question does not arise.

## Images are capped per request

The vision route accepts at most 64 images in a single request, and every image already in the
conversation is re-sent on every turn. Rendering a file and then reading back the render plus
several crops spends that budget quickly. Prefer one look at a full render over many crops of it,
and re-render at a larger size instead of re-cropping the same image repeatedly.

## Git access to git.chifor.me is automatic

`git clone`, `fetch`, `pull` and `push` over **HTTPS** to `https://git.chifor.me/...` authenticate
by themselves: `~/.gitconfig` attaches a credential helper to that authority, and the helper reads
the credential this deployment is provisioned with. Do not look for tokens in the environment (the
shell environment is scrubbed of credential-shaped variables on purpose), do not ask the user to
paste one, and do not suggest making a repository public. Just run git.

If a git command still fails with `could not read Username for 'https://git.chifor.me'`, look at
what else it printed, because that line alone does not say why:

- A line starting with `git-credential-openbao:` means the helper ran and the deployment has not
  been provisioned (or is mis-provisioned) with `GITEA_USER`/`GITEA_PAT` in OpenBao. That is an
  operator task, not something you can fix from here; report the helper's line verbatim.
- No such line means git never reached the helper: check that `git config --global -l` shows
  `credential.https://git.chifor.me.helper` and that the helper it names is executable. Do not
  print the contents of credential files.

The Gitea **REST API** (`/api/v1/...`) is not covered by this: the helper answers git, not curl.
