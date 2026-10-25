from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    internal_token: str
    hmac_secret: str
    mt5_http_url: str = "http://localhost:8000"
    mt5_http_timeout: float = 10.0
    # B1.1 — Tipo de broker seleccionable por variable de entorno.
    # mt5_demo = cuenta demo actual (comportamiento por defecto).
    # mt5_real = cuenta real; se cambia solo editando .env, sin tocar código.
    broker_type: Literal["mt5_demo", "mt5_real", "mock"] = "mt5_demo"
    # URL del broker. Vacío = usar mt5_http_url (comportamiento actual).
    broker_http_url: str = ""
    broker_timeout: float = 10.0
    min_volume: float = 0.01
    max_volume: float = 0.50
    max_total_positions: int = 5   # P2-4: exposición agregada de TODA la cuenta (todas las estrategias y pares)
    # G2-5 (orden Félix 2026-10-17, canario 0.09 vía .env): tope de RIESGO-1R
    # AGREGADO en EUR — el conteo no distingue 0.01 de 0.50 lot, esta capa sí.
    # None = desactivado (comportamiento previo byte-idéntico).
    max_total_risk_pct: float | None = None
    # G2-5/D3 (canario 4 vía .env): tope de posiciones MISMA DIRECCIÓN en la
    # cuenta — corta el apalancamiento del factor USD compartido entre pares
    # correlacionados (EURUSD/GBPUSD ρ≈0.7-0.9). 0 = desactivado.
    max_total_same_dir_positions: int = 0
    # 2026-10-21 (incidente EURUSD/USDCHF): guard de exposición USD. Si True,
    # rechaza una orden nueva cuyo signo USD sea contrario al de cualquier
    # posición abierta (incluidas las de otro magic/estrategia). Default
    # False para no cambiar entornos ni tests; el .env vivo lo activa con
    # USD_EXPOSURE_GUARD=1. No es una señal: no toca dirección, tamaño ni
    # bundle.
    usd_exposure_guard: bool = False
    # C1 (orden Félix 2026-10-20, informe de viabilidad): el riesgo por
    # operación se puede reducir con la env PPO_RISK_PCT (el default del
    # código conserva el valor histórico 0.03). Motivo (medido):
    #   · el edge es FINO (ratio 1,23:1) y NO está validado fuera de
    #     muestra (el holdout del bundle vivo da −0,53 €/operación);
    #   · una operación puede perder −22,30 € (4,4 % del capital) y hubo
    #     una racha de 11 pérdidas seguidas y un drawdown del 30,2 %
    #     (154,75 €) sobre una cuenta de 504 €;
    #   · bajar el tamaño NO mejora el ratio (el drawdown % es una propiedad
    #     de la secuencia de operaciones, no del tamaño) ⇒ lo que se
    #     protege con esto es el CAPITAL en euros, no el ratio.
    # Al validarse la evidencia (150 operaciones fuera de muestra con
    # p<0,05 por tramo), se devuelve PPO_RISK_PCT a 0.03.
    ppo_risk_pct: float = 0.03        # P2-2: tope de riesgo por trade del PPO (fracción del equity)
    order_max_age_hours: int = 48
    # G3-2 (2026-10-21): MÍNIMO DE PROTECCIÓN en € del trailing "Safe-4".
    # El EA (input `SL_LockSafetyNetEur`, v14.0) sólo mueve el SL a bloquear
    # el beneficio si el RIESGO EN € de la posición es ≥ este valor; con un
    # lote pequeño el trailing queda APAGADO (el EA hace `return` antes de
    # tocar el stop) y la posición nunca asegura nada. Por eso el backend
    # bloquea la entrada si el riesgo real de la operación no llega aquí
    # ("se protege o no se opera"). El valor espeja el input del EA: si
    # cambias el input en MT5, cámbialo aquí también.
    min_lock_eur: float = 4.0
    news_filter_enabled: bool = True
    news_blackout_minutes: int = 15

    # ─── Auto-Retrain ───────────────────────────────────────────────────────────
    retrain_enabled: bool = True          # Habilitar reentrenamiento automático
    trades_before_retrain: int = 50       # N trades antes de disparar retrain
    min_trades_for_retrain: int = 20     # Mínimo de trades para considerar datos suficientes
    retrain_auto_approve: bool = False    # P0-1: promoción automática nunca implícita; aprobar vía POST /api/v1/ai/retrain/approve
    retrain_interval_hours: int = 168     # P3-2d: retrain programado (7 días desde el último intento)

    # T-30 (orden Félix 2026-10-17, «ejecución del candidato H4/D1»): único
    # canario del contrato del CANDIDATO — con true, el retrain une los 4
    # canales H4/D1 (join asof de vela cerrada) y entrena con obs (20,32)
    # (trend + HF + memoria); con false (default) el pipeline es byte-idéntico
    # al vivo (20,25). Serve NUNCA lee este flag: allí el contrato lo decide
    # el ancho del bundle (derive_obs_contract). Sólo para el run
    # pre-registrado report-only (audits/t30-pre-registration §4).
    higher_tf_features: bool = False

    # G3 (2026-10-21): tope de posiciones ABIERTAS por par. 1 = la política
    # (una operación a la vez por par de divisas). El guardián de riesgo lo
    # aplica contra el conteo REAL del MT5 ⇒ el backend es la autoridad,
    # no la telemetría del EA. Antes era 5 (la política no la aplicaba
    # nadie). 0 = sin tope por par (desactivado).
    max_positions_per_pair: int = 1

    # G3 (2026-10-20, orden Félix «arranca USDCHF»): único canario del
    # contrato del CANDIDATO con el bloque de TENDENCIA (que desde G3 incluye
    # `lag_return_24`, el momentum de 24 h — la señal con skill medido en
    # USDCHF: +7,1 pp, n=481, p=0,001). Con true, el retrain entrena con
    # obs (20,29) (17+4 market + 8 state). Con false (default) el pipeline
    # es byte-idéntico al vivo (20,25). Serve NUNCA lee este flag: allí el
    # contrato lo decide el ancho del bundle (derive_obs_contract).
    # Nota: la reward `v3_edge` premia la dirección a +24 h
    # (REWARD_V3_DIRECTION_HORIZON_BARS=24) ⇒ sin esta feature el agente
    # estaba graduado en una magnitud que no veía.
    retrain_trend_features: bool = False

    # ─── P3-2b: Backup del bundle ─────────────────────────────────────────────
    model_backup_enabled: bool = True         # GCS además del backup local verificado
    model_backup_interval_hours: int = 24     # Cadencia del backup programado

    # ─── P3-2c: Monitor de drift ─────────────────────────────────────────────
    drift_check_interval_hours: int = 24      # Cadencia del check PSI + decay

    # ─── P3-1: Pares habilitados (mundos independientes, sin acoplamiento) ─────
    # Cada par construye su propio PairContext (modelo, riesgo, estado) vía
    # core.container — añadir un par NO toca el código de otro. 'enabled' es
    # obligatorio en cada especificación: listar un par nunca lo activa solo.
    # P3-3: GBPUSD canario listado pero NO habilitado — se entrena con el
    # gate canario y solo se activa (enabled=true) con el gate en verde
    # (límites del canario: daily_loss 2%, equity 20%).
    # Env (JSON): PAIRS='[{"symbol":"EURUSD","enabled":true},{"symbol":"GBPUSD","enabled":true}]'
    pairs: list[dict] = [
        {"symbol": "EURUSD", "enabled": True},
        {"symbol": "GBPUSD", "enabled": False,
         "daily_loss_pct": 2.0, "equity_allocation_pct": 20},
        # G3 · U-1 (2026-10-20, orden Félix «sí arranca USDCHF pero con
        # cabeza»): USDCHF queda LISTADO como canario APAGADO. Motivo medido
        # (definición canónica corregida, skill = exactitud − SU PROPIA BASE):
        # es el ÚNICO par de los 7 con skill real — momentum 24 h +7,1 pp
        # (n=481, IC 95 % [+2,6,+11,4], p=0,001 con 20 000 barajados); su
        # mecánica encaja con la cuenta (ATR H1 11,0 pips, spread 1,8 % del
        # ATR) y es una apuesta distinta (−0,69 de correlación con EURUSD).
        # Registrar NO habilita: el canario exige un bundle que pase el
        # gate + la condición documental A2 (doc/backlog-g3-usdchf-2026-10-20.md).
        # G3 · U-8 (2026-10-20, orden Félix «quiero el USDCHF con su modelo
        # bien reentrenado, funcionando y activo para que mañana pueda
        # comenzar a operar»): el decisor de USDCHF es la REGLA medida
        # (momentum de 24 h: +7,1 pp frente a su base, n=481, IC
        # [+2,6,+11,4], p=0,001) — el PPO NO aprende ese edge (4
        # experimentos, el gate los rechaza) ⇒ línea E4 del punto de
        # inflexión T-31: el poder va a quien tiene el edge medido.
        # Límites de canario: DD diario 2 %, 20 % del equity.
        {"symbol": "USDCHF", "enabled": False,
         "decision_source": "momentum24",
         "daily_loss_pct": 2.0, "equity_allocation_pct": 20},
    ]
    # G3 (2026-10-21, orden Félix «el sistema ha abierto una segunda
    # operación en USDCHF — eso contradice la política de una operación a
    # la vez por par»): el tope de posiciones por par es **1** (la política),
    # no 5. Antes el backend NO la aplicaba (el tope por par era 5) y la
    # política dependía del recuento del EA, que es frágil: el EA cuenta
    # con `PositionSelect(Symbol())` (UNA posición) y luego compara el
    # magic ⇒ en una cuenta en modo hedging, si el ticket más bajo del
    # símbolo no es el suyo, `currentPos` queda en 0 ⇒ el veto de posición
    # (que viaja en el payload del EA) no dispara y se abre una segunda.
    # Ahora la autoridad es el BACKEND (el conteo del MT5, que sí es
    # correcto) y el EA además cuenta bien (v13.9).

    # ─── P3-3: bloqueo por drawdown de cuenta (canario = 15%) ───────────────────
    # None (default) = solo alerta al 10% DD (comportamiento histórico).
    # Valor (p.ej. 15.0) = el guardian BLOQUEA aperturas nuevas cuando el
    # equity cae ≥ ese % desde el peak. Es de cuenta: aplica a todos los pares.
    max_drawdown_block_pct: float | None = None

    # ─── P0 / H1: gate de confianza de entrada (Félix 2026-10-09) ─────────────
    # Umbral mínimo de `effective_prob` (ml_prob × confidence_modifier) para
    # adelantar una propuesta BUY/SELL. 0.0 (default) = MECANISMO DESARMADO
    # (no-op: toda propuesta pasa, cero cambio de comportamiento).
    # Evidencia EN CONTRA pre-registrada en README (muestra n=18 de trades
    # 229–250): las bandas de confianza son INVERSIAS (<0.5 = +4.58 €/trade;
    # ≥0.7 = −1.89 €/trade) — un gate armado habría restado dinero esa
    # semana. Armar SOLO con (a) revisión N≥50 trades post-fix Qwen que lo
    # justifique, o (b) orden explícita de Félix (env MIN_ENTRY_CONFIDENCE).
    min_entry_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # ─── LR-1: skip de Qwen con posición abierta (Félix 2026-10-14) ──────────
    # Con posición abierta el juicio del LLM es decisionalmente neutro
    # (entradas vetadas por position_rejected en backend+EA, ventana de
    # reentrada apagada, CLOSE del PPO y time-stop sin veto LLM) → la llamada
    # al 8B (~40 s) sólo consumía GPU: 59 % de los ciclos de 7 días. true
    # (default) = stub neutro + traza `qwen_skipped` en audit; el ciclo
    # (PPO+dead-zone+time-stop+vetos+audit) sigue completo. Rollback
    # operativo sin redeploy: QWEN_SKIP_WHILE_OPEN=false.
    qwen_skip_while_open: bool = True

    # ─── T-07: breaker del feed H4/D1 (Félix 2026-10-15) ─────────────────────
    # El incidente MCP 14/10 mostró que el bug vive en el binding: la 1ª
    # petición H4/D1 al servidor wedgado vuelve a tumbar su sesión (cada
    # ciclo re-mataba el MCP). Tras el PRIMER fallo de fetch H4/D1, los
    # ciclos /predict omiten H4/D1 durante estos minutos (half-open: sonda
    # única al expirar) — la línea del prompt queda «Unavailable» (esquema
    # constante) y el MCP respira para los críticos equity/H1. Env:
    # H4D_BREAKER_COOLDOWN_MINUTES.
    h4d_breaker_cooldown_minutes: int = Field(default=30, ge=1, le=240)

    # ─── T-10: memoria a largo plazo (Félix 2026-10-15) ──────────────────────
    # Inyección DETERMINISTA (memory como dato, no como tool): el backend
    # consulta memory_api (LanceDB+fastembed, T5.4) directamente — nunca vía
    # el proxy :8090 (paridad 2026-10-15: 0 tool_calls en json_mode + system
    # inyectado = deriva). Modos: off (sin fetch, default) | shadow (fetch +
    # candidatos trazados en audit `memory`, CERO inyección — patrón D-5) |
    # on (inyección real del bloque `Relevant memories` tras las velas).
    # Kill switch sin redeploy: MEMORY_INJECTION_MODE=off. Diseño congelado
    # en backlog T-10 (categorías, umbral ≤0.4, top-K 3, fail-open 2 s).
    memory_api_url: str = "http://100.90.16.33:8095"
    memory_injection_mode: Literal["off", "shadow", "on"] = "off"

    # ─── Parche del agujero del feed (Félix 2026-10-12) ──────────────────────
    # El feed del MCP corta la historia en 2025-12-31 y sólo entrega el día
    # vivo (agujero 2026-01-01→10-11). Los CSV de `resultadosBT/` (MT5, UTC
    # puro) son la única fuente de 2026. Directorio con esos CSV; vacío =
    # parche desactivado (el feed pasa tal cual).
    csv_history_dir: str = ""

    # ─── Market Microstructure ────────────────────────────────────────────────
    microstructure_features_enabled: bool = False  # Añade VP + Orderbook al predict (requiere retrain del modelo si se activa)
    volume_profile_bins: int = 50         # Niveles de precio para Volume Profile
    volume_profile_lookback: int = 20     # Velas para calcular VP
    orderbook_levels: int = 5             # Niveles de profundidad para Orderbook Imbalance

    # ─── T-09 (Félix 2026-10-16): retrain report-only (no-promote) ─────────
    # El holdout congelado se reporta siempre, pero con report-only activo NO
    # se promociona SIEMPRE (cualquier gate.ok queda registrado como traza, sin
    # escritura de bundle). Habilidad de salir del modo (env RE领导班子) y
    # permitir promoción real es una Orden explícita.
    retrain_report_only: bool = False

    # Orden Félix 2026-10-19: «quiero que cuando el candidato sea
    # drásticamente mejor que el modelo vivo, se promocione el drásticamente
    # mejor». Vía de promoción ADICIONAL (services/promotion_rule.py): exige
    # que el candidato domine al modelo vivo en 8 criterios congelados
    # (4/4 folds, sharpe de holdout > 0, drawdown ≤ 10 % y ≤ la mitad del
    # vivo, ≤ la mitad de operaciones, reward y expectativa melhores) — con
    # suelo absoluto de riesgo ⇒ no es una puerta trasera.
    # El switch es explícito y separado: el congelamiento report-only sigue
    # gobernando la vía NORMAL (suelo absoluto) sin cambios.
    retrain_promote_drastic: bool = False

    # L1 (2026-10-20) — umbrales de la MEDICIÓN CANÓNICA (services/measurement).
    # "Lo no medido no tiene poder": sin muestra madura, la autoridad se
    # revoca; y "skill demostrada" exige distinguirse DEL AZAR (límite
    # inferior de Wilson > 50 %), no sólo no ser peor.
    measurement_min_samples: int = 30
    measurement_min_acc: float = 0.55

    class Config:
        env_file = "/app/.env"


settings = Settings()
