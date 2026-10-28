"""Fake ModelRepository en memoria (satisface ModelRepository por typing estructural).

Guarda modelos en un dict. Para tests de caracterización de RunAutoRetrain.
"""


class FakeModelRepository:
    def __init__(self, should_fail_save: bool = False):
        self._models = {}
        self._manifests = {}
        self._promoted = None
        self.should_fail_save = should_fail_save
        self.save_calls = []

    def save_model(self, model: object, metadata: dict) -> str:
        self.save_calls.append((model, metadata))
        if self.should_fail_save:
            raise RuntimeError("fake save_model error")
        model_id = f"model_{len(self._models)}"
        self._models[model_id] = (model, metadata)
        return model_id

    def load_latest(self, symbol: str) -> object | None:
        if not self._models:
            return None
        # Devuelve el último modelo guardado (cualquier símbolo, para tests)
        return list(self._models.values())[-1][0]

    def promote(self, model_id: str) -> None:
        self._promoted = model_id

    def get_manifest(self, model_path: str) -> dict:
        return self._manifests.get(model_path, {})
