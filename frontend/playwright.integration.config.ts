import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.PRODUCT_INTEGRATION_URL;
if (!baseURL) throw new Error('PRODUCT_INTEGRATION_URL must point to the explicitly started model/provider-free product fixture server.');
export default defineConfig({
  testDir: './e2e', testMatch: '**/*.integration.spec.ts', outputDir: './test-results/integration', workers: 1,
  timeout: 60_000, reporter: 'list',
  use: { baseURL, trace: 'retain-on-failure' },
  projects: [
    { name: 'actual-desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } } },
    { name: 'actual-mobile', use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } } },
  ],
});
