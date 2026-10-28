"""Tests de unidad: ApproveRetrain caso de uso."""

from src.application.use_cases.approve_retrain import (
    ApproveRetrain,
    ApproveRetrainRequest,
    ApproveRetrainResult,
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


class FakeConfig:
    def __init__(self, values=None):
        self.values = values or {}
    def get(self, key, default=None):
        return self.values.get(key, default)


# ── Caso 1: éxito → approved ──────────────────────────────────────────
def test_approve_retrain_exito_approved():
    repo = FakeTradeRepo(RetrainState(filled_count=10, retrain_in_progress=False, scheduled_due=False))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    config = FakeConfig({})
    uc = ApproveRetrain(repo, trainer, audit, config)
    res = uc.execute(ApproveRetrainRequest(symbol="EURUSD"))
    assert res.status == "approved"
    assert res.filled_count == 10
    assert res.scheduled_due is False
    assert res.min_trades_for_retrain == 5  # default de RetrainConfig
    assert trainer.called is True
    assert repo.saved[1].retrain_in_progress is True
    assert any(m[0] == "retrain_approved" and m[1]["status"] == "approved" for m in audit.messages)


# ── Caso 2: ya en progreso → already_running ──────────────────────────
def test_approve_retrain_ya_en_progreso():
    repo = FakeTradeRepo(RetrainState(filled_count=10, retrain_in_progress=True, scheduled_due=False))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    config = FakeConfig({})
    uc = ApproveRetrain(repo, trainer, audit, config)
    res = uc.execute(ApproveRetrainRequest(symbol="EURUSD"))
    assert res.status == "already_running"
    assert trainer.called is False
    assert repo.saved is None


# ── Caso 3: umbral no alcanzado y sin scheduled_due → no_pending ──────
def test_approve_retrain_no_pending():
    repo = FakeTradeRepo(RetrainState(filled_count=3, retrain_in_progress=False, scheduled_due=False))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    config = FakeConfig({})
    uc = ApproveRetrain(repo, trainer, audit, config)
    res = uc.execute(ApproveRetrainRequest(symbol="EURUSD"))
    assert res.status == "no_pending"
    assert res.filled_count == 3
    assert trainer.called is False


# ── Caso 4: scheduled_due → approved aunque umbral no alcanzado ───────
def test_approve_retrain_scheduled_due_aprueba_sin_umbral():
    repo = FakeTradeRepo(RetrainState(filled_count=3, retrain_in_progress=False, scheduled_due=True))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    config = FakeConfig({})
    uc = ApproveRetrain(repo, trainer, audit, config)
    res = uc.execute(ApproveRetrainRequest(symbol="EURUSD"))
    assert res.status == "approved"
    assert res.filled_count == 3
    assert trainer.called is True
    assert res.scheduled_due is True


# ── Caso 5: símbolo None → usa TRAINING_SYMBOL (con umbral alcanzado) ──
def test_approve_retrain_symbol_none_default():
    repo = FakeTradeRepo(RetrainState(filled_count=10, retrain_in_progress=False, scheduled_due=False))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    config = FakeConfig({})
    uc = ApproveRetrain(repo, trainer, audit, config)
    res = uc.execute(ApproveRetrainRequest())
    assert res.status == "approved"
    assert repo.saved[0] == "EURUSD"
