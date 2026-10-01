import json

from cryptography.fernet import Fernet

from app.platform.reset import TABLES, cleanup_statements, expected_snapshot, save_backup


def test_cleanup_keeps_platform_auth_and_removes_all_tenant_references():
    cipher = Fernet(Fernet.generate_key())
    snapshot = {name: [] for name in TABLES}
    platform = {
        "id": "platform",
        "email": "platform@example.com",
        "platform_role": "super_admin",
        "password_hash": "preserve",
        "mfa_secret": "preserve",
    }
    snapshot["users"] = [
        platform,
        {"id": "tenant", "email": "tenant@example.com", "platform_role": "none"},
    ]
    snapshot["organizations"] = [{"id": "organization"}]
    snapshot["memberships"] = [{"user_id": "platform", "organization_id": "organization"}]
    snapshot["auth_sessions"] = [
        {"id": "ps", "user_id": "platform"},
        {"id": "ts", "user_id": "tenant"},
    ]
    snapshot["auth_refresh_tokens"] = [{"session_id": "ps"}, {"session_id": "ts"}]
    good = {
        "id": "a",
        "user_id": "platform",
        "organization_id": None,
        "session_id": "ps",
        "target_type": None,
        "target_id": None,
    }
    snapshot["auth_audit_events"] = [
        good,
        {**good, "id": "b", "organization_id": "organization"},
        {**good, "id": "c", "target_type": "users", "target_id": "tenant"},
    ]
    snapshot["auth_email_challenges"] = [
        {"user_id": "platform", "email": "platform@example.com"},
        {"user_id": None, "email": "tenant@example.com"},
    ]
    snapshot["auth_mail_outbox"] = [
        {"id": email, "payload": cipher.encrypt(json.dumps({"email": email}).encode()).decode()}
        for email in ("platform@example.com", "tenant@example.com")
    ]
    expected = expected_snapshot(snapshot, cipher)
    assert expected["users"] == [platform]
    assert expected["auth_sessions"] == snapshot["auth_sessions"][:1]
    assert expected["auth_refresh_tokens"] == snapshot["auth_refresh_tokens"][:1]
    assert expected["auth_audit_events"] == [good]
    assert expected["auth_email_challenges"] == snapshot["auth_email_challenges"][:1]
    assert expected["auth_mail_outbox"] == snapshot["auth_mail_outbox"][:1]
    assert expected["organizations"] == expected["memberships"] == []


def test_cleanup_deletes_children_before_users_and_organizations():
    expected = {name: [] for name in TABLES}
    expected["users"] = [{"id": "platform"}]
    queries = cleanup_statements(expected)

    def position(table):
        return next(i for i, query in enumerate(queries) if f'"{table}"' in query)

    assert position("auth_refresh_tokens") < position("auth_sessions") < position("users")
    assert position("sale_lines") < position("sales") < position("organizations")
    assert position("memberships") < position("organizations")
    assert (
        queries[position("users")] == 'DELETE FROM public."users" WHERE "id" NOT IN (\'platform\')'
    )
    assert not any("alembic_version" in query for query in queries)


def test_recovery_snapshot_is_encrypted_and_owner_only(tmp_path):
    cipher = Fernet(Fernet.generate_key())
    raw = '{"password_hash":"private credential data"}'
    save_backup(tmp_path, "native", raw, cipher)
    path = tmp_path / "native.json.fernet"
    assert b"private credential data" not in path.read_bytes()
    assert cipher.decrypt(path.read_bytes()).decode() == raw
    assert path.stat().st_mode & 0o777 == 0o600
