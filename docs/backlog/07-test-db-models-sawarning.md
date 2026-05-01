---
title: "test(db_models): assert IntegrityError instead of swallowing SAWarning"
labels: ["test", "tech-debt"]
priority: P5
---

## Context

`tests/test_db_models.py::TestSessionRecordCRUD::test_duplicate_id_raises`
emits a SQLAlchemy warning visible in the test output:

```
SAWarning: New instance <SessionRecord at 0x...> with identity key
(<class 'audittrace.db.models.SessionRecord'>, ('dup_id',), None)
conflicts with persistent instance <SessionRecord at 0x...>
```

The test apparently relies on `db_session.flush()` to surface the conflict,
but the conflict happens at the identity-map level (a SQLAlchemy warning),
not the database integrity-constraint level (a `psycopg2.IntegrityError`
wrapped in `sqlalchemy.exc.IntegrityError`).

The result: the test passes but on a softer signal than it should, and the
warning is noise on every test run.

## Fix sketch

```python
import pytest
from sqlalchemy.exc import IntegrityError

def test_duplicate_id_raises(db_session):
    db_session.add(SessionRecord(id="dup_id", ...))
    db_session.commit()
    db_session.add(SessionRecord(id="dup_id", ...))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
```

Commit the first row, then try the second — that exercises the actual
PostgreSQL UNIQUE constraint and surfaces a real IntegrityError.

## Acceptance criteria

- No `SAWarning` in `pytest -W error::SAWarning` output
- Test still passes
- Test now actually verifies the database constraint, not the in-memory
  identity map
