"""CRM win MVP dataset and baseline contracts (issue #125).

Builds the reproducible point-in-time CRM-only dataset and evaluation
artifacts from the accepted CRM stage release selected by issue #149, entirely
inside the ingestion service that owns Neo4j batch execution and the Celery
runtime used later by the shadow scorer (issue #126).
"""
