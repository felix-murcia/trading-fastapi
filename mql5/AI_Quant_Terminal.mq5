#property copyright "AI Quant Terminal"
#property version   "10.1"
#property strict

#include <Trade\Trade.mqh>

//--- Forward Declarations
void ManageOpenPositions();
double CalculateLots(double slDistancePrice);
bool IsCircuitBreakerOpen();
void CloseAllPositions();
bool HasActivePosition();

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

input group "=== Position Sizing ==="
input double RiskPercent = 5.0; // Porcentaje de equity por trade
input int StopLossPips = 150; // Stop Loss en pips (era 500)
input int TakeProfitPips = 300; // Take Profit en pips (era 1000)
input bool UseATRForSL = true; // Usar ATR en vez de StopLossPips fijo
input int ATRPeriod = 14; // Periodo ATR

input group "=== Trailing Stop ==="
input double TrailingPercent = 0.25; // % del SL original (0.25 = 25%)
input int TrailingMinPips = 50; // Minimum trailing distance in pips
input int TrailingATRMult = 2; // ATR multiplier for min trailing (ATR*2)

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
   Print("[AI Terminal] Iniciado. Esperando instrucciones de la IA...");
   return INIT_SUCCEEDED;
  }

void OnTick()
  {
   // Gestión de posiciones cada 30 segundos (no cada tick)
   datetime now = TimeCurrent();
   if(now - lastManagedPositions >= 30) {
      ManageOpenPositions();
      lastManagedPositions = now;
   }

   datetime currentBar = iTime(Symbol(), PERIOD_CURRENT, 0);
   if(currentBar == lastCheckedBar) return;
   
   // Solo consulta al cerebro cuando se abre una nueva vela (H1 para el bot de Aprendizaje por Refuerzo)
   lastCheckedBar = currentBar;
   
   // Si ya tenemos una operacion abierta, bloqueamos la consulta a la IA para no sobreoperar
   // Eliminado: HasActivePosition() return para permitir Cierres Dinamicos
   
   if(!IsTesting() && EnableAI)
     {
      //--- Circuit Breaker Check
      if(IsCircuitBreakerOpen())
        {
         static datetime lastCircuitLog = 0;
         if(TimeCurrent() - lastCircuitLog > 60) {
            PrintFormat("[AI Terminal] Circuit Breaker ACTIVO. Esperando %d segundos...", CIRCUIT_BREAK_COOLDOWN);
            lastCircuitLog = TimeCurrent();
         }
         return;
        }

      string url = FastAPI_URL + "/api/v1/ai/predict";
      char postData[], result[];
      string headers = "Content-Type: application/json\r\n"
                       "X-Internal-Token: " + InternalToken + "\r\n";
                       
      int current_pos = 0;
      if (PositionsTotal() > 0) {
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
      
      //--- Retry Loop with Exponential Backoff
      string responseHeaders;
      int res = -1;
      bool success = false;
      
      for(int attempt = 0; attempt < MAX_RETRIES && !success; attempt++)
        {
         if(attempt > 0)
           {
            int delayMs = BASE_RETRY_DELAY_MS * (1 << (attempt - 1)); // 1s, 2s, 4s
            PrintFormat("[AI Terminal] Retry %d/%d en %d ms...", attempt + 1, MAX_RETRIES, delayMs);
            Sleep(delayMs);
           }
         res = WebRequest("POST", url, headers, 10000, postData, result, responseHeaders);
         if(res == 200) {
            success = true;
            break;
         }
         PrintFormat("[AI Terminal] Intento %d fallo: HTTP %d", attempt + 1, res);
        }
      
      //--- Circuit Breaker Update
      if(res != 200) {
         g_consecutiveErrors++;
         if(g_consecutiveErrors >= MAX_CIRCUIT_BREAKERS) {
            g_circuitBreakerReset = TimeCurrent() + CIRCUIT_BREAK_COOLDOWN;
            PrintFormat("[AI Terminal] CIRCUIT BREAKER ACTIVADO! %d errores consecutivos. Cooldown %d segundos.",
                        g_consecutiveErrors, CIRCUIT_BREAK_COOLDOWN);
         }
      } else {
         g_consecutiveErrors = 0; // Reset on success
      }
      
      if(res == 200)
        {
         string r = CharArrayToString(result);
         PrintFormat("[AI Terminal] Respuesta del Cerebro: %s", r);
         
         // Calcular SL dinámico basado en ATR o fijo
         double slPips = StopLossPips;
         if(UseATRForSL)
           {
            double atrArr[];
            int atrHandle = iATR(Symbol(), PERIOD_CURRENT, ATRPeriod);
            if(CopyBuffer(atrHandle, 0, 0, 1, atrArr) > 0)
              {
               // ATR en puntos (multiplicar por 1.5 para dar espacio)
               slPips = MathMax(atrArr[0] / SymbolInfoDouble(Symbol(), SYMBOL_POINT) * 1.5, StopLossPips);
              }
            IndicatorRelease(atrHandle);
           }
         
         double slDistance = slPips * SymbolInfoDouble(Symbol(), SYMBOL_POINT);
         double tpDistance = TakeProfitPips * SymbolInfoDouble(Symbol(), SYMBOL_POINT);
         double lot = CalculateLots(slDistance);
         double p   = SymbolInfoDouble(Symbol(), SYMBOL_POINT);
         int    dig = (int)SymbolInfoInteger(Symbol(), SYMBOL_DIGITS);
         
         PrintFormat("[AI Terminal] SL: %.0f pips, TP: %.0f pips, Lote: %.2f", slPips, TakeProfitPips, lot);
         
         if(StringFind(r, "\"decision\":\"CLOSE\"") >= 0)
           {
            Print("[AI Terminal] Cerebro indica CIERRE de posiciones activas.");
            CloseAllPositions();
           }
         else if(StringFind(r, "\"decision\":\"BUY\"") >= 0)
           {
            double entry = SymbolInfoDouble(Symbol(), SYMBOL_ASK);
            if (current_pos == 2) CloseAllPositions();
            if (current_pos != 1) {
               bool buyOk = trade.Buy(lot, Symbol(), entry, NormalizeDouble(entry - slDistance, dig), NormalizeDouble(entry + tpDistance, dig), "AI Predict BUY");
               PrintFormat("[AI Terminal] BUY enviado — entry=%.5f lot=%.2f SL=%.5f TP=%.5f → %s",
                           entry, lot, NormalizeDouble(entry - slDistance, dig), NormalizeDouble(entry + tpDistance, dig),
                           buyOk ? "OK" : "FALLO");
            }
           }
         else if(StringFind(r, "\"decision\":\"SELL\"") >= 0)
           {
            double entry = SymbolInfoDouble(Symbol(), SYMBOL_BID);
            if (current_pos == 1) CloseAllPositions();
            if (current_pos != 2) {
               bool sellOk = trade.Sell(lot, Symbol(), entry, NormalizeDouble(entry + slDistance, dig), NormalizeDouble(entry - tpDistance, dig), "AI Predict SELL");
               PrintFormat("[AI Terminal] SELL enviado — entry=%.5f lot=%.2f SL=%.5f TP=%.5f → %s",
                           entry, lot, NormalizeDouble(entry + slDistance, dig), NormalizeDouble(entry - tpDistance, dig),
                           sellOk ? "OK" : "FALLO");
            }
           }
        }
      else
        {
         PrintFormat("[AI Terminal] Todos los intentos fallaron. HTTP %d. Errores consecutivos: %d", res, g_consecutiveErrors);
        }
     }
  }

//+------------------------------------------------------------------+
//| Circuit Breaker Helper                                           |
//+------------------------------------------------------------------+
bool IsCircuitBreakerOpen()
  {
   if(g_consecutiveErrors >= MAX_CIRCUIT_BREAKERS && g_circuitBreakerReset > TimeCurrent())
     {
      return true;
     }
   // Reset if cooldown expired without errors
   if(g_circuitBreakerReset > 0 && g_circuitBreakerReset <= TimeCurrent())
     {
      g_consecutiveErrors = 0;
      g_circuitBreakerReset = 0;
      Print("[AI Terminal] Circuit Breaker RESET. Reactivando consultas...");
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Lot Calculator                                                   |
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
   double stepLot  = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_STEP);
   double minLot   = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MIN);
   double maxLot   = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MAX);
   double rawLots  = riskMoney / lossPerLot;
   double lots     = MathFloor((rawLots / stepLot) + 1e-7) * stepLot;
   return MathMin(maxLot, MathMax(minLot, lots));
  }

void ManageOpenPositions()
  {
   if(PositionsTotal() == 0) return; // Early exit si no hay posiciones

   int digits = (int)SymbolInfoInteger(Symbol(), SYMBOL_DIGITS);
   double pointVal = SymbolInfoDouble(Symbol(), SYMBOL_POINT);

   // Cache ATR handle para evitar crear/destruir cada tick
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

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == Symbol() && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
        {
         long type       = PositionGetInteger(POSITION_TYPE);
         double openPrice= PositionGetDouble(POSITION_PRICE_OPEN);
         double currentSL= PositionGetDouble(POSITION_SL);
         double tpPrice  = PositionGetDouble(POSITION_TP);
         double currentPr= PositionGetDouble(POSITION_PRICE_CURRENT);
         
         if(type == POSITION_TYPE_BUY)
           {
            double riskDist = openPrice - currentSL;
            double trailDist = riskDist * TrailingPercent; // 25% del riesgo original
            double minTrail = atrPips; // dinámico según volatilidad
            if(riskDist > 0 && currentPr >= (openPrice + riskDist) && currentSL < openPrice)
              {
               double newSL = NormalizeDouble(openPrice + MathMax(trailDist, minTrail), digits);
               trade.PositionModify(ticket, newSL, tpPrice);
              }
           }
         else if(type == POSITION_TYPE_SELL)
           {
            double riskDist = currentSL - openPrice;
            double trailDist = riskDist * TrailingPercent; // 25% del riesgo original
            double minTrail = atrPips; // dinámico según volatilidad
            if(riskDist > 0 && currentPr <= (openPrice - riskDist) && currentSL > openPrice)
              {
               double newSL = NormalizeDouble(openPrice - MathMax(trailDist, minTrail), digits);
               trade.PositionModify(ticket, newSL, tpPrice);
              }
           }
        }
     }
  }

bool HasActivePosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0)
        {
         if(PositionGetString(POSITION_SYMBOL) == Symbol() && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
           {
            return true;
           }
        }
     }
   return false;
  }

void CloseAllPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0)
        {
         if(PositionGetString(POSITION_SYMBOL) == Symbol() && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
           {
            trade.PositionClose(ticket);
           }
        }
     }
  }
