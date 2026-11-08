// providers/qwen-cloud1.mjs - qwen3.8-27b on cloud1's vLLM, behind its llama-swap (see providers/lib/qwen-backend.mjs).
// cloud1 also serves Qwen-Image on demand: while the image is resident or pending, the router keeps cloud1 out of rotation
// (its availability probe) and a direct request answers 503 model_start_failed. Do not run this while an image is in use.
// In the full gate since cloud1 went on the route and passed its own gate, 2026-11-07 (runbook llm-router.md,
// § cloud1 as the third qwen3.8-27b backend). Taken off the route again, it is refused at setup, in seconds.
import { qwenProvider } from './lib/qwen-backend.mjs';

export default qwenProvider({ name: 'qwen-cloud1', host: 'cloud1', server: 'http://192.168.0.27:8080/v1', tap: 'flex', account: 'lan-192.168.0.27-8080' });
