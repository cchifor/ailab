// providers/qwen.mjs - qwen3.8-27b on cloud2's vLLM (see providers/lib/qwen-backend.mjs).
import { qwenProvider } from './lib/qwen-backend.mjs';

export default qwenProvider({ name: 'qwen', host: 'cloud2', server: 'http://192.168.0.28:18020/v1', tap: 'vllm', account: 'lan-192.168.0.28-18020' });
