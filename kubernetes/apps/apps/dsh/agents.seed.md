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
