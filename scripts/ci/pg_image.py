"""The ONE Postgres image every test/CI Postgres runs (LC-9 TEST-PG-PARITY-18).

Production runs PostgreSQL 18.3 (operator decision "POSTGRES 18 EVERYWHERE",
2026-10-28), so every throwaway Postgres used by the test suites, the
``test-rls-local`` make target, the neuter harness and the CI service
container runs the same major.minor. This module is the single definition:

* Python spawn sites import :data:`PG_TEST_IMAGE`.
* The Makefile reads it with ``sed`` (see ``PG_TEST_IMAGE`` there).
* ``.github/workflows/ci.yml`` cannot import anything (``services.*.image``
  has no ``env`` context), so it carries a literal that
  ``tests/test_pg_image_pin.py`` asserts equals this constant.

The line below must stay a plain ``PG_TEST_IMAGE = "<image>"`` assignment: the
Makefile parses it textually.
"""

PG_TEST_IMAGE = "postgres:18.3"
