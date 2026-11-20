"""Test modules that use ONE Postgres server shared with other workers.

CI's ``test`` job sets ``AUDITTRACE_TEST_POSTGRES_URL`` to a single service
Postgres, so under pytest-xdist every worker talks to the same server. Roles are
cluster-global (not per schema or per database): ``tests/test_summariser_bypassrls.py``
drops its roles after every test while other workers still hold grants to them,
and the other modules create roles with a check-then-create. These modules
therefore run in one xdist group (``--dist loadgroup``), i.e. on ONE worker, which
is exactly the serial order that is known to pass. Locally each worker starts its
own container, so the grouping costs only the time those modules take on one
worker.

``tests/test_shared_postgres_group.py`` recomputes this set from the sources, so a
new module that reaches the shared server without being listed here fails the
suite instead of failing CI intermittently.
"""

from __future__ import annotations

XDIST_GROUP = "shared-postgres"

SHARED_POSTGRES_MODULES: frozenset[str] = frozenset(
    {
        "tests/integration/test_lc10a_session_recall.py",
        "tests/test_acl_ownership_rls.py",
        "tests/test_acl_write_path_lapsed_clock.py",
        "tests/test_acl_write_path_rls.py",
        "tests/test_console_acl_routes_rls_postgres.py",
        "tests/test_console_store_rls_postgres.py",
        "tests/test_lc9_migration_034_pg.py",
        "tests/test_lc9_wp2a_pg.py",
        "tests/test_lc9_wp2b_pg.py",
        "tests/test_mcp_rls_isolation.py",
        "tests/test_refusal_ingest_rls_postgres.py",
        "tests/test_rls_isolation.py",
        "tests/test_sqlalchemy_spans.py",
        "tests/test_summariser_bypassrls.py",
    }
)
