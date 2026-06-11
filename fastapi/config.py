from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    internal_token: str
    hmac_secret: str
    mt5_http_url: str = "http://192.168.0.107:8000"
    mt5_http_timeout: float = 10.0
    risk_per_trade: float = 0.01
    max_volume: float = 0.09
    min_volume: float = 0.04
    max_open_orders_total: int = 2
    max_open_orders_per_pair: int = 1
    llm_confidence_threshold: float = 0.70
    llm_agent_timeout: float = 30.0
    news_blackout_minutes: int = 30
    atr_volatility_multiplier: float = 2.0
    atr_sl_multiplier: float = 1.5   # SL = ATR × este factor (M5)
    sl_risk_usd: float = 15.0        # riesgo máximo por operación en USD
    min_pair_score: float = 0.3      # score mínimo del par para operar
    simple_pipeline_enabled: bool = False   # flujo simple señal → orden (workflowsimple)
    signal_cooldown_minutes: int = 15       # máx 1 orden por símbolo en esta ventana
    fixed_volume: float = 0.05              # volumen fijo del flujo simple (todos los pares)
    order_max_age_hours: int = 48
    rr_min: float = 2.0              # R:R fijo 2:1 → TP siempre el doble del SL
    use_provider: str = "openrouter"
    openrouter_api_key: str = ""
    openrouter_api_url: str = "https://openrouter.ai/api/v1/chat/completions"
    openrouter_model: str = "openai/gpt-4o-mini"
    gemini_api_key: str = ""
    gemini_api_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    gemini_model: str = "gemini-2.0-flash-lite"
    groq_api_key: str = ""
    groq_api_url: str = "https://api.groq.com/openai/v1/chat/completions"
    groq_model: str = "llama-3.3-70b-versatile"

    class Config:
        env_file = "/app/.env"


settings = Settings()
