import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import neo4j from 'neo4j-driver';
import { getSession } from '../graph/client.js';

function requestId(request: FastifyRequest): string {
  return (request.headers['x-request-id'] as string) ?? crypto.randomUUID();
}

export default async function survivorshipRoutes(app: FastifyInstance): Promise<void> {
  // -----------------------------------------------------------------------
  // POST /v1/persons/:person_id/golden-profile/recompute
  // -----------------------------------------------------------------------
  app.post('/v1/persons/:person_id/golden-profile/recompute', async (request: FastifyRequest, reply: FastifyReply) => {
    const { person_id } = request.params as { person_id: string };
    const reqId = requestId(request);

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        // Verify person exists and is active
        const personCheck = await tx.run(
          `MATCH (p:Person {person_id: $person_id, status: 'active'})
           RETURN p.person_id AS person_id`,
          { person_id }
        );

        if (personCheck.records.length === 0) {
          return { not_found: true };
        }

        // Gather all HAS_FACT relationships with their source trust info.
        // HAS_FACT goes Person -> SourceRecord, with attribute data on the rel.
        const factsResult = await tx.run(
          `MATCH (p:Person {person_id: $person_id})-[f:HAS_FACT]->(sr:SourceRecord)
           MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
           RETURN f.attribute_name AS attribute_name,
                  f.attribute_value AS attribute_value,
                  f.quality_flag AS quality_flag,
                  f.confidence AS confidence,
                  f.observed_at AS observed_at,
                  sr.source_record_pk AS source_record_pk,
                  ss.field_trust[f.attribute_name] AS trust_tier
           ORDER BY attribute_name`,
          { person_id }
        );

        // Check for survivorship overrides
        const overrideResult = await tx.run(
          `MATCH (p:Person {person_id: $person_id})
           RETURN p.survivorship_overrides AS overrides`,
          { person_id }
        );
        const overrides = (overrideResult.records[0]?.get('overrides') as Record<string, Record<string, unknown>>) ?? {};

        // Build golden profile from facts using survivorship logic.
        // Trust tier ordering: tier_1 > tier_2 > tier_3 > tier_4
        // Within same tier: prefer most recent observed_at.
        // Override takes precedence over computed.
        const trustRank: Record<string, number> = {
          tier_1: 1,
          tier_2: 2,
          tier_3: 3,
          tier_4: 4,
        };

        const bestByField: Record<string, {
          value: unknown;
          trustRank: number;
          observedAt: string;
          qualityFlag: string;
        }> = {};

        for (const record of factsResult.records) {
          const attrName = String(record.get('attribute_name'));
          const attrValue = record.get('attribute_value');
          const qualityFlag = String(record.get('quality_flag') ?? 'valid');
          const trustTier = String(record.get('trust_tier') ?? 'tier_4');
          const sourcePk = String(record.get('source_record_pk'));
          const observedAt = String(record.get('observed_at') ?? '');

          // Skip invalid quality flags
          if (qualityFlag === 'invalid_format' || qualityFlag === 'placeholder_value') {
            continue;
          }

          // Check override
          const overrideKey = `preferred_${attrName}`;
          if (overrides[overrideKey] && overrides[overrideKey].source_record_pk === sourcePk) {
            bestByField[attrName] = {
              value: attrValue,
              trustRank: 0, // override always wins
              observedAt,
              qualityFlag,
            };
            continue;
          }

          const rank = trustRank[trustTier] ?? 4;
          const current = bestByField[attrName];

          if (!current || rank < current.trustRank || (rank === current.trustRank && observedAt > current.observedAt)) {
            bestByField[attrName] = { value: attrValue, trustRank: rank, observedAt, qualityFlag };
          }
        }

        // Resolve preferred address: find best address from LIVES_AT rels
        const addressResult = await tx.run(
          `MATCH (p:Person {person_id: $person_id})-[la:LIVES_AT]->(addr:Address)
           WHERE la.is_active = true AND la.quality_flag IN ['valid', 'partial_parse']
           MATCH (sr:SourceRecord {source_record_pk: la.source_record_pk})-[:FROM_SOURCE]->(ss:SourceSystem)
           RETURN addr.address_id AS address_id,
                  la.last_seen_at AS last_seen_at,
                  ss.field_trust['address'] AS trust_tier
           ORDER BY ss.field_trust['address'], la.last_seen_at DESC
           LIMIT 1`,
          { person_id }
        );

        const preferredAddressId = addressResult.records.length > 0
          ? String(addressResult.records[0].get('address_id'))
          : null;

        // Compute completeness score
        const goldenFields = ['full_name', 'phone', 'email', 'dob'];
        const filledCount = goldenFields.filter((f) => bestByField[f]?.value != null).length;
        const addressBonus = preferredAddressId ? 1 : 0;
        const completeness = (filledCount + addressBonus) / (goldenFields.length + 1);

        // Update person node with recomputed golden profile
        await tx.run(
          `MATCH (p:Person {person_id: $person_id})
           SET p.preferred_full_name = $full_name,
               p.preferred_phone = $phone,
               p.preferred_email = $email,
               p.preferred_dob = $dob,
               p.preferred_address_id = $address_id,
               p.profile_completeness_score = $completeness,
               p.golden_profile_computed_at = datetime(),
               p.golden_profile_version = $version,
               p.updated_at = datetime()`,
          {
            person_id,
            full_name: bestByField['full_name']?.value ?? null,
            phone: bestByField['phone']?.value ?? null,
            email: bestByField['email']?.value ?? null,
            dob: bestByField['dob']?.value ?? null,
            address_id: preferredAddressId,
            completeness,
            version: `computed-${new Date().toISOString()}`,
          }
        );

        // Create audit event
        await tx.run(
          `MATCH (p:Person {person_id: $person_id})
           CREATE (me:MergeEvent {
             merge_event_id: randomUUID(),
             event_type: 'survivorship_override',
             actor_type: 'system',
             actor_id: 'golden_profile_recompute',
             reason: 'Golden profile recomputed',
             metadata: {},
             created_at: datetime()
           })
           CREATE (me)-[:SURVIVOR]->(p)`,
          { person_id }
        );

        return { success: true, completeness };
      });

      if ('not_found' in result && result.not_found) {
        return reply.status(404).send({
          error: { code: 'person_not_found', message: 'Person not found or not active.' },
          meta: { request_id: reqId },
        });
      }

      return reply.send({
        data: {
          person_id,
          status: 'recomputed',
          profile_completeness_score: result.completeness,
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });

  // -----------------------------------------------------------------------
  // POST /v1/persons/:person_id/survivorship-overrides
  // -----------------------------------------------------------------------
  app.post('/v1/persons/:person_id/survivorship-overrides', async (request: FastifyRequest, reply: FastifyReply) => {
    const { person_id } = request.params as { person_id: string };
    const body = request.body as {
      attribute_name: string;
      selected_source_record_pk: string;
      reason: string;
    };
    const reqId = requestId(request);

    if (!body.attribute_name || !body.selected_source_record_pk || !body.reason) {
      return reply.status(400).send({
        error: {
          code: 'invalid_request',
          message: 'attribute_name, selected_source_record_pk, and reason are required.',
        },
        meta: { request_id: reqId },
      });
    }

    const session = getSession(neo4j.session.WRITE);
    try {
      const result = await session.executeWrite(async (tx) => {
        // Verify person exists
        const personCheck = await tx.run(
          `MATCH (p:Person {person_id: $person_id, status: 'active'})
           RETURN p.person_id AS person_id, p.survivorship_overrides AS overrides`,
          { person_id }
        );

        if (personCheck.records.length === 0) {
          return { not_found: true };
        }

        // Verify source record exists and is linked to this person
        const srCheck = await tx.run(
          `MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})-[:LINKED_TO]->(p:Person {person_id: $person_id})
           RETURN sr.source_record_pk AS pk`,
          { source_record_pk: body.selected_source_record_pk, person_id }
        );

        if (srCheck.records.length === 0) {
          return { sr_not_found: true };
        }

        // Verify the source record has a HAS_FACT with the named attribute
        const factCheck = await tx.run(
          `MATCH (p:Person {person_id: $person_id})-[f:HAS_FACT {attribute_name: $attribute_name}]->(sr:SourceRecord {source_record_pk: $source_record_pk})
           RETURN f.attribute_value AS value`,
          {
            person_id,
            attribute_name: body.attribute_name.replace('preferred_', ''),
            source_record_pk: body.selected_source_record_pk,
          }
        );

        if (factCheck.records.length === 0) {
          return { fact_not_found: true };
        }

        const selectedValue = factCheck.records[0].get('value');

        // Update survivorship overrides on Person node
        const existingOverrides = (personCheck.records[0].get('overrides') as Record<string, unknown>) ?? {};
        const overrideEntry = {
          source_record_pk: body.selected_source_record_pk,
          reason: body.reason,
          actor_type: 'reviewer',
          actor_id: 'current_user',
          created_at: new Date().toISOString(),
        };

        const updatedOverrides = { ...existingOverrides, [body.attribute_name]: overrideEntry };

        await tx.run(
          `MATCH (p:Person {person_id: $person_id})
           SET p.survivorship_overrides = $overrides,
               p.updated_at = datetime()`,
          { person_id, overrides: updatedOverrides }
        );

        // Update the golden profile field directly
        const fieldName = body.attribute_name.startsWith('preferred_')
          ? body.attribute_name
          : `preferred_${body.attribute_name}`;

        await tx.run(
          `MATCH (p:Person {person_id: $person_id})
           SET p[$field_name] = $value, p.updated_at = datetime()`,
          { person_id, field_name: fieldName, value: selectedValue }
        );

        return {
          attribute_name: body.attribute_name,
          selected_source_record_pk: body.selected_source_record_pk,
          selected_value: selectedValue,
        };
      });

      if ('not_found' in result && result.not_found) {
        return reply.status(404).send({
          error: { code: 'person_not_found', message: 'Person not found or not active.' },
          meta: { request_id: reqId },
        });
      }

      if ('sr_not_found' in result && result.sr_not_found) {
        return reply.status(404).send({
          error: { code: 'not_found', message: 'Source record not found or not linked to this person.' },
          meta: { request_id: reqId },
        });
      }

      if ('fact_not_found' in result && result.fact_not_found) {
        return reply.status(422).send({
          error: {
            code: 'unprocessable_entity',
            message: 'No attribute fact found for the given attribute_name on the selected source record.',
          },
          meta: { request_id: reqId },
        });
      }

      return reply.send({
        data: {
          person_id,
          attribute_name: result.attribute_name,
          selected_source_record_pk: result.selected_source_record_pk,
          status: 'applied',
        },
        meta: { request_id: reqId },
      });
    } finally {
      await session.close();
    }
  });
}
