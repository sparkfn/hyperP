import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import neo4j from 'neo4j-driver';
import { getSession } from '../graph/client.js';
import { LIST_REVIEW_CASES, GET_REVIEW_CASE } from '../graph/queries.js';
import { ApiReviewActionType } from '../types/index.js';

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

function requestId(request: FastifyRequest): string {
  return (request.headers['x-request-id'] as string) ?? crypto.randomUUID();
}

const API_ACTION_TYPES = new Set(Object.values(ApiReviewActionType));

export default async function reviewRoutes(app: FastifyInstance): Promise<void> {
  // -----------------------------------------------------------------------
  // GET /v1/review-cases
  // -----------------------------------------------------------------------
  app.get('/v1/review-cases', async (request: FastifyRequest, reply: FastifyReply) => {
    const {
      queue_state,
      assigned_to,
      priority_lte,
      cursor,
      limit: rawLimit,
    } = request.query as Record<string, string | undefined>;
    const limit = Math.min(parseInt(rawLimit ?? '20', 10) || 20, 100);
    const skip = cursor ? parseInt(Buffer.from(cursor, 'base64').toString(), 10) : 0;
    const reqId = requestId(request);
    const session = getSession(neo4j.session.READ);

    try {
      const res = await session.run(LIST_REVIEW_CASES, {
        queue_state: queue_state ?? null,
        assigned_to: assigned_to ?? null,
        priority_lte: priority_lte ? neo4j.int(parseInt(priority_lte, 10)) : null,
        skip: neo4j.int(skip),
        limit: neo4j.int(limit + 1),
      });

      const hasMore = res.records.length > limit;
      const cases = res.records.slice(0, limit).map((r) => {
        const rc = r.get('review_case') as Record<string, unknown>;
        const md = r.get('match_decision') as Record<string, unknown>;
        return {
          review_case_id: String(rc.review_case_id),
          queue_state: String(rc.queue_state),
          priority: toNumber(rc.priority),
          assigned_to: rc.assigned_to as string | null,
          follow_up_at: toISOStringOrNull(rc.follow_up_at),
          sla_due_at: toISOStringOrNull(rc.sla_due_at),
          match_decision: {
            match_decision_id: String(md.match_decision_id),
            engine_type: String(md.engine_type),
            decision: String(md.decision),
            confidence: toNumber(md.confidence),
          },
        };
      });

      const nextCursor = hasMore
        ? Buffer.from(String(skip + limit)).toString('base64')
        : null;

      return reply.send({
        data: cases,
        meta: { request_id: reqId, next_cursor: nextCursor },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // GET /v1/review-cases/:review_case_id
  // -----------------------------------------------------------------------
  app.get('/v1/review-cases/:review_case_id', async (request: FastifyRequest, reply: FastifyReply) => {
    const { review_case_id } = request.params as { review_case_id: string };
    const reqId = requestId(request);
    const session = getSession(neo4j.session.READ);

    try {
      const res = await session.run(GET_REVIEW_CASE, { review_case_id });

      if (res.records.length === 0) {
        return reply.status(404).send({
          error: { code: 'review_case_not_found', message: 'Review case was not found.' },
          meta: { request_id: reqId },
        });
      }

      const r = res.records[0];
      const rc = r.get('review_case') as Record<string, unknown>;
      const md = r.get('match_decision') as Record<string, unknown>;
      const leftEntity = r.get('left_entity') as Record<string, unknown> | null;
      const leftAddr = r.get('left_address') as Record<string, unknown> | null;
      const rightEntity = r.get('right_entity') as Record<string, unknown> | null;
      const rightAddr = r.get('right_address') as Record<string, unknown> | null;

      return reply.send({
        data: {
          review_case_id: String(rc.review_case_id),
          queue_state: String(rc.queue_state),
          priority: toNumber(rc.priority),
          assigned_to: rc.assigned_to as string | null,
          follow_up_at: toISOStringOrNull(rc.follow_up_at),
          sla_due_at: toISOStringOrNull(rc.sla_due_at),
          resolution: rc.resolution as string | null,
          resolved_at: toISOStringOrNull(rc.resolved_at),
          actions: rc.actions ?? [],
          match_decision: {
            match_decision_id: String(md.match_decision_id),
            engine_type: String(md.engine_type),
            engine_version: String(md.engine_version),
            policy_version: String(md.policy_version),
            decision: String(md.decision),
            confidence: toNumber(md.confidence),
            reasons: md.reasons ?? [],
            blocking_conflicts: md.blocking_conflicts ?? [],
            created_at: toISOStringOrNull(md.created_at) ?? '',
          },
          comparison: {
            left_entity: leftEntity
              ? {
                  ...leftEntity,
                  preferred_address: leftAddr?.address_id ? leftAddr : null,
                }
              : null,
            right_entity: rightEntity
              ? {
                  ...rightEntity,
                  preferred_address: rightAddr?.address_id ? rightAddr : null,
                }
              : null,
          },
          created_at: toISOStringOrNull(rc.created_at) ?? '',
          updated_at: toISOStringOrNull(rc.updated_at) ?? '',
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // POST /v1/review-cases/:review_case_id/assign
  // -----------------------------------------------------------------------
  app.post('/v1/review-cases/:review_case_id/assign', async (request: FastifyRequest, reply: FastifyReply) => {
    const { review_case_id } = request.params as { review_case_id: string };
    const { assigned_to } = request.body as { assigned_to: string };
    const reqId = requestId(request);

    if (!assigned_to) {
      return reply.status(400).send({
        error: { code: 'invalid_request', message: 'assigned_to is required.' },
        meta: { request_id: reqId },
      });
    }

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        const res = await tx.run(
          `MATCH (rc:ReviewCase {review_case_id: $review_case_id})
           WHERE rc.queue_state IN ['open', 'assigned']
           SET rc.assigned_to = $assigned_to,
               rc.queue_state = 'assigned',
               rc.updated_at = datetime(),
               rc.actions = rc.actions + [{
                 action_type: 'assign',
                 actor_type: 'system',
                 actor_id: $assigned_to,
                 notes: null,
                 created_at: toString(datetime())
               }]
           RETURN rc {
             .review_case_id, .queue_state, .assigned_to, .priority,
             .follow_up_at, .sla_due_at, .updated_at
           } AS review_case`,
          { review_case_id, assigned_to }
        );
        return res.records[0] ?? null;
      });

      if (!result) {
        return reply.status(404).send({
          error: { code: 'review_case_not_found', message: 'Review case was not found or is not assignable.' },
          meta: { request_id: reqId },
        });
      }

      const rc = result.get('review_case') as Record<string, unknown>;
      return reply.send({
        data: {
          review_case_id: String(rc.review_case_id),
          queue_state: String(rc.queue_state),
          assigned_to: String(rc.assigned_to),
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // POST /v1/review-cases/:review_case_id/actions
  // -----------------------------------------------------------------------
  app.post('/v1/review-cases/:review_case_id/actions', async (request: FastifyRequest, reply: FastifyReply) => {
    const { review_case_id } = request.params as { review_case_id: string };
    const body = request.body as {
      action_type: string;
      notes?: string;
      metadata?: {
        create_manual_lock?: boolean;
        follow_up_at?: string;
        escalation_reason?: string;
      };
    };
    const reqId = requestId(request);

    if (!body.action_type || !API_ACTION_TYPES.has(body.action_type as ApiReviewActionType)) {
      return reply.status(400).send({
        error: {
          code: 'invalid_request',
          message: `action_type must be one of: ${[...API_ACTION_TYPES].join(', ')}`,
        },
        meta: { request_id: reqId },
      });
    }

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        // Determine new queue_state and resolution based on action_type
        let newQueueState: string;
        let resolution: string | null = null;

        switch (body.action_type) {
          case 'merge':
            newQueueState = 'resolved';
            resolution = 'merge';
            break;
          case 'reject':
            newQueueState = 'resolved';
            resolution = 'reject';
            break;
          case 'manual_no_match':
            newQueueState = 'resolved';
            resolution = 'manual_no_match';
            break;
          case 'defer':
            newQueueState = 'deferred';
            break;
          case 'escalate':
            newQueueState = 'assigned';
            break;
          default:
            newQueueState = 'open';
        }

        const setClauses = [
          'rc.queue_state = $new_queue_state',
          'rc.updated_at = datetime()',
          `rc.actions = rc.actions + [{
            action_type: $action_type,
            actor_type: 'reviewer',
            actor_id: 'current_user',
            notes: $notes,
            created_at: toString(datetime())
          }]`,
        ];

        if (resolution) {
          setClauses.push('rc.resolution = $resolution');
          setClauses.push('rc.resolved_at = datetime()');
        }

        if (body.metadata?.follow_up_at) {
          setClauses.push('rc.follow_up_at = datetime($follow_up_at)');
        }

        const res = await tx.run(
          `MATCH (rc:ReviewCase {review_case_id: $review_case_id})
           WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
           SET ${setClauses.join(', ')}
           RETURN rc {
             .review_case_id, .queue_state, .resolution
           } AS review_case`,
          {
            review_case_id,
            new_queue_state: newQueueState,
            action_type: body.action_type,
            notes: body.notes ?? null,
            resolution,
            follow_up_at: body.metadata?.follow_up_at ?? null,
          }
        );

        if (res.records.length === 0) return null;

        // If manual_no_match, create persistent lock between the two persons
        if (body.action_type === 'manual_no_match') {
          await tx.run(
            `MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
             MATCH (md)-[:ABOUT_LEFT]->(left:Person)
             MATCH (md)-[:ABOUT_RIGHT]->(right:Person)
             WITH left, right,
                  CASE WHEN left.person_id < right.person_id THEN left ELSE right END AS a,
                  CASE WHEN left.person_id < right.person_id THEN right ELSE left END AS b
             CREATE (a)-[:NO_MATCH_LOCK {
               lock_id: randomUUID(),
               lock_type: 'manual_no_match',
               reason: $notes,
               actor_type: 'reviewer',
               actor_id: 'current_user',
               expires_at: null,
               created_at: datetime()
             }]->(b)`,
            { review_case_id, notes: body.notes ?? 'Manual no-match from review' }
          );
        }

        return res.records[0];
      });

      if (!result) {
        return reply.status(404).send({
          error: { code: 'review_case_not_found', message: 'Review case was not found or is not actionable.' },
          meta: { request_id: reqId },
        });
      }

      const rc = result.get('review_case') as Record<string, unknown>;
      return reply.send({
        data: {
          review_case_id: String(rc.review_case_id),
          queue_state: String(rc.queue_state),
          resolution: rc.resolution as string | null,
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });
}
