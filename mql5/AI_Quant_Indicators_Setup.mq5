/*
  AI_Quant_Indicators_Setup.mq5 (v1.1 — 2026-10-21, corrección compilación)
  Script que añade los indicadores del contrato ForexTradingEnv v3 (20,25) H1.
  No modifica el EA (AI_Quant_Terminal.mq5 v14.0) ni opera de forma autónoma.
  Uso: abrir gráfico H1 del par, arrastrar este archivo y ejecutar (F5 / Play).
*/
#property strict
#property script_show_inputs

input string   SymbolFilter_ = "";        // Vacío = símbolo del gráfico actual
input int      Timeframe_   = PERIOD_H1; // H1 = contrato del sistema

// Colores visuales alineados con el contrato (referencia, no obligatorio)
#define C_SMA20     C'0,123,186'
#define C_MACD     C'34,139,34'
#define C_MACD_SIG C'220,20,60'
#define C_MACD_HIST C'128,128,128'
#define C_ATR      C'255,127,14'
#define C_RSI      C'148,103,189'
#define C_ADX      C'44,160,44'
#define C_SMA200   C'214,39,40'
#define C_BB_UP    C'169,169,169'
#define C_BB_DOWN  C'169,169,169'
#define C_BB_MID   C'31,119,180'

void OnStart()
{
   string sym = (StringLen(SymbolFilter_) > 0) ? SymbolFilter_ : Symbol();
   int tf_int = (Timeframe_ == PERIOD_CURRENT) ? (int)PERIOD_H1 : (int)Timeframe_;

   long chart_id = ChartID();
   if(chart_id == 0)
   {
      Alert("No se encontró gráfico activo. Abre el gráfico del par y vuelve a ejecutar.");
      return;
   }

   // 1. SMA20 (base contrato: sma20, dist_sma20, bb_pos)
   int h_sma20 = (int)iMA(sym, (ENUM_TIMEFRAMES)tf_int, 20, 0, MODE_SMA, PRICE_CLOSE);
   if(h_sma20 == INVALID_HANDLE) Print("[AI_Setup] SMA20: INVALID_HANDLE");
   ChartIndicatorAdd((long)chart_id, 0, (int)h_sma20);

   // 2. MACD — EMA12 / EMA26 / Señal EMA9 (macd, macd_signal, macd_hist)
   int h_macd = (int)iMACD(sym, (ENUM_TIMEFRAMES)tf_int, 12, 26, 9, PRICE_CLOSE);
   if(h_macd == INVALID_HANDLE) Print("[AI_Setup] MACD: INVALID_HANDLE");
   ChartIndicatorAdd((long)chart_id, 0, (int)h_macd);

   // 3. ATR14 (volatilidad / production_stops / trailing EA)
   int h_atr = (int)iATR(sym, (ENUM_TIMEFRAMES)tf_int, 14);
   if(h_atr == INVALID_HANDLE) Print("[AI_Setup] ATR14: INVALID_HANDLE");
   ChartIndicatorAdd((long)chart_id, 1, (int)h_atr);

   // 4. RSI14 (oscilador normalizado en obs)
   int h_rsi = (int)iRSI(sym, (ENUM_TIMEFRAMES)tf_int, 14, PRICE_CLOSE);
   if(h_rsi == INVALID_HANDLE) Print("[AI_Setup] RSI14: INVALID_HANDLE");
   ChartIndicatorAdd((long)chart_id, 1, (int)h_rsi);

   // 5. ADX14 (tendencia / bloque T-22 compartido PPO↔LLM)
   int h_adx = (int)iADX(sym, (ENUM_TIMEFRAMES)tf_int, 14);
   if(h_adx == INVALID_HANDLE) Print("[AI_Setup] ADX14: INVALID_HANDLE");
   ChartIndicatorAdd((long)chart_id, 1, (int)h_adx);

   // 6. SMA200 (estructura largo plazo / contexto Qwen / dist_sma200, slope)
   int h_sma200 = (int)iMA(sym, (ENUM_TIMEFRAMES)tf_int, 200, 0, MODE_SMA, PRICE_CLOSE);
   if(h_sma200 == INVALID_HANDLE) Print("[AI_Setup] SMA200: INVALID_HANDLE");
   ChartIndicatorAdd((long)chart_id, 0, (int)h_sma200);

   // 7. Bollinger Bands — SMA20, desviación 2.5 (bb_pos)
   int h_bb = (int)iBands(sym, (ENUM_TIMEFRAMES)tf_int, 20, 2.5, 0, PRICE_CLOSE);
   if(h_bb == INVALID_HANDLE) Print("[AI_Setup] BB(20,2.5): INVALID_HANDLE");
   ChartIndicatorAdd((long)chart_id, 0, (int)h_bb);

   // 8. Swing aproximación (fractal 2 vecinos) — referencia visual al swing_window=2
   int h_fract = (int)iFractals(sym, (ENUM_TIMEFRAMES)tf_int);
   if(h_fract == INVALID_HANDLE) Print("[AI_Setup] Fractals (Swing aproximado): INVALID_HANDLE");
   ChartIndicatorAdd((long)chart_id, 0, (int)h_fract);

   PrintFormat("[AI_Quant_Setup] Completado en gráfico %I64d (%s, TF=%s): SMA20 | MACD(12,26,9) | ATR14 | RSI14 | ADX14 | SMA200 | BB(20,2.5) | Fractals(Swing)",
               chart_id, sym, EnumToString((ENUM_TIMEFRAMES)tf_int));
   Alert("AI_Quant_Setup completado. Gráfico: ", sym, " | TF: ", EnumToString((ENUM_TIMEFRAMES)tf_int),
         "\nIndicadores: SMA20, MACD 12/26/9, ATR14, RSI14, ADX14, SMA200, BB 20/2.5, Fractals.");
}
