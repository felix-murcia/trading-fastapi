"""Conftest para tests/unit/infrastructure: establece env vars requeridas
por pydantic Settings antes de cualquier import de config.

Sin esto, `from config import settings` falla con Field required.
"""

import os

os.environ.setdefault("database_url", "postgresql://test:test@localhost/test")
os.environ.setdefault("internal_token", "test-token")
os.environ.setdefault("hmac_secret", "test-hmac")
os.environ.setdefault("mt5_http_url", "http://localhost:8000")
