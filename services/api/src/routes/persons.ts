import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import neo4j from 'neo4j-driver';
import { getSession } from '../graph/client.js';
import {
  FIND_PERSON_BY_IDENTIFIER,
  GET_PERSON_BY_ID,
  GET_PERSON_SOURCE_RECORDS,
  GET_PERSON_CONNECTIONS_IDENTIFIER,
  GET_PERSON_CONNECTIONS_ADDRESS,
  GET_PERSON_CONNECTIONS_ALL,
  SEARCH_PERSONS,
  GET_PERSON_AUDIT,
  GET_PERSON_MATCHES,
} from '../graph/queries.js';
import type { Person, AddressSummary, PersonConnection, ApiResponse } from '../types/index.js';

function toISOStringOrNull(val: unknown): string | null {
  if (val == null) return null;
  if (typeof val === 'string') return val;
  if (typeof val === 'object' && 'toStandardDate' in (val as Record<string, unknown>)) {
    return (val as { toStandardDate(): Date }).toStandardDate().toISOString();
  }
  return String(val);
}

function toNumber(val: unknown): number {
  if (val == null) return 0;
  if (typeof val === 'number') return val;
  if (typeof val === 'object' && 'toNumber' in (val as Record<string, unknown>)) {
    return (val as { toNumber(): number }).toNumber();
  }
  return Number(val);
}

function mapPerson(record: Record<string, unknown>, addressKey = 'preferred_address'): Person {
  const p = record.person as Record<string, unknown>;
  const addr = record[addressKey] as Record<string, unknown> | null;

  return {
    person_id: String(p.person_id),
    status: String(p.status) as Person['status'],
    is_high_value: Boolean(p.is_high_value),
    is_high_risk: Boolean(p.is_high_risk),
    preferred_full_name: p.preferred_full_name as string | null,
    preferred_phone: p.preferred_phone as string | null,
    preferred_email: p.preferred_email as string | null,
    preferred_dob: p.preferred_dob as string | null,
    preferred_address: addr?.address_id
      ? {
          address_id: String(addr.address_id),
          unit_number: (addr.unit_number as string) ?? null,
          street_number: (addr.street_number as string) ?? null,
          street_name: (addr.street_name as string) ?? null,
          city: (addr.city as string) ?? null,
          postal_code: (addr.postal_code as string) ?? null,
          country_code: (addr.country_code as string) ?? null,
          normalized_full: (addr.normalized_full as string) ?? null,
        }
      : null,
    profile_completeness_score: toNumber(p.profile_completeness_score),
    golden_profile_computed_at: toISOStringOrNull(p.golden_profile_computed_at),
    golden_profile_version: (p.golden_profile_version as string) ?? null,
    source_record_count: toNumber(record.source_record_count ?? 0),
    created_at: toISOStringOrNull(p.created_at) ?? '',
    updated_at: toISOStringOrNull(p.updated_at) ?? '',
  };
}

function requestId(request: FastifyRequest): string {
  return (request.headers['x-request-id'] as string) ?? crypto.randomUUID();
}

export default async function personRoutes(app: FastifyInstance): Promise<void> {
  // -----------------------------------------------------------------------
  // GET /v1/persons/search
  // -----------------------------------------------------------------------
  app.get('/v1/persons/search', async (request: FastifyRequest, reply: FastifyReply) => {
    const {
      identifier_type,
      value,
      q,
      status,
      cursor,
      limit: rawLimit,
    } = request.query as Record<string, string | undefined>;

    const limit = Math.min(parseInt(rawLimit ?? '20', 10) || 20, 100);
    const skip = cursor ? parseInt(Buffer.from(cursor, 'base64').toString(), 10) : 0;
    const reqId = requestId(request);

    if (!identifier_type && !value && !q) {
      return reply.status(400).send({
        error: { code: 'invalid_request', message: 'Provide identifier_type+value or q.' },
        meta: { request_id: reqId },
      });
    }

    const session = getSession(neo4j.session.READ);
    try {
      let results: Person[];

      if (identifier_type && value) {
        const res = await session.run(FIND_PERSON_BY_IDENTIFIER, {
          identifier_type,
          value,
        });
        results = res.records.map((r) => {
          const obj = Object.fromEntries(r.keys.map((k) => [k, r.get(k)]));
          return mapPerson(obj);
        });
      } else if (q) {
        if (q.length < 3) {
          return reply.status(400).send({
            error: { code: 'invalid_request', message: 'Free-text query q requires at least 3 characters.' },
            meta: { request_id: reqId },
          });
        }
        const res = await session.run(SEARCH_PERSONS, {
          query: q,
          status: status ?? null,
          skip: neo4j.int(skip),
          limit: neo4j.int(limit + 1),
        });
        results = res.records.slice(0, limit).map((r) => {
          const obj = Object.fromEntries(r.keys.map((k) => [k, r.get(k)]));
          return mapPerson(obj);
        });

        const hasMore = res.records.length > limit;
        const nextCursor = hasMore
          ? Buffer.from(String(skip + limit)).toString('base64')
          : null;

        return reply.send({
          data: results,
          meta: { request_id: reqId, next_cursor: nextCursor },
        } satisfies ApiResponse<Person[]>);
      } else {
        results = [];
      }

      return reply.send({
        data: results,
        meta: { request_id: reqId },
      } satisfies ApiResponse<Person[]>);
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // GET /v1/persons/:person_id
  // -----------------------------------------------------------------------
  app.get('/v1/persons/:person_id', async (request: FastifyRequest, reply: FastifyReply) => {
    const { person_id } = request.params as { person_id: string };
    const reqId = requestId(request);
    const session = getSession(neo4j.session.READ);

    try {
      const res = await session.run(GET_PERSON_BY_ID, { person_id });

      if (res.records.length === 0) {
        return reply.status(404).send({
          error: { code: 'person_not_found', message: 'Person not found.' },
          meta: { request_id: reqId },
        });
      }

      const rec = res.records[0];
      const obj = Object.fromEntries(rec.keys.map((k) => [k, rec.get(k)]));
      const person = mapPerson(obj);

      return reply.send({
        data: person,
        meta: { request_id: reqId },
      } satisfies ApiResponse<Person>);
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // GET /v1/persons/:person_id/source-records
  // -----------------------------------------------------------------------
  app.get('/v1/persons/:person_id/source-records', async (request: FastifyRequest, reply: FastifyReply) => {
    const { person_id } = request.params as { person_id: string };
    const { cursor, limit: rawLimit } = request.query as Record<string, string | undefined>;
    const limit = Math.min(parseInt(rawLimit ?? '20', 10) || 20, 100);
    const skip = cursor ? parseInt(Buffer.from(cursor, 'base64').toString(), 10) : 0;
    const reqId = requestId(request);
    const session = getSession(neo4j.session.READ);

    try {
      const res = await session.run(GET_PERSON_SOURCE_RECORDS, {
        person_id,
        skip: neo4j.int(skip),
        limit: neo4j.int(limit + 1),
      });

      const hasMore = res.records.length > limit;
      const records = res.records.slice(0, limit).map((r) => {
        const sr = r.get('source_record') as Record<string, unknown>;
        return {
          source_record_pk: String(sr.source_record_pk),
          source_system: String(r.get('source_system')),
          source_record_id: String(sr.source_record_id),
          source_record_version: sr.source_record_version as string | null,
          link_status: String(sr.link_status),
          linked_person_id: String(r.get('linked_person_id')),
          observed_at: toISOStringOrNull(sr.observed_at) ?? '',
          ingested_at: toISOStringOrNull(sr.ingested_at) ?? '',
        };
      });

      const nextCursor = hasMore
        ? Buffer.from(String(skip + limit)).toString('base64')
        : null;

      return reply.send({
        data: records,
        meta: { request_id: reqId, next_cursor: nextCursor },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // GET /v1/persons/:person_id/connections
  // -----------------------------------------------------------------------
  app.get('/v1/persons/:person_id/connections', async (request: FastifyRequest, reply: FastifyReply) => {
    const { person_id } = request.params as { person_id: string };
    const {
      connection_type = 'all',
      identifier_type,
      cursor,
      limit: rawLimit,
    } = request.query as Record<string, string | undefined>;
    const limit = Math.min(parseInt(rawLimit ?? '20', 10) || 20, 100);
    const skip = cursor ? parseInt(Buffer.from(cursor, 'base64').toString(), 10) : 0;
    const reqId = requestId(request);
    const session = getSession(neo4j.session.READ);

    try {
      let query: string;
      switch (connection_type) {
        case 'identifier':
          query = GET_PERSON_CONNECTIONS_IDENTIFIER;
          break;
        case 'address':
          query = GET_PERSON_CONNECTIONS_ADDRESS;
          break;
        default:
          query = GET_PERSON_CONNECTIONS_ALL;
      }

      const res = await session.run(query, {
        person_id,
        identifier_type: identifier_type ?? null,
        skip: neo4j.int(skip),
        limit: neo4j.int(limit + 1),
      });

      const hasMore = res.records.length > limit;
      const connections: PersonConnection[] = res.records.slice(0, limit).map((r) => ({
        person_id: String(r.get('person_id')),
        status: String(r.get('status')) as PersonConnection['status'],
        preferred_full_name: r.get('preferred_full_name') as string | null,
        hops: toNumber(r.get('hops')),
        shared_identifiers: ((r.get('shared_identifiers') ?? []) as Array<Record<string, string>>).map((si) => ({
          identifier_type: si.identifier_type ?? '',
          normalized_value: si.normalized_value ?? '',
        })),
        shared_addresses: ((r.get('shared_addresses') ?? []) as Array<Record<string, string>>).map((sa) => ({
          address_id: sa.address_id ?? '',
          normalized_full: sa.normalized_full ?? null,
        })),
      }));

      const nextCursor = hasMore
        ? Buffer.from(String(skip + limit)).toString('base64')
        : null;

      return reply.send({
        data: connections,
        meta: { request_id: reqId, next_cursor: nextCursor },
      } satisfies ApiResponse<PersonConnection[]>);
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // GET /v1/persons/:person_id/relationships (post-MVP placeholder)
  // -----------------------------------------------------------------------
  app.get('/v1/persons/:person_id/relationships', async (request: FastifyRequest, reply: FastifyReply) => {
    const reqId = requestId(request);
    return reply.send({
      data: [],
      meta: { request_id: reqId },
    });
  });

  // -----------------------------------------------------------------------
  // GET /v1/persons/:person_id/audit
  // -----------------------------------------------------------------------
  app.get('/v1/persons/:person_id/audit', async (request: FastifyRequest, reply: FastifyReply) => {
    const { person_id } = request.params as { person_id: string };
    const { cursor, limit: rawLimit } = request.query as Record<string, string | undefined>;
    const limit = Math.min(parseInt(rawLimit ?? '20', 10) || 20, 100);
    const skip = cursor ? parseInt(Buffer.from(cursor, 'base64').toString(), 10) : 0;
    const reqId = requestId(request);
    const session = getSession(neo4j.session.READ);

    try {
      const res = await session.run(GET_PERSON_AUDIT, {
        person_id,
        skip: neo4j.int(skip),
        limit: neo4j.int(limit + 1),
      });

      const hasMore = res.records.length > limit;
      const events = res.records.slice(0, limit).map((r) => {
        const me = r.get('merge_event') as Record<string, unknown>;
        return {
          merge_event_id: String(me.merge_event_id),
          event_type: String(me.event_type),
          actor_type: String(me.actor_type),
          actor_id: String(me.actor_id),
          reason: me.reason as string | null,
          metadata: me.metadata ?? {},
          created_at: toISOStringOrNull(me.created_at) ?? '',
          absorbed_person_id: r.get('absorbed_person_id') as string | null,
          survivor_person_id: r.get('survivor_person_id') as string | null,
          triggered_by_decision_id: r.get('triggered_by_decision_id') as string | null,
        };
      });

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

  // -----------------------------------------------------------------------
  // GET /v1/persons/:person_id/matches
  // -----------------------------------------------------------------------
  app.get('/v1/persons/:person_id/matches', async (request: FastifyRequest, reply: FastifyReply) => {
    const { person_id } = request.params as { person_id: string };
    const { cursor, limit: rawLimit } = request.query as Record<string, string | undefined>;
    const limit = Math.min(parseInt(rawLimit ?? '20', 10) || 20, 100);
    const skip = cursor ? parseInt(Buffer.from(cursor, 'base64').toString(), 10) : 0;
    const reqId = requestId(request);
    const session = getSession(neo4j.session.READ);

    try {
      const res = await session.run(GET_PERSON_MATCHES, {
        person_id,
        skip: neo4j.int(skip),
        limit: neo4j.int(limit + 1),
      });

      const hasMore = res.records.length > limit;
      const decisions = res.records.slice(0, limit).map((r) => {
        const md = r.get('match_decision') as Record<string, unknown>;
        return {
          match_decision_id: String(md.match_decision_id),
          engine_type: String(md.engine_type),
          engine_version: String(md.engine_version),
          policy_version: String(md.policy_version),
          decision: String(md.decision),
          confidence: toNumber(md.confidence),
          reasons: md.reasons as string[] ?? [],
          blocking_conflicts: md.blocking_conflicts as string[] ?? [],
          created_at: toISOStringOrNull(md.created_at) ?? '',
          left_person_id: r.get('left_person_id') as string | null,
          right_person_id: r.get('right_person_id') as string | null,
        };
      });

      const nextCursor = hasMore
        ? Buffer.from(String(skip + limit)).toString('base64')
        : null;

      return reply.send({
        data: decisions,
        meta: { request_id: reqId, next_cursor: nextCursor },
      });
    } finally {
      await session.close();
    }
  });
}
