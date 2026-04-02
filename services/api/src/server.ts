import { config } from './config/env.js';
import { closeDriver } from './graph/client.js';
import { buildApp } from './app.js';

async function main(): Promise<void> {
  const app = await buildApp();

  // Graceful shutdown
  const shutdown = async (signal: string) => {
    app.log.info(`Received ${signal}, shutting down gracefully...`);
    try {
      await app.close();
      await closeDriver();
      app.log.info('Server and Neo4j driver closed.');
      process.exit(0);
    } catch (err) {
      app.log.error({ err }, 'Error during shutdown');
      process.exit(1);
    }
  };

  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  try {
    await app.listen({ port: config.port, host: '0.0.0.0' });
    app.log.info(`Server listening on port ${config.port}`);
  } catch (err) {
    app.log.error({ err }, 'Failed to start server');
    await closeDriver();
    process.exit(1);
  }
}

main();
