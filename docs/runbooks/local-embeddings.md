# Local document embeddings

`bge-m3-local-v1` is an independent text encoder served through the main
LiteLLM gateway. It can support Qwen, OpenAI, and other text generators; the
generator does not need to share its provider. Document and query vectors
must use the same persisted embedding profile.

The deployment pins BAAI/bge-m3 revision
`5617a9f61b028005a4858fdac845db406aefb181` and TEI image digest
`2538ea1c9640d3763b15af668039d24172d063b42337b0c27796fc2be180c78d`.
The init container verifies every downloaded artifact against
`embedding-assets.sha256`. The weights are unchanged; a separate serving
configuration limits input to 1,024 native tokens. Truncation is disabled.
Oversized requests fail explicitly instead of silently dropping source text.

The serving contract is 1,024-dimensional, L2-normalized dense vectors,
at most eight inputs per request, and at most eight concurrent inputs.
Callers must chunk with the pinned tokenizer, account for special tokens,
and send requests within these limits. The gateway does not fall back to a
different encoder. Neither padding vectors nor changing their model labels
is a migration.

## Qualification and activation

Run the real semantic qualification against the OpenAI-compatible base URL:

```sh
EMBEDDING_TEST_API_KEY=<gateway-client-key> python3 scripts/validate-local-embeddings.py \
  --base-url https://<gateway>/v1 --output embedding-qualification.json
```

Supply the key through the estate credential runner, not shell history.
The check verifies dimensions, normalization, English and French retrieval,
repeated-input stability, and oversized-input rejection. It uses fixed
synthetic sentences and does not read application documents.

Local qualification also exercised a full batch of eight inputs, each
exactly 1,024 tokens: all eight returned 1,024-dimensional vectors in
8.22 seconds with a four-CPU, six-GiB container limit. Startup, inference,
and checksum-verified cold downloads were tested as UID 1000, with a
read-only root filesystem. These are local measurements, not ailab latency
guarantees.

Deploy the encoder and verify gateway-to-encoder requests before enabling
platform consumers. A gateway rollout alone does not prove that the new
encoder is ready. Knowledge must support the new dimensionality and pin
the same profile for indexing and query embedding before the App pipeline
is activated. Existing OpenAI indexes retain their original route and
dimensions until an explicit reindex completes.

## Availability and recovery

This is one CPU replica with a node-local cache and a `Recreate` strategy.
Restarts cause a brief outage, and loss of its node requires restoring or
recreating the cache on a healthy node. It is not a highly available tier.
The cache contains only public, reproducible model artifacts, not document
data. A cold start needs outbound HTTPS to Hugging Face and its artifact
hosts. Corrupt or partial downloads cannot become the serving model.

The model service is ClusterIP-only. Its ingress policy allows the main
LiteLLM pods in the AI namespace; application clients use the authenticated
gateway. No Kubernetes service-account token is mounted in the encoder.
