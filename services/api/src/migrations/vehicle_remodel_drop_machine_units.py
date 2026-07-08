"""One-shot migration: drop MachineUnit nodes + constraints (post Vehicle-remodel).

Idempotent. Safe to re-run (count == 0 / no constraints to drop is fine).

Usage:
    uv run python -m src.migrations.vehicle_remodel_drop_machine_units
"""

from __future__ import annotations

import asyncio
import logging

from neo4j import AsyncSession

from src.graph.client import close_driver, get_driver

log = logging.getLogger(__name__)

#: Delete every :MachineUnit node (detaching any surviving relationships).
_DELETE_MACHINE_UNITS = """
MATCH (u:MachineUnit)
DETACH DELETE u
RETURN count(u) AS deleted_count
"""

#: Discover constraints that target the MachineUnit node label.
_PROBE_MACHINE_UNIT_CONSTRAINTS = """
SHOW CONSTRAINTS
YIELD name, entityType, labelsOrTypes
WHERE entityType = 'NODE' AND 'MachineUnit' IN labelsOrTypes
RETURN name AS name
"""


#: Drop a single constraint by name. The name is a Cypher parameter — never
#: string-interpolate an identifier into a Cypher statement; Neo4j requires
#: identifiers to come from parameters for plan caching and to avoid parser
#: injection on untrusted input.
_DROP_CONSTRAINT = """
DROP CONSTRAINT $name IF EXISTS
"""


async def drop_machine_unit_nodes(session: AsyncSession) -> int:
    """Detach-delete every MachineUnit node. Returns the deleted count."""
    result = await session.run(_DELETE_MACHINE_UNITS)
    record = await result.single()
    count = int(record["deleted_count"]) if record else 0
    await result.consume()
    return count


async def drop_machine_unit_constraints(session: AsyncSession) -> list[str]:
    """Probe for MachineUnit constraints and drop each; return the dropped names.

    Loops probe → drop-one → reprobe so a probe that races a concurrent drop
    picks up any remaining constraints. Each ``DROP CONSTRAINT`` is issued
    with ``$name`` as a Cypher parameter (the Neo4j driver requires
    identifiers via parameters for plan caching).
    """
    dropped: list[str] = []
    while True:
        cursor = await session.run(_PROBE_MACHINE_UNIT_CONSTRAINTS)
        names = [record["name"] async for record in cursor]
        await cursor.consume()
        if not names:
            return dropped
        # Drop one at a time, then re-probe. This keeps each DROP isolated
        # so a failure leaves the probe-during-error in a known state, and
        # makes the call pattern predictable (probe + drop per iteration).
        name = names[0]
        await session.run(_DROP_CONSTRAINT, name=name)
        dropped.append(name)


async def run() -> tuple[int, list[str]]:
    """Run the migration in a single write session. Returns (nodes_deleted, constraints_dropped)."""
    async with get_driver().session(default_access_mode="WRITE") as session:
        deleted = await drop_machine_unit_nodes(session)
        dropped = await drop_machine_unit_constraints(session)
    log.info(
        "vehicle_remodel migration: deleted %d MachineUnit nodes; dropped %d constraints %s",
        deleted,
        len(dropped),
        dropped,
    )
    return deleted, dropped


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        nodes, constraints = asyncio.run(run())
    finally:
        asyncio.run(close_driver())
    print(
        f"vehicle_remodel migration: deleted {nodes} MachineUnit nodes; "
        f"dropped {len(constraints)} constraints"
    )


if __name__ == "__main__":
    main()
