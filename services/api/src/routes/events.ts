import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import neo4j from 'neo4j-driver';
import { getSession } from '../graph/client.js';

function requestId(request: FastifyRequest): string {
  return (request.headers['x-request-id'] as string) ?? crypto.randomUUID();
}

/**
 * Downstream event polling endpoint. Events are derived from MergeEvent nodes
 * and other lifecycle records. Designed for future migration to a push-based
 * delivery mechanism (webhooks, message queues).
 */
export default async function eventRoutes(app: FastifyInstance): Promise<void> {
  // -----------------------------------------------------------------------
  // GET /v1/events
  // -----------------------------------------------------------------------
  app.get('/v1/events', async (request: FastifyRequest, reply: FastifyReply) => {
    const {
      since,
      event_type,
      cursor,
      limit: rawLimit,
    } = request.query as Record<string, string | undefined>;

    const reqId = requestId(request);

    if (!since) {
      return reply.status(400).send({
        error: { code: 'invalid_request', message: 'since parameter is required (ISO 8601 timestamp).' },
        meta: { request_id: reqId },
      });
    }

    const limit = Math.min(parseInt(rawLimit ?? '50', 10) || 50, 200);
    const skip = cursor ? parseInt(Buffer.from(cursor, 'base64').toString(), 10) : 0;

    const session = getSession(neo4j.session.READ);
    try {
      // Map MergeEvent.event_type to the downstream event_type vocabulary.
      // MergeEvent covers: person_created, auto_merge, manual_merge,
      // review_reject, manual_no_match, unmerge, person_split,
      // survivorship_override.
      const eventTypeFilter = event_type ? `AND me.event_type = $event_type` : '';

      const res = await session.run(
        `MATCH (me:MergeEvent)
         WHERE me.created_at >= datetime($since)
         ${eventTypeFilter}
         OPTIONAL MATCH (me)-[:ABSORBED]->(absorbed:Person)
         OPTIONAL MATCH (me)-[:SURVIVOR]->(survivor:Person)
         WITH me, collect(DISTINCT absorbed.person_id) + collect(DISTINCT survivor.person_id) AS pids
         WITH me, [x IN pids WHERE x IS NOT NULL] AS affected_person_ids
         RETURN me.merge_event_id AS event_id,
                CASE me.event_type
                  WHEN 'auto_merge' THEN 'person_merged'
                  WHEN 'manual_merge' THEN 'person_merged'
                  WHEN 'unmerge' THEN 'person_unmerged'
                  WHEN 'person_created' THEN 'person_created'
                  WHEN 'survivorship_override' THEN 'golden_profile_updated'
                  WHEN 'review_reject' THEN 'review_case_resolved'
                  WHEN 'manual_no_match' THEN 'review_case_resolved'
                  ELSE me.event_type
                END AS event_type,
                affected_person_ids,
                me.metadata AS metadata,
                toString(me.created_at) AS created_at
         ORDER BY me.created_at ASC
         SKIP $skip LIMIT $limit`,
        {
          since,
          event_type: event_type ?? null,
          skip: neo4j.int(skip),
          limit: neo4j.int(limit + 1),
        }
      );

      const hasMore = res.records.length > limit;
      const events = res.records.slice(0, limit).map((r) => ({
        event_id: String(r.get('event_id')),
        event_type: String(r.get('event_type')),
        affected_person_ids: (r.get('affected_person_ids') as string[]) ?? [],
        metadata: r.get('metadata') ?? {},
        created_at: String(r.get('created_at')),
      }));

      const nextCursor = hasMore
        ? Buffer.from(String(skip + limit)).toString('base64')
        : null;

      return reply.send({
        data: events,
        meta: { request_id: reqId, next_cursor: nextCursor },
      });
    } finally {
      await session.close();
    }
  });
}
