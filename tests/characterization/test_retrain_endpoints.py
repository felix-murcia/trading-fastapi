"""Test de caracterización: retrain endpoints (Sesión 9).

Captura el comportamiento legacy de los 3 endpoints POST con lógica de
negocio (force_retrain, promote_candidate, approve_retrain) usando fakes
de los puertos de dominio. No toca PPO real, no toca MT5 real, no red.

Estos casos definen el contrato que los nuevos casos de uso deben cumplir.
"""

from src.application.use_cases.approve_retrain import ApproveRetrain, ApproveRetrainRequest
from src.application.use_cases.force_retrain import ForceRetrain, ForceRetrainRequest
from src.application.use_cases.promote_candidate import PromoteCandidate, PromoteCandidateRequest
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


class FakeModelRepo:
    def __init__(self, manifest=None, should_fail=False):
        self.manifest = manifest
        self.should_fail = should_fail
        self.promoted_id = None
    def get_manifest(self, model_path):
        if self.should_fail:
            raise RuntimeError("repo boom")
        return self.manifest
    def promote(self, model_id):
        self.promoted_id = model_id


# ════════════════════════════════════════════════════════════════════════
# force_retrain — 4 casos
# ════════════════════════════════════════════════════════════════════════

def test_char_force_retrain_exito_triggered():
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


def test_char_force_retrain_ya_en_progreso():
    repo = FakeTradeRepo(RetrainState(filled_count=3, retrain_in_progress=True))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    uc = ForceRetrain(repo, trainer, audit)
    res = uc.execute(ForceRetrainRequest(symbol="EURUSD"))
    assert res.status == "already_running"
    assert res.filled_count == 3
    assert trainer.called is False
    assert repo.saved is None


def test_char_force_retrain_symbol_none_default_eurusd():
    repo = FakeTradeRepo(None)
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    uc = ForceRetrain(repo, trainer, audit)
    res = uc.execute(ForceRetrainRequest())
    assert res.status == "triggered"
    assert repo.saved[0] == "EURUSD"


def test_char_force_retrain_trainer_falla_propaga():
    repo = FakeTradeRepo(RetrainState(filled_count=0, retrain_in_progress=False))
    trainer = FakeModelTrainer(should_fail=True)
    audit = FakeAudit()
    uc = ForceRetrain(repo, trainer, audit)
    try:
        uc.execute(ForceRetrainRequest(symbol="GBPUSD"))
        assert False, "debió lanzar"
    except RuntimeError:
        pass


# ═════════════════════════════════════════════════════════════════════════
# approve_retrain — 4 casos
# ═════════════════════════════════════════════════════════════════════════

def test_char_approve_retrain_exito_approved():
    repo = FakeTradeRepo(RetrainState(filled_count=10, retrain_in_progress=False, scheduled_due=False))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    config = FakeConfig({})
    uc = ApproveRetrain(repo, trainer, audit, config)
    res = uc.execute(ApproveRetrainRequest(symbol="EURUSD"))
    assert res.status == "approved"
    assert res.filled_count == 10
    assert res.scheduled_due is False
    assert res.min_trades_for_retrain == 5
    assert trainer.called is True


def test_char_approve_retrain_ya_en_progreso():
    repo = FakeTradeRepo(RetrainState(filled_count=10, retrain_in_progress=True, scheduled_due=False))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    config = FakeConfig({})
    uc = ApproveRetrain(repo, trainer, audit, config)
    res = uc.execute(ApproveRetrainRequest(symbol="EURUSD"))
    assert res.status == "already_running"
    assert trainer.called is False
    assert repo.saved is None


def test_char_approve_retrain_no_pending():
    repo = FakeTradeRepo(RetrainState(filled_count=3, retrain_in_progress=False, scheduled_due=False))
    trainer = FakeModelTrainer()
    audit = FakeAudit()
    config = FakeConfig({})
    uc = ApproveRetrain(repo, trainer, audit, config)
    res = uc.execute(ApproveRetrainRequest(symbol="EURUSD"))
    assert res.status == "no_pending"
    assert res.filled_count == 3
    assert trainer.called is False


def test_char_approve_retrain_scheduled_due_aprueba_sin_umbral():
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


# ═════════════════════════════════════════════════════════════════════════
# promote_candidate — 4 casos
# ═════════════════════════════════════════════════════════════════════════

def test_char_promote_candidate_exito_promoted():
    manifest = {"sha256": "abc123", "obs_shape": [20, 25]}
    repo = FakeModelRepo(manifest=manifest)
    audit = FakeAudit()
    uc = PromoteCandidate(repo, audit)
    res = uc.execute(PromoteCandidateRequest(symbol="EURUSD", exp_id=42))
    assert res.promoted is True
    assert res.exp_id == 42
    assert res.model_sha256 == "abc123"
    assert repo.promoted_id == "candidate_EURUSD_exp42"


def test_char_promote_candidate_no_manifest():
    repo = FakeModelRepo(manifest=None)
    audit = FakeAudit()
    uc = PromoteCandidate(repo, audit)
    res = uc.execute(PromoteCandidateRequest(symbol="GBPUSD", exp_id=7))
    assert res.promoted is False
    assert res.exp_id == 7
    assert res.reason == "no live model manifest"
    assert repo.promoted_id is None


def test_char_promote_candidate_repo_falla_propaga():
    repo = FakeModelRepo(manifest={"sha256": "x"}, should_fail=True)
    audit = FakeAudit()
    uc = PromoteCandidate(repo, audit)
    try:
        uc.execute(PromoteCandidateRequest(symbol="EURUSD", exp_id=1))
        assert False, "debió lanzar"
    except RuntimeError:
        pass


def test_char_promote_candidate_manifest_sin_sha256():
    repo = FakeModelRepo(manifest={})
    audit = FakeAudit()
    uc = PromoteCandidate(repo, audit)
    res = uc.execute(PromoteCandidateRequest(symbol="EURUSD", exp_id=99))
    assert res.promoted is True
    assert res.model_sha256 is None
    assert repo.promoted_id == "candidate_EURUSD_exp99"
