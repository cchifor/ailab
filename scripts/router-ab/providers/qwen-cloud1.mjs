// providers/qwen-cloud1.mjs - qwen3.8-27b on cloud1's vLLM, behind its llama-swap (see providers/lib/qwen-backend.mjs).
// cloud1 also serves Qwen-Image on demand: while the image is resident or pending, the router keeps cloud1 out of rotation
// (its availability probe) and a direct request answers 503 model_start_failed. Do not run this while an image is in use.
// OPT-IN while cloud1 is not an enabled deployment of the route: a run without --providers leaves it out, and naming it
// before then is refused at setup. Drop optIn once cloud1 is on the route and has passed its gate (runbook llm-router.md,
// § cloud1 as the third qwen3.8-27b backend).
import { qwenProvider } from './lib/qwen-backend.mjs';

export default qwenProvider({ name: 'qwen-cloud1', host: 'cloud1', server: 'http://192.168.0.27:8080/v1', tap: 'flex', account: 'lan-192.168.0.27-8080', optIn: true });
