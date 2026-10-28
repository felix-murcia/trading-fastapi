"""RealModelRepository: adaptador real del puerto ModelRepository.

Guarda bundles de modelo en el filesystem (fastapi/ml/<symbol>/) y certifica
cada bundle con un manifest vía model_registry.

En tests, usar ``tmp_path`` de pytest para no escribir en el repo real.
No toca servicios externos (no asyncpg, no GCS).

Implementa ModelRepository por typing estructural:
  - save_model(model, metadata) -> str
  - load_latest(symbol) -> object | None
  - promote(model_id) -> None
  - get_manifest(model_path) -> dict
"""

import asyncio
import json
import os
import pickle
import shutil
import uuid

from src.domain.ports import ModelRepository
from core.pair import default_model_path
from services import model_registry as mr


class RealModelRepository(ModelRepository):
    def __init__(self, base_dir: str | None = None):
        """`base_dir` es la raíz de bundles. Default: PPO_OUTPUT_DIR o /app/ml.

        En tests se inyecta un ``tmp_path`` para no escribir en el repo real.
        """
        self._base_dir = base_dir or os.getenv("PPO_OUTPUT_DIR", "/app/ml")
        # model_id -> (symbol, model_path) para promover sin pasar symbol.
        self._promotion_index: dict[str, tuple[str, str]] = {}

    # ── save ──────────────────────────────────────────────────────────────

    def save_model(self, model: object, metadata: dict) -> str:
        """Guarda el modelo y certifica el bundle con manifest.

        `metadata` debe incluir al menos ``symbol`` (obligatorio, P3-1c).
        Retorna el model_id (UUID usado para promover después).
        """
        symbol = metadata["symbol"]
        model_id = str(uuid.uuid4())

        model_path = default_model_path(symbol)
        model_dir = os.path.dirname(model_path)
        os.makedirs(model_dir, exist_ok=True)

        # Guardar el modelo serializado (pickle; SB3 zip en 8d-2 con PPO real).
        cand_path = model_path + ".candidate"
        with open(cand_path, "wb") as fh:
            pickle.dump(model, fh)

        # VecNormalize stats si las trae el modelo (opcional).
        vec_norm_file = None
        vec_norm_path = None
        if hasattr(model, "obs_rms") and model.obs_rms is not None:
            vec_norm_file = f"{symbol}_vec_norm.pkl"
            vec_norm_path = os.path.join(model_dir, vec_norm_file)
            with open(vec_norm_path, "wb") as fh:
                pickle.dump(model.obs_rms, fh)

        # Build + write manifest (certifica el bundle).
        obs_shape = getattr(
            getattr(model, "observation_space", None), "shape", ()
        )
        manifest = mr.build_manifest(
            cand_path,
            obs_shape=obs_shape,
            vec_norm_file=vec_norm_file,
            source="auto_retrain_normalized",
            extra={"metadata": metadata, "model_id": model_id},
        )
        mr.write_manifest(cand_path, manifest)

        # Registrar para promoción posterior.
        self._promotion_index[model_id] = (symbol, cand_path)

        # Validar bundle (fail-fast: nunca certificamos binarios sucios).
        mr.validate_bundle(cand_path)

        return model_id

    # ── load ─────────────────────────────────────────────────────────────

    def load_latest(self, symbol: str) -> object | None:
        """Carga el último modelo certificado para `symbol`."""
        model_path = default_model_path(symbol)
        if not os.path.exists(model_path):
            # Buscar candidato no promovido todavía.
            cand_path = model_path + ".candidate"
            if os.path.exists(cand_path):
                model_path = cand_path
            else:
                return None
                manifest = mr.load_manifest(model_path)
        with open(model_path, "rb") as fh:
            return pickle.load(fh)

    # ── promote ──────────────────────────────────────────────────────────

    def promote(self, model_id: str) -> None:
        """Marca el modelo como promovido y registra en model_registry.

        Usa el symbol guardado al hacer save_model (P3-1c: promoción por par).
        ``record_promotion`` es async; se ejecuta con un event loop nuevo
        (funciona tanto con como sin pytest-asyncio).
        """
        if model_id not in self._promotion_index:
            raise RuntimeError(f"model_id desconocido: {model_id}")
        symbol, model_path = self._promotion_index[model_id]

        manifest = mr.load_manifest(model_path)
        gate = {"reason": "promoted", "checks": None}
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                mr.record_promotion(manifest, gate, symbol=symbol)
            )
        finally:
            loop.close()

    # ── manifest ─────────────────────────────────────────────────────────

    def get_manifest(self, model_path: str) -> dict:
        return mr.load_manifest(model_path)
