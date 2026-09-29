from plugin_core.credentials import BilibiliCredentialManager


def test_bilibili_credentials_are_encrypted_at_rest(tmp_path) -> None:
    manager = BilibiliCredentialManager(tmp_path)
    credential = {
        "sessdata": "session-secret-value",
        "bili_jct": "csrf-secret-value",
        "dedeuserid": "12345",
        "buvid3": "browser-id",
    }

    manager.save(credential)

    encrypted_file = tmp_path / "credential.enc"
    assert encrypted_file.is_file()
    assert b"session-secret-value" not in encrypted_file.read_bytes()
    assert b"csrf-secret-value" not in encrypted_file.read_bytes()
    assert manager.load() == credential
    assert manager.status()["status"] == "logged_in"


def test_credential_manager_reports_missing_login(tmp_path) -> None:
    manager = BilibiliCredentialManager(tmp_path)

    assert manager.load() is None
    assert manager.status()["status"] == "not_configured"
