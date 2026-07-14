"""Source-record lifecycle schema statements required by API writes."""

CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT = """CREATE CONSTRAINT source_record_identity_lock_unique IF NOT EXISTS
FOR (lock:SourceRecordIdentityLock)
REQUIRE (lock.source_system, lock.source_record_id) IS UNIQUE"""
