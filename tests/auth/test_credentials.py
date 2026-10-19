import json
from unittest.mock import patch

from qobuz_proxy.auth.credentials import load_user_token, save_user_token


class TestUserTokenPersistence:
    def test_save_and_load_user_token(self, tmp_path):
        cache_file = tmp_path / "credentials.json"
        cache_file.write_text(json.dumps({"app_id": "123", "app_secret": "secret"}))
        with patch("qobuz_proxy.auth.credentials.CACHE_FILE", cache_file):
            with patch("qobuz_proxy.auth.credentials.CACHE_DIR", tmp_path):
                save_user_token("999", "tok_abc", "test@example.com")
                result = load_user_token()
        assert result is not None
        assert result["user_id"] == "999"
        assert result["user_auth_token"] == "tok_abc"
        assert result["email"] == "test@example.com"

    def test_load_user_token_missing_file(self, tmp_path):
        cache_file = tmp_path / "nonexistent.json"
        with patch("qobuz_proxy.auth.credentials.CACHE_FILE", cache_file):
            result = load_user_token()
        assert result is None

    def test_load_user_token_no_token_in_file(self, tmp_path):
        cache_file = tmp_path / "credentials.json"
        cache_file.write_text(json.dumps({"app_id": "123", "app_secret": "s"}))
        with patch("qobuz_proxy.auth.credentials.CACHE_FILE", cache_file):
            result = load_user_token()
        assert result is None

    def test_save_preserves_app_credentials(self, tmp_path):
        cache_file = tmp_path / "credentials.json"
        cache_file.write_text(json.dumps({"app_id": "123", "app_secret": "secret"}))
        with patch("qobuz_proxy.auth.credentials.CACHE_FILE", cache_file):
            with patch("qobuz_proxy.auth.credentials.CACHE_DIR", tmp_path):
                save_user_token("999", "tok", "e@x.com")
        data = json.loads(cache_file.read_text())
        assert data["app_id"] == "123"
        assert data["app_secret"] == "secret"
        assert data["user_id"] == "999"
