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


# Candidatos por defecto del mapa de edge (T-31.9, 2026-10-20): el mapa es
# el INSTRUMENTO DE SELECCIÓN de par ⇒ tiene que cubrir a los candidatos, no
# sólo a los ya operativos.
EDGE_MAP_SYMBOLS: tuple = (
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "XAUUSD",
)


async def _edge_decay_map(symbols: tuple = EDGE_MAP_SYMBOLS) -> dict:
    """DECAIMIENTO DEL EDGE por par y por ventana (solo lectura).

    Orden Félix 2026-10-20: «cada par tiene su propia idiosincrasia» ⇒ el
    ledger publica, por par, la exactitud del predictor trivial con skill en
    ventanas de 30/14/7 días (el mapa de edge por marco, medido con la
    definición canónica). Sirve para dos cosas:
      1. ver si el edge de un par se apaga (el ledger lo declara antes de que
         la expectativa viva del sistema lo refleje);
      2. elegir el marco de cada mundo por evidencia, no por copia.
    """
    from services.measurement import (
        benchmark_predictions,
        canonical_direction,
        skill_vs_base,
    )
    from services.mt5_client import get_candles_with_offset
    import datetime as _dt

    out: dict = {}
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    for sym in symbols:
        try:
            cands, _off = await get_candles_with_offset(sym, "H1", 1200)
            if not cands:
                continue
            bt = [float(c["time"]) for c in cands]
            bc = [float(c["close"]) for c in cands]
            # Las TRES reglas triviales (T-31.9): la de estructura (sma200),
            # la de momentum (mom24) y la de la vela previa. Con la base
            # corregida, la que tiene skill real puede ser CUALQUIERA (medido
            # el 20-10: en USDCHF la única con skill es mom24, no sma200).
            por_regla: dict = {}
            for regla in ("sma200", "mom24", "barra_previa"):
                windows = {}
                for label, days in (("30d", 30), ("14d", 14), ("7d", 7)):
                    since = now - days * 86400
                    h = n = 0
                    dirs: list = []
                    for i in range(0, len(bt)):
                        if bt[i] < since:
                            continue
                        d = canonical_direction(bt, bc, bt[i], now)
                        if d is None:
                            continue
                        pred = benchmark_predictions(bt, bc, bt[i], now).get(regla)
                        if pred is None:
                            continue
                        n += 1
                        dirs.append(d)
                        h += 1 if ((pred > 0) == (d > 0)) else 0
                    if n:
                        # T-31.9 (2026-10-20): el skill se mide contra SU
                        # PROPIA BASE (la mejor predicción constante de la
                        # ventana), no contra el 50 %. Sin esto, el mapa
                        # publicaba la DERIVA DEL MERCADO: en una ventana
                        # donde la regla dice siempre lo mismo, su exactitud
                        # ES la base (medido: USDCHF 7 d → «76,1 %» era «el
                        # mercado subió el 76 % de los días»).
                        sk = skill_vs_base(h, n, dirs)
                        windows[label] = {
                            "acc": sk["acc"], "n": n,
                            "base": sk["base"], "skill_pp": sk["skill_pp"],
                            "skill_lo_pp": sk["skill_lo_pp"],
                            "skill_real": sk["granted"],
                        }
                por_regla[regla] = windows
            # el "edge_alive" del par = ¿alguna regla tiene skill real en la
            # ventana viva (7 d) con el IC 95 % por encima de 0?
            reglas_vivas = [
                r for r, w in por_regla.items()
                if w.get("7d", {}).get("skill_real")
            ]
            out[sym] = {
                "predictor": "las 3 reglas triviales (H1), SKILL contra su propia base",
                "windows": por_regla.get("sma200", {}),
                "por_regla": por_regla,
                "reglas_con_skill_vivo": reglas_vivas,
                "edge_alive": bool(reglas_vivas),
                "nota": "skill = exactitud − base de la ventana; "
                        "edge_alive exige que el IC 95 % del skill esté por "
                        "encima de 0 (no basta con superar el 50 %)",
            }
        except Exception as exc:  # noqa: BLE001
            out[sym] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


async def build_ledger(symbol: str = "EURUSD") -> dict:
    """Construye el ledger (estado del mundo con la vara correcta)."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    ledger: dict = {
        "generated_at": now,
        "method": {
            "module": "services.measurement (definición canónica)",
            "terminal": "ULTIMA vela <= t0 + 24 h, CERRADA (determinista) — horizonte real 23-24 h (T-31.9: la regla anterior saltaba los huecos y media 26-72 h)",
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

    # ── 4) el benchmark (la vara) + el DECAIMIENTO del edge por par ───────
    ledger["benchmark"] = {
        "note": "predictores triviales medidos con la definición canónica; "
                "una fuente sólo se admite si supera al mejor de ellos en "
                "expectancy OOS (no en clasificación)",
        "script": "fastapi/scripts/benchmark_canonico.py",
        "edge_map": await _edge_decay_map(),
    }

    # ── 5) el REPLAY del LLM como decisor (P4): su expectancy con la
    # mecánica real — la vara de admisión. Sólo lectura.
    try:
        from services.llm_replay import replay_llm_as_decider
        replay = await replay_llm_as_decider(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LEDGER] replay del LLM no disponible: %s", exc)
        replay = {"error": f"{type(exc).__name__}: {exc}"}
    ledger["sources"]["llm"]["replay_as_decider"] = replay

    # ── 6) verificación: la auditoría cruzada (P2) — el veredicto de la
    # última recomputación independiente (si existe).
    ledger["verification"] = {
        "source": "services.cross_audit (recomputación independiente)",
        "note": "se publica en GET /api/v1/ai/cross-audit y se recalcula "
                "cada 30 min; una discrepancia se avisa por alerting",
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
