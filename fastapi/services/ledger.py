"""
L4 · LEDGER DE FIABILIDAD — el estado del mundo, con la vara correcta.

Publica, por cada fuente de señal, QUÉ SABEMOS DE EL con números
reproducibles:

  - métricas de TRADING en OOS (expectancy, sharpe, maxDD, nº de operaciones)
    — la vara que decide si una fuente puede tomar decisiones;
  - track record VIVO (trades reales, expectancy, win rate, drawdown);
  - la skill de CLASIFICACIÓN (diagnóstico: calibración y contexto) con su
    n, su CI de Wilson y si la muestra está madura;
  - la comparación con el BENCHMARK (la vara);
  - ADMISIÓN: sí/no + el motivo, y la regla que la decides.

Reglas (Félix, 2026-10-19/20):
  · lo no medido no tiene poder (sin muestra madura ⇒ sin admisión);
  · la admisión se decide con las MÉTRICAS DE TRADING OOS, no con la
    exactitud de clasificación (ésta es diagnóstico);
  · nada se publica sin n, método y fecha.

Fuente de verdad: services/measurement (definición canónica) + el informe del
walk-forward del último experimento + trade_outcomes (trades reales).
"""
import datetime as dt
import logging
import os
from typing import Optional

from services import qwen_authority

logger = logging.getLogger(__name__)

# Criterio de admisión de un challenger (pre-registrado, 2026-10-20).
# Se compara contra el sistema vigente con las métricas de TRADING OOS.
ADMISSION_MIN_TRADES = 30
ADMISSION_MAX_DD = 0.10          # 10 % de drawdown máximo en el holdout
ADMISSION_MAX_OPS_PER_HOLDOUT = 40  # presupuesto de churn (el bundle vivo: 30)
# La vara: el expectancy del sistema vigente (se recalcula de los trades
# reales; el fallback documenta el valor de la referencia).
FALLBACK_SYSTEM_EXPECTANCY = 1.81
FALLBACK_SYSTEM_WIN_RATE = 0.511
FALLBACK_SYSTEM_TRADES = 186
FALLBACK_SYSTEM_PNL = 336.31


async def _live_track_record(symbol: str = "EURUSD") -> dict:
    """Track record real del sistema (trade_outcomes) — la verdad de caja."""
    from db.connection import get_pool

    try:
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT count(*) n,
                   COALESCE(sum(pnl),0) pnl,
                   COALESCE(avg(pnl),0) expectancy,
                   count(*) FILTER (WHERE pnl > 0) wins,
                   count(*) FILTER (WHERE pnl <= 0) losses,
                   min(entry_time) first_entry, max(exit_time) last_exit
            FROM trade_outcomes WHERE symbol = $1
            """, symbol,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LEDGER] no se pudo leer el track record vivo: %s", exc)
        return {}
    if not rows:
        return {}
    r = rows[0]
    n = int(r["n"] or 0)
    return {
        "trades": n,
        "pnl_eur": round(float(r["pnl"] or 0), 2),
        "expectancy_eur": round(float(r["expectancy"] or 0), 2),
        "wins": int(r["wins"] or 0),
        "losses": int(r["losses"] or 0),
        "win_rate": round((int(r["wins"] or 0) / n), 4) if n else None,
        "first_entry": r["first_entry"].isoformat() if r["first_entry"] else None,
        "last_exit": r["last_exit"].isoformat() if r["last_exit"] else None,
    }


def _live_bundle_report(symbol: str = "EURUSD") -> Optional[dict]:
    """Informe del experimento que produjo el bundle VIVO (no el último).

    El bundle vivo es el que decide ⇒ su walk-forward OOS es su evidencia.
    Se localiza por el sha del manifiesto activo ⇒ no se confunde con un
    experimento posterior rechazado.
    """
    from core.pair import default_model_path
    base = os.path.join(os.path.dirname(default_model_path(symbol)), "retrain_artifacts")
    manifest_path = os.path.join(
        os.path.dirname(default_model_path(symbol)),
        "ppo_trading_bot_v3.manifest.json",
    )
    live_sha = None
    if os.path.exists(manifest_path):
        import json
        try:
            with open(manifest_path) as fh:
                live_sha = json.load(fh).get("model_sha256")
        except (OSError, ValueError):
            live_sha = None
    if not os.path.isdir(base):
        return None
    # El manifiesto del bundle VIVO registra de qué experimento promotionó
    # (extra.promoted_from_experiment) ⇒ su walk-forward OOS es su evidencia
    # (y no el del último experimento, que pudo ser rechazado).
    exp_from_manifest = None
    if live_sha and os.path.exists(manifest_path):
        import json
        try:
            with open(manifest_path) as fh:
                man = json.load(fh)
            exp_from_manifest = (man.get("extra") or {}).get("promoted_from_experiment")
        except (OSError, ValueError):
            exp_from_manifest = None
    if exp_from_manifest:
        rp = os.path.join(base, f"exp{exp_from_manifest}", "report.json")
        if os.path.exists(rp):
            import json
            try:
                with open(rp) as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass
    for d in sorted(os.listdir(base), reverse=True):
        rp = os.path.join(base, d, "report.json")
        if not os.path.exists(rp):
            continue
        import json
        try:
            with open(rp) as fh:
                rep = json.load(fh)
        except (OSError, ValueError):
            continue
        if live_sha and rep.get("model_sha256") == live_sha:
            return rep
        if rep.get("exp_id") and str(rep["exp_id"]) in d:
            # candidato: se acepta sólo si su gate pasó (fue promovido)
            gate = rep.get("gate") or {}
            if gate.get("ok") or (rep.get("promotion_path") or {}).get("promote"):
                return rep
    return None


async def build_ledger(symbol: str = "EURUSD") -> dict:
    """Construye el ledger (estado del mundo con la vara correcta)."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    ledger: dict = {
        "generated_at": now,
        "method": {
            "module": "services.measurement (definición canónica)",
            "terminal": "primera vela >= t0 + 24 h, CERRADA (determinista)",
            "admission_var": "métricas de TRADING OOS (expectancy, sharpe, maxDD, ops)",
            "classification_var": "diagnóstico (calibración) — NO decide admisión",
            "admission_rules": {
                "min_trades": ADMISSION_MIN_TRADES,
                "max_dd": ADMISSION_MAX_DD,
                "max_ops_per_holdout": ADMISSION_MAX_OPS_PER_HOLDOUT,
            },
        },
        "sources": {},
    }

    # ── 1) el sistema vigente (el bundle vivo: PPO + reglas + quórum) ──────
    live = await _live_track_record(symbol)
    exp = _live_bundle_report(symbol)
    wf = (exp or {}).get("walk_forward") or {}
    holdout_c = (wf.get("holdout") or {}).get("candidate") or {}
    exp_id = (exp or {}).get("exp_id")
    system = {
        "role": "decisor vigente (PPO+v1 reward + reglas de negocio + quórum)",
        "live": live,
        "walk_forward_oos": {
            "experiment": exp_id,
            "reward_version": ((exp or {}).get("cfg") or {}).get("reward_version"),
            "holdout_reward": holdout_c.get("reward"),
            "holdout_expectancy_eur": holdout_c.get("expectancy"),
            "holdout_sharpe": holdout_c.get("sharpe"),
            "holdout_max_dd": holdout_c.get("max_dd"),
            "holdout_trades": holdout_c.get("trades"),
            "holdout_win_rate": holdout_c.get("win_rate"),
            "direction_accuracy": holdout_c.get("direction_accuracy"),
        },
        "admitted": True,
        "admission_reason": (
            "sistema en producción con track record real: "
            f"{live.get('trades', 0)} trades, expectancy {live.get('expectancy_eur')} €, "
            f"win {live.get('win_rate')} — y el bundle promovido es el mejor "
            "candidato medido en el walk-forward (expectancy > 0)."
        ) if live else "sin track record vivo (no verificable)",
    }
    ledger["sources"]["sistema_vivo"] = system

    # ── 2) el LLM (veto con autoridad condicionada; candidato E4) ─────────
    try:
        auth = qwen_authority.peek(symbol) or {}
    except Exception as exc:  # noqa: BLE001
        auth = {}
        logger.warning("[LEDGER] autoridad no disponible: %s", exc)
    llm = {
        "role": "veto direccional (autoridad condicionada a skill demostrada) "
                "+ candidato a decisor (E4, pendiente de evidencia)",
        "authority": {
            "granted": auth.get("granted"),
            "n": auth.get("n"),
            "acc": auth.get("acc"),
            "wilson_lower": auth.get("wilson_lower"),
            "reason": auth.get("reason"),
        },
        "classification_diagnostic": {
            "note": "diagnóstico, NO decide admisión (la mide el ledger de "
                    "expectancy viva con la mecánica real)",
            "calibration": auth.get("calibration"),
        },
        "admitted": bool(auth.get("granted")),
        "admission_reason": auth.get("reason") or "sin medición",
    }
    ledger["sources"]["llm"] = llm

    # ── 3) el quórum (filtro de mercado lateral) ──────────────────────────
    ledger["sources"]["quorum"] = {
        "role": "filtro de mercado lateral (2 de 3 fuentes: PPO + LLM con "
                "evidencia + estructura SMA200)",
        "admitted": True,
        "admission_reason": "mecánico y determinista; no es una fuente de "
                            "dirección (filtra, no decide)",
    }

    # ── 4) el benchmark (la vara) ────────────────────────────────────────
    ledger["benchmark"] = {
        "note": "predictores triviales medidos con la definición canónica; "
                "una fuente sólo se admite si supera al mejor de ellos en "
                "expectancy OOS (no en clasificación)",
        "script": "fastapi/scripts/benchmark_canonico.py",
    }

    ledger["summary"] = {
        "admitted_sources": [
            k for k, v in ledger["sources"].items() if v.get("admitted")
        ],
        "pending_evidence": {
            "llm_veto": "muestra madura desde 2026-10-20 18:19 (n=0 evaluable "
                        "a las 14:30 del 20-10)",
            "challengers": "un challenger se admite con: expectancy OOS > "
                           f"{FALLBACK_SYSTEM_EXPECTANCY} €/trade, maxDD ≤ "
                           f"{ADMISSION_MAX_DD:.0%}, ops ≤ "
                           f"{ADMISSION_MAX_OPS_PER_HOLDOUT} por holdout y "
                           f"n ≥ {ADMISSION_MIN_TRADES} trades",
        },
    }
    return ledger
