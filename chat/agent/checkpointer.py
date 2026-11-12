"""The Postgres checkpointer that keeps each chat session's Agent thread.

One connection pool per process, opened on first use, on the same database as
Django. ``PostgresSaver`` needs autocommit connections that return dict rows.
"""
import threading

from django.conf import settings

_lock = threading.Lock()
_pool = None


def _conninfo(db=None) -> str:
    from psycopg.conninfo import make_conninfo

    db = db or settings.DATABASES["default"]
    return make_conninfo(
        dbname=db.get("NAME") or "",
        user=db.get("USER") or "",
        password=db.get("PASSWORD") or "",
        host=db.get("HOST") or "",
        port=str(db.get("PORT") or ""),
    )


def _get_pool():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool

                _pool = ConnectionPool(
                    _conninfo(),
                    min_size=1,
                    max_size=int(getattr(settings, "AGENT_CHECKPOINT_POOL_SIZE", 5)),
                    kwargs={
                        "autocommit": True,
                        "prepare_threshold": 0,
                        "row_factory": dict_row,
                    },
                    open=True,
                )
    return _pool


def get_checkpointer():
    """A ``PostgresSaver`` over the shared pool."""
    from langgraph.checkpoint.postgres import PostgresSaver

    return PostgresSaver(_get_pool())


def setup_tables(db=None) -> None:
    """Create or upgrade the checkpoint tables in ``db`` (a Django
    ``settings_dict``; the default database when omitted). Run by a chat
    migration."""
    from psycopg import Connection
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres import PostgresSaver

    with Connection.connect(
        _conninfo(db), autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as conn:
        PostgresSaver(conn).setup()
