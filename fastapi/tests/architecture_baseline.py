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

# fichero::función → líneas (medido 2026-10-22, suite 1293 passed)
BIG_FUNCS = {
    "routers/ai.py::predict_direction": 1230,
    "services/auto_retrain.py::_do_retrain_core": 441,
    "ml/trading_env_v2.py::step": 373,
    "services/qwen_client.py::query_qwen_unified": 372,
    "services/cross_audit.py::run_cross_audit": 264,
    "ml/trading_env_v2.py::engineer_market_features": 207,
    "ml/train_ppo_v3.py::main": 161,
    "services/order_manager.py::prepare": 147,
    "services/daily_ledger.py::build_daily_entry": 145,
    "services/llm_replay.py::replay_llm_as_decider": 144,
    "ml/trading_env_v2.py::__init__": 139,
    "services/drift_monitor.py::run_drift_check": 138,
    "services/strategist.py::evaluate_criteria": 138,
    "services/qwen_authority.py::_measure": 132,
    "services/ledger.py::build_ledger": 126,
    "routers/smc.py::get_analytics": 124,
    "services/trade_contract.py::evaluate_trade": 115,
    "services/walk_forward.py::gate_walk_forward": 115,
    "main.py::lifespan": 112,
    "services/auto_retrain.py::record_trade_filled": 110,
    "services/auto_retrain.py::_evaluate_model": 108,
    "services/trade_journal.py::_query_qwen_trade_insight": 106,
    "routers/ai.py::_higher_tf_context_line": 100,
    "services/promotion_rule.py::evaluate_drastic_improvement": 97,
    "services/qwen_client.py::query_qwen_sltp_validator": 92,
    "services/ledger.py::_edge_decay_map": 86,
    "routers/ai.py::pipeline_health": 85,
    "services/market_microstructure.py::compute_volume_profile": 84,
    "services/auto_retrain.py::_do_retrain": 83,
    "routers/ai.py::trade_filled_webhook": 80,
}

# fichero → ocurrencias de `except Exception` (medido 2026-10-22: 82 en total)
EXCEPT_EXCEPTION = {
    "main.py": 7,
    "ml/trading_env_v2.py": 2,
    "ml/train_ppo.py": 1,
    "ml/train_ppo_v3.py": 1,
    "routers/ai.py": 6,
    "routers/smc.py": 5,
    "services/alerting.py": 2,
    "services/alignment_metric.py": 1,
    "services/auto_retrain.py": 2,
    "services/cross_audit.py": 9,
    "services/daily_ledger.py": 7,
    "services/drift_monitor.py": 4,
    "services/equity_curve.py": 1,
    "services/ledger.py": 4,
    "services/llm_replay.py": 1,
    "services/market_microstructure.py": 3,
    "services/model_backup.py": 6,
    "services/news_filter.py": 1,
    "services/news_scraper.py": 1,
    "services/order_manager.py": 3,
    "services/position_ownership.py": 1,
    "services/predict_silence.py": 2,
    "services/qwen_authority.py": 2,
    "services/risk_guardian.py": 1,
    "services/risk_state_store.py": 2,
    "services/structured_logging.py": 1,
    "services/timebase.py": 2,
}
