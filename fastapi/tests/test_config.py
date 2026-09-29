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

    def test_sl_risk_usd_defaults(self, monkeypatch):
        """sl_risk_usd/sl_mult default = 15.0/15.0/1.5 — defectos del CÓDIGO,
        no del entorno. El .env del contenedor define overrides (SL_MULT=1.2):
        para probar el default hay que quitarlos (monkeypatch los restaura)."""
        for var in ("sl_risk_usd", "SL_RISK_USD",
                    "sl_risk_usd_xauusd", "SL_RISK_USD_XAUUSD",
                    "sl_mult", "SL_MULT"):
            monkeypatch.delenv(var, raising=False)
        os.environ["database_url"] = "postgresql://user:pass@localhost/db"
        os.environ["internal_token"] = "test-token"
        os.environ["hmac_secret"] = "test-hmac"
        import importlib
        import config
        importlib.reload(config)
        from config import Settings
        settings = Settings()
        assert settings.sl_risk_usd == 15.0
        assert settings.sl_risk_usd_xauusd == 15.0
        assert settings.sl_mult == 1.5
