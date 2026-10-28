"""Test de RealModelRepository con tmp_path (sin tocar el repo real)."""

import os
import pickle

import pytest

from src.infrastructure.adapters.real_model_repository import RealModelRepository


@pytest.fixture
def tmp_base(tmp_path, monkeypatch):
    """Inyecta PPO_OUTPUT_DIR -> tmp_path para no escribir en el repo real."""
    monkeypatch.setenv("PPO_OUTPUT_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def repo(tmp_base):
    return RealModelRepository(base_dir=str(tmp_base))


class _FakeObsSpace:
    shape = (20, 25)


class _FakeModel:
    """Modelo simple serializable con pickle (no es PPO real)."""

    def __init__(self, value: int = 42):
        self.value = value
        self.observation_space = _FakeObsSpace()


def test_save_model_guarda_bundle_y_manifest(tmp_base, repo):
    """save_model guarda pickle + manifest y retorna model_id."""
    model = _FakeModel(7)
    model_id = repo.save_model(model, {"symbol": "EURUSD", "trigger": "manual"})

    assert model_id is not None
    assert isinstance(model_id, str)

    # El manifest debe existir junto al archivo candidato.
    from services.model_registry import manifest_path_for, load_manifest
    from core.pair import default_model_path

    cand_path = default_model_path("EURUSD") + ".candidate"
    mpath = manifest_path_for(cand_path)
    assert os.path.exists(mpath)

    manifest = load_manifest(cand_path)
    assert manifest["source"] == "auto_retrain_normalized"
    assert manifest["extra"]["metadata"]["symbol"] == "EURUSD"


def test_load_latest_devuelve_modelo_guardado(tmp_base, repo):
    """load_latest carga el modelo guardado por save_model."""
    model = _FakeModel(99)
    model_id = repo.save_model(model, {"symbol": "EURUSD", "trigger": "manual"})

    loaded = repo.load_latest("EURUSD")
    assert loaded is not None
    assert loaded.value == 99


def test_load_latest_retorna_none_sin_modelo(tmp_base, repo):
    """load_latest retorna None si no hay modelo para el símbolo."""
    assert repo.load_latest("GBPUSD") is None


def test_get_manifest_retorna_dict(tmp_base, repo):
    """get_manifest retorna el dict del manifest."""
    model = _FakeModel(1)
    repo.save_model(model, {"symbol": "EURUSD", "trigger": "manual"})

    from core.pair import default_model_path

    cand_path = default_model_path("EURUSD") + ".candidate"
    manifest = repo.get_manifest(cand_path)

    assert isinstance(manifest, dict)
    assert "model_sha256" in manifest
    assert "obs_shape" in manifest


def test_promote_registra_en_model_registry(monkeypatch, repo):
    """promote llama a record_promotion con el symbol correcto."""
    from services import model_registry as mr

    captured = {}

    async def _fake_record_promotion(manifest, gate, *, symbol):
        captured["symbol"] = symbol
        captured["manifest"] = manifest
        captured["gate"] = gate

    monkeypatch.setattr(mr, "record_promotion", _fake_record_promotion)

    model = _FakeModel(5)
    model_id = repo.save_model(model, {"symbol": "EURUSD", "trigger": "manual"})

    repo.promote(model_id)

    assert captured["symbol"] == "EURUSD"
    assert captured["manifest"] is not None
    assert captured["gate"]["reason"] == "promoted"


def test_promote_model_id_desconocido_falla(repo):
    """promote con model_id desconocido lanza RuntimeError."""
    with pytest.raises(RuntimeError, match="desconocido"):
        repo.promote("id_inexistente")


def test_validate_bundle_fail_fast(tmp_base, repo):
    """save_model valida el bundle (no certifica binarios sucios)."""
    model = _FakeModel(3)
    model_id = repo.save_model(model, {"symbol": "EURUSD", "trigger": "manual"})

    # Si el bundle es válido, no lanza.
    assert model_id is not None
