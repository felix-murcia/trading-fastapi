"""
Tests for config.py Settings validation.
"""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError


class TestSettings:
    """Tests for Settings/BaseConfig validation."""

    def test_database_url_required(self):
        """database_url is mandatory."""
        # Limpia el entorno PARA ESTE TEST y lo restaura al terminar: un
        # clear() sin restaurar deja PPO_OUTPUT_DIR fuera de la sesión y los
        # lectores a tiempo de llamada (core.pair.default_model_path) resuelven
        # /app/ml → rompía la suite en orden completo (P3-1b).
        saved_env = dict(os.environ)
        try:
            os.environ.clear()
            os.environ["database_url"] = "postgresql://user:pass@localhost/db"
            os.environ["internal_token"] = "test-token"
            os.environ["hmac_secret"] = "test-hmac"
            os.environ["mt5_http_url"] = "http://localhost:8000"
            # Re-import to pick up new env
            import importlib
            import config
            importlib.reload(config)
            from config import Settings
            settings = Settings()
            assert settings.database_url == "postgresql://user:pass@localhost/db"
        finally:
            os.environ.clear()
            os.environ.update(saved_env)

    def test_mt5_url_has_default(self):
        """mt5_http_url should default to localhost:8000."""
        os.environ["database_url"] = "postgresql://user:pass@localhost/db"
        os.environ["internal_token"] = "test-token"
        os.environ["hmac_secret"] = "test-hmac"
        import importlib
        import config
        importlib.reload(config)
        from config import Settings
        settings = Settings()
        assert settings.mt5_http_url == "http://localhost:8000"

    def test_news_filter_enabled_defaults_true(self):
        """news_filter_enabled should default to True."""
        os.environ["database_url"] = "postgresql://user:pass@localhost/db"
        os.environ["internal_token"] = "test-token"
        os.environ["hmac_secret"] = "test-hmac"
        import importlib
        import config
        importlib.reload(config)
        from config import Settings
        settings = Settings()
        assert settings.news_filter_enabled is True

    def test_broker_type_defaults_to_mt5_demo(self):
        """broker_type defaults to mt5_demo."""
        os.environ["database_url"] = "postgresql://user:pass@localhost/db"
        os.environ["internal_token"] = "test-token"
        os.environ["hmac_secret"] = "test-hmac"
        import importlib
        import config
        importlib.reload(config)
        from config import Settings
        settings = Settings()
        assert settings.broker_type == "mt5_demo"

    def test_broker_type_read_from_env(self):
        """broker_type is read from BROKER_TYPE env var."""
        os.environ["database_url"] = "postgresql://user:pass@localhost/db"
        os.environ["internal_token"] = "test-token"
        os.environ["hmac_secret"] = "test-hmac"
        os.environ["BROKER_TYPE"] = "mt5_real"
        import importlib
        import config
        importlib.reload(config)
        from config import Settings
        settings = Settings()
        assert settings.broker_type == "mt5_real"

    def test_broker_type_invalid_raises(self):
        """An invalid broker_type raises ValidationError during module reload."""
        saved_env = dict(os.environ)
        try:
            os.environ["database_url"] = "postgresql://user:pass@localhost/db"
            os.environ["internal_token"] = "test-token"
            os.environ["hmac_secret"] = "test-hmac"
            os.environ["BROKER_TYPE"] = "binance"
            import importlib
            import config
            with pytest.raises(ValidationError):
                importlib.reload(config)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)

    def test_broker_http_url_defaults_empty(self):
        """broker_http_url defaults to empty (falls back to mt5_http_url)."""
        saved_env = dict(os.environ)
        try:
            os.environ["database_url"] = "postgresql://user:pass@localhost/db"
            os.environ["internal_token"] = "test-token"
            os.environ["hmac_secret"] = "test-hmac"
            import importlib
            import config
            importlib.reload(config)
            from config import Settings
            settings = Settings()
            assert settings.broker_http_url == ""
        finally:
            os.environ.clear()
            os.environ.update(saved_env)

    def test_broker_http_url_read_from_env(self):
        """broker_http_url is read from BROKER_HTTP_URL env var."""
        saved_env = dict(os.environ)
        try:
            os.environ["database_url"] = "postgresql://user:pass@localhost/db"
            os.environ["internal_token"] = "test-token"
            os.environ["hmac_secret"] = "test-hmac"
            os.environ["BROKER_HTTP_URL"] = "http://100.81.112.95:8000"
            import importlib
            import config
            importlib.reload(config)
            from config import Settings
            settings = Settings()
            assert settings.broker_http_url == "http://100.81.112.95:8000"
        finally:
            os.environ.clear()
            os.environ.update(saved_env)

    def test_broker_timeout_defaults(self):
        """broker_timeout defaults to 10.0."""
        saved_env = dict(os.environ)
        try:
            os.environ["database_url"] = "postgresql://user:pass@localhost/db"
            os.environ["internal_token"] = "test-token"
            os.environ["hmac_secret"] = "test-hmac"
            import importlib
            import config
            importlib.reload(config)
            from config import Settings
            settings = Settings()
            assert settings.broker_timeout == 10.0
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
