import { FastifyInstance } from 'fastify';
import { verifyConnectivity } from '../graph/client.js';

export default async function healthRoutes(app: FastifyInstance): Promise<void> {
  app.get('/health', async (_request, reply) => {
    try {
      await verifyConnectivity();
      return reply.send({
        status: 'ok',
        neo4j: 'connected',
        timestamp: new Date().toISOString(),
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'unknown error';
      return reply.status(503).send({
        status: 'degraded',
        neo4j: 'disconnected',
        error: message,
        timestamp: new Date().toISOString(),
      });
    }
  });
}
