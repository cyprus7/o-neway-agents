import { defineConfig, devices } from '@playwright/test';

// Dev-only runner: real React UI, isolated loopback Vite server, fixture APIs.
export default defineConfig({
  testDir: './e2e',
  testIgnore: '**/*.integration.spec.ts',
  outputDir: './test-results/fixtures',
  fullyParallel: true,
  workers: 4,
  timeout: 30_000,
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:4179', trace: 'retain-on-failure' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } } },
    { name: 'tablet', use: { ...devices['Desktop Chrome'], viewport: { width: 768, height: 1024 } } },
    { name: 'mobile', use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } } },
    { name: 'narrow', use: { ...devices['Desktop Chrome'], viewport: { width: 320, height: 740 } } },
  ],
  globalSetup: './e2e/fixtures/start-vite.ts',
});
