"""Infrastructure package: pluggable document-processing backends.

Ported from the OLD project's ``infrastructure/`` tree and re-expressed in NEW
conventions. The three backends (Celery, Kafka, Temporal) all converge on the
same shared work function (``processing.handlers.run_document_processing``) so a
document is extracted/embedded identically no matter which transport delivered
the request. Every celery/kafka/temporal/confluent/temporalio import stays lazy
(inside functions, or module-level decorators only in the Temporal workflow
module) so importing anything here never opens a broker/server connection.
"""
