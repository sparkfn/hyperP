import Fastify, { FastifyInstance } from 'fastify';
import cors from '@fastify/cors';
import sensible from '@fastify/sensible';
import { config } from './config/env.js';
import healthRoutes from './routes/health.js';
import personRoutes from './routes/persons.js';
import reviewRoutes from './routes/review.js';
import mergeRoutes from './routes/merge.js';
import survivorshipRoutes from './routes/survivorship.js';
import ingestRoutes from './routes/ingest.js';
import adminRoutes from './routes/admin.js';
import eventRoutes from './routes/events.js';

export async function buildApp(): Promise<FastifyInstance> {
  const app = Fastify({
    logger: {
      level: config.logLevel,
      transport:
        process.env.NODE_ENV !== 'production'
          ? { target: 'pino/file', options: { destination: 1 } }
          : undefined,
    },
    genReqId: () => crypto.randomUUID(),
    requestIdHeader: 'x-request-id',
  });

  // --- Plugins ---
  await app.register(cors, {
    origin: true,
    credentials: true,
  });

  await app.register(sensible);

  // --- Route plugins ---
  await app.register(healthRoutes);
  await app.register(personRoutes);
  await app.register(reviewRoutes);
  await app.register(mergeRoutes);
  await app.register(survivorshipRoutes);
  await app.register(ingestRoutes);
  await app.register(adminRoutes);
  await app.register(eventRoutes);

  // --- Global error handler ---
  app.setErrorHandler((error: Error & { statusCode?: number }, request, reply) => {
    const statusCode = error.statusCode ?? 500;
    const reqId = request.id as string;

    request.log.error({ err: error }, 'Request error');

    return reply.status(statusCode).send({
      error: {
        code: statusCode >= 500 ? 'internal_error' : 'invalid_request',
        message: statusCode >= 500 ? 'An internal error occurred.' : error.message,
      },
      meta: { request_id: reqId },
    });
  });

  // --- Not-found handler ---
  app.setNotFoundHandler((_request, reply) => {
    return reply.status(404).send({
      error: { code: 'not_found', message: 'Route not found.' },
      meta: { request_id: _request.id as string },
    });
  });

  return app;
}
