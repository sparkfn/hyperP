"""Durable bounded continuation state for WhatsAdmin chat extraction.

Two layers are kept separate:

- :class:`WhatsAdminCursor` — the immutable-window continuation position carried
  by the graph checkpoint (subphase, session/chat/message position, durable
  entry references).
- :class:`WhatsAdminBoundedState` — the durable entries the cursor references
  (stored chat pages, prepared extraction output, committed chat versions) and
  the conversion between stored entries and shared chat bundles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from neo4j import ManagedTransaction
from pydantic.types import JsonValue

from src.config import get_settings
from src.connectors.whatsadmin_api.credentials import WHATSADMIN_ENTITIES, WhatsAdminEntity
from src.connectors.whatsadmin_api.models import (
    PreparedChat,
    StoredBundle,
    StoredChatPage,
    StoredParticipant,
    StoredSession,
)
from src.connectors.whatsadmin_api.watermark import (
    BoundedEntryKind,
    bounded_digest,
    bounded_entry_key,
    committed_version_key,
)
from src.connectors.whatsapp.connector import _ChatBundle, _Participant
from src.graph.client import Neo4jClient
from src.graph.incremental_checkpoints import (
    Neo4jCheckpointRedis,
    TransactionBoundCheckpoints,
)

BoundedSubphase = Literal["sessions", "chats", "extract", "commit", "terminal"]

#: Every durable cursor subphase, in continuation order.
SUBPHASES: tuple[BoundedSubphase, ...] = (
    "sessions",
    "chats",
    "extract",
    "commit",
    "terminal",
)

#: Cursor schema version; a checkpoint written by another version is rejected.
CURSOR_VERSION = 1

#: Keys the bounded adapter requires in ``RunScope.source_window``.
_WINDOW_KEYS = frozenset(
    {
        "contract_version",
        "entity_key",
        "window_id",
        "completed_lower_bound",
        "requested_as_of_upper_bound",
    }
)


class WhatsAdminWindowError(ValueError):
    """The requested source window is missing or malformed."""


@dataclass(frozen=True)
class WhatsAdminWindow:
    """The immutable source window one bounded WhatsAdmin run may read."""

    contract_version: str
    entity_key: WhatsAdminEntity
    window_id: str
    completed_lower_bound: str | None
    requested_as_of_upper_bound: str


@dataclass(frozen=True)
class WhatsAdminCursor:
    """The graph checkpoint continuation carried across bounded units."""

    subphase: BoundedSubphase
    contract_version: str
    credential_fingerprint: str
    window_id: str
    lower_bound: str | None
    upper_bound: str
    snapshot_id: str | None
    sessions_cursor: str | None
    session_queue: tuple[StoredSession, ...]
    sessions_processed: int
    chats_cursor: str | None
    bundles_digest: str | None
    bundle_count: int
    bundle_index: int
    prepared_digest: str | None
    chat_id: str | None
    source_version: str | None
    retry_replay_id: str | None
    retry_source_record_id: str | None
    extract_attempts: int
    completed_sessions: tuple[str, ...]

    def current_session(self) -> StoredSession | None:
        """Return the session whose chats this cursor is walking."""
        return self.session_queue[0] if self.session_queue else None

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "cursor_version": CURSOR_VERSION,
            "subphase": self.subphase,
            "contract_version": self.contract_version,
            "credential_fingerprint": self.credential_fingerprint,
            "window_id": self.window_id,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "snapshot_id": self.snapshot_id,
            "sessions_cursor": self.sessions_cursor,
            "session_queue": [entry.model_dump() for entry in self.session_queue],
            "sessions_processed": self.sessions_processed,
            "chats_cursor": self.chats_cursor,
            "bundles_digest": self.bundles_digest,
            "bundle_count": self.bundle_count,
            "bundle_index": self.bundle_index,
            "prepared_digest": self.prepared_digest,
            "chat_id": self.chat_id,
            "source_version": self.source_version,
            "retry_replay_id": self.retry_replay_id,
            "retry_source_record_id": self.retry_source_record_id,
            "extract_attempts": self.extract_attempts,
            "completed_sessions": list(self.completed_sessions),
        }

    @staticmethod
    def from_payload(value: dict[str, JsonValue]) -> WhatsAdminCursor:
        """Parse a durable cursor, rejecting unknown or malformed state."""
        if value.get("cursor_version") != CURSOR_VERSION:
            raise ValueError("bounded WhatsAdmin cursor version is unsupported")
        phase = _required_text(value, "subphase")
        if phase not in SUBPHASES:
            raise ValueError("bounded WhatsAdmin cursor subphase is invalid")
        subphase: BoundedSubphase = phase
        return WhatsAdminCursor(
            subphase=subphase,
            contract_version=_required_text(value, "contract_version"),
            credential_fingerprint=_required_text(value, "credential_fingerprint"),
            window_id=_required_text(value, "window_id"),
            lower_bound=_optional_text(value, "lower_bound"),
            upper_bound=_required_text(value, "upper_bound"),
            snapshot_id=_optional_text(value, "snapshot_id"),
            sessions_cursor=_optional_text(value, "sessions_cursor"),
            session_queue=_session_queue(value.get("session_queue")),
            sessions_processed=_required_count(value, "sessions_processed"),
            chats_cursor=_optional_text(value, "chats_cursor"),
            bundles_digest=_optional_text(value, "bundles_digest"),
            bundle_count=_required_count(value, "bundle_count"),
            bundle_index=_required_count(value, "bundle_index"),
            prepared_digest=_optional_text(value, "prepared_digest"),
            chat_id=_optional_text(value, "chat_id"),
            source_version=_optional_text(value, "source_version"),
            retry_replay_id=_optional_text(value, "retry_replay_id"),
            retry_source_record_id=_optional_text(value, "retry_source_record_id"),
            extract_attempts=_required_count(value, "extract_attempts"),
            completed_sessions=_text_tuple(value.get("completed_sessions"), "completed_sessions"),
        )


def window_from_source_window(source_window: dict[str, JsonValue]) -> WhatsAdminWindow:
    """Read the immutable bounded WhatsAdmin window; never fabricate a field."""
    if set(source_window) != _WINDOW_KEYS:
        raise WhatsAdminWindowError("bounded WhatsAdmin source window has an invalid shape")
    entity_value = _required_text(source_window, "entity_key")
    if entity_value not in WHATSADMIN_ENTITIES:
        raise WhatsAdminWindowError("bounded WhatsAdmin source window entity is invalid")
    entity_key: WhatsAdminEntity = entity_value
    return WhatsAdminWindow(
        contract_version=_required_text(source_window, "contract_version"),
        entity_key=entity_key,
        window_id=_required_text(source_window, "window_id"),
        completed_lower_bound=_optional_text(source_window, "completed_lower_bound"),
        requested_as_of_upper_bound=_required_text(source_window, "requested_as_of_upper_bound"),
    )


def store_bundle(bundle: _ChatBundle) -> StoredBundle:
    """Convert a shared chat bundle into its durable form."""
    if not bundle.source_version:
        raise ValueError("bounded chat bundle requires an immutable source version")
    return StoredBundle(
        chat_id=bundle.chat_id,
        chat_name=bundle.chat_name,
        session_id=bundle.session_id,
        whatsapp_user_id=bundle.whatsapp_user_id,
        tenant=bundle.tenant,
        msg_text=bundle.msg_text,
        observed_at=bundle.observed_at,
        source_version=bundle.source_version,
        participants=[
            StoredParticipant(
                jid=item.jid,
                phone=item.phone,
                name=item.name,
                role=item.role,
            )
            for item in bundle.participants
        ],
        message_endpoints=list(bundle.message_endpoints),
        session_phone=bundle.session_phone,
        source_id_scope=bundle.source_id_scope,
    )


def bundle_from_stored(stored: StoredBundle) -> _ChatBundle:
    """Rebuild the shared chat bundle stored for one chat version."""
    return _ChatBundle(
        chat_id=stored.chat_id,
        chat_name=stored.chat_name,
        session_id=stored.session_id,
        whatsapp_user_id=stored.whatsapp_user_id,
        tenant=stored.tenant,
        msg_text=stored.msg_text,
        observed_at=stored.observed_at,
        participants=[
            _Participant(item.jid, item.phone, item.name, item.role) for item in stored.participants
        ],
        message_endpoints=list(stored.message_endpoints),
        session_phone=stored.session_phone,
        source_id_scope=stored.source_id_scope,
        source_version=stored.source_version,
    )


def retry_source_record_id(stored: StoredBundle) -> str:
    """Return the stable chat-level source-record identity of one stored chat."""
    scoped = (
        f"{stored.source_id_scope}-{stored.chat_id}"
        if stored.source_id_scope is not None
        else stored.chat_id
    )
    return f"whatsapp-chat-{scoped}-person-1"


def prepared_chat(
    stored: StoredBundle,
    envelopes: list[dict[str, JsonValue]],
) -> PreparedChat:
    """Record one chat version's explicit processed extraction outcome."""
    return PreparedChat(
        chat_id=stored.chat_id,
        source_version=stored.source_version,
        outcome="extracted",
        envelopes=envelopes,
    )


def extraction_replay_id(
    entity_key: str,
    session_id: str,
    chat_id: str,
    source_version: str,
) -> str:
    """Return the attempt-independent replay identity of one chat extraction."""
    return "sha256:" + bounded_digest(entity_key, session_id, chat_id, source_version, "extract")


def bundles_digest(session_id: str, cursor: str | None, offset: int) -> str:
    """Return the stable durable-entry identity of one stored chat page."""
    return bounded_digest("bundles", session_id, cursor or "first", str(offset))


def prepared_digest(session_id: str, chat_id: str, source_version: str) -> str:
    """Return the stable durable-entry identity of one prepared extraction."""
    return bounded_digest("prepared", session_id, chat_id, source_version)


def credential_fingerprint(api_key: str) -> str:
    """Return a non-secret identity for one entity credential."""
    return "sha256:" + bounded_digest("whatsadmin-credential", api_key)


def parse_timestamp(value: str, field: str) -> datetime:
    """Parse an ISO timestamp, requiring an explicit offset."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"bounded WhatsAdmin {field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"bounded WhatsAdmin {field} must be timezone-aware")
    return parsed


class WhatsAdminBoundedState:
    """Generation-scoped durable entries referenced by one bounded cursor.

    Reads go through the graph checkpoint store; the commit transaction writes
    the entries and the checkpoint that references them together.
    """

    def __init__(self, store: Neo4jCheckpointRedis) -> None:
        if store.reset_generation is None:
            raise ValueError("bounded state requires a generation-scoped checkpoint store")
        self._store = store

    def bind_transaction(
        self,
        tx: ManagedTransaction,
        *,
        terminal_authorized: bool = False,
    ) -> TransactionBoundCheckpoints:
        """Return the commit-transaction view of this run's checkpoint store."""
        return self._store.bind_transaction(tx, terminal_authorized=terminal_authorized)

    def read_bundles(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        digest: str,
    ) -> StoredChatPage | None:
        raw = self.read_entry("bundles", entity_key, session_id, digest)
        return None if raw is None else StoredChatPage.model_validate_json(raw)

    def read_prepared(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        digest: str,
    ) -> PreparedChat | None:
        raw = self.read_entry("prepared", entity_key, session_id, digest)
        return None if raw is None else PreparedChat.model_validate_json(raw)

    def read_entry(
        self,
        kind: BoundedEntryKind,
        entity_key: WhatsAdminEntity,
        session_id: str,
        digest: str,
    ) -> str | None:
        return self._store.get(bounded_entry_key(kind, entity_key, session_id, digest))

    def committed_version(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        chat_id: str,
    ) -> str | None:
        return self._store.get(committed_version_key(entity_key, session_id, chat_id))

    def stage_entry(
        self,
        kind: BoundedEntryKind,
        entity_key: WhatsAdminEntity,
        session_id: str,
        digest: str,
        payload: str,
    ) -> None:
        """Write one resumable entry and confirm it is durable.

        Staging is a cache, never progress: only the commit transaction advances
        the checkpoint that references the entry, so a lost or repeated staging
        write can neither skip nor duplicate work.
        """
        name = bounded_entry_key(kind, entity_key, session_id, digest)
        self._store.set(name, payload, status="resume")
        if self._store.get(name) != payload:
            raise RuntimeError("bounded WhatsAdmin entry was not durable")

    def close(self) -> None:
        self._store.close()


_shared_client: Neo4jClient | None = None


def bounded_graph_client() -> Neo4jClient:
    """Return the process-lifetime graph client used by the bounded adapter."""
    global _shared_client
    if _shared_client is None:
        _shared_client = Neo4jClient(get_settings())
    return _shared_client


def _required_text(value: dict[str, JsonValue], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"bounded WhatsAdmin cursor requires {key}")
    return item


def _optional_text(value: dict[str, JsonValue], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ValueError(f"bounded WhatsAdmin cursor {key} must be text")
    return item


def _required_count(value: dict[str, JsonValue], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise ValueError(f"bounded WhatsAdmin cursor requires {key}")
    return item


def _text_tuple(value: JsonValue, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"bounded WhatsAdmin cursor {key} must be text")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"bounded WhatsAdmin cursor {key} must be text")
        items.append(item)
    return tuple(items)


def _session_queue(value: JsonValue) -> tuple[StoredSession, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("bounded WhatsAdmin cursor session queue must be a list")
    entries: list[StoredSession] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("bounded WhatsAdmin cursor session queue is invalid")
        entries.append(StoredSession.model_validate(item))
    return tuple(entries)
