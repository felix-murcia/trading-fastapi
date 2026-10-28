"""Caso de uso: ValidateVetoChain.

Replica la lógica de `apply_mechanical_vetoes` (fastapi/services/veto_chain.py)
aislada: solo importa de src.domain/. Los umbrales se inyectan vía
TradingThresholds y ConfigProvider (puertos).

No lee os.environ. No toca infraestructura.

Orden de vetos (paridad exacta con legacy):
    time-stop → re-entrada → posición → contrato → confianza →
    Qwen degradado → consistencia USD → sesgo de confianza

Si se necesita un puerto nuevo para acceder a configuración dinámica,
se añade a src/domain/ports/ como Protocol.
"""

from dataclasses import dataclass
from typing import Optional, Sequence

from src.domain.ports import Clock, ConfigProvider, TradeRepository  # noqa: F401
from src.domain.value_objects.thresholds import TradingThresholds


@dataclass(frozen=True)
class ValidateVetoChainInput:
    cycle_id: str
    symbol: str
    decision: str
    env_position: int
    position_raw: int
    steps_h1: int
    time_stop_bars: int
    contract_ok: bool
    contract_reason: str
    contract_lots: float
    contract_sl_pips: float
    contract_tp_pips: float
    contract_ppl_eur: float
    contract_risk_eur: float
    contract_reward_gross_eur: float
    contract_cost_eur: float
    contract_reward_net_eur: float
    contract_lock_eur: float
    contract_cap_eur: float
    lots: float
    tp_pips: float
    min_lock_eur: float
    reentry_window: bool
    bars_since_close: int
    churn_bars: int
    qwen_ok: bool
    qwen_reentry_ok: Optional[bool]
    min_entry_confidence: float
    effective_prob: float
    open_positions: Sequence[dict]
    qwen: dict


@dataclass(frozen=True)
class ValidateVetoChainOutput:
    decision: str
    time_stop: bool
    reentry_ok: Optional[bool]
    reward_eur: Optional[float]
    reward_below_min: bool
    contract_rejected: bool
    position_rejected: bool
    low_confidence: bool


class ValidateVetoChain:
    def __init__(self, repo: TradeRepository, clock: Clock,
                 thresholds: TradingThresholds,
                 config: Optional[ConfigProvider] = None):
        self._repo = repo
        self._clock = clock
        self._thresholds = thresholds
        self._config = config

    def execute(self, inp: ValidateVetoChainInput) -> ValidateVetoChainOutput:
        decision, time_stop = self._time_stop(inp)
        reentry_ok = self._resolve_reentry(inp)
        return self._apply_hold_chain(inp, decision, time_stop, reentry_ok)

    def _time_stop(self, inp: ValidateVetoChainInput) -> tuple[str, bool]:
        if inp.env_position != 0 and inp.steps_h1 >= inp.time_stop_bars:
            return "CLOSE", True
        return inp.decision, False

    def _resolve_reentry(self, inp: ValidateVetoChainInput) -> Optional[bool]:
        if not inp.reentry_window:
            return None
        if inp.qwen_ok:
            return bool(inp.qwen_reentry_ok if inp.qwen_reentry_ok is not None else True)
        return inp.bars_since_close > 0

    def _apply_hold_chain(
        self, inp: ValidateVetoChainInput, decision: str,
        time_stop: bool, reentry_ok: Optional[bool],
    ) -> ValidateVetoChainOutput:
        reward_eur = (
            inp.contract_reward_net_eur
            if (inp.contract_ok and decision in ("BUY", "SELL") and inp.lots > 0.0)
            else None
        )
        reward_below_min = False
        contract_rejected = (not inp.contract_ok) and inp.contract_reason != "sizing_inviable"
        position_rejected = inp.env_position != 0 and decision in ("BUY", "SELL")
        low_confidence = False

        decision, position_rejected = self._check_position_veto(
            inp, decision, position_rejected
        )
        if decision != "HOLD":
            decision, contract_rejected, reward_below_min = self._check_contract_veto(
                inp, decision, contract_rejected, reward_below_min
            )
        if decision != "HOLD":
            decision = self._check_reentry_veto(inp, reentry_ok, decision)
        if decision != "HOLD":
            decision, low_confidence = self._check_low_confidence_veto(
                inp, decision, low_confidence
            )
        if decision != "HOLD":
            decision = self._check_qwen_degraded_veto(inp, decision)
        if decision != "HOLD":
            decision = self._check_usd_consistency_veto(inp, decision)
        if decision != "HOLD":
            decision = self._check_bias_confidence_veto(inp, decision)

        return ValidateVetoChainOutput(
            decision=decision,
            time_stop=time_stop,
            reentry_ok=reentry_ok,
            reward_eur=reward_eur,
            reward_below_min=reward_below_min,
            contract_rejected=contract_rejected,
            position_rejected=position_rejected,
            low_confidence=low_confidence,
        )

    def _check_position_veto(
        self, inp: ValidateVetoChainInput, decision: str,
        position_rejected: bool,
    ) -> tuple[str, bool]:
        if position_rejected:
            return "HOLD", True
        return decision, False

    def _check_contract_veto(
        self, inp: ValidateVetoChainInput, decision: str,
        contract_rejected: bool, reward_below_min: bool,
    ) -> tuple[str, bool, bool]:
        if contract_rejected and decision in ("BUY", "SELL"):
            reward_below_min = inp.contract_reason in (
                "tp_no_paga_bloqueo", "tp_no_cubre_costes",
            )
            return "HOLD", True, reward_below_min
        return decision, False, reward_below_min

    def _check_reentry_veto(
        self, inp: ValidateVetoChainInput, reentry_ok: Optional[bool],
        decision: str,
    ) -> str:
        if inp.reentry_window and reentry_ok is False and decision in ("BUY", "SELL"):
            return "HOLD"
        return decision

    def _check_low_confidence_veto(
        self, inp: ValidateVetoChainInput, decision: str,
        low_confidence: bool,
    ) -> tuple[str, bool]:
        if (
            inp.min_entry_confidence > 0.0
            and inp.effective_prob < inp.min_entry_confidence
            and decision in ("BUY", "SELL")
        ):
            low_confidence = True
            return "HOLD", True
        return decision, low_confidence

    def _check_qwen_degraded_veto(
        self, inp: ValidateVetoChainInput, decision: str,
    ) -> str:
        if not inp.qwen_ok and decision in ("BUY", "SELL"):
            return "HOLD"
        return decision

    def _check_usd_consistency_veto(
        self, inp: ValidateVetoChainInput, decision: str,
    ) -> str:
        if decision not in ("BUY", "SELL"):
            return decision
        usd_ok, _ = self._usd_pair_consistency_veto(inp)
        if not usd_ok:
            return "HOLD"
        return decision

    def _usd_pair_consistency_veto(
        self, inp: ValidateVetoChainInput
    ) -> tuple[bool, str]:
        if not inp.open_positions:
            return True, ""
        current_symbol = inp.symbol
        current_direction = inp.decision
        if current_direction not in ("BUY", "SELL"):
            return True, ""
        if "USD" not in current_symbol:
            return True, ""
        for pos in inp.open_positions:
            pos_symbol = pos.get("symbol", "")
            pos_direction = pos.get("type", "") or pos.get("direction", "")
            if not pos_symbol or not pos_direction:
                continue
            if pos_direction not in ("BUY", "SELL"):
                continue
            if "USD" not in pos_symbol:
                continue
            if pos_symbol == current_symbol:
                continue
            if self._usd_contradiction(current_symbol, current_direction,
                                       pos_symbol, pos_direction):
                return False, (
                    f"USD_CONFLICT:{pos_symbol}:{pos_direction} "
                    f"vs {current_symbol}:{current_direction}"
                )
        return True, ""

    def _usd_perspective(self, symbol: str, direction: str) -> Optional[str]:
        base = symbol[:3]
        quote = symbol[3:]
        if base == "USD":
            return "fuerte" if direction == "BUY" else "débil"
        if quote == "USD":
            return "débil" if direction == "BUY" else "fuerte"
        return None

    def _usd_contradiction(self, sym1: str, dir1: str, sym2: str, dir2: str) -> bool:
        p1 = self._usd_perspective(sym1, dir1)
        p2 = self._usd_perspective(sym2, dir2)
        if p1 is None or p2 is None:
            return False
        return p1 == p2

    def _check_bias_confidence_veto(
        self, inp: ValidateVetoChainInput, decision: str,
    ) -> str:
        if decision not in ("BUY", "SELL"):
            return decision
        qwen_data = inp.qwen or {}
        bias_ok, _ = self._bias_low_confidence_veto(decision, qwen_data)
        if not bias_ok:
            return "HOLD"
        return decision

    def _bias_low_confidence_veto(
        self, decision: str, qwen: dict
    ) -> tuple[bool, str]:
        if decision not in ("BUY", "SELL"):
            return True, ""
        bias = qwen.get("bias", "NEUTRAL")
        if bias == "NEUTRAL":
            return False, f"BIAS_NEUTRAL:no thesis for {decision}"
        return True, ""
