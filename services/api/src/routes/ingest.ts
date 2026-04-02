import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import neo4j from 'neo4j-driver';
import { getSession } from '../graph/client.js';

function requestId(request: FastifyRequest): string {
  return (request.headers['x-request-id'] as string) ?? crypto.randomUUID();
}

/**
 * Thin pass-through ingestion endpoints. Heavy ingestion logic (normalization,
 * candidate generation, matching) lives in the Python service. These endpoints
 * provide the HTTP contract and persist lightweight metadata in Neo4j.
 */
export default async function ingestRoutes(app: FastifyInstance): Promise<void> {
  // -----------------------------------------------------------------------
  // POST /v1/ingest/:source_key/records
  // -----------------------------------------------------------------------
  app.post('/v1/ingest/:source_key/records', async (request: FastifyRequest, reply: FastifyReply) => {
    const { source_key } = request.params as { source_key: string };
    const body = request.body as {
      ingest_type: string;
      ingest_run_id?: string;
      records: Array<{
        source_record_id: string;
        source_record_version?: string;
        observed_at: string;
        record_hash: string;
        identifiers: Array<{ type: string; value: string; is_verified?: boolean }>;
        attributes: Record<string, unknown>;
        raw_payload?: Record<string, unknown>;
      }>;
    };
    const reqId = requestId(request);

    if (!body.records || body.records.length === 0) {
      return reply.status(400).send({
        error: { code: 'invalid_request', message: 'At least one record is required.' },
        meta: { request_id: reqId },
      });
    }

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        // Verify source system exists
        const ssCheck = await tx.run(
          `MATCH (ss:SourceSystem {source_key: $source_key, is_active: true})
           RETURN ss.source_system_id AS id`,
          { source_key }
        );

        if (ssCheck.records.length === 0) {
          return { source_not_found: true };
        }

        // Create or link to ingest run
        let ingestRunId = body.ingest_run_id;
        if (!ingestRunId) {
          const runResult = await tx.run(
            `MATCH (ss:SourceSystem {source_key: $source_key})
             CREATE (ir:IngestRun {
               ingest_run_id: randomUUID(),
               run_type: $ingest_type,
               status: 'started',
               started_at: datetime(),
               record_count: 0,
               rejected_count: 0,
               metadata: {}
             })
             CREATE (ir)-[:FROM_SOURCE]->(ss)
             RETURN ir.ingest_run_id AS ingest_run_id`,
            { source_key, ingest_type: body.ingest_type }
          );
          ingestRunId = String(runResult.records[0].get('ingest_run_id'));
        }

        // Create source records
        const results: Array<{ source_record_id: string; status: string }> = [];
        let acceptedCount = 0;
        let rejectedCount = 0;

        for (const record of body.records) {
          try {
            await tx.run(
              `MATCH (ss:SourceSystem {source_key: $source_key})
               MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
               CREATE (sr:SourceRecord {
                 source_record_pk: randomUUID(),
                 source_record_id: $source_record_id,
                 source_record_version: $source_record_version,
                 link_status: 'pending_review',
                 observed_at: datetime($observed_at),
                 ingested_at: datetime(),
                 record_hash: $record_hash,
                 raw_payload: $raw_payload,
                 normalized_payload: $attributes,
                 metadata: {},
                 retention_expires_at: null
               })
               CREATE (sr)-[:FROM_SOURCE]->(ss)
               CREATE (sr)-[:PART_OF_RUN]->(ir)`,
              {
                source_key,
                ingest_run_id: ingestRunId,
                source_record_id: record.source_record_id,
                source_record_version: record.source_record_version ?? null,
                observed_at: record.observed_at,
                record_hash: record.record_hash,
                raw_payload: record.raw_payload ?? {},
                attributes: record.attributes ?? {},
              }
            );
            results.push({ source_record_id: record.source_record_id, status: 'accepted' });
            acceptedCount++;
          } catch {
            results.push({ source_record_id: record.source_record_id, status: 'rejected' });
            rejectedCount++;
          }
        }

        // Update run counters
        await tx.run(
          `MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
           SET ir.record_count = ir.record_count + $accepted,
               ir.rejected_count = ir.rejected_count + $rejected`,
          { ingest_run_id: ingestRunId, accepted: neo4j.int(acceptedCount), rejected: neo4j.int(rejectedCount) }
        );

        return {
          accepted_count: acceptedCount,
          rejected_count: rejectedCount,
          ingest_run_id: ingestRunId,
          results,
        };
      });

      if ('source_not_found' in result && result.source_not_found) {
        return reply.status(404).send({
          error: { code: 'not_found', message: `Source system '${source_key}' not found or inactive.` },
          meta: { request_id: reqId },
        });
      }

      return reply.send({
        data: result,
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // POST /v1/ingest/:source_key/runs
  // -----------------------------------------------------------------------
  app.post('/v1/ingest/:source_key/runs', async (request: FastifyRequest, reply: FastifyReply) => {
    const { source_key } = request.params as { source_key: string };
    const body = request.body as {
      run_type: string;
      metadata?: Record<string, unknown>;
    };
    const reqId = requestId(request);

    if (!body.run_type) {
      return reply.status(400).send({
        error: { code: 'invalid_request', message: 'run_type is required.' },
        meta: { request_id: reqId },
      });
    }

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        const res = await tx.run(
          `MATCH (ss:SourceSystem {source_key: $source_key, is_active: true})
           CREATE (ir:IngestRun {
             ingest_run_id: randomUUID(),
             run_type: $run_type,
             status: 'started',
             started_at: datetime(),
             finished_at: null,
             record_count: 0,
             rejected_count: 0,
             metadata: $metadata
           })
           CREATE (ir)-[:FROM_SOURCE]->(ss)
           RETURN ir.ingest_run_id AS ingest_run_id,
                  ir.status AS status,
                  toString(ir.started_at) AS started_at`,
          {
            source_key,
            run_type: body.run_type,
            metadata: body.metadata ?? {},
          }
        );

        if (res.records.length === 0) {
          return { source_not_found: true };
        }

        return {
          ingest_run_id: String(res.records[0].get('ingest_run_id')),
          status: String(res.records[0].get('status')),
          started_at: String(res.records[0].get('started_at')),
        };
      });

      if ('source_not_found' in result && result.source_not_found) {
        return reply.status(404).send({
          error: { code: 'not_found', message: `Source system '${source_key}' not found or inactive.` },
          meta: { request_id: reqId },
        });
      }

      return reply.status(201).send({
        data: result,
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // PATCH /v1/ingest/:source_key/runs/:ingest_run_id
  // -----------------------------------------------------------------------
  app.patch('/v1/ingest/:source_key/runs/:ingest_run_id', async (request: FastifyRequest, reply: FastifyReply) => {
    const { source_key, ingest_run_id } = request.params as { source_key: string; ingest_run_id: string };
    const body = request.body as {
      status: string;
      finished_at?: string;
      metadata?: Record<string, unknown>;
    };
    const reqId = requestId(request);

    if (!body.status) {
      return reply.status(400).send({
        error: { code: 'invalid_request', message: 'status is required.' },
        meta: { request_id: reqId },
      });
    }

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        const res = await tx.run(
          `MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})-[:FROM_SOURCE]->(ss:SourceSystem {source_key: $source_key})
           SET ir.status = $status,
               ir.finished_at = CASE WHEN $finished_at IS NOT NULL THEN datetime($finished_at) ELSE ir.finished_at END,
               ir.metadata = CASE WHEN $metadata IS NOT NULL THEN $metadata ELSE ir.metadata END
           RETURN ir.ingest_run_id AS ingest_run_id,
                  ir.status AS status,
                  toString(ir.finished_at) AS finished_at`,
          {
            ingest_run_id,
            source_key,
            status: body.status,
            finished_at: body.finished_at ?? null,
            metadata: body.metadata ?? null,
          }
        );

        if (res.records.length === 0) {
          return { not_found: true };
        }

        return {
          ingest_run_id: String(res.records[0].get('ingest_run_id')),
          status: String(res.records[0].get('status')),
          finished_at: res.records[0].get('finished_at') as string | null,
        };
      });

      if ('not_found' in result && result.not_found) {
        return reply.status(404).send({
          error: { code: 'not_found', message: 'Ingest run not found.' },
          meta: { request_id: reqId },
        });
      }

      return reply.send({
        data: result,
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // GET /v1/ingest/runs/:ingest_run_id
  // -----------------------------------------------------------------------
  app.get('/v1/ingest/runs/:ingest_run_id', async (request: FastifyRequest, reply: FastifyReply) => {
    const { ingest_run_id } = request.params as { ingest_run_id: string };
    const reqId = requestId(request);
    const session = getSession(neo4j.session.READ);

    try {
      const res = await session.run(
        `MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
         OPTIONAL MATCH (ir)-[:FROM_SOURCE]->(ss:SourceSystem)
         RETURN ir {
           .ingest_run_id, .run_type, .status,
           .record_count, .rejected_count, .metadata
         } AS run,
         toString(ir.started_at) AS started_at,
         toString(ir.finished_at) AS finished_at,
         ss.source_key AS source_key`,
        { ingest_run_id }
      );

      if (res.records.length === 0) {
        return reply.status(404).send({
          error: { code: 'not_found', message: 'Ingest run not found.' },
          meta: { request_id: reqId },
        });
      }

      const r = res.records[0];
      const run = r.get('run') as Record<string, unknown>;

      return reply.send({
        data: {
          ...run,
          started_at: r.get('started_at'),
          finished_at: r.get('finished_at'),
          source_key: r.get('source_key'),
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });
}
