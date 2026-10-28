"""Configuración para tests de caracterización (legado)."""

import os
import sys
from unittest.mock import MagicMock

# Mock de pandas ANTES de insertar fastapi en el path, para evitar segfault
# de pyarrow al importar módulos que lo usan (veto_chain, trade_setup, etc.)
sys.modules['pandas'] = MagicMock()

# Permitir importar desde fastapi/ (core.rules, services.position_sizing, etc.)
FASTAPI_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fastapi")
if FASTAPI_DIR not in sys.path:
    sys.path.insert(0, FASTAPI_DIR)

# Env vars requeridas por config.py (Pydantic Settings) antes de cualquier import de fastapi
os.environ.setdefault("DATABASE_URL", "postgresql://trading:trading@localhost:5432/trading")
os.environ.setdefault("INTERNAL_TOKEN", "test-token")
os.environ.setdefault("HMAC_SECRET", "test-hmac-secret")
os.environ.setdefault("MT5_HTTP_URL", "http://localhost:8090")
