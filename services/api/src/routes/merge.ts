import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import neo4j from 'neo4j-driver';
import { getSession } from '../graph/client.js';

function requestId(request: FastifyRequest): string {
  return (request.headers['x-request-id'] as string) ?? crypto.randomUUID();
}

export default async function mergeRoutes(app: FastifyInstance): Promise<void> {
  // -----------------------------------------------------------------------
  // POST /v1/persons/manual-merge
  // -----------------------------------------------------------------------
  app.post('/v1/persons/manual-merge', async (request: FastifyRequest, reply: FastifyReply) => {
    const body = request.body as {
      from_person_id: string;
      to_person_id: string;
      reason: string;
      recompute_golden_profile?: boolean;
    };
    const reqId = requestId(request);

    if (!body.from_person_id || !body.to_person_id || !body.reason) {
      return reply.status(400).send({
        error: { code: 'invalid_request', message: 'from_person_id, to_person_id, and reason are required.' },
        meta: { request_id: reqId },
      });
    }

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        // Check for hard no-match lock
        const lockCheck = await tx.run(
          `MATCH (a:Person {person_id: $left})-[lock:NO_MATCH_LOCK]-(b:Person {person_id: $right})
           WHERE lock.lock_type = 'manual_no_match'
             AND (lock.expires_at IS NULL OR lock.expires_at > datetime())
           RETURN count(lock) > 0 AS is_locked`,
          {
            left: body.from_person_id < body.to_person_id ? body.from_person_id : body.to_person_id,
            right: body.from_person_id < body.to_person_id ? body.to_person_id : body.from_person_id,
          }
        );

        if (lockCheck.records[0]?.get('is_locked')) {
          return { blocked: true };
        }

        // Verify both persons exist and are active
        const personCheck = await tx.run(
          `MATCH (absorbed:Person {person_id: $from_id, status: 'active'})
           MATCH (survivor:Person {person_id: $to_id, status: 'active'})
           RETURN absorbed, survivor`,
          { from_id: body.from_person_id, to_id: body.to_person_id }
        );

        if (personCheck.records.length === 0) {
          return { not_found: true };
        }

        // Execute merge in a single transaction
        const mergeResult = await tx.run(
          `MATCH (absorbed:Person {person_id: $from_id})
           MATCH (survivor:Person {person_id: $to_id})

           // 1. Create merge event
           CREATE (me:MergeEvent {
             merge_event_id: randomUUID(),
             event_type: 'manual_merge',
             actor_type: 'admin',
             actor_id: 'current_user',
             reason: $reason,
             metadata: {},
             created_at: datetime()
           })
           CREATE (me)-[:ABSORBED]->(absorbed)
           CREATE (me)-[:SURVIVOR]->(survivor)

           // 2. Rewire source records
           WITH absorbed, survivor, me
           OPTIONAL MATCH (sr:SourceRecord)-[old_link:LINKED_TO]->(absorbed)
           FOREACH (_ IN CASE WHEN old_link IS NOT NULL THEN [1] ELSE [] END |
             DELETE old_link
             CREATE (sr)-[:LINKED_TO {linked_at: datetime()}]->(survivor)
             CREATE (me)-[:AFFECTED_RECORD]->(sr)
           )

           // 3. Rewire IDENTIFIED_BY relationships
           WITH absorbed, survivor, me
           OPTIONAL MATCH (absorbed)-[old_id:IDENTIFIED_BY]->(id:Identifier)
           FOREACH (_ IN CASE WHEN old_id IS NOT NULL THEN [1] ELSE [] END |
             CREATE (survivor)-[:IDENTIFIED_BY {
               is_verified: old_id.is_verified,
               verification_method: old_id.verification_method,
               is_active: old_id.is_active,
               quality_flag: old_id.quality_flag,
               first_seen_at: old_id.first_seen_at,
               last_seen_at: old_id.last_seen_at,
               last_confirmed_at: old_id.last_confirmed_at,
               source_system_key: old_id.source_system_key,
               source_record_pk: old_id.source_record_pk
             }]->(id)
             DELETE old_id
           )

           // 3b. Rewire LIVES_AT relationships
           WITH absorbed, survivor, me
           OPTIONAL MATCH (absorbed)-[old_addr:LIVES_AT]->(addr:Address)
           FOREACH (_ IN CASE WHEN old_addr IS NOT NULL THEN [1] ELSE [] END |
             CREATE (survivor)-[:LIVES_AT {
               is_active: old_addr.is_active,
               is_verified: old_addr.is_verified,
               source_system_key: old_addr.source_system_key,
               source_record_pk: old_addr.source_record_pk,
               first_seen_at: old_addr.first_seen_at,
               last_seen_at: old_addr.last_seen_at,
               last_confirmed_at: old_addr.last_confirmed_at,
               quality_flag: old_addr.quality_flag
             }]->(addr)
             DELETE old_addr
           )

           // 3c. Rewire HAS_FACT relationships (Person -> SourceRecord)
           WITH absorbed, survivor, me
           OPTIONAL MATCH (absorbed)-[old_fact:HAS_FACT]->(sr_fact:SourceRecord)
           FOREACH (_ IN CASE WHEN old_fact IS NOT NULL THEN [1] ELSE [] END |
             CREATE (survivor)-[:HAS_FACT {
               attribute_name: old_fact.attribute_name,
               attribute_value: old_fact.attribute_value,
               source_trust_tier: old_fact.source_trust_tier,
               confidence: old_fact.confidence,
               quality_flag: old_fact.quality_flag,
               is_current_hint: old_fact.is_current_hint,
               observed_at: old_fact.observed_at,
               created_at: old_fact.created_at
             }]->(sr_fact)
             DELETE old_fact
           )

           // 4. Mark absorbed and create MERGED_INTO
           WITH absorbed, survivor, me
           SET absorbed.status = 'merged', absorbed.updated_at = datetime()
           CREATE (absorbed)-[:MERGED_INTO {
             merge_event_id: me.merge_event_id,
             actor: 'current_user',
             timestamp: datetime()
           }]->(survivor)

           // 5. Path compression: anyone who merged into absorbed now points to survivor
           WITH absorbed, survivor, me
           OPTIONAL MATCH (prev:Person)-[old_merge:MERGED_INTO]->(absorbed)
           FOREACH (_ IN CASE WHEN old_merge IS NOT NULL THEN [1] ELSE [] END |
             CREATE (prev)-[:MERGED_INTO {
               merge_event_id: old_merge.merge_event_id,
               actor: old_merge.actor,
               timestamp: old_merge.timestamp
             }]->(survivor)
             DELETE old_merge
           )

           // 6. Update survivor timestamp
           WITH survivor, me
           SET survivor.updated_at = datetime()

           RETURN me.merge_event_id AS merge_event_id`,
          {
            from_id: body.from_person_id,
            to_id: body.to_person_id,
            reason: body.reason,
          }
        );

        if (mergeResult.records.length === 0) {
          return { not_found: true };
        }

        return {
          merge_event_id: String(mergeResult.records[0].get('merge_event_id')),
        };
      });

      if ('blocked' in result && result.blocked) {
        return reply.status(409).send({
          error: { code: 'merge_blocked', message: 'A no-match lock exists between these persons.' },
          meta: { request_id: reqId },
        });
      }

      if ('not_found' in result && result.not_found) {
        return reply.status(404).send({
          error: { code: 'person_not_found', message: 'One or both persons not found or not active.' },
          meta: { request_id: reqId },
        });
      }

      return reply.status(200).send({
        data: {
          merge_event_id: result.merge_event_id,
          from_person_id: body.from_person_id,
          to_person_id: body.to_person_id,
          status: 'completed',
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // POST /v1/persons/unmerge
  // -----------------------------------------------------------------------
  app.post('/v1/persons/unmerge', async (request: FastifyRequest, reply: FastifyReply) => {
    const body = request.body as {
      merge_event_id: string;
      reason: string;
    };
    const reqId = requestId(request);

    if (!body.merge_event_id || !body.reason) {
      return reply.status(400).send({
        error: { code: 'invalid_request', message: 'merge_event_id and reason are required.' },
        meta: { request_id: reqId },
      });
    }

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        // Verify merge event exists and find absorbed/survivor persons
        const eventCheck = await tx.run(
          `MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
           MATCH (me)-[:ABSORBED]->(absorbed:Person)
           MATCH (me)-[:SURVIVOR]->(survivor:Person)
           WHERE absorbed.status = 'merged'
           RETURN absorbed.person_id AS absorbed_id,
                  survivor.person_id AS survivor_id`,
          { merge_event_id: body.merge_event_id }
        );

        if (eventCheck.records.length === 0) {
          return { not_found: true };
        }

        const absorbedId = String(eventCheck.records[0].get('absorbed_id'));
        const survivorId = String(eventCheck.records[0].get('survivor_id'));

        // Reactivate absorbed person and remove MERGED_INTO
        await tx.run(
          `MATCH (absorbed:Person {person_id: $absorbed_id})-[mi:MERGED_INTO]->(survivor:Person {person_id: $survivor_id})
           DELETE mi
           SET absorbed.status = 'active', absorbed.updated_at = datetime()`,
          { absorbed_id: absorbedId, survivor_id: survivorId }
        );

        // Create unmerge audit event
        await tx.run(
          `MATCH (absorbed:Person {person_id: $absorbed_id})
           MATCH (survivor:Person {person_id: $survivor_id})
           CREATE (ume:MergeEvent {
             merge_event_id: randomUUID(),
             event_type: 'unmerge',
             actor_type: 'admin',
             actor_id: 'current_user',
             reason: $reason,
             metadata: {original_merge_event_id: $original_merge_event_id},
             created_at: datetime()
           })
           CREATE (ume)-[:ABSORBED]->(absorbed)
           CREATE (ume)-[:SURVIVOR]->(survivor)`,
          {
            absorbed_id: absorbedId,
            survivor_id: survivorId,
            reason: body.reason,
            original_merge_event_id: body.merge_event_id,
          }
        );

        // Note: source records stay with the surviving person but are flagged
        // for review (per design doc: "post-merge source records stay with the
        // surviving person but are flagged for review").
        await tx.run(
          `MATCH (me:MergeEvent {merge_event_id: $merge_event_id})-[:AFFECTED_RECORD]->(sr:SourceRecord)
           SET sr.link_status = 'pending_review'`,
          { merge_event_id: body.merge_event_id }
        );

        return {
          absorbed_person_id: absorbedId,
          survivor_person_id: survivorId,
        };
      });

      if ('not_found' in result && result.not_found) {
        return reply.status(404).send({
          error: { code: 'not_found', message: 'Merge event not found or already unmerged.' },
          meta: { request_id: reqId },
        });
      }

      return reply.status(200).send({
        data: {
          merge_event_id: body.merge_event_id,
          absorbed_person_id: result.absorbed_person_id,
          survivor_person_id: result.survivor_person_id,
          status: 'unmerged',
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // POST /v1/locks/person-pair
  // -----------------------------------------------------------------------
  app.post('/v1/locks/person-pair', async (request: FastifyRequest, reply: FastifyReply) => {
    const body = request.body as {
      left_person_id: string;
      right_person_id: string;
      lock_type: string;
      reason: string;
      expires_at?: string | null;
    };
    const reqId = requestId(request);

    if (!body.left_person_id || !body.right_person_id || !body.lock_type || !body.reason) {
      return reply.status(400).send({
        error: { code: 'invalid_request', message: 'left_person_id, right_person_id, lock_type, and reason are required.' },
        meta: { request_id: reqId },
      });
    }

    // Enforce left < right ordering to prevent duplicate locks
    const leftId = body.left_person_id < body.right_person_id ? body.left_person_id : body.right_person_id;
    const rightId = body.left_person_id < body.right_person_id ? body.right_person_id : body.left_person_id;

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        // Check for existing active lock
        const existing = await tx.run(
          `MATCH (a:Person {person_id: $left})-[lock:NO_MATCH_LOCK]-(b:Person {person_id: $right})
           WHERE lock.expires_at IS NULL OR lock.expires_at > datetime()
           RETURN lock.lock_id AS lock_id`,
          { left: leftId, right: rightId }
        );

        if (existing.records.length > 0) {
          return { conflict: true, lock_id: String(existing.records[0].get('lock_id')) };
        }

        const res = await tx.run(
          `MATCH (a:Person {person_id: $left})
           MATCH (b:Person {person_id: $right})
           CREATE (a)-[lock:NO_MATCH_LOCK {
             lock_id: randomUUID(),
             lock_type: $lock_type,
             reason: $reason,
             actor_type: 'admin',
             actor_id: 'current_user',
             expires_at: CASE WHEN $expires_at IS NOT NULL THEN datetime($expires_at) ELSE null END,
             created_at: datetime()
           }]->(b)
           RETURN lock.lock_id AS lock_id`,
          {
            left: leftId,
            right: rightId,
            lock_type: body.lock_type,
            reason: body.reason,
            expires_at: body.expires_at ?? null,
          }
        );

        if (res.records.length === 0) {
          return { not_found: true };
        }

        return { lock_id: String(res.records[0].get('lock_id')) };
      });

      if ('conflict' in result && result.conflict) {
        return reply.status(409).send({
          error: {
            code: 'manual_lock_conflict',
            message: 'An active lock already exists between these persons.',
            details: { existing_lock_id: result.lock_id },
          },
          meta: { request_id: reqId },
        });
      }

      if ('not_found' in result && result.not_found) {
        return reply.status(404).send({
          error: { code: 'person_not_found', message: 'One or both persons not found.' },
          meta: { request_id: reqId },
        });
      }

      return reply.status(201).send({
        data: {
          lock_id: result.lock_id,
          left_person_id: leftId,
          right_person_id: rightId,
          lock_type: body.lock_type,
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // DELETE /v1/locks/:lock_id
  // -----------------------------------------------------------------------
  app.delete('/v1/locks/:lock_id', async (request: FastifyRequest, reply: FastifyReply) => {
    const { lock_id } = request.params as { lock_id: string };
    const reqId = requestId(request);

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        const res = await tx.run(
          `MATCH ()-[lock:NO_MATCH_LOCK {lock_id: $lock_id}]->()
           DELETE lock
           RETURN $lock_id AS deleted_lock_id`,
          { lock_id }
        );
        return res.records.length > 0;
      });

      if (!result) {
        return reply.status(404).send({
          error: { code: 'not_found', message: 'Lock not found.' },
          meta: { request_id: reqId },
        });
      }

      return reply.status(200).send({
        data: { lock_id, status: 'deleted' },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });
}
