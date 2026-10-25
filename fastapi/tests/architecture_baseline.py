"""Línea base CONGELADA de deuda arquitectónica — medida el 2026-10-22.

NO es una lista de permiso: es un trinquete. `test_architecture_rules.py`
exige que la deuda NUNCA crezca:

  · ninguna función NUEVA de 80+ líneas,
  · ninguna función existente puede alargarse,
  · ningún `except Exception` nuevo ni más de los que ya hay por fichero,
  · ninguna violación de capas nueva.

Ámbito: código de ejecución (`main.py`, `config.py`, `routers/`, `services/`,
`core/`, `ml/` sin `ml/archive/`). Tests y `scripts/` quedan fuera a
propósito: son herramientas, no runtime.

Cuando una entrada desaparezca (refactor o borrado), se BORRA de aquí: la
deuda sólo puede encoger. Añadir una entrada es una decisión consciente que
debe justificarse en el commit y en README §9.
"""

# fichero::función → líneas (medido 2026-10-22, suite 1276 passed + 2 xfailed)
BIG_FUNCS = {
    # H-I (2026-10-22): 1230 → 994 tras extraer trade_setup y veto_chain.
    "routers/ai.py::predict_direction": 995,
    "services/auto_retrain.py::_do_retrain_core": 441,
    "ml/trading_env_v2.py::step": 373,
    "services/qwen_client.py::query_qwen_unified": 372,
    "services/cross_audit.py::run_cross_audit": 267,
    "ml/trading_env_v2.py::engineer_market_features": 207,
    "ml/train_ppo_v3.py::main": 161,
    "services/order_manager.py::prepare": 147,
    "services/daily_ledger.py::build_daily_entry": 157,
    "services/llm_replay.py::replay_llm_as_decider": 144,
    "ml/trading_env_v2.py::__init__": 139,
    "services/drift_monitor.py::run_drift_check": 138,
    "services/strategist.py::evaluate_criteria": 138,
    "services/qwen_authority.py::_measure": 135,
    "services/ledger.py::build_ledger": 126,
    "services/ledger.py::_edge_decay_map": 90,
    "routers/smc.py::get_analytics": 124,
    "services/trade_contract.py::evaluate_trade": 115,
    "services/walk_forward.py::gate_walk_forward": 115,
    "main.py::lifespan": 112,
    "services/auto_retrain.py::record_trade_filled": 125,
    "services/auto_retrain.py::_evaluate_model": 108,
    "services/trade_journal.py::_query_qwen_trade_insight": 106,
    "routers/ai.py::_higher_tf_context_line": 105,
    "services/promotion_rule.py::evaluate_drastic_improvement": 97,
    "services/qwen_client.py::query_qwen_sltp_validator": 92,
    "routers/ai.py::pipeline_health": 95,
    "services/market_microstructure.py::compute_volume_profile": 84,
    "services/auto_retrain.py::_do_retrain": 83,
    "routers/ai.py::trade_filled_webhook": 122,
    # P1.2b (2026-10-25): trade_filled_webhook y record_trade_filled crecen
    # al aceptar costes de ejecución (entry_price, commission, fee, slippage,
    # quote_bid/ask, quote_time) y calcular slippage.
    # B6.2 (2026-10-25): broker_offset_hours crece al aceptar market_port y
    # prescindir de httpx directo; build_daily_entry crece al obtener el
    # puerto del registro cuando no se inyecta.
    "services/timebase.py::broker_offset_hours": 82,
}

# fichero → ocurrencias de `except Exception` (medido 2026-10-22: 0 tras H-K).
# La captura genérica se sustituyó por `services/error_taxonomy.py`:
# DB / HTTP / PARSE / MODEL / LOOP. Los bugs (AttributeError, NameError,
# ImportError, AssertionError) ya NO se degradan.
EXCEPT_EXCEPTION = {}
