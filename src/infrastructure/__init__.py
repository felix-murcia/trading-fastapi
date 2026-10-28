"""Capa de infraestructura: adaptadores reales de los puertos de dominio.

src/infrastructure/ es la ÚNICA capa que puede importar de fastapi,
sqlalchemy, httpx, MetaTrader5, qwen. src/domain/ y src/application/
siguen aislados (CMD2 CLEAN).
"""
