/*
AI_Quant_Terminal_v3.mq5 (Refactorizado)
========================================
Terminal de Ejecución y Telemetría para Cerebro Autónomo (FastAPI + PPO v3).
- Rol del EA: Sensor de datos y ejecutor pasivo.
- Rol del Servidor: Decisión direccional, dimensionamiento de lotes, SL, TP y gestión de riesgo.
*/
#property copyright "AI Quant Terminal v3"
#property version   "12.0"
#property strict

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

CTrade   trade;
datetime lastCheckedBar = 0;
datetime g_circuitBreakerReset = 0;
int      g_consecutiveErrors = 0;
ulong    g_lastClosedTicket = 0;

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
   PrintFormat("[AI Terminal] Iniciado v12.0. Conectando a %s", FastAPI_URL);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
//| Ciclo Principal (Por barra cerrada de H1)                        |
//+------------------------------------------------------------------+
void OnTick()
  {
   // Ejecutar exclusivamente en la apertura de una nueva barra
   datetime currentBar = iTime(Symbol(), PERIOD_CURRENT, 0);
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
            stepsInTrade = (int)((TimeCurrent() - pTime) / PeriodSeconds(PERIOD_CURRENT));
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

       // Ejecutar BUY
       if(decision == "BUY")
         {
          if(currentPos == 2) CloseAllPositions(); // Si estaba vendido, cierra primero
          if(currentPos != 1)
            {
             double ask = SymbolInfoDouble(Symbol(), SYMBOL_ASK);
             double spread = (ask - SymbolInfoDouble(Symbol(), SYMBOL_BID));
             double slPrice = NormalizeDouble(ask - slDist - spread, digits);
             double tpPrice = NormalizeDouble(ask + tpDist + spread, digits);
             bool placed = trade.Buy(lot, Symbol(), ask, slPrice, tpPrice, "AI-Predict-v3");
             if(!placed)
               PrintFormat("[AI Terminal] ERROR BUY retcode=%d comment=%s | ask=%.5f sl=%.5f tp=%.5f slDist=%.5f tpDist=%.5f spread=%.5f minStop=%.5f",
                            (int)trade.ResultRetcode(), trade.ResultComment(), ask, slPrice, tpPrice, slDist, tpDist, spread, minStopDist);
            }
         }
       // Ejecutar SELL
       else if(decision == "SELL")
         {
          if(currentPos == 1) CloseAllPositions(); // Si estaba comprado, cierra primero
          if(currentPos != 2)
            {
             double bid = SymbolInfoDouble(Symbol(), SYMBOL_BID);
             double spread = (SymbolInfoDouble(Symbol(), SYMBOL_ASK) - bid);
             double slPrice = NormalizeDouble(bid + slDist + spread, digits);
             double tpPrice = NormalizeDouble(bid - tpDist - spread, digits);
             bool placed = trade.Sell(lot, Symbol(), bid, slPrice, tpPrice, "AI-Predict-v3");
             if(!placed)
               PrintFormat("[AI Terminal] ERROR SELL retcode=%d comment=%s | bid=%.5f sl=%.5f tp=%.5f slDist=%.5f tpDist=%.5f spread=%.5f minStop=%.5f",
                            (int)trade.ResultRetcode(), trade.ResultComment(), bid, slPrice, tpPrice, slDist, tpDist, spread, minStopDist);
            }
         }
     }
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

    long dealEntry = HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
    if(dealEntry != DEAL_ENTRY_OUT && dealEntry != DEAL_ENTRY_OUT_BY)
      {
       PrintFormat("[AI Terminal] OnTradeTransaction descartado: dealEntry=%d ticket=%I64u", (int)dealEntry, dealTicket);
       return;
      }

    if(dealTicket == g_lastClosedTicket)
      {
       PrintFormat("[AI Terminal] OnTradeTransaction descartado: duplicado ticket=%I64u", dealTicket);
       return;
      }

    long positionId = HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);
    if(positionId <= 0) positionId = (long)trans.position;

    datetime historyFrom = (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME) - 86400 * 30;
    datetime historyTo   = (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME) + 60;

    ulong openingDeal = 0;
    if(HistorySelect(historyFrom, historyTo))
      {
       for(int i = 0; i < HistoryDealsTotal(); i++)
         {
          ulong hTicket = HistoryDealGetTicket(i);
          if(hTicket <= 0) continue;
          if(HistoryDealGetInteger(hTicket, DEAL_POSITION_ID) == positionId &&
             HistoryDealGetInteger(hTicket, DEAL_ENTRY) == DEAL_ENTRY_IN)
            {
             openingDeal = hTicket;
             break;
            }
         }
      }

    if(openingDeal <= 0)
      {
       PrintFormat("[AI Terminal] OnTradeTransaction descartado: openingDeal=0 positionId=%I64d ticket=%I64u", positionId, dealTicket);
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
    StringToCharArray(body, postData, 0, StringLen(body));
    ArrayResize(postData, StringLen(body));

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