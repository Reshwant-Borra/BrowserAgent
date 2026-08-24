from __future__ import annotations

from agent.logging_utils import redact_dict


def test_redacts_known_secret_keys():
    out = redact_dict({"password": "hunter2", "auth_token": "abc123", "username": "bob"})
    assert out["password"] == "***REDACTED***"
    assert out["auth_token"] == "***REDACTED***"
    assert out["username"] == "bob"


def test_does_not_redact_hashes_or_ids_by_value_shape():
    # A SHA-256 hex digest is exactly the kind of long opaque string that must survive
    # redaction — it's the primary diagnostic signal for no-op/loop detection debugging.
    long_hash = "a" * 64
    out = redact_dict({"pre_state_hash": long_hash, "action_fingerprint": "click:1:{}"})
    assert out["pre_state_hash"] == long_hash
    assert out["action_fingerprint"] == "click:1:{}"


def test_recurses_into_nested_dicts_and_lists():
    out = redact_dict({"params": {"password": "hunter2"}, "items": [{"token": "xyz"}, {"name": "ok"}]})
    assert out["params"]["password"] == "***REDACTED***"
    assert out["items"][0]["token"] == "***REDACTED***"
    assert out["items"][1]["name"] == "ok"
