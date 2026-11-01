# The ONE Postgres image every test/CI Postgres runs (LC-9 TEST-PG-PARITY-18).
# Production runs PostgreSQL 18.3 ("POSTGRES 18 EVERYWHERE", 2026-10-28).
# Single definition, read by BOTH sides with no external tool:
#   * the Makefile `include`s this file;
#   * scripts/ci/pg_image.py parses it (Python spawn sites import PG_TEST_IMAGE);
#   * .github/workflows/ci.yml cannot read it (services.*.image has no env
#     context) so it carries a literal that tests/test_pg_image_pin.py asserts
#     equals this value.
# Keep this a plain `PG_TEST_IMAGE = <image>` line: both parsers rely on it.
PG_TEST_IMAGE = postgres:18.3
