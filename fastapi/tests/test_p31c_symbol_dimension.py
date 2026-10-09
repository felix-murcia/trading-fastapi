"""
P3-1c — dimensión `symbol` en la persistencia: cada mundo tiene SU historia.

- retrain_state: claves con namespace por par (`retrain_state.<SYMBOL>.<campo>`)
  y estados en caché independientes — los contadores de un par nunca alimentan
  el disparo de otro.
- record_promotion / start_experiment: `symbol` OBLIGATORIO — ninguna fila de
  modelo o experimento existe sin declarar su mundo.
- backup: namespace local y GCS por par; purgar la raíz legada jamás toca los
  namespaces de otros pares, y restore de un mundo jamás mira los de otro.
- record_trade_filled: el umbral de retrain de un par jamás dispara el
  pipeline de otro (guard explícito, visible en log).
- schema.sql: columnas symbol + DDL de retrain_state (fresh install completo).
"""
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.pair import LEGACY_SYMBOL, default_model_path as core_model_path
from services import auto_retrain as ar
from services import experiment_tracker, model_backup as mb, model_registry as mr
from services.model_registry import build_manifest, manifest_path_for, write_manifest


# ─── retrain_state: claves y caché por par ───────────────────────────────────

class TestStateNamespace:
    def test_row_key_namespaces_by_symbol(self):
        assert ar._retrain_state_row_key("filled_count", "GBPUSD") == \
            "retrain_state.GBPUSD.filled_count"
        # por defecto, el mundo del pipeline (legado)
        assert ar._retrain_state_row_key("filled_count") == "retrain_state.EURUSD.filled_count"
        assert LEGACY_SYMBOL == "EURUSD"

    @pytest.mark.asyncio
    async def test_load_requests_only_own_namespace(self, monkeypatch):
        seen: dict = {}

        class FakePool:
            async def fetch(self, query, params):
                seen["keys"] = list(params)
                return []

        monkeypatch.setattr("db.connection.get_pool", lambda: FakePool())
        spy = AsyncMock(return_value=[object()])
        monkeypatch.setattr(ar, "_load_persisted_outcomes", spy)

        await ar._load_retrain_state("GBPUSD")

        # Solo claves del mundo pedido — jamás mezcla namespaces
        assert seen["keys"] == [
            "retrain_state.GBPUSD.filled_count",
            "retrain_state.GBPUSD.last_retrain_time",
            "retrain_state.GBPUSD.scheduled_due",
            "retrain_state.GBPUSD.schedule_anchor",
        ]
        # El bootstrap de trades históricos también es de ESE par
        spy.assert_awaited_once_with("GBPUSD")

    @pytest.mark.asyncio
    async def test_state_cache_is_independent_per_symbol(self, monkeypatch):
        class FakePool:
            async def fetch(self, query, params):
                out = []
                for key in params:
                    if key == "retrain_state.EURUSD.filled_count":
                        out.append({"key": key, "value": "7"})
                    elif key == "retrain_state.GBPUSD.filled_count":
                        out.append({"key": key, "value": "3"})
                return out

        monkeypatch.setattr("db.connection.get_pool", lambda: FakePool())
        monkeypatch.setattr(ar, "_states", {})

        eu = await ar._ensure_retrain_state("EURUSD")
        gb = await ar._ensure_retrain_state("GBPUSD")

        assert eu is not gb
        assert eu.filled_count == 7
        assert gb.filled_count == 3

        # Mutar un mundo no toca al otro; la caché devuelve el MISMO estado
        eu.filled_count = 99
        assert (await ar._ensure_retrain_state("GBPUSD")).filled_count == 3
        assert ar.get_state("EURUSD").filled_count == 99


# ─── Filas sin mundo declarado = error de contrato ───────────────────────────

class TestSymbolRequired:
    @pytest.mark.asyncio
    async def test_record_promotion_requires_symbol(self):
        with pytest.raises(TypeError):
            await mr.record_promotion({"model_sha256": "x"}, {"reason": "x"})

    @pytest.mark.asyncio
    async def test_start_experiment_requires_symbol(self):
        with pytest.raises(TypeError):
            await experiment_tracker.start_experiment("manual")


# ─── backup: namespace local por par, purga y restore aislados ──────────────

@pytest.fixture
def pair_bundles(tmp_path, monkeypatch):
    """Bundles certificados de EURUSD (raíz legada) y GBPUSD (namespace)."""
    monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(mb.settings, "model_backup_enabled", False)
    for rel in ("", "GBPUSD"):
        d = tmp_path / rel
        d.mkdir(parents=True, exist_ok=True)
        z = d / "ppo_trading_bot_v3.zip"
        z.write_bytes(f"model-{rel or 'EURUSD'}".encode())
        v = d / "ppo_trading_bot_v3_vec_norm.pkl"
        v.write_bytes(b"vec-bytes")
        write_manifest(str(z), build_manifest(
            str(z), obs_shape=(20, 24), vec_norm_file=v.name, source="test",
        ))
    return tmp_path


class TestBackupNamespace:
    def test_backups_dir_legacy_root_vs_namespace(self, pair_bundles):
        assert mb.backups_dir("EURUSD") == str(pair_bundles / "backups")
        assert mb.backups_dir("GBPUSD") == str(pair_bundles / "backups" / "GBPUSD")

    def test_default_model_path_delegates_to_core_pair(self, pair_bundles):
        # convención de rutas ÚNICA: services no redefine la convención
        assert mb.default_model_path("GBPUSD") == core_model_path("GBPUSD") == \
            str(pair_bundles / "GBPUSD" / "ppo_trading_bot_v3.zip")

    @pytest.mark.asyncio
    async def test_backup_stores_in_pair_namespace(self, pair_bundles):
        result = await mb.backup_model(symbol="GBPUSD")

        assert result["symbol"] == "GBPUSD"
        assert result["status"] in ("local_success_gcs_disabled", "success")
        dest = Path(result["local"]["path"])
        assert dest.parent == pair_bundles / "backups" / "GBPUSD"
        meta = json.loads((dest / "backup.json").read_text())
        assert meta["symbol"] == "GBPUSD"

        # EURUSD sigue en la raíz legada
        eu = await mb.backup_model(symbol="EURUSD")
        assert Path(eu["local"]["path"]).parent == pair_bundles / "backups"

    @pytest.mark.asyncio
    async def test_prune_never_deletes_other_pair_namespaces(self, pair_bundles):
        base = pair_bundles / "backups"
        # Namespace de GBPUSD con un backup real dentro
        g = base / "GBPUSD" / "fedcba987654"
        g.mkdir(parents=True)
        (g / "backup.json").write_text("{}")
        # 12 backups reales de EURUSD en la raíz (mtime decreciente)
        now = time.time()
        for i in range(12):
            d = base / f"{i:012x}"
            d.mkdir()
            (d / "backup.json").write_text("{}")
            os.utime(d, (now - i, now - i))

        removed = mb._prune_local("EURUSD", keep=3)

        # Se purgan solo backups reales de ESTE par…
        assert len(removed) == 9
        # …y el namespace de otro par jamás es candidato
        assert str(g) not in removed
        assert g.exists() and (g / "backup.json").exists()
        survivors = [p for p in base.iterdir() if p.is_dir() and p.name != "GBPUSD"]
        assert len(survivors) == 3

    @pytest.mark.asyncio
    async def test_restore_scans_only_own_namespace(self, pair_bundles):
        eu = await mb.backup_model(symbol="EURUSD")
        gb = await mb.backup_model(symbol="GBPUSD")

        # El backup de GBPUSD es MÁS nuevo (la tentación de restaurar mal)
        newer = time.time() + 100
        os.utime(gb["local"]["path"], (newer, newer))

        # Sin bundle activo: restore habilitado
        zip_p = pair_bundles / "ppo_trading_bot_v3.zip"
        for f in (zip_p, Path(manifest_path_for(str(zip_p))),
                  pair_bundles / "ppo_trading_bot_v3_vec_norm.pkl"):
            f.unlink()

        result = await mb.restore_latest_model("EURUSD")

        assert result["status"] == "restored"
        # Restaura el bundle de SU par — jamás bytes de otro mundo
        assert result["source"] == eu["local"]["path"]
        assert zip_p.exists()


# ─── El umbral de un par jamás dispara el pipeline de otro ──────────────────

class TestFilledCountIsolation:
    def _wire(self, monkeypatch, preseed: dict[str, int]):
        monkeypatch.setattr(ar, "_states", {
            sym: ar.RetrainState(filled_count=n) for sym, n in preseed.items()
        })
        monkeypatch.setattr(ar, "_persist_trade_outcome", AsyncMock(return_value=42))
        monkeypatch.setattr(ar, "_save_retrain_state", AsyncMock())
        monkeypatch.setattr(ar, "_spawn_retrain_task", MagicMock())
        monkeypatch.setattr("services.trade_journal.record_trade_closed", AsyncMock())
        monkeypatch.setattr(ar.settings, "retrain_auto_approve", True)
        # P2-3: fuera de cooldown por defecto (los tests de cooldown lo fijan).
        monkeypatch.setattr(ar, "_cooldown_active", AsyncMock(return_value=False))

    async def _fill(self, symbol: str):
        await ar.record_trade_filled(
            symbol=symbol, entry_time=1000.0, exit_time=2000.0,
            pnl=5.0, pnl_pct=0.01, direction="LONG",
            sl_hit=False, tp_hit=True, exit_reason="tp",
            deal_ticket=9000 + hash(symbol) % 1000,
        )

    @pytest.mark.asyncio
    async def test_other_pair_at_threshold_spawns_its_own_retrain(self, monkeypatch):
        # P3-3: el pipeline está parametrizado — el umbral de GBPUSD dispara
        # el retrain de GBPUSD (su symbol viaja al spawn), jamás el de otro.
        self._wire(monkeypatch, {"GBPUSD": ar.RetrainConfig.min_trades_for_retrain - 1})

        await self._fill("GBPUSD")

        # El contador de GBPUSD llegó al umbral…
        assert ar.get_state("GBPUSD").filled_count == ar.RetrainConfig.min_trades_for_retrain
        # …y su mundo es el que dispara su pipeline
        ar._spawn_retrain_task.assert_called_once_with("trade_count", symbol="GBPUSD")

    @pytest.mark.asyncio
    async def test_training_pair_at_threshold_spawns_as_before(self, monkeypatch):
        self._wire(monkeypatch, {ar.TRAINING_SYMBOL: ar.RetrainConfig.min_trades_for_retrain - 1})

        await self._fill(ar.TRAINING_SYMBOL)

        ar._spawn_retrain_task.assert_called_once_with(
            "trade_count", symbol=ar.TRAINING_SYMBOL
        )

    @pytest.mark.asyncio
    async def test_other_pair_threshold_blocked_without_auto_approve(self, monkeypatch):
        # auto_approve=false → el umbral de GBPUSD queda PENDIENTE (sin spawn):
        # la única vía es approve?symbol=GBPUSD (fallo silencioso imposible).
        self._wire(monkeypatch, {"GBPUSD": ar.RetrainConfig.min_trades_for_retrain - 1})
        monkeypatch.setattr(ar.settings, "retrain_auto_approve", False)

        await self._fill("GBPUSD")

        ar._spawn_retrain_task.assert_not_called()


# ─── schema.sql: fresh install refleja la dimensión por par ─────────────────

def test_schema_declares_symbol_dimension():
    schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
    with open(schema_path, encoding="utf-8") as f:
        schema = f.read()
    # Filas de modelo y experimentos declaran su mundo
    assert "model_registry" in schema and "symbol" in schema
    assert "CREATE TABLE IF NOT EXISTS training_experiments" in schema
    exp_block = schema.split("CREATE TABLE IF NOT EXISTS training_experiments")[1]
    assert "symbol" in exp_block.split(");")[0]
    # retrain_state existe en el esquema (antes solo vivía en la BD en vivo)
    assert "CREATE TABLE IF NOT EXISTS retrain_state" in schema
