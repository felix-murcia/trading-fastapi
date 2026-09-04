/*
AI_Quant_Terminal_v3.mq5
========================
EA que soporta predicción AUTÓNOMA del modelo PPO v3.

Cuando el servidor FastAPI responde con campos desde el modelo v3:
  - volume    → usa ese lote directamente (sin CalculateLots)
  - sl_pips   → usa esa distancia de SL directamente
  - tp_pips   → usa esa distancia de TP directamente

Cuando NO hay campos v3 (modelo v2 legacy), cae back a los inputs estáticos
de RiskPercent / StopLossPips / TakeProfitPips.

Cambios vs v10.1:
  - JSON parsing extensible para volume/sl_pips/tp_pips/model_version
  -Uso directo de lot/SL/TP del modelo cuando están disponibles
  - Backwards compatible con respuestas v2 (sin esos campos)
*/
#property copyright "AI Quant Terminal v3"
#property version   "11.0"
#property strict

#include <Trade\Trade.mqh>

//--- Forward Declarations
void ManageOpenPositions();
double CalculateLots(double slDistancePrice);
bool IsCircuitBreakerOpen();
void CloseAllPositions();
bool HasActivePosition();
void NotifyTradeClosed(ulong ticket, string comment,
                       double entryPrice, double closePrice, double volume,
                       double pnl, long posType, datetime entryTime);
void NotifyTradeOpened(string direction, double entryPrice);

//--- Macros MQL5
#define IsTesting() ((bool)MQLInfoInteger(MQL_TESTER))

//--- Retry & Circuit Breaker
#define MAX_RETRIES           3
#define BASE_RETRY_DELAY_MS   1000    // 1 segundo base
#define MAX_CIRCUIT_BREAKERS  5       // Max errors before cooldown
#define CIRCUIT_BREAK_COOLDOWN 300     // 5 minutos de cooldown

input group "=== AI Brain Server ==="
input string FastAPI_URL = "http://YOUR_FASTAPI_IP:8090"; // IP del servidor FastAPI
input string InternalToken = "YOUR_INTERNAL_TOKEN_HERE"; // Setear en MT5 Inputs o variable env
input bool EnableAI= true;

input group "=== Position Sizing (Legacy / Fallback v2) ==="
input double RiskPercent = 5.0;       // % equity por trade (fallback si no hay modelo v3)
input int StopLossPips = 150;        // SL fallback (pips)
input int TakeProfitPips = 300;       // TP fallback (pips)
input bool UseATRForSL = true;       // Usar ATR en vez de StopLossPips fijo
input int ATRPeriod = 14;

input group "=== Autonomous Mode (v3 Model) ==="
input bool UseModelRiskParams = true; // Si true: usa volume/sl/tp del modelo directamente

input group "=== Trailing Stop ==="
input double TrailingPercent = 0.25;
input int TrailingMinPips = 15;
input int TrailingATRMult = 2;

input group "=== Terminal Management ==="
input ulong MagicNumber = 90001;

CTrade trade;
datetime lastCheckedBar = 0;
datetime lastManagedPositions = 0;

//--- Circuit Breaker State
datetime g_circuitBreakerReset = 0;
int g_consecutiveErrors = 0;

int OnInit()
  {
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(30);
   trade.SetTypeFillingBySymbol(Symbol());
   Print("[AI Terminal v3] Iniciado. Autonomous=", UseModelRiskParams);
   return INIT_SUCCEEDED;
  }

void OnTick()
  {
   datetime now = TimeCurrent();
   if(now - lastManagedPositions >= 30) {
      ManageOpenPositions();
      lastManagedPositions = now;
   }

   datetime currentBar = iTime(Symbol(), PERIOD_CURRENT, 0);
   if(currentBar == lastCheckedBar) return;
   lastCheckedBar = currentBar;
   
   if(!IsTesting() && EnableAI)
     {
      if(IsCircuitBreakerOpen())
        {
         static datetime lastCircuitLog = 0;
         if(TimeCurrent() - lastCircuitLog > 60) {
            PrintFormat("[AI Terminal v3] Circuit Breaker ACTIVO. Esperando %d segundos...", CIRCUIT_BREAK_COOLDOWN);
            lastCircuitLog = TimeCurrent();
         }
         return;
        }

      string url = FastAPI_URL + "/api/v1/ai/predict";
      char postData[], result[];
      string headers = "Content-Type: application/json\r\n"
                       "X-Internal-Token: " + InternalToken + "\r\n";
      
      int current_pos = 0;
      if(PositionsTotal() > 0) {
         for(int i = PositionsTotal() - 1; i >= 0; i--) {
            ulong t = PositionGetTicket(i);
            if(PositionGetString(POSITION_SYMBOL) == Symbol() && PositionGetInteger(POSITION_MAGIC) == MagicNumber) {
               current_pos = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : 2;
               break;
            }
         }
      }
      string body = StringFormat("{\"symbol\":\"%s\",\"timeframe\":\"H1\",\"position\":%d}", Symbol(), current_pos);
      StringToCharArray(body, postData, 0, StringLen(body));
      ArrayResize(postData, StringLen(body));

      string responseHeaders;
      int res = -1;
      bool success = false;

      for(int attempt = 0; attempt < MAX_RETRIES && !success; attempt++) {
         if(attempt > 0) Sleep(BASE_RETRY_DELAY_MS * attempt);
         res = WebRequest("POST", url, headers, 30000, postData, result, responseHeaders);
         success = (res == 200);
      }

      if(res == 200)
        {
         g_consecutiveErrors = 0;
         string r = CharArrayToString(result);
         PrintFormat("[AI Terminal v3] Respuesta del Cerebro: %s", r);
         
         // ───────────────────────────────────────────────────────────────────
         // PARSE v3 AUTONOMOUS PARAMETERS desde JSON
         // Se extraen del JSON si el modelo v3 los incluyó en la respuesta
         // ───────────────────────────────────────────────────────────────────
         double modelLot = 0.0;
         double modelSlPips = 0.0;
         double modelTpPips = 0.0;
         bool hasModelParams = false;
         
         if(UseModelRiskParams)
           {
            // Intentar extraer "volume" (lote directo del modelo)
            string volumeToken = "\"volume\":";
            int volIdx = StringFind(r, volumeToken);
            if(volIdx >= 0) {
               string volSub = StringSubstr(r, volIdx + StringLen(volumeToken));
               int endVol = StringFind(volSub, ",");
               if(endVol < 0) endVol = StringFind(volSub, "}");
               if(endVol > 0) {
                  string volStr = StringSubstr(volSub, 0, endVol);
                  modelLot = StringToDouble(volStr);
               }
            }
            
            // Extraer "sl_pips"
            string slToken = "\"sl_pips\":";
            int slIdx = StringFind(r, slToken);
            if(slIdx >= 0) {
               string slSub = StringSubstr(r, slIdx + StringLen(slToken));
               int endSl = StringFind(slSub, ",");
               if(endSl < 0) endSl = StringFind(slSub, "}");
               if(endSl > 0) {
                  string slStr = StringSubstr(slSub, 0, endSl);
                  modelSlPips = StringToDouble(slStr);
               }
            }
            
            // Extraer "tp_pips"
            string tpToken = "\"tp_pips\":";
            int tpIdx = StringFind(r, tpToken);
            if(tpIdx >= 0) {
               string tpSub = StringSubstr(r, tpIdx + StringLen(tpToken));
               int endTp = StringFind(tpSub, ",");
               if(endTp < 0) endTp = StringFind(tpSub, "}");
               if(endTp > 0) {
                  string tpStr = StringSubstr(tpSub, 0, endTp);
                  modelTpPips = StringToDouble(tpStr);
               }
            }
            
            // Si tenemos los 3 parámetros del modelo → modo autónomo
            if(modelLot > 0.0 && modelSlPips > 0.0 && modelTpPips > 0.0) {
               hasModelParams = true;
               PrintFormat("[AI Terminal v3] MODO AUTÓNOMO: lot=%.2f sl=%.1f tp=%.1f",
                           modelLot, modelSlPips, modelTpPips);
            }
           }
         
         // ───────────────────────────────────────────────────────────────────
         // CALCULAR PARÁMETROS DE TRADE
         // ───────────────────────────────────────────────────────────────────
         double slPips, tpPips, lot;
         double slDistance, tpDistance;
         double p = SymbolInfoDouble(Symbol(), SYMBOL_POINT);
         int dig = (int)SymbolInfoInteger(Symbol(), SYMBOL_DIGITS);
         
         if(hasModelParams)
           {
            // ── MODO AUTÓNOMO (modelo v3) ───────────────────────────────────
            slPips = modelSlPips;
            tpPips = modelTpPips;
            lot    = modelLot;
            slDistance = slPips * p;
            tpDistance = tpPips * p;
           }
         else
           {
            // ── MODO LEGACY (modelo v2 o sin parámetros v3) ─────────────────
            slPips = StopLossPips;
            if(UseATRForSL) {
               double atrArr[];
               int atrHandle = iATR(Symbol(), PERIOD_CURRENT, ATRPeriod);
               if(CopyBuffer(atrHandle, 0, 0, 1, atrArr) > 0) {
                  slPips = MathMax(atrArr[0] / p * 1.5, StopLossPips);
               }
               IndicatorRelease(atrHandle);
            }
            tpPips = TakeProfitPips;
            slDistance = slPips * p;
            tpDistance = tpPips * p;
            lot = CalculateLots(slDistance);
           }
         
         PrintFormat("[AI Terminal v3] SL: %.0f pips, TP: %.0f pips, Lote: %.2f", slPips, tpPips, lot);
         
         // ───────────────────────────────────────────────────────────────────
         // DECISIÓN Y EJECUCIÓN
         // ───────────────────────────────────────────────────────────────────
         if(StringFind(r, "\"decision\":\"CLOSE\"") >= 0)
           {
            Print("[AI Terminal v3] Cerebro indica CIERRE de posiciones.");
            CloseAllPositions();
           }
         else if(StringFind(r, "\"decision\":\"BUY\"") >= 0)
           {
            double entry = SymbolInfoDouble(Symbol(), SYMBOL_ASK);
            if(current_pos == 2) CloseAllPositions();
            if(current_pos != 1) {
               bool buyOk = trade.Buy(lot, Symbol(), entry,
                                      NormalizeDouble(entry - slDistance, dig),
                                      NormalizeDouble(entry + tpDistance, dig),
                                      "AI Predict BUY v3[sl][tp]");
               PrintFormat("[AI Terminal v3] BUY — entry=%.5f lot=%.2f SL=%.5f TP=%.5f → %s",
                           entry, lot, NormalizeDouble(entry - slDistance, dig),
                           NormalizeDouble(entry + tpDistance, dig),
                           buyOk ? "OK" : "FALLO");
               if(buyOk) NotifyTradeOpened("BUY", entry);
            }
           }
         else if(StringFind(r, "\"decision\":\"SELL\"") >= 0)
           {
            double entry = SymbolInfoDouble(Symbol(), SYMBOL_BID);
            if(current_pos == 1) CloseAllPositions();
            if(current_pos != 2) {
               bool sellOk = trade.Sell(lot, Symbol(), entry,
                                       NormalizeDouble(entry + slDistance, dig),
                                       NormalizeDouble(entry - tpDistance, dig),
                                       "AI Predict SELL v3[sl][tp]");
               PrintFormat("[AI Terminal v3] SELL — entry=%.5f lot=%.2f SL=%.5f TP=%.5f → %s",
                           entry, lot, NormalizeDouble(entry + slDistance, dig),
                           NormalizeDouble(entry - tpDistance, dig),
                           sellOk ? "OK" : "FALLO");
               if(sellOk) NotifyTradeOpened("SELL", entry);
            }
           }
        }
      else
        {
         g_consecutiveErrors++;
         if(g_consecutiveErrors >= MAX_CIRCUIT_BREAKERS)
           {
            g_circuitBreakerReset = TimeCurrent() + CIRCUIT_BREAK_COOLDOWN;
           }
         PrintFormat("[AI Terminal v3] Error HTTP %d. Intentos fallidos: %d", res, g_consecutiveErrors);
        }
     }
  }

//+------------------------------------------------------------------+
//| Lot Calculator (legacy fallback)                                  |
//+------------------------------------------------------------------+
double CalculateLots(double slDistancePrice)
  {
   if(slDistancePrice <= 0) return SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MIN);
   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney  = equity * (RiskPercent / 100.0);
   double tickValue  = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_VALUE);
   double tickSize   = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0 || tickValue <= 0) return SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MIN);
   double lossPerLot = (slDistancePrice / tickSize) * tickValue;
   if(lossPerLot <= 0) return SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MIN);
   double stepLot    = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_STEP);
   double minLot     = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MIN);
   double maxLot     = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MAX);
   double rawLots    = riskMoney / lossPerLot;
   double lots       = MathFloor((rawLots / stepLot) + 1e-7) * stepLot;
   return MathMin(maxLot, MathMax(minLot, lots));
  }

//+------------------------------------------------------------------+
//| Manage Open Positions (Trailing Stop)                             |
//+------------------------------------------------------------------+
void ManageOpenPositions()
  {
   if(PositionsTotal() == 0) return;

   int digits = (int)SymbolInfoInteger(Symbol(), SYMBOL_DIGITS);
   double pointVal = SymbolInfoDouble(Symbol(), SYMBOL_POINT);

   static int cachedAtrHandle = INVALID_HANDLE;
   static datetime atrCacheTime = 0;
   double atrArr[];
   if(cachedAtrHandle == INVALID_HANDLE || TimeCurrent() - atrCacheTime > 60) {
      if(cachedAtrHandle != INVALID_HANDLE) IndicatorRelease(cachedAtrHandle);
      cachedAtrHandle = iATR(Symbol(), PERIOD_CURRENT, ATRPeriod);
      atrCacheTime = TimeCurrent();
   }
   double atrPips = TrailingMinPips * pointVal;
   if(cachedAtrHandle != INVALID_HANDLE && CopyBuffer(cachedAtrHandle, 0, 0, 1, atrArr) > 0) {
      atrPips = MathMax(atrArr[0] * TrailingATRMult, TrailingMinPips * pointVal);
   }

   long minStopLevel = SymbolInfoInteger(Symbol(), SYMBOL_TRADE_STOPS_LEVEL);
   double minStopDist = minStopLevel * pointVal;
   double dig = SymbolInfoInteger(Symbol(), SYMBOL_DIGITS);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == Symbol() && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
        {
         long type        = PositionGetInteger(POSITION_TYPE);
         double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
         double currentSL = PositionGetDouble(POSITION_SL);
         double tpPrice   = PositionGetDouble(POSITION_TP);
         double currentPr = PositionGetDouble(POSITION_PRICE_CURRENT);

         if(type == POSITION_TYPE_BUY)
           {
            double riskDist = openPrice - currentSL;
            if(riskDist <= 0) continue;
            double newSL = currentPr - atrPips;
            double minSL = openPrice + (TrailingPercent * riskDist);
            double candNewSL = MathMax(newSL, minSL);
            // Validar: SL debe estar por debajo del precio actual Y respetar distancia mínima
            double slDistFromPr = currentPr - candNewSL;
            if(candNewSL > currentSL + pointVal && candNewSL < currentPr && slDistFromPr >= minStopDist) {
               trade.PositionModify(ticket, NormalizeDouble(candNewSL, dig), tpPrice);
            }
           }
         else if(type == POSITION_TYPE_SELL)
           {
            double riskDist = currentSL - openPrice;
            if(riskDist <= 0) continue;
            double newSL = currentPr + atrPips;
            double maxSL = currentPr - atrPips;
            double minSL = openPrice - (TrailingPercent * riskDist);
            double candNewSL = MathMin(newSL, maxSL);
            // Validar: SL debe estar por encima del precio actual Y respetar distancia mínima
            double slDistFromPr = candNewSL - currentPr;
            if(candNewSL < currentSL - pointVal && candNewSL > currentPr && slDistFromPr >= minStopDist) {
               trade.PositionModify(ticket, NormalizeDouble(candNewSL, dig), tpPrice);
            }
           }
        }
     }
  }

//+------------------------------------------------------------------+
//| Circuit Breaker                                                  |
//+------------------------------------------------------------------+
bool IsCircuitBreakerOpen()
  {
   if(g_consecutiveErrors >= MAX_CIRCUIT_BREAKERS && g_circuitBreakerReset > TimeCurrent())
      return true;
   if(g_circuitBreakerReset > 0 && g_circuitBreakerReset <= TimeCurrent())
     {
      g_consecutiveErrors = 0;
      g_circuitBreakerReset = 0;
      Print("[AI Terminal v3] Circuit Breaker RESET.");
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Close All Positions                                              |
//+------------------------------------------------------------------+
void CloseAllPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == Symbol() && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
        {
         // Capture all data BEFORE closing (PositionGet* fails after close)
         string comment    = PositionGetString(POSITION_COMMENT);
         double entryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
         double closePrice = SymbolInfoDouble(Symbol(), SYMBOL_BID);
         double volume     = PositionGetDouble(POSITION_VOLUME);
         double pnl        = PositionGetDouble(POSITION_PROFIT);
         long   posType    = PositionGetInteger(POSITION_TYPE);
         datetime entryTime= (datetime)PositionGetInteger(POSITION_TIME);

         trade.PositionClose(ticket);
         NotifyTradeClosed(ticket, comment, entryPrice, closePrice, volume, pnl, posType, entryTime);
        }
     }
  }

//+------------------------------------------------------------------+
//| Notify FastAPI that a trade was closed (data captured BEFORE close)|
//+------------------------------------------------------------------+
void NotifyTradeClosed(ulong ticket, string comment,
                       double entryPrice, double closePrice, double volume,
                       double pnl, long posType, datetime entryTime)
  {
   string url = FastAPI_URL + "/api/v1/ai/trade/filled";
   string direction = (posType == POSITION_TYPE_BUY) ? "LONG" : "SHORT";

   // Parse exit reason from comment
   string exitReason = "manual";
   bool slHit = false;
   bool tpHit = false;
   if(StringFind(comment, "[sl") >= 0) { slHit = true; exitReason = "sl"; }
   else if(StringFind(comment, "[tp") >= 0) { tpHit = true; exitReason = "tp"; }

   // Calculate pnl_pct
   double pnlPct = 0.0;
   if(entryPrice > 0 && volume > 0) {
      double directionMult = (posType == POSITION_TYPE_BUY) ? 1.0 : -1.0;
      double priceDiff = (closePrice - entryPrice) * directionMult;
      double tickValue = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_VALUE);
      double tickSize = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_SIZE);
      if(tickSize > 0 && tickValue > 0) {
         pnlPct = (priceDiff / tickSize) * tickValue * volume / (entryPrice * volume) * 100.0;
      }
   }

   // Build JSON body
   string body = StringFormat(
      "{\"symbol\":\"%s\",\"entry_time\":\"%s\",\"exit_time\":\"%s\","
      "\"pnl\":%.2f,\"pnl_pct\":%.4f,\"direction\":\"%s\","
      "\"sl_hit\":%s,\"tp_hit\":%s,\"exit_reason\":\"%s\"}",
      Symbol(),
      IntegerToString(entryTime),
      IntegerToString(TimeCurrent()),
      pnl, pnlPct, direction,
      slHit ? "true" : "false",
      tpHit ? "true" : "false",
      exitReason
   );

   char postData[], result[];
   string headers = "Content-Type: application/json\r\n"
                    "X-Internal-Token: " + InternalToken + "\r\n";
   StringToCharArray(body, postData, 0, StringLen(body));
   ArrayResize(postData, StringLen(body));

   string responseHeaders;
   int res = WebRequest("POST", url, headers, 5000, postData, result, responseHeaders);
   if(res == 200) {
      PrintFormat("[AI Terminal v3] Trade closed notified: ticket=%d pnl=%.2f exit=%s", ticket, pnl, exitReason);
   } else {
      PrintFormat("[AI Terminal v3] NotifyTradeClosed failed: HTTP %d", res);
   }
  }

//+------------------------------------------------------------------+
//| Log trade open to file for debugging                              |
//+------------------------------------------------------------------+
void NotifyTradeOpened(string direction, double entryPrice)
  {
   datetime now = TimeCurrent();
   string entryLog = StringFormat(
      "%s|%s|%s|%.5f\n",
      IntegerToString(now),
      direction,
      Symbol(),
      entryPrice
   );
   
   int handle = FileOpen("trade_entries.csv", FILE_READ|FILE_WRITE|FILE_SHARE_WRITE|FILE_CSV, ',');
   if(handle != INVALID_HANDLE) {
      FileSeek(handle, 0, SEEK_END);
      FileWriteString(handle, entryLog);
      FileClose(handle);
   }
   
   PrintFormat("[AI Terminal v3] Trade OPENED: %s @ %.5f", direction, entryPrice);
  }

//+------------------------------------------------------------------+
