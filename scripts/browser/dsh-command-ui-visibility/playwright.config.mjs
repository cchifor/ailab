import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'.',testMatch:'*.spec.mjs',workers:1,retries:0,timeout:90000,reporter:[['list'],['json',{outputFile:'report.json'}]],outputDir:'test-results',use:{trace:'off',video:'off'}});
