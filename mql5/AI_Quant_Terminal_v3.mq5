/*
AI_Quant_Terminal_v3.mq5 (Refactorizado)
========================================
Terminal de Ejecución y Telemetría para Cerebro Autónomo (FastAPI + PPO v3).
- Rol del EA: Sensor de datos y ejecutor pasivo.
- Rol del Servidor: Decisión direccional, dimensionamiento de lotes, SL, TP y gestión de riesgo.
*/
#property copyright "AI Quant Terminal v3"
#property version   "12.8"
#property strict

#define EA_VERSION "12.8"

#include <Trade\Trade.mqh>

#define MAX_RETRIES             3
#define BASE_RETRY_DELAY_MS     1000
#define MAX_CIRCUIT_BREAKERS    5
#define CIRCUIT_BREAK_COOLDOWN  300

input group "=== Configuración del Cerebro ==="
input string FastAPI_URL    = "http://127.0.0.1:8090"; // URL del servidor FastAPI
input string InternalToken  = "YOUR_INTERNAL_TOKEN";  // Token de autenticación
input bool   EnableAI       = true;                    // Habilitar operativa por IA
input ulong  MagicNumber    = 90001;                   // Identificador de órdenes

input group "=== Trailing Stop ==="
input bool   EnableTrailing  = true;                    // Activar trailing stop
input double TrailingPercent = 0.25;                    // % del riesgo original que se protege (0.25 = 25%)
input int    TrailingMinPips = 15;                      // Distancia mínima de trailing en pips
input int    TrailingATRMult  = 2;                      // Multiplicador ATR para distancia mínima dinámica
input int    TrailingATRPeriod = 14;                    // Periodo ATR para trailing dinámico
input int    TrailingManageEverySec = 5;                 // Cada cuántos segundos revisar trailing (0 = cada tick)

CTrade   trade;
datetime lastCheckedBar = 0;
datetime g_circuitBreakerReset = 0;
int      g_consecutiveErrors = 0;
ulong    g_lastClosedTicket = 0;
datetime g_lastTrailingCheck = 0;
int      g_atrHandle = INVALID_HANDLE;

//+------------------------------------------------------------------+
//| Parser JSON ligero y tolerante a tipos                           |
//+------------------------------------------------------------------+
string GetJsonValue(const string json, const string key)
  {
   string token = "\"" + key + "\":";
   int start = StringFind(json, token);
   if(start < 0) return "";
   start += StringLen(token);

   // Omitir espacios
   while(start < StringLen(json) && (StringGetCharacter(json, start) == ' ' || StringGetCharacter(json, start) == '\t'))
      start++;

   if(start >= StringLen(json)) return "";

   ushort firstChar = StringGetCharacter(json, start);
   if(firstChar == '"') // Valor string
     {
      start++;
      int end = StringFind(json, "\"", start);
      if(end < 0) return "";
      return StringSubstr(json, start, end - start);
     }
   else // Valor numérico / booleano / null
     {
      int end = start;
      while(end < StringLen(json))
        {
         ushort c = StringGetCharacter(json, end);
         if(c == ',' || c == '}' || c == ']' || c == ' ' || c == '\r' || c == '\n') break;
         end++;
        }
      return StringSubstr(json, start, end - start);
     }
  }

//+------------------------------------------------------------------+
//| Inicialización                                                   |
//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(30);
   trade.SetTypeFillingBySymbol(Symbol());
   PrintFormat("[AI Terminal] Iniciado v%s. Conectando a %s", EA_VERSION, FastAPI_URL);
   if(EnableTrailing && TrailingATRMult > 0)
     {
      g_atrHandle = iATR(Symbol(), PERIOD_CURRENT, TrailingATRPeriod);
      PrintFormat("[TRAILING] ATR handle=%d period=%d mult=%d", g_atrHandle, TrailingATRPeriod, TrailingATRMult);
     }
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
//| Ciclo Principal (Por barra cerrada de H1)                        |
//+------------------------------------------------------------------+
void OnTick()
  {
   // 0. TRAILING STOP — se ejecuta cada tick (o cada N segundos) de forma independiente
   if(EnableTrailing)
      ManageTrailingStop();

   // Ejecutar exclusivamente en la apertura de una nueva barra
   datetime currentBar = iTime(Symbol(), PERIOD_H1, 0);
   if(currentBar == lastCheckedBar) return;
   lastCheckedBar = currentBar;

   if(!EnableAI || MQLInfoInteger(MQL_TESTER)) return;

   // Circuito de protección ante caídas de red
   if(g_consecutiveErrors >= MAX_CIRCUIT_BREAKERS && g_circuitBreakerReset > TimeCurrent())
     {
      PrintFormat("[AI Terminal] Circuit Breaker activo. Enfriamiento hasta: %s", TimeToString(g_circuitBreakerReset));
      return;
     }

   // 1. RECOLECCIÓN DE TELEMETRÍA (Sensor)
   int currentPos = 0;       // 0=Flat, 1=Long, 2=Short
   double entryPrice = 0.0;
   double currentSL  = 0.0;
   double currentTP  = 0.0;
   int stepsInTrade  = 0;

   if(PositionSelect(Symbol()))
     {
      if(PositionGetInteger(POSITION_MAGIC) == MagicNumber)
        {
         long posType = PositionGetInteger(POSITION_TYPE);
         currentPos = (posType == POSITION_TYPE_BUY) ? 1 : 2;
         entryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
         currentSL  = PositionGetDouble(POSITION_SL);
         currentTP  = PositionGetDouble(POSITION_TP);
         datetime pTime = (datetime)PositionGetInteger(POSITION_TIME);
         if(pTime > 0)
            stepsInTrade = (int)((TimeCurrent() - pTime) / PeriodSeconds(PERIOD_H1));
        }
     }

   // Construcción del payload extendido
   string body = StringFormat(
      "{\"symbol\":\"%s\",\"timeframe\":\"H1\",\"position\":%d,\"entry_price\":%.5f,\"sl_price\":%.5f,\"tp_price\":%.5f,\"steps_in_trade\":%d}",
      Symbol(), currentPos, entryPrice, currentSL, currentTP, stepsInTrade
   );

   // 2. LLAMADA AL CEREBRO
   char postData[], resultArr[];
   string headers = "Content-Type: application/json\r\n"
                    "X-Internal-Token: " + InternalToken + "\r\n";
   StringToCharArray(body, postData, 0, StringLen(body));
   ArrayResize(postData, StringLen(body));

   string responseHeaders;
   int res = -1;
   for(int attempt = 0; attempt < MAX_RETRIES; attempt++)
     {
      if(attempt > 0) Sleep(BASE_RETRY_DELAY_MS * attempt);
      ArrayFree(resultArr);
       res = WebRequest("POST", FastAPI_URL + "/api/v1/ai/predict", headers, 30000, postData, resultArr, responseHeaders);
      if(res == 200) break;
     }

   if(res != 200)
     {
      g_consecutiveErrors++;
      if(g_consecutiveErrors >= MAX_CIRCUIT_BREAKERS)
         g_circuitBreakerReset = TimeCurrent() + CIRCUIT_BREAK_COOLDOWN;
      PrintFormat("[AI Terminal] Error HTTP %d comunicando con Cerebro.", res);
      return;
     }

   g_consecutiveErrors = 0;
   string r = CharArrayToString(resultArr);
   PrintFormat("[AI Terminal] Respuesta Cerebro: %s", r);

   // 3. EJECUCIÓN PURA DE LA ORDEN (Actuador)
   string decision = GetJsonValue(r, "decision");

   // A. Si la decisión es cerrar
   if(decision == "CLOSE")
     {
      Print("[AI Terminal] Orden de CIERRE ejecutada por el Cerebro.");
      CloseAllPositions();
      return;
     }

   // B. Si la decisión es mantener
   if(decision == "HOLD")
     {
      Print("[AI Terminal] Decisión HOLD. Sin cambios en mercado.");
      return;
     }

    // C. Si la decisión es operar (BUY / SELL)
    if(decision == "BUY" || decision == "SELL")
      {
       double rawVolume = StringToDouble(GetJsonValue(r, "volume"));
       double slPips    = StringToDouble(GetJsonValue(r, "sl_pips"));
       double tpPips    = StringToDouble(GetJsonValue(r, "tp_pips"));

       if(rawVolume <= 0 || slPips <= 0 || tpPips <= 0)
         {
          PrintFormat("[AI Terminal] Descartado: Parámetros inválidos recibidos (vol=%.2f, sl=%.1f, tp=%.1f)", rawVolume, slPips, tpPips);
          return;
         }

       // Solo se permite ejecutar si no hay posición abierta.
       // Mientras haya posición activa, la salida debe ser exclusivamente por SL/TP.
       if(currentPos != 0)
         {
          PrintFormat("[AI Terminal] Decisión %s descartada: ya hay posición activa currentPos=%d", decision, currentPos);
          return;
         }

       // Normalizar exclusivamente según reglas del Broker (Lotes y Stops)
       double stepLot = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_STEP);
       double minLot  = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MIN);
       double maxLot  = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MAX);
       double lot     = MathFloor(rawVolume / stepLot + 0.00001) * stepLot;
       lot            = MathMax(minLot, MathMin(maxLot, lot));

       int digits     = (int)SymbolInfoInteger(Symbol(), SYMBOL_DIGITS);
       double point   = SymbolInfoDouble(Symbol(), SYMBOL_POINT);
       double pipSize = (digits == 3 || digits == 5) ? point * 10.0 : point;
       double spread  = (decision == "BUY")
                        ? (SymbolInfoDouble(Symbol(), SYMBOL_ASK) - SymbolInfoDouble(Symbol(), SYMBOL_BID))
                        : (SymbolInfoDouble(Symbol(), SYMBOL_ASK) - SymbolInfoDouble(Symbol(), SYMBOL_BID));

       double slDist  = slPips * pipSize;
       double tpDist  = tpPips * pipSize;

        long minStopLevel = SymbolInfoInteger(Symbol(), SYMBOL_TRADE_STOPS_LEVEL);
        double spreadDist = 3.0 * pipSize;
        double minStopDist = MathMax(minStopLevel * point, spreadDist);
        if(slDist < minStopDist) slDist = minStopDist;
        if(tpDist < minStopDist) tpDist = minStopDist;

        double currentSpread = SymbolInfoDouble(Symbol(), SYMBOL_ASK) - SymbolInfoDouble(Symbol(), SYMBOL_BID);
        double buffer = MathMax(currentSpread, 2.0 * pipSize);

        // Ejecutar BUY
        if(decision == "BUY")
          {
           if(currentPos != 0)
             {
              PrintFormat("[AI Terminal] Decisión BUY descartada: ya hay posición activa currentPos=%d", currentPos);
              return;
             }
           double ask = SymbolInfoDouble(Symbol(), SYMBOL_ASK);
           double slPrice = NormalizeDouble(ask - slDist - buffer, digits);
           double tpPrice = NormalizeDouble(ask + tpDist, digits);
           if(ask - slPrice < minStopDist) slPrice = NormalizeDouble(ask - minStopDist, digits);
           if(tpPrice - ask < minStopDist) tpPrice = NormalizeDouble(ask + minStopDist, digits);
           bool placed = trade.Buy(lot, Symbol(), ask, slPrice, tpPrice, "AI-Predict-v3");
           if(!placed)
             PrintFormat("[AI Terminal] ERROR BUY retcode=%d comment=%s | ask=%.5f sl=%.5f tp=%.5f slDist=%.5f tpDist=%.5f buffer=%.5f minStop=%.5f",
                          (int)trade.ResultRetcode(), trade.ResultComment(), ask, slPrice, tpPrice, slDist, tpDist, buffer, minStopDist);
          }
        // Ejecutar SELL
        else if(decision == "SELL")
          {
           if(currentPos != 0)
             {
              PrintFormat("[AI Terminal] Decisión SELL descartada: ya hay posición activa currentPos=%d", currentPos);
              return;
             }
           double bid = SymbolInfoDouble(Symbol(), SYMBOL_BID);
           double slPrice = NormalizeDouble(bid + slDist + buffer, digits);
           double tpPrice = NormalizeDouble(bid - tpDist, digits);
           if(slPrice - bid < minStopDist) slPrice = NormalizeDouble(bid + minStopDist, digits);
           if(bid - tpPrice < minStopDist) tpPrice = NormalizeDouble(bid - minStopDist, digits);
           bool placed = trade.Sell(lot, Symbol(), bid, slPrice, tpPrice, "AI-Predict-v3");
           if(!placed)
             PrintFormat("[AI Terminal] ERROR SELL retcode=%d comment=%s | bid=%.5f sl=%.5f tp=%.5f slDist=%.5f tpDist=%.5f buffer=%.5f minStop=%.5f",
                          (int)trade.ResultRetcode(), trade.ResultComment(), bid, slPrice, tpPrice, slDist, tpDist, buffer, minStopDist);
          }
     }
  }

//+------------------------------------------------------------------+
//| Trailing Stop independiente del ciclo H1                         |
//+------------------------------------------------------------------+
void ManageTrailingStop()
  {
   if(TrailingManageEverySec > 0)
     {
      datetime now = TimeCurrent();
      if(g_lastTrailingCheck > 0 && (now - g_lastTrailingCheck) < TrailingManageEverySec)
         return;
      g_lastTrailingCheck = now;
     }

   ulong ticket = 0;
   double openPrice = 0.0;
   double currentSL  = 0.0;
   double currentTP  = 0.0;
   long   posType = -1;
   int digits = (int)SymbolInfoInteger(Symbol(), SYMBOL_DIGITS);
   double point = SymbolInfoDouble(Symbol(), SYMBOL_POINT);
   double pipSize = (digits == 3 || digits == 5) ? point * 10.0 : point;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t <= 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != Symbol()) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

      ticket = t;
      posType = PositionGetInteger(POSITION_TYPE);
      openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      currentSL  = PositionGetDouble(POSITION_SL);
      currentTP  = PositionGetDouble(POSITION_TP);
      break;
     }

   if(ticket == 0 || posType < 0) return;

   if(currentSL <= 0)
     {
      PrintFormat("[TRAILING] SL inicial no válido (%.5f). Se requiere SL colocado por el EA.", currentSL);
      return;
     }

   if(posType == POSITION_TYPE_BUY && currentSL >= openPrice)
     {
      PrintFormat("[TRAILING] SL inválido para BUY (sl=%.5f >= entry=%.5f).", currentSL, openPrice);
      return;
     }
   if(posType == POSITION_TYPE_SELL && currentSL <= openPrice)
     {
      PrintFormat("[TRAILING] SL inválido para SELL (sl=%.5f <= entry=%.5f).", currentSL, openPrice);
      return;
     }

   double atr = 0.0;
   if(g_atrHandle != INVALID_HANDLE && TrailingATRMult > 0)
     {
      double atrArr[];
      if(CopyBuffer(g_atrHandle, 0, 0, 1, atrArr) == 1 && atrArr[0] > 0)
         atr = atrArr[0];
     }

   double minTrailPips = (double)TrailingMinPips;
   if(TrailingATRMult > 0 && atr > 0)
      minTrailPips = MathMax(minTrailPips, (atr / pipSize) * (double)TrailingATRMult);

   bool modified = false;

   if(posType == POSITION_TYPE_BUY)
     {
      double riskDist = openPrice - currentSL;
      if(riskDist <= 0) return;
      double trailDistPrice = riskDist * TrailingPercent;
      double minTrailPrice = minTrailPips * pipSize;
      double newSL = NormalizeDouble(openPrice + MathMax(trailDistPrice, minTrailPrice), digits);

      double currentPr = SymbolInfoDouble(Symbol(), SYMBOL_ASK);
      if(currentPr >= (openPrice + riskDist) && newSL > currentSL)
        {
         if(trade.PositionModify(ticket, newSL, currentTP))
           {
            modified = true;
            PrintFormat("[TRAILING] BUY ticket=%I64u SL movido %.5f -> %.5f (risk=%.1f pips, trail=%.1f pips)",
                        ticket, currentSL, newSL, riskDist/pipSize, (newSL-openPrice)/pipSize);
           }
        }
     }
   else if(posType == POSITION_TYPE_SELL)
     {
      double riskDist = currentSL - openPrice;
      if(riskDist <= 0) return;
      double trailDistPrice = riskDist * TrailingPercent;
      double minTrailPrice = minTrailPips * pipSize;
      double newSL = NormalizeDouble(openPrice - MathMax(trailDistPrice, minTrailPrice), digits);

      double currentPr = SymbolInfoDouble(Symbol(), SYMBOL_BID);
      if(currentPr <= (openPrice - riskDist) && newSL < currentSL)
        {
         if(trade.PositionModify(ticket, newSL, currentTP))
           {
            modified = true;
            PrintFormat("[TRAILING] SELL ticket=%I64u SL movido %.5f -> %.5f (risk=%.1f pips, trail=%.1f pips)",
                        ticket, currentSL, newSL, riskDist/pipSize, (openPrice-newSL)/pipSize);
           }
        }
     }

   if(!modified)
      PrintFormat("[TRAILING] No hay movimiento SL. type=%d open=%.5f sl=%.5f atr=%.5f minTrail=%.1f pips",
                  (int)posType, openPrice, currentSL, atr, minTrailPips);
  }


//+------------------------------------------------------------------+
//| Cierra todas las posiciones gestionadas por este EA              |
//+------------------------------------------------------------------+
void CloseAllPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == Symbol() && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
        {
         trade.PositionClose(ticket);
        }
     }
  }

//+------------------------------------------------------------------+
//| Notificación asíncrona de cierre de trade hacia el Cerebro       |
//+------------------------------------------------------------------+
 void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest& request,
                        const MqlTradeResult& result)
   {
    if(trans.type != TRADE_TRANSACTION_DEAL_ADD)
      {
       PrintFormat("[AI Terminal] OnTradeTransaction descartado: type=%d", (int)trans.type);
       return;
      }

    ulong dealTicket = trans.deal;
    if(dealTicket <= 0 || trans.symbol != Symbol())
      {
       PrintFormat("[AI Terminal] OnTradeTransaction descartado: deal=%I64u symbol=%s", dealTicket, trans.symbol);
       return;
      }

    long positionId = (long)trans.position;
    if(positionId <= 0)
      {
       PrintFormat("[AI Terminal] OnTradeTransaction descartado: positionId=0 deal=%I64u", dealTicket);
       return;
      }

    datetime historyFrom = TimeCurrent() - 86400 * 90;
    datetime historyTo   = TimeCurrent() + 60;

     PrintFormat("[AI Terminal] Buscando openingDeal: dealTicket=%I64u positionId=%I64d", dealTicket, positionId);

     ulong openingDeal = 0;
     if(HistorySelectByPosition(positionId))
       {
        for(int i = 0; i < HistoryDealsTotal(); i++)
          {
           ulong hTicket = HistoryDealGetTicket(i);
           if(hTicket <= 0) continue;
           if(hTicket == dealTicket) continue;
           long hEntry = HistoryDealGetInteger(hTicket, DEAL_ENTRY);
           long hType = HistoryDealGetInteger(hTicket, DEAL_TYPE);
           if(hEntry == DEAL_ENTRY_IN && (hType == DEAL_TYPE_BUY || hType == DEAL_TYPE_SELL))
             {
              openingDeal = hTicket;
              break;
             }
          }
       }

    if(openingDeal <= 0)
      {
       PrintFormat("[AI Terminal] OnTradeTransaction descartado: openingDeal=0 positionId=%I64d deal=%I64u",
                    positionId, dealTicket);
       return;
      }

    long openingMagic = HistoryDealGetInteger(openingDeal, DEAL_MAGIC);
    if(openingMagic != (long)MagicNumber)
      {
       PrintFormat("[AI Terminal] OnTradeTransaction descartado: magic=%d esperado=%d", (int)openingMagic, (int)MagicNumber);
       return;
      }

   double entryPrice = HistoryDealGetDouble(openingDeal, DEAL_PRICE);
   double exitPrice  = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
   double pnl        = HistoryDealGetDouble(dealTicket, DEAL_PROFIT);
   long openingType  = HistoryDealGetInteger(openingDeal, DEAL_TYPE);
   datetime entryTime= (datetime)HistoryDealGetInteger(openingDeal, DEAL_TIME);
   datetime closeTime= (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME);

   bool isBuy = (openingType == DEAL_TYPE_BUY);
   string direction = isBuy ? "LONG" : "SHORT";

   // Clasificar razón de salida por propiedad DEAL_REASON nativa
   long dealReason = HistoryDealGetInteger(dealTicket, DEAL_REASON);
   string comment  = HistoryDealGetString(dealTicket, DEAL_COMMENT);
   string exitReason = "manual";
   bool slHit = false, tpHit = false;

   if(dealReason == DEAL_REASON_SL || StringFind(comment, "sl") >= 0)
     {
      slHit = true;
      exitReason = "sl";
     }
   else if(dealReason == DEAL_REASON_TP || StringFind(comment, "tp") >= 0)
     {
      tpHit = true;
      exitReason = "tp";
     }

   // PnL porcentual exacto sobre variación del precio subyacente
   double pnlPct = 0.0;
   if(entryPrice > 0.0)
     {
      double dir = isBuy ? 1.0 : -1.0;
      pnlPct = ((exitPrice - entryPrice) / entryPrice) * dir * 100.0;
     }

    // Enviar webhook de telemetría a FastAPI
    string body = "";
    StringConcatenate(body, "{",
       "\"symbol\":\"", Symbol(), "\",",
       "\"entry_time\":\"", IntegerToString(entryTime), "\",",
       "\"exit_time\":\"", IntegerToString(closeTime), "\",",
       "\"pnl\":", DoubleToString(pnl, 2), ",",
       "\"pnl_pct\":", DoubleToString(pnlPct, 4), ",",
       "\"direction\":\"", direction, "\",",
       "\"sl_hit\":", slHit ? "true" : "false", ",",
       "\"tp_hit\":", tpHit ? "true" : "false", ",",
       "\"exit_reason\":\"", exitReason, "\",",
       "\"deal_ticket\":", IntegerToString((long)dealTicket), ",",
       "\"position_id\":", IntegerToString(positionId),
       "}"
    );
    PrintFormat("[AI Terminal] Webhook body: %s", body);

    char postData[], resultArr[];
    string headers = "Content-Type: application/json\r\n"
                     "X-Internal-Token: " + InternalToken + "\r\n";
    int len = StringToCharArray(body, postData, 0, WHOLE_ARRAY, CP_UTF8);
    ArrayResize(postData, len - 1);

    string responseHeaders;
    int res = WebRequest("POST", FastAPI_URL + "/api/v1/ai/trade/filled", headers, 10000, postData, resultArr, responseHeaders);
    PrintFormat("[AI Terminal] Webhook HTTP res=%d Err=%d", res, GetLastError());
    if(res == 200)
      {
       g_lastClosedTicket = dealTicket;
       PrintFormat("[AI Terminal] Cierre reportado exitosamente. Ticket=%I64u PnL=%.2f Motivo=%s", dealTicket, pnl, exitReason);
      }
    else
      {
       PrintFormat("[AI Terminal] Error reportando trade cerrado HTTP %d (Err %d)", res, GetLastError());
      }
   }
//+------------------------------------------------------------------+
//| Desinicialización                                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_atrHandle != INVALID_HANDLE)
     {
      IndicatorRelease(g_atrHandle);
      g_atrHandle = INVALID_HANDLE;
     }
  }
//+------------------------------------------------------------------+