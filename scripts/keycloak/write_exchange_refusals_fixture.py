"""Turn one Keycloak measurement into the fixture the upgrade guard reads.

    write_exchange_refusals_fixture.py <measurement.json> <container-run.txt> <fixture.json>

Called by ``run-exchange-refusals-measurement.sh`` after the measurement. The
fixture keeps, per measured cause, whose fault it is and the status and
``error`` field Keycloak answered: never a token, a secret or a description.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

IMAGE = "quay.io/keycloak/keycloak:24.0"
COMMENT = (
    "Written by scripts/keycloak/run-exchange-refusals-measurement.sh. Do not "
    "edit by hand: re-run the measurement when the pinned Keycloak changes."
)


def main() -> None:
    measurement, run_record, fixture = (Path(arg) for arg in sys.argv[1:4])
    measured = json.loads(measurement.read_text(encoding="utf-8"))
    record = dict(
        line.split("=", 1)
        for line in run_record.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    image_id = record["image_id"]
    # Every digest docker knows this image by (sha256:... only, no repository).
    digests = sorted(
        {entry.rsplit("@", 1)[1] for entry in record.get("repo_digests", "").split()}
        | {image_id}
    )
    out = {
        "_comment": COMMENT,
        "image": IMAGE,
        "image_id": image_id,
        "image_digests": digests,
        "keycloak_version": measured["keycloak_version"],
        "cases": {
            name: {
                field: case.get(field) for field in ("cause_class", "status", "error")
            }
            for name, case in measured["cases"].items()
        },
    }
    fixture.write_text(
        json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
