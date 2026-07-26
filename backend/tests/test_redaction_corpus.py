"""Redaction corpus.

The original gap existed because no test asserted real credential shapes — only
keyword-shaped ones. Four of eight shapes leaked (2026-07-26): bare JWTs,
connection strings, cloud keys and PEM blocks. These are exactly what appears in
production pod logs, and they reach reports on disk, the HTTP API, and the model.

Redaction runs at the collection boundary, so anything that escapes here escapes
everywhere.
"""

import pytest

from app.ai.evidence_redactor import EvidenceRedactor

REDACTOR = EvidenceRedactor()

LEAKY = {
    "explicit assignment": "db connect failed password=hunter2 retrying",
    "bearer header": "GET /v1 401 Authorization: Bearer abc.def.ghi",
    "bare JWT": "auth ok eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVP-mB92K",
    "postgres DSN": "dsn=postgres://admin:S3cr3tPw@db.internal:5432/app",
    "mongodb DSN": "connecting mongodb://root:letmein@mongo:27017/admin",
    "aws access key": "assuming role with AKIAIOSFODNN7EXAMPLE",
    "aws secret": "aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
    "google api key": "maps AIzaSyD-1234567890abcdefghijklmnopqrstuv call failed",
    "github token": "clone failed ghp_1234567890abcdefghijklmnopqrstuvwxyz",
    "slack token": "posting xoxb-EXAMPLE-NOT-A-REAL-TOKEN failed",
    "stripe key": "charge failed sk_live_abcdefghijklmnop1234",
    "private key": (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxGZ0\nvQIDAQAB\n"
        "-----END RSA PRIVATE KEY-----"
    ),
    "token assignment": "启动 token: abcd1234efgh5678",
    "api key assignment": "api_key=sk-proj-abcdef123456",
}

SECRET_FRAGMENTS = [
    "hunter2",
    "abc.def.ghi",
    "dBjftJeZ4CVP",
    "S3cr3tPw",
    "letmein",
    "AKIAIOSFODNN7EXAMPLE",
    "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
    "AIzaSyD-1234567890abcdefghijklmnopqrstuv",
    "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
    "xoxb-EXAMPLE-NOT-A-REAL-TOKEN",
    "sk_live_abcdefghijklmnop1234",
    "MIIEowIBAAKCAQEAxGZ0",
    "abcd1234efgh5678",
    "sk-proj-abcdef123456",
]


@pytest.mark.parametrize("name,text", LEAKY.items(), ids=list(LEAKY))
def test_credential_shapes_are_redacted(name, text):
    result = str(REDACTOR.redact(text))
    assert "[REDACTED]" in result, f"{name} was not redacted at all"


def test_no_secret_fragment_survives_anywhere():
    """Belt and braces: no known secret value appears in any redacted output."""
    joined = " ".join(str(REDACTOR.redact(text)) for text in LEAKY.values())
    leaked = [fragment for fragment in SECRET_FRAGMENTS if fragment in joined]
    assert leaked == [], f"secret fragments survived redaction: {leaked}"


def test_surrounding_context_is_preserved():
    """Redaction must not destroy the diagnostic value of the line."""
    result = str(REDACTOR.redact("dsn=postgres://admin:S3cr3tPw@db.internal:5432/app"))

    assert "postgres://admin:" in result
    assert "@db.internal:5432/app" in result
    assert "S3cr3tPw" not in result


def test_redaction_is_idempotent():
    once = str(REDACTOR.redact("password=hunter2"))
    twice = str(REDACTOR.redact(once))
    assert once == twice


def test_ordinary_log_lines_are_untouched():
    """False positives destroy evidence, so benign text must survive intact."""
    benign = [
        "Started container web in 1.2s",
        "GET /healthz 200 4ms",
        "Reconciling deployment prod/web generation=7",
        "connection to 10.0.0.5:5432 established",
        "image registry.example.com/web:1.4.2 pulled",
    ]
    for line in benign:
        assert str(REDACTOR.redact(line)) == line, f"false positive on: {line}"


def test_nested_structures_are_covered():
    payload = {
        "logs": [{"line": "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SflKxwRJSMeKKF2Q"}],
        "env": {"DATABASE_URL": "mysql://root:toor@db:3306/app"},
    }
    result = str(REDACTOR.redact(payload))

    assert "SflKxwRJSMeKKF2Q" not in result
    assert "toor" not in result


def test_secret_data_keys_are_still_redacted_by_name():
    """The pre-existing key-name rule must keep working alongside shape rules.

    The value is masked while the key name survives — which is what you want:
    "this pod reads db-password" is diagnostic, its value is not.
    """
    result = REDACTOR.redact({"data": {"db-password": "aHVudGVyMg==", "port": "5432"}})

    assert result["data"]["db-password"] == "[REDACTED]"
    assert result["data"]["port"] == "5432"
