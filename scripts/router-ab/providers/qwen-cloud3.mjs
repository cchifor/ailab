// providers/qwen-cloud3.mjs - qwen3.8-27b on cloud3's vLLM, behind llama-swap (see providers/lib/qwen-backend.mjs).
import { qwenProvider } from './lib/qwen-backend.mjs';

export default qwenProvider({ name: 'qwen-cloud3', host: 'cloud3', server: 'http://192.168.0.26:8080/v1', tap: 'llama', account: 'lan-192.168.0.26-8080' });
