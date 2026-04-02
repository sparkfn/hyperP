import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import neo4j from 'neo4j-driver';
import { getSession } from '../graph/client.js';

function requestId(request: FastifyRequest): string {
  return (request.headers['x-request-id'] as string) ?? crypto.randomUUID();
}

export default async function adminRoutes(app: FastifyInstance): Promise<void> {
  // -----------------------------------------------------------------------
  // GET /v1/source-systems
  // -----------------------------------------------------------------------
  app.get('/v1/source-systems', async (request: FastifyRequest, reply: FastifyReply) => {
    const reqId = requestId(request);
    const session = getSession(neo4j.session.READ);

    try {
      const res = await session.run(
        `MATCH (ss:SourceSystem)
         RETURN ss {
           .source_system_id, .source_key, .display_name,
           .system_type, .is_active, .field_trust,
           .created_at, .updated_at
         } AS source_system
         ORDER BY ss.source_key`
      );

      const systems = res.records.map((r) => {
        const ss = r.get('source_system') as Record<string, unknown>;
        return ss;
      });

      return reply.send({
        data: systems,
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // GET /v1/source-systems/:source_key/field-trust
  // -----------------------------------------------------------------------
  app.get('/v1/source-systems/:source_key/field-trust', async (request: FastifyRequest, reply: FastifyReply) => {
    const { source_key } = request.params as { source_key: string };
    const reqId = requestId(request);
    const session = getSession(neo4j.session.READ);

    try {
      const res = await session.run(
        `MATCH (ss:SourceSystem {source_key: $source_key})
         RETURN ss.field_trust AS field_trust,
                ss.source_key AS source_key,
                ss.display_name AS display_name`,
        { source_key }
      );

      if (res.records.length === 0) {
        return reply.status(404).send({
          error: { code: 'not_found', message: `Source system '${source_key}' not found.` },
          meta: { request_id: reqId },
        });
      }

      const r = res.records[0];
      return reply.send({
        data: {
          source_key: r.get('source_key'),
          display_name: r.get('display_name'),
          field_trust: r.get('field_trust') ?? {},
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // PATCH /v1/source-systems/:source_key/field-trust
  // -----------------------------------------------------------------------
  app.patch('/v1/source-systems/:source_key/field-trust', async (request: FastifyRequest, reply: FastifyReply) => {
    const { source_key } = request.params as { source_key: string };
    const body = request.body as Record<string, string>;
    const reqId = requestId(request);

    if (!body || Object.keys(body).length === 0) {
      return reply.status(400).send({
        error: { code: 'invalid_request', message: 'Provide at least one field trust update.' },
        meta: { request_id: reqId },
      });
    }

    // Validate trust tier values
    const validTiers = new Set(['tier_1', 'tier_2', 'tier_3', 'tier_4']);
    for (const [field, tier] of Object.entries(body)) {
      if (!validTiers.has(tier)) {
        return reply.status(400).send({
          error: {
            code: 'invalid_request',
            message: `Invalid trust tier '${tier}' for field '${field}'. Must be one of: tier_1, tier_2, tier_3, tier_4.`,
          },
          meta: { request_id: reqId },
        });
      }
    }

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        // Read current field_trust, merge updates, write back
        const current = await tx.run(
          `MATCH (ss:SourceSystem {source_key: $source_key})
           RETURN ss.field_trust AS field_trust`,
          { source_key }
        );

        if (current.records.length === 0) {
          return { not_found: true };
        }

        const existingTrust = (current.records[0].get('field_trust') as Record<string, string>) ?? {};
        const updatedTrust = { ...existingTrust, ...body };

        await tx.run(
          `MATCH (ss:SourceSystem {source_key: $source_key})
           SET ss.field_trust = $field_trust,
               ss.updated_at = datetime()`,
          { source_key, field_trust: updatedTrust }
        );

        return { field_trust: updatedTrust };
      });

      if ('not_found' in result && result.not_found) {
        return reply.status(404).send({
          error: { code: 'not_found', message: `Source system '${source_key}' not found.` },
          meta: { request_id: reqId },
        });
      }

      return reply.send({
        data: {
          source_key,
          field_trust: result.field_trust,
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });
}
