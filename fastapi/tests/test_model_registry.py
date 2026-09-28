"""
Tests del Model Registry (P0-5): certificación e integridad de bundles.

Cubren el contrato que load_inference_artifacts() exige en arranque:
manifest presente, hashes SHA-256 coherentes y obs_shape certificado.
"""
import json
import os

import pytest

from services.model_registry import (
    assert_obs_shape,
    build_manifest,
    load_manifest,
    manifest_path_for,
    validate_bundle,
    write_manifest,
)


def _make_model(tmp_path, name="model.zip", content=b"MODEL-BYTES-v1"):
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


class TestBundleRoundtrip:
    def test_build_write_validate_roundtrip(self, tmp_path):
        model = _make_model(tmp_path)
        manifest = build_manifest(model, obs_shape=(20, 24), vec_norm_file=None, source="unit_test")
        write_manifest(model, manifest)

        loaded = validate_bundle(model)
        assert loaded["model_sha256"] == manifest["model_sha256"]
        assert loaded["vec_norm_file"] is None
        assert loaded["obs_shape"] == [20, 24]
        assert loaded["source"] == "unit_test"

    def test_manifest_lives_next_to_model(self, tmp_path):
        model = _make_model(tmp_path)
        assert manifest_path_for(model) == os.path.splitext(model)[0] + ".manifest.json"

    def test_vecnorm_included_and_verified(self, tmp_path):
        model = _make_model(tmp_path)
        (tmp_path / "stats.pkl").write_bytes(b"RMS-STATS")
        manifest = build_manifest(model, obs_shape=(20, 24), vec_norm_file="stats.pkl", source="unit_test")
        write_manifest(model, manifest)

        loaded = validate_bundle(model)
        assert loaded["vec_norm_file"] == "stats.pkl"
        assert loaded["vec_norm_sha256"] == manifest["vec_norm_sha256"]

    def test_load_manifest_reads_written_file(self, tmp_path):
        model = _make_model(tmp_path)
        manifest = build_manifest(model, obs_shape=(20, 24), vec_norm_file=None, source="unit_test")
        write_manifest(model, manifest)
        assert load_manifest(model)["model_file"] == "model.zip"


class TestValidationFailures:
    def test_missing_manifest_raises(self, tmp_path):
        model = _make_model(tmp_path)
        with pytest.raises(RuntimeError, match="Manifest ausente"):
            validate_bundle(model)

    def test_corrupt_manifest_raises(self, tmp_path):
        model = _make_model(tmp_path)
        with open(manifest_path_for(model), "w") as f:
            f.write("{not json")
        with pytest.raises(RuntimeError, match="Manifest corrupto"):
            validate_bundle(model)

    def test_incomplete_manifest_raises(self, tmp_path):
        model = _make_model(tmp_path)
        with open(manifest_path_for(model), "w") as f:
            json.dump({"schema_version": 1}, f)
        with pytest.raises(RuntimeError, match="Manifest incompleto"):
            validate_bundle(model)

    def test_tampered_model_raises(self, tmp_path):
        model = _make_model(tmp_path)
        write_manifest(
            model,
            build_manifest(model, obs_shape=(20, 24), vec_norm_file=None, source="unit_test"),
        )
        with open(model, "ab") as f:
            f.write(b"X")
        with pytest.raises(RuntimeError, match="Hash del modelo NO coincide"):
            validate_bundle(model)

    def test_tampered_vecnorm_raises(self, tmp_path):
        model = _make_model(tmp_path)
        stats = tmp_path / "stats.pkl"
        stats.write_bytes(b"RMS-STATS")
        write_manifest(
            model,
            build_manifest(model, obs_shape=(20, 24), vec_norm_file="stats.pkl", source="unit_test"),
        )
        stats.write_bytes(b"TAMPERED")
        with pytest.raises(RuntimeError, match="VecNormalize NO coincide"):
            validate_bundle(model)

    def test_missing_vecnorm_file_at_build_raises(self, tmp_path):
        model = _make_model(tmp_path)
        with pytest.raises(RuntimeError, match="Archivo requerido ausente"):
            build_manifest(model, obs_shape=(20, 24), vec_norm_file="no_existe.pkl", source="unit_test")

    def test_missing_model_file_raises(self, tmp_path):
        gone = str(tmp_path / "no_model.zip")
        with pytest.raises(RuntimeError, match="Archivo requerido ausente"):
            build_manifest(gone, obs_shape=(20, 24), vec_norm_file=None, source="unit_test")

    def test_source_required(self, tmp_path):
        model = _make_model(tmp_path)
        with pytest.raises(RuntimeError, match="source"):
            build_manifest(model, obs_shape=(20, 24), vec_norm_file=None, source="")


class TestObsShapeContract:
    def test_matching_shape_ok(self):
        assert_obs_shape({"obs_shape": [20, 24]}, (20, 24))

    def test_mismatch_raises(self):
        with pytest.raises(RuntimeError, match="obs_shape incoherente"):
            assert_obs_shape({"obs_shape": [20, 24]}, (10, 21))

    def test_none_shape_raises(self):
        with pytest.raises(RuntimeError, match="sin observation_space"):
            assert_obs_shape({"obs_shape": [20, 24]}, None)
