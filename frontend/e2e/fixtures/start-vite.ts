import { createServer } from 'vite';

// Own the server directly so Windows test teardown does not need taskkill rights.
export default async function startVite() {
  const server = await createServer({ server: { host: '127.0.0.1', port: 4179, strictPort: true } });
  await server.listen();
  return async () => { await server.close(); };
}
