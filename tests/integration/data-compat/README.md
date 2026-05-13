# Data-compat validation harness

Operator-facing test loop for validating that a candidate subchart
image can read the data already on a production PVC, **before** the
helm upgrade that swaps the image. Born from the 2026-05-13
chart-hardening incident where Chart-A's `bitnami/* → bitnamilegacy/*`
flip looked clean in CI (fresh PVCs) but broke production on the
first StatefulSet pod roll because the pre-sunset Bitnami images
had been mislabeled (`bitnami/postgresql:17.6.0` actually shipped
PG 18.3; `bitnami/redis:7.2.5` shipped Redis 8.6.2).

The kind smoke test cannot catch this class — every kind run starts
with fresh PVCs, so the data-binary version coupling is invisible.
This harness fills that gap for the operator.

## When to use

Run this **before any `helm upgrade` that changes**:

- `postgresql.image.repository` / `postgresql.image.tag`
- `redis.image.repository` / `redis.image.tag`
- The chart's `Chart.lock` postgresql or redis subchart pin
- (Future) ChromaDB or RabbitMQ image refs

If the harness reports PASS, the candidate is safe to deploy. If it
reports FAIL, the candidate binary cannot read the existing data —
either don't deploy, or plan a data-migration.

ADR-049 evidence rule: any PR that touches a subchart image tag MUST
cite a recent `test-image-compat.sh` PASS in its `## Validation`
section.

## Quick start

```bash
# 1. Capture a fresh snapshot from the live cluster (scales the SS
#    to 0 briefly — ~10-30s downtime per service).
scripts/snapshot-pvc.sh postgres ~/work/audittrace-private/data-snapshots/$(date +%Y-%m-%d)
scripts/snapshot-pvc.sh redis    ~/work/audittrace-private/data-snapshots/$(date +%Y-%m-%d)

# 2. Test a candidate image against the snapshot. Exit 0=PASS, 1=FAIL.
scripts/test-image-compat.sh postgres bitnamilegacy/postgresql:17.6.0-debian-12-r4
scripts/test-image-compat.sh redis    bitnamilegacy/redis:8.0.3-debian-12-r3

# 3. PASS → safe to ship the helm change. FAIL → don't deploy, plan
#    a migration.
```

## Architecture

Three pieces, intentionally simple:

- `docker-compose.data-compat.yml` — defines `postgres-candidate` +
  `redis-candidate` services. Each service runs the underlying binary
  directly (bypassing the Bitnami wrapper) so the verdict markers
  (`Ready to accept connections` / `FATAL: database files are
  incompatible` / `Can't handle RDB format`) appear in the first ~10s
  of stdout.

- `scripts/snapshot-pvc.sh <service> <output-dir>` — scales the
  StatefulSet to 0, mounts the PVC read-only in a busybox probe pod,
  `kubectl cp`s the binary data dir out, scales the StatefulSet back
  to 1. Snapshot lives at `<output-dir>/<service>/`.

- `scripts/test-image-compat.sh <service> <image:tag> [<snapshot-dir>]` —
  copies the snapshot to a scratch dir (DBs write on startup), chowns
  to UID 1001 (Bitnami's pod user), invokes `docker compose run` the
  candidate against the scratch, greps the first 10s of stdout for
  success/failure markers. Exit 0 = PASS, 1 = FAIL, 2 = env problem.

## Why bypass the Bitnami wrapper

Bitnami's container entrypoint wraps the actual binary in a startup
script that retries on transient errors + shells out config
generation. That hides FATAL postgres/redis errors behind 10-30s of
unhelpful wrapper noise and "back to retry" behavior. By invoking the
binary directly with explicit config flags, the verdict surfaces in
the first second of container stdout — clean and grep-able.

The downside: the harness can't validate the Bitnami wrapper itself.
That's fine — we test against the cluster's actual binary, and the
wrapper has been stable across releases. The chart's kind smoke test
exercises the full wrapper end-to-end for fresh-install validation.

## Snapshot semantics

`snapshot-pvc.sh` captures a **binary data dir**, not a logical dump:

- For postgres: the full `/bitnami/postgresql/` mount (including `data/`
  with `PG_VERSION`, `base/`, `global/`, etc.).
- For redis: the full `/bitnami/redis/` mount (including
  `appendonlydir/`).

A binary snapshot is the correct format for testing "does this binary
READ the on-disk state." A `pg_dumpall` SQL dump tests a different
question ("can this binary restore my data into a fresh instance") —
useful but not what we're after here.

Snapshots are operator-private (live under
`~/work/audittrace-private/data-snapshots/<DATE>/`) and **must not**
be committed to git — they contain real data.

## Live evidence (2026-05-13)

The harness reproduces all four known-truth cases from today's
incident-recovery work:

```
service   case   rc   want
postgres  FAIL   1    1     (bitnamilegacy/postgresql:17.6.0 vs PG 18 data)
postgres  PASS   0    0     (localhost:5000/audittrace/postgresql:18.3-bitnami-frozen-apr17 vs PG 18 data)
redis     FAIL   1    1     (bitnamilegacy/redis:8.0.3-debian-12-r3 vs RDB v13)
redis     PASS   0    0     (localhost:5000/audittrace/redis:8.6.2-bitnami-frozen-apr17 vs RDB v13)
```

Had Chart-A's `bitnami → bitnamilegacy` image-flip PR cited this
harness output for the existing production snapshot, the FAIL would
have surfaced offline — no live-cluster CrashLoopBackOff would have
followed.

## Future extensions

- ChromaDB + RabbitMQ services in the compose file (when those PVCs
  acquire data we care about preserving across image swaps).
- Public-registry-hosted frozen images (e.g. `ghcr.io/...`) so the
  kind CI smoke test can also test against prod-matching binaries
  instead of the chart's fresh-install defaults.

## Anchors

- `feedback_test_image_changes_locally_first` — the rule this harness
  implements.
- `project_followup_data_compat_docker_compose` — original backlog
  entry that scoped this work.
- `project_bitnami_systemic_tag_mislabel` — the forensic that made
  the harness load-bearing.
- `project_session_20260513` — incident + recovery timeline.
