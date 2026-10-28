"""Tests de unidad: ForceRetrain caso de uso."""

from src.application.use_cases.force_retrain import (
    ForceRetrain,
    ForceRetrainRequest,
    ForceRetrainResult,
)
from src.domain.rules.retrain_state import RetrainState


class FakeTradeRepo:
    def __init__(self, state=None):
        self.state = state
        self.saved = None
    def load_state(self, symbol):
        return self.state
    def save_state(self, symbol, state):
        self.saved = (symbol, state)


class FakeModelTrainer:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.called = False
    def train(self, dataset, config):
        self.called = True
        if self.should_fail:
            raise RuntimeError("trainer boom")


class FakeAudit:
    def __init__(self):
        self.messages = []
    def log(self, event, details):
        self.messages.append((event, details))


# ── Caso 1: éxito → triggered ─────────────────────────────────────────
def test_force_retrain_exito_triggered():
    repo = FakeTradeRepo(RetrainState(filled_count=7, retrain_in_progress=False))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    uc = ForceRetrain(repo, trainer, audit)
    res = uc.execute(ForceRetrainRequest(symbol="EURUSD"))
    assert res.status == "triggered"
    assert res.filled_count == 7
    assert res.in_progress is True
    assert trainer.called is True
    assert repo.saved[1].retrain_in_progress is True
    assert any(m[0] == "retrain_forced" and m[1]["status"] == "triggered" for m in audit.messages)


# ── Caso 2: ya en progreso → already_running ──────────────────────────
def test_force_retrain_ya_en_progreso():
    repo = FakeTradeRepo(RetrainState(filled_count=3, retrain_in_progress=True))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    uc = ForceRetrain(repo, trainer, audit)
    res = uc.execute(ForceRetrainRequest(symbol="EURUSD"))
    assert res.status == "already_running"
    assert res.filled_count == 3
    assert res.in_progress is True
    assert trainer.called is False
    assert repo.saved is None  # no se guarda nada


# ── Caso 3: símbolo None → usa TRAINING_SYMBOL (EURUSD) ───────────────
def test_force_retrain_symbol_none_default():
    repo = FakeTradeRepo(None)
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    uc = ForceRetrain(repo, trainer, audit)
    res = uc.execute(ForceRetrainRequest())
    assert res.status == "triggered"
    assert repo.saved[0] == "EURUSD"


# ── Caso 4: model trainer falla → error propaga ───────────────────────
def test_force_retrain_trainer_falla_propaga():
    repo = FakeTradeRepo(RetrainState(filled_count=0, retrain_in_progress=False))
    trainer = FakeModelTrainer(should_fail=True)
    audit = FakeAudit()
    uc = ForceRetrain(repo, trainer, audit)
    try:
        uc.execute(ForceRetrainRequest(symbol="GBPUSD"))
        assert False, "debió lanzar"
    except RuntimeError as exc:
        assert str(exc) == "trainer boom"
