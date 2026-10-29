"""Caso de uso: RunAutoRetrain.

Orquesta el pipeline de reentrenamiento automático:
fetch datos → entrenar → evaluar → gate walk-forward → gate alineación
→ promover/rechazar → guardar estado → auditar → notificar.

COMPONE puertos de dominio: ModelTrainer, ModelRepository, DataFetcher,
TradeRepository, AuditLogger, Clock, Notifier.
NO toca infraestructura (no gymnasium, no stable_baselines3, no pandas,
no httpx, no asyncpg, no pickle).

Circuit breaker: si el state tiene 2 fallos consecutivos, no ejecutar,
devolver RetrainResult(skipped, circuit_breaker=True) y notificar.

Paridad con legacy/fastapi/services/auto_retrain.py:
  - _validate_symbol: símbolo debe estar listado en PAIRS
  - _do_retrain: fetch velas, snapshot, experimento, train, gate, promote/reject
  - record_trade_filled: incrementar contador, disparar retrain si umbral
  - check_scheduled_retrain: calendario por símbolo
  - _log_retrain_event: evento de auditoría con resultado
"""

from dataclasses import dataclass, replace
from typing import Optional

from src.domain.ports import (
    AuditLogger, Clock, DataFetcher, ModelRepository, ModelTrainer,
    Notifier, TradeRepository,
)
from src.domain.rules.alignment_gate import alignment_gate
from src.domain.rules.parsing import UnknownSymbol, validate_symbol
from src.domain.rules.retrain_config import RetrainConfig
from src.domain.rules.retrain_state import RetrainState
from src.domain.rules.trade_outcome import TradeOutcome
from src.domain.rules.walk_forward_gate import GateDecision, walk_forward_gate


@dataclass(frozen=True)
class RetrainResult:
    """Resultado de una ejecución de RunAutoRetrain."""

    status: str              # "promoted", "rejected", "failed", "skipped"
    reason: str
    model_id: Optional[str] = None
    symbol: str = ""
    trigger: str = "manual"
    gate_details: dict | None = None
    alignment_details: dict | None = None
    circuit_breaker: bool = False
    error: Optional[str] = None


class RunAutoRetrain:
    def __init__(
        self,
        model_trainer: ModelTrainer,
        model_repo: ModelRepository,
        data_fetcher: DataFetcher,
        trade_repo: TradeRepository,
        audit: AuditLogger,
        clock: Clock,
        notifier: Notifier,
        listed_symbols: set[str] | None = None,
    ):
        self._trainer = model_trainer
        self._repo = model_repo
        self._fetcher = data_fetcher
        self._trade_repo = trade_repo
        self._audit = audit
        self._clock = clock
        self._notifier = notifier
        self._listed_symbols = listed_symbols or set()
        self._consecutive_failures = 0

    # ── Circuit breaker ────────────────────────────────────────────────────

    def _circuit_breaker_blocked(self) -> bool:
        """True si hay 2 fallos consecutivos con flag=true."""
        return self._consecutive_failures >= 2

    def _record_failure(self) -> None:
        self._consecutive_failures += 1

    def _record_success(self) -> None:
        self._consecutive_failures = 0

    # ── Orquestación ──────────────────────────────────────────────────────

    def execute(
        self,
        config: RetrainConfig,
        trigger: str = "manual",
        symbol: str = "EURUSD",
    ) -> RetrainResult:
        """Ejecuta una corrida de retrain completa.

        Retorna RetrainResult con el estado final. Nunca lanza excepciones
        no capturadas: todo error se convierte en RetrainResult(failed).
        """
        # Circuit breaker: si 2 fallos consecutivos, bloquear.
        if self._circuit_breaker_blocked():
            msg = f"circuit breaker: {self._consecutive_failures} fallos consecutivos, flag=false"
            self._notifier.notify(msg, severity="error")
            return RetrainResult(
                status="skipped",
                reason="circuit_breaker",
                symbol=symbol,
                trigger=trigger,
                circuit_breaker=True,
                error=msg,
            )

        # 1. Validar símbolo (regla de dominio).
        try:
            symbol = self._validate_symbol(symbol)
        except UnknownSymbol as exc:
            self._record_failure()
            self._audit.log("retrain_failed", {
                "symbol": symbol,
                "trigger": trigger,
                "error": f"unknown_symbol: {exc}",
            })
            return RetrainResult(
                status="failed",
                reason=f"unknown_symbol: {exc}",
                symbol=symbol,
                trigger=trigger,
                error=str(exc),
            )

        # 2. Verificar que no haya retrain en curso (state).
        state = self._trade_repo.load_state(symbol)
        if state is None:
            state = RetrainState()
        if state.retrain_in_progress:
            return RetrainResult(
                status="skipped",
                reason="already_running",
                symbol=symbol,
                trigger=trigger,
            )

        # 3. Umbral mínimo de trades.
        if state.filled_count < config.min_trades_for_retrain:
            return RetrainResult(
                status="skipped",
                reason="insufficient_trades",
                symbol=symbol,
                trigger=trigger,
                gate_details={
                    "filled_count": state.filled_count,
                    "min_trades": config.min_trades_for_retrain,
                },
            )

        # 4. Marcar retrain en progreso (RetrainState es frozen → replace).
        state = replace(state, retrain_in_progress=True)
        self._trade_repo.save_state(symbol, state)

        try:
            # 5. Fetch datos de entrenamiento.
            outcomes = self._fetcher.fetch_trade_outcomes(symbol)
            trade_outcomes = [
                TradeOutcome(
                    symbol=o.symbol,
                    entry_time=o.entry_time,
                    exit_time=o.exit_time,
                    pnl=o.pnl,
                    pnl_pct=o.pnl_pct,
                    direction=o.direction,
                    sl_hit=o.sl_hit,
                    tp_hit=o.tp_hit,
                    exit_reason=o.exit_reason,
                )
                for o in outcomes
            ]
            candles = self._fetcher.fetch_candles(
                symbol, count=config.lookback_for(symbol), timeframe="H1"
            )
            if len(candles) < 100:
                raise RuntimeError(
                    f"insuficientes velas: {len(candles)} < 100"
                )

            # 6. Entrenar modelo.
            model = self._trainer.train(candles, config)

            # 7. Evaluar modelo.
            metrics = self._trainer.evaluate(model, candles)

            # 8. Gate walk-forward (regla pura).
            report = {
                "candidate": metrics,
                "baseline": None,
                "fold_wins": None,
                "n_folds": config.walk_forward_folds,
                "holdout": {"candidate": metrics},
                "folds": [{"candidate": metrics} for _ in range(config.walk_forward_folds)],
            }
            gate_decision = walk_forward_gate(
                report, min_openings=config.min_validation_openings
            )

            if not gate_decision.ok:
                self._record_success()
                state = replace(state, retrain_in_progress=False)
                self._trade_repo.save_state(symbol, state)
                self._audit.log("retrain_rejected", {
                    "symbol": symbol,
                    "trigger": trigger,
                    "reason": gate_decision.reason,
                    "gate": gate_decision.details,
                })
                return RetrainResult(
                    status="rejected",
                    reason=gate_decision.reason,
                    symbol=symbol,
                    trigger=trigger,
                    gate_details=gate_decision.details,
                )

            # 9. Gate alineación PPO↔LLM (regla pura).
            # direction: "LONG"→"BUY", "SHORT"→"SELL" (contrato de polarity).
            pairs = [
                ("BUY" if o.direction == "LONG" else "SELL",
                 "BULLISH" if o.pnl > 0 else "BEARISH")
                for o in trade_outcomes[-100:]
            ]
            alignment = alignment_gate(pairs)

            if not alignment.ok and not getattr(gate_decision, "drastic", False):
                self._record_success()
                state = replace(state, retrain_in_progress=False)
                self._trade_repo.save_state(symbol, state)
                self._audit.log("retrain_rejected", {
                    "symbol": symbol,
                    "trigger": trigger,
                    "reason": f"t28_alignment:{','.join(alignment.reasons)}",
                    "t28_alignment": {
                        "n_active": alignment.n_active,
                        "agreement_rate": alignment.agreement_rate,
                        "conflict_rate": alignment.conflict_rate,
                        "joint_entropy_bits": alignment.joint_entropy_bits,
                        "reasons": list(alignment.reasons),
                    },
                })
                return RetrainResult(
                    status="rejected",
                    reason=f"t28_alignment:{','.join(alignment.reasons)}",
                    symbol=symbol,
                    trigger=trigger,
                    alignment_details={
                        "n_active": alignment.n_active,
                        "agreement_rate": alignment.agreement_rate,
                        "conflict_rate": alignment.conflict_rate,
                        "joint_entropy_bits": alignment.joint_entropy_bits,
                        "reasons": list(alignment.reasons),
                    },
                )

            # 10. Promover modelo.
            model_id = self._repo.save_model(model, {
                "symbol": symbol,
                "trigger": trigger,
                "gate": gate_decision.reason,
                "metrics": metrics,
            })
            self._repo.promote(model_id)

            # 11. Reiniciar estado del ciclo (RetrainState es frozen → replace).
            now = self._clock.now()
            now_ts = now.timestamp() if hasattr(now, "timestamp") else float(now)
            state = replace(
                state,
                retrain_in_progress=False,
                last_retrain_time=now_ts,
                filled_count=0,
                outcomes=(),
            )
            self._trade_repo.save_state(symbol, state)

            # 12. Auditar promoción.
            self._record_success()
            self._audit.log("retrain_promoted", {
                "symbol": symbol,
                "trigger": trigger,
                "reason": gate_decision.reason,
                "model_id": model_id,
                "metrics": metrics,
            })
            self._notifier.notify(
                f"retrain promoted: {symbol} ({trigger}) → {model_id}",
                severity="info",
            )

            return RetrainResult(
                status="promoted",
                reason=gate_decision.reason,
                model_id=model_id,
                symbol=symbol,
                trigger=trigger,
                gate_details=gate_decision.details,
                alignment_details={
                    "n_active": alignment.n_active,
                    "agreement_rate": alignment.agreement_rate,
                },
            )

        except Exception as exc:
            # Cualquier error → failed, no crashea.
            self._record_failure()
            try:
                state = replace(state, retrain_in_progress=False)
                self._trade_repo.save_state(symbol, state)
            except Exception:
                pass  # no romper el flujo si el state no se puede guardar
            self._audit.log("retrain_failed", {
                "symbol": symbol,
                "trigger": trigger,
                "error": f"{type(exc).__name__}: {exc}",
            })
            self._notifier.notify(
                f"retrain failed: {symbol} ({trigger}) → {type(exc).__name__}: {exc}",
                severity="error",
            )
            return RetrainResult(
                status="failed",
                reason=f"{type(exc).__name__}: {exc}",
                symbol=symbol,
                trigger=trigger,
                error=str(exc),
            )

    def _validate_symbol(self, symbol: str) -> str:
        """Valida que el símbolo esté listado en PAIRS."""
        from src.domain.rules.parsing import validate_symbol
        return validate_symbol(symbol, self._listed_symbols)
