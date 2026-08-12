"""Unit tests for the IdP-brokering export transform (SPEC #403).

Falsifiable test (a) from the spec: a fixture kcadm IdP JSON carrying a
client secret must come out of the transform with NO plaintext secret
string present anywhere in the output — and a neutered (no-op) transform
must be shown to leak it, proving the positive assertion is not vacuous
(`feedback_vacuous_neuter_test_antipattern`).
"""

from __future__ import annotations

import copy
import json

import pytest

from scripts.deploy import idp_export

# The exact shape kcadm's `GET identity-provider/instances` returns (mirrors
# the JSON `scripts/setup-idp-federation.sh` POSTs on the way in).
FIXTURE_KCADM_IDP: list[dict] = [
    {
        "alias": "google-test",
        "providerId": "oidc",
        "enabled": True,
        "trustEmail": False,
        "storeToken": False,
        "addReadTokenRoleOnCreate": False,
        "firstBrokerLoginFlowAlias": "first broker login",
        "config": {
            "issuer": "https://accounts.google.com",
            "authorizationUrl": "https://accounts.google.com/o/oauth2/v2/auth",
            "tokenUrl": "https://oauth2.googleapis.com/token",
            "userInfoUrl": "https://openidconnect.googleapis.com/v1/userinfo",
            "jwksUrl": "https://www.googleapis.com/oauth2/v3/certs",
            "clientId": "keycloak-google-broker",
            "clientSecret": "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it",
            "defaultScope": "openid email profile",
            "syncMode": "FORCE",
        },
    }
]

FIXTURE_MAPPERS: list[dict] = [
    {
        "name": "email-mapper",
        "identityProviderAlias": "google-test",
        "identityProviderMapper": "oidc-user-attribute-idp-mapper",
        "config": {"syncMode": "FORCE", "claim": "email", "user.attribute": "email"},
    }
]

PLAINTEXT_SECRET = "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it"


# ── externalize_idp_secrets: the core guarantee ────────────────────────────


def test_externalize_idp_secrets_removes_plaintext_secret():
    result = idp_export.externalize_idp_secrets(FIXTURE_KCADM_IDP)
    serialized = json.dumps(result)
    assert PLAINTEXT_SECRET not in serialized
    assert (
        result[0]["config"]["clientSecret"] == "${vault:idp/google-test/client_secret}"
    )


def test_externalize_idp_secrets_neutered_would_leak_plaintext():
    """Falsifiability demo: a neutered transform (identity function, as if
    the substitution step were deleted) leaves the plaintext secret in the
    output. This is exactly the regression `externalize_idp_secrets` exists
    to prevent — proving the assertion above is not vacuous.
    """
    neutered = copy.deepcopy(FIXTURE_KCADM_IDP)  # no substitution applied
    serialized = json.dumps(neutered)
    assert PLAINTEXT_SECRET in serialized


def test_externalize_idp_secrets_does_not_mutate_input():
    original = copy.deepcopy(FIXTURE_KCADM_IDP)
    idp_export.externalize_idp_secrets(FIXTURE_KCADM_IDP)
    assert FIXTURE_KCADM_IDP == original


def test_externalize_idp_secrets_leaves_non_secret_fields_untouched():
    result = idp_export.externalize_idp_secrets(FIXTURE_KCADM_IDP)
    assert result[0]["config"]["issuer"] == "https://accounts.google.com"
    assert result[0]["config"]["clientId"] == "keycloak-google-broker"
    assert result[0]["alias"] == "google-test"
    assert result[0]["providerId"] == "oidc"


def test_externalize_idp_secrets_skips_entries_with_no_secret():
    no_secret = [
        {
            "alias": "okta-noop",
            "providerId": "oidc",
            "config": {"issuer": "https://example.okta.com"},
        }
    ]
    result = idp_export.externalize_idp_secrets(no_secret)
    assert "clientSecret" not in result[0]["config"]


def test_externalize_idp_secrets_unconditionally_replaces_empty_string_secret():
    """The scrub is UNCONDITIONAL on presence, not gated on the source value
    being truthy — an empty-string `clientSecret` (falsy, but still a
    present secret field kcadm could plausibly emit) must still come out
    as the Vault placeholder, not survive untouched. This is exactly the
    class of bug a `if config.get(key):` truthy-check would reintroduce.
    """
    entry = [
        {
            "alias": "empty-secret-idp",
            "providerId": "oidc",
            "config": {"clientId": "x", "clientSecret": ""},
        }
    ]
    result = idp_export.externalize_idp_secrets(entry)
    assert (
        result[0]["config"]["clientSecret"]
        == "${vault:idp/empty-secret-idp/client_secret}"
    )


def test_externalize_idp_secrets_neutered_truthy_gate_would_leak_empty_secret_bypass():
    """Falsifiability demo for the unconditional-scrub guard: a neutered
    transform that gates the replacement on `if config.get(key):` (truthy,
    the pre-fix shape) leaves an empty-string secret field UNREPLACED —
    proving the unconditional-presence assertion above is not vacuous.
    """
    entry = {
        "alias": "empty-secret-idp",
        "config": {"clientId": "x", "clientSecret": ""},
    }
    neutered_config = dict(entry["config"])
    for key in idp_export._SECRET_CONFIG_KEYS:
        if neutered_config.get(key):  # truthy gate — the neutered (buggy) shape
            neutered_config[key] = "${vault:idp/empty-secret-idp/client_secret}"
    assert neutered_config["clientSecret"] == ""  # unreplaced — the leak this guards


def test_externalize_idp_secrets_missing_alias_raises():
    with pytest.raises(ValueError, match="no 'alias'"):
        idp_export.externalize_idp_secrets(
            [{"providerId": "oidc", "config": {"clientSecret": "x"}}]
        )


def test_externalize_idp_secrets_empty_list_returns_empty_list():
    assert idp_export.externalize_idp_secrets([]) == []


# ── assert_no_plaintext_client_secret: defence in depth ────────────────────


def test_assert_no_plaintext_client_secret_passes_on_externalized_input():
    externalized = idp_export.externalize_idp_secrets(FIXTURE_KCADM_IDP)
    idp_export.assert_no_plaintext_client_secret(externalized)  # no raise


def test_assert_no_plaintext_client_secret_rejects_raw_secret():
    with pytest.raises(ValueError, match="non-externalized"):
        idp_export.assert_no_plaintext_client_secret(FIXTURE_KCADM_IDP)


def test_assert_no_plaintext_client_secret_names_the_offending_alias():
    with pytest.raises(ValueError, match="google-test"):
        idp_export.assert_no_plaintext_client_secret(FIXTURE_KCADM_IDP)


def test_assert_no_plaintext_client_secret_ignores_absent_secret():
    idp_export.assert_no_plaintext_client_secret(
        [{"alias": "okta-noop", "config": {}}]
    )  # no raise


# ── build_export_payload: the end-to-end contract ──────────────────────────


def test_build_export_payload_externalizes_and_bundles_mappers():
    payload = idp_export.build_export_payload(FIXTURE_KCADM_IDP, FIXTURE_MAPPERS)
    assert PLAINTEXT_SECRET not in json.dumps(payload)
    assert payload["identityProviders"][0]["config"]["clientSecret"] == (
        "${vault:idp/google-test/client_secret}"
    )
    assert payload["identityProviderMappers"] == FIXTURE_MAPPERS


def test_build_export_payload_defaults_mappers_to_empty_list():
    payload = idp_export.build_export_payload(FIXTURE_KCADM_IDP)
    assert payload["identityProviderMappers"] == []


def test_build_export_payload_missing_alias_raises():
    """build_export_payload propagates externalize_idp_secrets' ValueError
    rather than silently emitting a payload it could not verify.
    """
    with pytest.raises(ValueError, match="no 'alias'"):
        idp_export.build_export_payload(
            [{"providerId": "oidc", "config": {"clientSecret": "x"}}]
        )


# ── _find_json_array_span / patch_realm_document ────────────────────────────
# charts/audittrace/files/realm-audittrace.json embeds Go/Helm template
# directives ({{- range ... }}) elsewhere in the document, so it is not
# valid JSON as a whole and cannot go through json.loads(). This fixture
# reproduces that exact shape (a template directive BEFORE the two target
# fields) to prove the patch touches only its two target spans.
CHART_STYLE_DOCUMENT = """{
  "realm": "audittrace",
  "clients": [
    {
      "clientId": "audittrace-webui",
      "redirectUris": [
{{- range $i, $uri := .Values.keycloak.webui.redirectUris }}{{ if $i }},{{ end }}
        "{{ $uri }}"
{{- end }}
      ]
    }
  ],
  "identityProviders": [],
  "identityProviderMappers": [],
  "users": []
}
"""

PLAIN_JSON_DOCUMENT = json.dumps(
    {
        "realm": "audittrace",
        "identityProviders": [],
        "identityProviderMappers": [],
        "users": [],
    },
    indent=2,
)


def test_find_json_array_span_locates_empty_array():
    start, end = idp_export._find_json_array_span(
        PLAIN_JSON_DOCUMENT, "identityProviders"
    )
    assert PLAIN_JSON_DOCUMENT[start:end] == "[]"


def test_find_json_array_span_locates_populated_array_with_nesting():
    doc = '{"a": [{"nested": ["x", "]", "["]}], "b": 1}'
    start, end = idp_export._find_json_array_span(doc, "a")
    assert doc[start:end] == '[{"nested": ["x", "]", "["]}]'


def test_find_json_array_span_missing_key_raises():
    with pytest.raises(ValueError, match="not found"):
        idp_export._find_json_array_span(PLAIN_JSON_DOCUMENT, "noSuchKey")


def test_find_json_array_span_non_array_value_raises():
    with pytest.raises(ValueError, match="not a JSON array"):
        idp_export._find_json_array_span('{"realm": "audittrace"}', "realm")


def test_find_json_array_span_handles_escaped_quotes_inside_strings():
    """A `]` or `[` inside a string containing an escaped quote (`\\"`)
    must not be miscounted as a real bracket. Exercises the escape-handling
    branch of the bracket-matcher directly."""
    doc = r'{"a": ["contains \" a quote and ] a bracket", "b"], "c": 1}'
    start, end = idp_export._find_json_array_span(doc, "a")
    assert doc[start:end] == r'["contains \" a quote and ] a bracket", "b"]'


def test_find_json_array_span_unterminated_array_raises():
    with pytest.raises(ValueError, match="unterminated"):
        idp_export._find_json_array_span('{"a": ["never closes"', "a")


def test_patch_realm_document_replaces_only_target_fields():
    payload = idp_export.build_export_payload(FIXTURE_KCADM_IDP, FIXTURE_MAPPERS)
    patched = idp_export.patch_realm_document(CHART_STYLE_DOCUMENT, payload)

    # The Helm template directive elsewhere in the document is untouched.
    assert "{{- range $i, $uri := .Values.keycloak.webui.redirectUris }}" in patched
    assert "{{ $uri }}" in patched
    # The target fields now carry the exported IdP.
    assert '"alias": "google-test"' in patched
    assert PLAINTEXT_SECRET not in patched
    assert '"${vault:idp/google-test/client_secret}"' in patched


def test_patch_realm_document_result_is_valid_json_for_the_plain_variant():
    """On the NON-templated document, the patched result must still be
    valid, loadable JSON (the chart variant can't be json.loads()'d at all
    because of the Helm directives, but the plain keycloak/ copy must)."""
    payload = idp_export.build_export_payload(FIXTURE_KCADM_IDP, FIXTURE_MAPPERS)
    patched = idp_export.patch_realm_document(PLAIN_JSON_DOCUMENT, payload)
    parsed = json.loads(patched)
    assert parsed["identityProviders"][0]["alias"] == "google-test"
    assert parsed["identityProviderMappers"] == FIXTURE_MAPPERS
    assert parsed["realm"] == "audittrace"  # untouched sibling field


def test_patch_realm_document_is_idempotent_on_repeat_application():
    payload = idp_export.build_export_payload(FIXTURE_KCADM_IDP, FIXTURE_MAPPERS)
    once = idp_export.patch_realm_document(PLAIN_JSON_DOCUMENT, payload)
    twice = idp_export.patch_realm_document(once, payload)
    assert json.loads(once) == json.loads(twice)


def test_patch_realm_document_ignores_keys_absent_from_payload():
    partial_payload = {"identityProviders": []}
    patched = idp_export.patch_realm_document(PLAIN_JSON_DOCUMENT, partial_payload)
    # identityProviderMappers is untouched because the payload never
    # mentioned it.
    assert '"identityProviderMappers": []' in patched


# ── CLI: _load_json_arg / _write_output / main ──────────────────────────────


def test_load_json_arg_none_returns_none():
    assert idp_export._load_json_arg(None) is None


def test_load_json_arg_reads_file(tmp_path):
    p = tmp_path / "idps.json"
    p.write_text(json.dumps(FIXTURE_KCADM_IDP), encoding="utf-8")
    assert idp_export._load_json_arg(str(p)) == FIXTURE_KCADM_IDP


def test_load_json_arg_reads_stdin(monkeypatch):
    import io

    monkeypatch.setattr(
        idp_export.sys, "stdin", io.StringIO(json.dumps(FIXTURE_KCADM_IDP))
    )
    assert idp_export._load_json_arg("-") == FIXTURE_KCADM_IDP


def test_write_output_to_file(tmp_path):
    dest = tmp_path / "out.json"
    idp_export._write_output({"a": 1}, str(dest))
    assert json.loads(dest.read_text(encoding="utf-8")) == {"a": 1}


def test_write_output_to_file_text_has_no_raw_secret_and_carries_placeholder(
    tmp_path,
):
    """The regression this guards: CodeQL flagged `fh.write(text)` in
    `_write_output` as `py/clear-text-storage-sensitive-data` (#403 PR).
    Asserts the actual WRITTEN FILE TEXT — not just the returned payload
    object — contains no raw secret and does carry the required
    `config.clientSecret` placeholder (a cold `--import-realm` needs the
    field present, just not the raw value).
    """
    dest = tmp_path / "export.json"
    payload = idp_export.build_export_payload(FIXTURE_KCADM_IDP, FIXTURE_MAPPERS)
    idp_export._write_output(payload, str(dest))
    written_text = dest.read_text(encoding="utf-8")
    assert PLAINTEXT_SECRET not in written_text
    assert '"clientSecret": "${vault:idp/google-test/client_secret}"' in written_text


def test_write_output_to_stdout_text_has_no_raw_secret_and_carries_placeholder(
    capsys,
):
    """Same guard as the file-write test above, for the OTHER CodeQL-flagged
    sink: `sys.stdout.write(text)` (`py/clear-text-logging-sensitive-data`,
    #403 PR). Asserts the actual bytes written to stdout, not the payload
    object alone.
    """
    payload = idp_export.build_export_payload(FIXTURE_KCADM_IDP, FIXTURE_MAPPERS)
    idp_export._write_output(payload, "-")
    written_text = capsys.readouterr().out
    assert PLAINTEXT_SECRET not in written_text
    assert '"clientSecret": "${vault:idp/google-test/client_secret}"' in written_text


def test_patch_realm_document_written_text_has_no_raw_secret_and_carries_placeholder():
    """The regression this guards, for the third callsite that produces
    committable text: `patch_realm_document`'s returned string is what
    `scripts/export-idp-federation.sh` ultimately writes back into the two
    committed realm JSON files. Same two assertions as the `_write_output`
    guards above, against the text `patch_realm_document` actually
    returns.
    """
    payload = idp_export.build_export_payload(FIXTURE_KCADM_IDP, FIXTURE_MAPPERS)
    patched_text = idp_export.patch_realm_document(PLAIN_JSON_DOCUMENT, payload)
    assert PLAINTEXT_SECRET not in patched_text
    assert '"clientSecret": "${vault:idp/google-test/client_secret}"' in patched_text


def test_main_end_to_end_file_to_file(tmp_path):
    idps_path = tmp_path / "idps.json"
    mappers_path = tmp_path / "mappers.json"
    out_path = tmp_path / "export.json"
    idps_path.write_text(json.dumps(FIXTURE_KCADM_IDP), encoding="utf-8")
    mappers_path.write_text(json.dumps(FIXTURE_MAPPERS), encoding="utf-8")

    rc = idp_export.main(
        [
            "--identity-providers",
            str(idps_path),
            "--mappers",
            str(mappers_path),
            "--output",
            str(out_path),
        ]
    )

    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert PLAINTEXT_SECRET not in out_path.read_text(encoding="utf-8")
    assert payload["identityProviders"][0]["alias"] == "google-test"
    assert payload["identityProviderMappers"] == FIXTURE_MAPPERS


def test_main_no_mappers_arg_defaults_empty(tmp_path, capsys):
    idps_path = tmp_path / "idps.json"
    idps_path.write_text(json.dumps(FIXTURE_KCADM_IDP), encoding="utf-8")

    rc = idp_export.main(["--identity-providers", str(idps_path)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["identityProviderMappers"] == []


def test_main_missing_alias_returns_error_exit_code(tmp_path):
    idps_path = tmp_path / "bad.json"
    idps_path.write_text(
        json.dumps([{"providerId": "oidc", "config": {"clientSecret": "x"}}]),
        encoding="utf-8",
    )
    rc = idp_export.main(["--identity-providers", str(idps_path)])
    assert rc == 1


def test_main_bad_json_returns_error_exit_code(tmp_path):
    idps_path = tmp_path / "broken.json"
    idps_path.write_text("{not json", encoding="utf-8")
    rc = idp_export.main(["--identity-providers", str(idps_path)])
    assert rc == 1


def test_main_missing_file_returns_error_exit_code(tmp_path):
    rc = idp_export.main(
        ["--identity-providers", str(tmp_path / "does-not-exist.json")]
    )
    assert rc == 1


def test_build_arg_parser_requires_identity_providers():
    parser = idp_export.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


# ── CLI: --patch-into ────────────────────────────────────────────────────────


def test_main_patch_into_patches_both_realm_file_shapes(tmp_path):
    """End-to-end: one plain-JSON target (keycloak/realm-audittrace.json's
    shape) and one Helm-templated target
    (charts/audittrace/files/realm-audittrace.json's shape) both get
    patched in place, in the same invocation, with no plaintext secret in
    either.
    """
    idps_path = tmp_path / "idps.json"
    mappers_path = tmp_path / "mappers.json"
    plain_target = tmp_path / "keycloak-realm.json"
    chart_target = tmp_path / "chart-realm.json"

    idps_path.write_text(json.dumps(FIXTURE_KCADM_IDP), encoding="utf-8")
    mappers_path.write_text(json.dumps(FIXTURE_MAPPERS), encoding="utf-8")
    plain_target.write_text(PLAIN_JSON_DOCUMENT, encoding="utf-8")
    chart_target.write_text(CHART_STYLE_DOCUMENT, encoding="utf-8")

    rc = idp_export.main(
        [
            "--identity-providers",
            str(idps_path),
            "--mappers",
            str(mappers_path),
            "--patch-into",
            str(plain_target),
            str(chart_target),
            "--output",
            str(tmp_path / "export.json"),
        ]
    )

    assert rc == 0
    plain_text = plain_target.read_text(encoding="utf-8")
    chart_text = chart_target.read_text(encoding="utf-8")
    assert PLAINTEXT_SECRET not in plain_text
    assert PLAINTEXT_SECRET not in chart_text
    assert json.loads(plain_text)["identityProviders"][0]["alias"] == "google-test"
    assert "{{- range $i, $uri" in chart_text  # Helm directive survives
    assert '"alias": "google-test"' in chart_text


def test_main_patch_into_missing_target_returns_error_exit_code(tmp_path):
    idps_path = tmp_path / "idps.json"
    idps_path.write_text(json.dumps(FIXTURE_KCADM_IDP), encoding="utf-8")

    rc = idp_export.main(
        [
            "--identity-providers",
            str(idps_path),
            "--patch-into",
            str(tmp_path / "does-not-exist.json"),
        ]
    )
    assert rc == 1


def test_main_patch_into_missing_key_in_target_returns_error_exit_code(tmp_path):
    idps_path = tmp_path / "idps.json"
    bad_target = tmp_path / "bad-realm.json"
    idps_path.write_text(json.dumps(FIXTURE_KCADM_IDP), encoding="utf-8")
    bad_target.write_text('{"realm": "audittrace"}', encoding="utf-8")

    rc = idp_export.main(
        [
            "--identity-providers",
            str(idps_path),
            "--patch-into",
            str(bad_target),
        ]
    )
    assert rc == 1
