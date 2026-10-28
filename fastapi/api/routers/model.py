"""Subrouter: observabilidad del modelo (bajo riesgo).

Endpoints:
  GET /model/status    — bundle en memoria vs disco certificado (in_sync)
  GET /model/drift     — PSI de features + decay de métricas

Los paths públicos NO cambian: prefix="/api/v1/ai" en main.py.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from services.drift_monitor import run_drift_check
from services.model_registry import load_manifest

from .._shared import _BRAINS, _get_registry, _sole_enabled_symbol
from core.pair import default_model_path
from .deps import verify_token

router = APIRouter()


@router.get("/model/status")
async def model_status(symbol: Optional[str] = None, _: None = Depends(verify_token)):
    """P1-2: qué bundle corre EN MEMORIA vs qué certifica EL DISCO (in_sync).

    P3-1b: responde por par (`?symbol=`); con un único par habilitado se
    resuelve solo. Varios (o cero) pares habilitados sin elegir → 400: la
    ambigüedad no se resuelve por heurística.

    `in_sync=false` = hubo una promoción que no se recargó (o un cambio en disco
    no certificado): el operador ve de inmediato disco ≠ memoria.
    """
    sym = symbol or _sole_enabled_symbol()
    if not sym:
        raise HTTPException(
            status_code=400,
            detail="Varios (o cero) pares habilitados en PAIRS: especifique ?symbol=",
        )
    if _get_registry().get(sym) is None:
        # Observabilidad sí, pero no se lee el disco de un par que no existe:
        # 404 con detalle en vez de un 500 sin explicación.
        raise HTTPException(
            status_code=404,
            detail=f"Par {sym!r} no configurado en PAIRS",
        )

    brain = _BRAINS.get(sym)
    memory = dict(brain.bundle) if brain is not None else {}
    disk = load_manifest(default_model_path(sym))  # RuntimeError si no está certificado
    in_sync = (
        memory.get("model_sha256") == disk["model_sha256"]
        and memory.get("vec_norm_sha256") == disk.get("vec_norm_sha256")
    )
    return {
        "symbol": sym,
        "memory": memory,
        "disk": {
            "model_sha256": disk["model_sha256"],
            "vec_norm_sha256": disk.get("vec_norm_sha256"),
            "source": disk["source"],
            "created_at": disk.get("created_at"),
        },
        "in_sync": in_sync,
    }


@router.get("/model/drift")
async def model_drift(_: None = Depends(verify_token)):
    """P3-2c: PSI de features vivas vs baseline del entreno + decay de métricas.

    Devuelve SIEMPRE el reporte (los fallos parciales quedan reflejados y
    alertados en lugar de romper el endpoint de observabilidad).
    """
    return await run_drift_check()
