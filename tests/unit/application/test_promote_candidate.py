"""Tests de unidad: PromoteCandidate caso de uso."""

from src.application.use_cases.promote_candidate import (
    PromoteCandidate,
    PromoteCandidateRequest,
    PromoteCandidateResult,
)


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


class FakeAudit:
    def __init__(self):
        self.messages = []
    def log(self, event, details):
        self.messages.append((event, details))


# ── Caso 1: éxito → promoted ──────────────────────────────────────────
def test_promote_candidate_exito_promoted():
    manifest = {"sha256": "abc123", "obs_shape": [20, 25]}
    repo = FakeModelRepo(manifest=manifest)
    audit = FakeAudit()
    uc = PromoteCandidate(repo, audit)
    res = uc.execute(PromoteCandidateRequest(symbol="EURUSD", exp_id=42))
    assert res.promoted is True
    assert res.exp_id == 42
    assert res.reason == "promoted by rule"
    assert res.model_sha256 == "abc123"
    assert repo.promoted_id == "candidate_EURUSD_exp42"
    assert any(m[0] == "candidate_promoted" and m[1]["promoted"] is True for m in audit.messages)


# ── Caso 2: sin manifiesto vivo → not promoted ────────────────────────
def test_promote_candidate_no_manifest():
    repo = FakeModelRepo(manifest=None)
    audit = FakeAudit()
    uc = PromoteCandidate(repo, audit)
    res = uc.execute(PromoteCandidateRequest(symbol="GBPUSD", exp_id=7))
    assert res.promoted is False
    assert res.exp_id == 7
    assert res.reason == "no live model manifest"
    assert repo.promoted_id is None
    assert any(m[0] == "candidate_promotion_failed" for m in audit.messages)


# ── Caso 3: model repo falla → error propaga ──────────────────────────
def test_promote_candidate_repo_falla_propaga():
    repo = FakeModelRepo(manifest={"sha256": "x"}, should_fail=True)
    audit = FakeAudit()
    uc = PromoteCandidate(repo, audit)
    try:
        uc.execute(PromoteCandidateRequest(symbol="EURUSD", exp_id=1))
        assert False, "debió lanzar"
    except RuntimeError as exc:
        assert str(exc) == "repo boom"


# ── Caso 4: manifiesto sin sha256 → promoted con model_sha256 None ─────
def test_promote_candidate_manifest_sin_sha256():
    repo = FakeModelRepo(manifest={})
    audit = FakeAudit()
    uc = PromoteCandidate(repo, audit)
    res = uc.execute(PromoteCandidateRequest(symbol="EURUSD", exp_id=99))
    assert res.promoted is True
    assert res.model_sha256 is None
    assert repo.promoted_id == "candidate_EURUSD_exp99"
