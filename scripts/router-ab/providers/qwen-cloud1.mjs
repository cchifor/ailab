// providers/qwen-cloud1.mjs - qwen3.8-27b on cloud1's vLLM, behind its llama-swap (see providers/lib/qwen-backend.mjs).
// cloud1 also serves Qwen-Image on demand: while the image is resident or pending, the router keeps cloud1 out of rotation
// (its availability probe) and a direct request answers 503 model_start_failed. Do not run this while an image is in use.
// cloud1 went on the route and passed its own gate on 2026-11-07; the full gate has run it since 2026-11-08 (runbook
// llm-router.md, § cloud1 as the third qwen3.8-27b backend). With cloud1 off the route it is refused at setup, in
// seconds: the report marks it INVALID and the other providers still run.
import { qwenProvider } from './lib/qwen-backend.mjs';

export default qwenProvider({ name: 'qwen-cloud1', host: 'cloud1', server: 'http://192.168.0.27:8080/v1', tap: 'flex', account: 'lan-192.168.0.27-8080' });
