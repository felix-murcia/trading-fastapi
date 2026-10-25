/*
AI_Quant_Terminal_v3.mq5 (Refactorizado)
========================================
Terminal de Ejecución y Telemetría para Cerebro Autónomo (FastAPI + PPO v3).
- Rol del EA: Sensor de datos y ejecutor pasivo.
- Rol del Servidor: Decisión direccional, dimensionamiento de lotes, SL, TP y gestión de riesgo.
*/
#property copyright "AI Quant Terminal"
#property version   "15.0"
#property strict

#define EA_VERSION "15.0"

#include <Trade\Trade.mqh>

#define MAX_RETRIES             3
#define BASE_RETRY_DELAY_MS     1000
#define MAX_CIRCUIT_BREAKERS    5
#define CIRCUIT_BREAK_COOLDOWN  300
#define OUTBOX_FILE            "trade_filled_outbox.jsonl"
#define OUTBOX_REJECTED_FILE   "trade_filled_conflicts.jsonl"
#define OUTBOX_MAX_PER_FLUSH   10
// v14.0 (2026-10-21): el guard de CLOSE era 0.03 y bloqueaba el time-stop de
// posiciones de 0.02 (EURUSD, 21-10). El mínimo del bróker es 0.01; por
// debajo de eso no hay nada que cerrar.
#define MIN_CLOSE_VOLUME        0.01

input group "=== Configuración del Cerebro ==="
input string FastAPI_URL    = "http://127.0.0.1:8090"; // URL del servidor FastAPI
input string InternalToken  = "YOUR_INTERNAL_TOKEN";  // Token de autenticación
input bool   EnableAI       = true;                    // Habilitar operativa por IA
input ulong  MagicNumber    = 90001;                   // Identificador de órdenes
// G2-2 (2026-10-17): escalonado de sondeo por INSTANCIA (mundos separados
// por par): EURUSD 0 → poll en el 1er tick tras apertura M5 (:00/:05);
// GBPUSD 120 → :02/:07 ⇒ ≥2 s entre ciclos de los dos EA. La barra NO se
// consume hasta cumplirse el desfase. Con 0, la condición nunca cumple
// (TimeCurrent()-barra ≥ 0) ⇒ byte-idéntico al comportamiento previo.
input int    PollOffsetSeconds = 0;                    // Retardo del sondeo tras apertura M5 (s; 0 = 1er tick)

input group "=== Trailing Stop ==="
input bool   EnableTrailing  = true;                    // Activar trailing stop
input double TrailingPercent = 0.25;                    // % del riesgo original que se protege (0.25 = 25%)
input int    TrailingMinPips = 15;                      // Distancia mínima de trailing en pips
input int    TrailingATRMult  = 2;                      // Multiplicador ATR para distancia mínima dinámica
input int    TrailingATRPeriod = 14;                    // Periodo ATR para trailing dinámico
input int    TrailingManageEverySec = 5;                 // Cada cuántos segundos revisar trailing (0 = cada tick)

input group "=== Safe-4 (Félix 2026-10-15): bloqueo mínimo de +SL_LockSafetyNetEur ==="
input bool   EnableSafeLock      = true;    // Activar el cierre arriba de pérdida (lock mínimo)
input double SL_LockSafetyNetEur = 4.0;     // Beneficio en EUR mínimo para proteger el stop antes de 1R
input int    SafeLockTrailMinPips  = 15;      // Pips mínimos para el primer disparo de acoplamiento

input group "=== Breakeven ==="
input bool   EnableBreakeven = true;                    // Mover SL a la entrada cuando el precio recupera 1R (P1)

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
string EscapeJsonString(const string src)
  {
   string result = "";
   int len = StringLen(src);
   for(int i = 0; i < len; i++)
     {
      string ch = StringSubstr(src, i, 1);
      if(ch == "\\") result += "\\\\";
      else if(ch == "\"") result += "\\\"";
      else if(ch == "\n") result += "\\n";
      else if(ch == "\r") result += "\\r";
      else if(ch == "\t") result += "\\t";
      else result += ch;
     }
   return result;
  }

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
void ReportEADecision(const string cycle_id, const string received, const string action, const string raw)
  {
   string escaped = EscapeJsonString(raw);
   double mt5_now = (double)TimeCurrent();
   string body = StringFormat(
      "{\"cycle_id\":\"%s\",\"symbol\":\"%s\",\"received_decision\":\"%s\",\"action\":\"%s\",\"reason\":\"\",\"raw_response\":\"%s\",\"mt5_time\":%.3f}",
      cycle_id, Symbol(), received, action, escaped, mt5_now
   );
   char postData[], resultArr[];
   string headers = "Content-Type: application/json\r\n"
                    "X-Internal-Token: " + InternalToken + "\r\n";
   StringToCharArray(body, postData, 0, StringLen(body));
   ArrayResize(postData, StringLen(body));
   string responseHeaders;
   int res = WebRequest("POST", FastAPI_URL + "/api/v1/ai/ea/decision", headers, 10000, postData, resultArr, responseHeaders);
   PrintFormat("[AI Terminal] EA decision reported cycle=%s decision=%s action=%s HTTP=%d", cycle_id, received, action, res);
  }

//+------------------------------------------------------------------+
//| Gate del servidor (P0-3): ninguna orden se coloca sin pasar por   |
//| /v1/orders/prepare (idempotencia + Risk Guardian + validadores).  |
//| Fail-closed: si el gate no responde, NO se coloca la orden.       |
//+------------------------------------------------------------------+
bool RequestOrderPermission(const string cycle_id, const string order_type,
                            const double entry, const double sl, const double tp,
                            const double volume, long &orderDbId, string &refReason)
  {
   refReason = "";
   orderDbId = 0;
   string body = "";
   StringConcatenate(body, "{",
      "\"cycle_id\":\"", cycle_id, "\",",
      "\"symbol\":\"", Symbol(), "\",",
      "\"type\":\"", order_type, "\",",
      "\"entry\":", DoubleToString(entry, 5), ",",
      "\"sl\":", DoubleToString(sl, 5), ",",
      "\"tp\":", DoubleToString(tp, 5), ",",
      "\"volume\":", DoubleToString(volume, 2),
      "}");
   char postData[], resultArr[];
   string headers = "Content-Type: application/json\r\n"
                    "X-Internal-Token: " + InternalToken + "\r\n";
   StringToCharArray(body, postData, 0, StringLen(body));
   ArrayResize(postData, StringLen(body));
   string responseHeaders;
   int res = WebRequest("POST", FastAPI_URL + "/v1/orders/prepare", headers, 10000, postData, resultArr, responseHeaders);
   if(res != 200)
     {
      refReason = "gate_unreachable_http_" + IntegerToString(res);
      PrintFormat("[AI Terminal] GATE inaccesible (HTTP %d) — orden NO colocada (fail-closed). body=%s", res, body);
      return false;
     }
   string r = CharArrayToString(resultArr);
   if(GetJsonValue(r, "approved") == "true")
       {
        orderDbId = StringToInteger(GetJsonValue(r, "order_db_id"));
        if(orderDbId > 0)
           return true;
        refReason = "gate_missing_order_db_id";
        PrintFormat("[AI Terminal] GATE aprobó %s sin order_db_id — orden NO colocada", order_type);
        return false;
       }
   refReason = GetJsonValue(r, "rejection_reason");
   if(refReason == "") refReason = "rejected_no_reason";
   PrintFormat("[AI Terminal] GATE rechazó %s: %s | resp=%s", order_type, refReason, r);
   return false;
  }

//+------------------------------------------------------------------+
//| Outbox de cierres (P0-4 EA-side): si /trade/filled falla, el     |
//| cuerpo queda persistido en disco y se reintenta en cada ciclo.    |
//| El servidor deduplica por deal_ticket → reintento seguro.         |
//+------------------------------------------------------------------+
void SaveToOutbox(const string body)
  {
   int h = FileOpen(OUTBOX_FILE, FILE_WRITE|FILE_TXT, '\n');
   if(h == INVALID_HANDLE)
     {
      PrintFormat("[AI Terminal] OUTBOX: no se pudo abrir %s (err=%d) — cierre NO reportado", OUTBOX_FILE, GetLastError());
      return;
     }
   FileSeek(h, 0, SEEK_END);
   FileWrite(h, body);
   FileClose(h);
   PrintFormat("[AI Terminal] OUTBOX: cierre encolado para reintento: %s", body);
  }

bool SaveRejectedToOutbox(const string body)
  {
   int h = FileOpen(OUTBOX_REJECTED_FILE, FILE_READ|FILE_WRITE|FILE_TXT, '\n');
   if(h == INVALID_HANDLE)
      h = FileOpen(OUTBOX_REJECTED_FILE, FILE_WRITE|FILE_TXT, '\n');
   if(h == INVALID_HANDLE)
     {
      PrintFormat("[AI Terminal] OUTBOX: no se pudo guardar conflicto en %s (err=%d)",
                  OUTBOX_REJECTED_FILE, GetLastError());
      return false;
     }
   FileSeek(h, 0, SEEK_END);
   FileWrite(h, body);
   FileClose(h);
   return true;
  }

void FlushTradeOutbox()
  {
   if(!FileIsExist(OUTBOX_FILE)) return;

   int h = FileOpen(OUTBOX_FILE, FILE_READ|FILE_TXT);
   if(h == INVALID_HANDLE) return;

   string lines[];
   int count = 0;
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      if(StringLen(line) == 0) continue;
      ArrayResize(lines, count + 1);
      lines[count] = line;
      count++;
     }
   FileClose(h);
   if(count == 0)
     {
      FileDelete(OUTBOX_FILE);
      return;
     }

   string remaining[];
   int rem = 0, sent = 0, attempted = 0;
   for(int i = 0; i < count; i++)
     {
      bool keep = true;
      if(attempted < OUTBOX_MAX_PER_FLUSH)
        {
         char postData[], resultArr[];
         string headers = "Content-Type: application/json\r\n"
                          "X-Internal-Token: " + InternalToken + "\r\n";
         StringToCharArray(lines[i], postData, 0, StringLen(lines[i]));
         ArrayResize(postData, StringLen(lines[i]));
         string responseHeaders;
         int res = WebRequest("POST", FastAPI_URL + "/api/v1/ai/trade/filled", headers, 10000, postData, resultArr, responseHeaders);
         attempted++;
         if(res == 200)
           {
            keep = false;
            sent++;
            PrintFormat("[AI Terminal] OUTBOX: cierre reenviado OK (%d/%d)", sent, count);
           }
         else if(res == 409)
           {
            if(SaveRejectedToOutbox(lines[i]))
              {
               keep = false;
               PrintFormat("[AI Terminal] OUTBOX: HTTP 409 permanente — cierre apartado en %s",
                           OUTBOX_REJECTED_FILE);
              }
            else
               Print("[AI Terminal] OUTBOX: HTTP 409 no pudo ponerse en cuarentena; queda pendiente");
           }
         else
            PrintFormat("[AI Terminal] OUTBOX: reintento falló HTTP %d — queda pendiente", res);
        }
      if(keep)
        {
         ArrayResize(remaining, rem + 1);
         remaining[rem] = lines[i];
         rem++;
        }
     }

   if(rem == 0)
     {
      if(!FileDelete(OUTBOX_FILE))
         PrintFormat("[AI Terminal] OUTBOX: no se pudo eliminar %s (err=%d); los reintentos duplicados serán deduplicados por el servidor",
                     OUTBOX_FILE, GetLastError());
     }
   else
     {
      string temporaryFile = OUTBOX_FILE + ".tmp";
      int hw = FileOpen(temporaryFile, FILE_WRITE|FILE_TXT, '\n');
      if(hw == INVALID_HANDLE)
        {
         PrintFormat("[AI Terminal] OUTBOX: no se pudo crear %s (err=%d) — el outbox original se conserva",
                     temporaryFile, GetLastError());
         return;
        }
      for(int k = 0; k < rem; k++)
         FileWrite(hw, remaining[k]);
      FileClose(hw);
      if(!FileMove(temporaryFile, 0, OUTBOX_FILE, FILE_REWRITE))
        {
         PrintFormat("[AI Terminal] OUTBOX: no se pudo reemplazar %s (err=%d) — se conserva el original",
                     OUTBOX_FILE, GetLastError());
         FileDelete(temporaryFile);
         return;
        }
      PrintFormat("[AI Terminal] OUTBOX: %d cierres pendientes tras reintento", rem);
     }
  }

void OnTick()
  {
   // 0. TRAILING STOP — se ejecuta cada tick (o cada N segundos) de forma independiente
   if(EnableTrailing)
      ManageTrailingStop();

   // 0b. BREAKEVEN (P1) — SL a la entrada al +1R; se evalúa DESPUÉS del
   // trailing, en el mismo orden que el env (_apply_trailing →
   // _apply_breakeven): si el trailing ya puso el SL ≥ entrada, riskDist ≤ 0
   // y ManageBreakeven no hace nada (paridad D3).
   if(EnableBreakeven)
      ManageBreakeven();

   // Ejecutar exclusivamente en la apertura de una nueva barra M5
   datetime currentBar = iTime(Symbol(), PERIOD_M5, 0);
   if(currentBar == lastCheckedBar) return;
   // G2-2: desfase de sondeo por instancia — la barra NO se consume hasta
   // cumplirse el offset (con 0 la condición es siempre falsa ⇒ idéntico
   // al gate clásico de 1er tick tras la apertura).
   if(TimeCurrent() - currentBar < PollOffsetSeconds) return;
   lastCheckedBar = currentBar;

   if(!EnableAI || MQLInfoInteger(MQL_TESTER)) return;

   // Circuito de protección ante caídas de red
   if(g_consecutiveErrors >= MAX_CIRCUIT_BREAKERS && g_circuitBreakerReset > TimeCurrent())
     {
      PrintFormat("[AI Terminal] Circuit Breaker activo. Enfriamiento hasta: %s", TimeToString(g_circuitBreakerReset));
      return;
     }

   // Reintento de cierres pendientes (outbox) — el servidor deduplica por deal_ticket
   FlushTradeOutbox();

   // 1. RECOLECCIÓN DE TELEMETRÍA (Sensor)
   int currentPos = 0;       // 0=Flat, 1=Long, 2=Short
   double entryPrice = 0.0;
   double currentSL  = 0.0;
   double currentTP  = 0.0;
   int stepsInTrade  = 0;

   // G3 (2026-10-21, orden Félix «el sistema ha abierto una segunda
   // operación en USDCHF — contradice la política de una operación a la vez
   // por par»): ANTES se usaba `PositionSelect(Symbol())` + filtro por magic.
   // En una cuenta en modo HEDGING, `PositionSelect` elige UNA sola
   // posición (la de menor ticket) ⇒ si ésa no es la de este EA (otra
   // instancia, una operación manual o un cierre reciente), el recuento
   // `currentPos` quedaba en 0 CON una posición abierta ⇒ el veto de
   // posición (que viaja en este payload) no disparaba y el backend abría
   // una SEGUNDA operación del mismo par. Ahora se CUENTAN todas las
   // posiciones del símbolo con MI magic (y, si hay más de una —brecha
   // heredada—, se reporta la más reciente y la cuenta queda visible).
   int myPositions = 0;
   int foreignPositions = 0;
   ulong newestTicket = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != Symbol()) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber)
        {
         // v14.0 (2026-10-21): posición del símbolo con OTRO magic (heredada,
         // manual u otra instancia). El EA no la cuenta —su contrato es su
         // magic— pero la REPORTA para que el desfase sea visible; el backend
         // cuenta TODAS las posiciones del símbolo en MT5 y bloquea el duplicado.
         foreignPositions++;
         continue;
        }
      myPositions++;
      if(tk > newestTicket)
        {
         newestTicket = tk;
         long posType = PositionGetInteger(POSITION_TYPE);
         currentPos = (posType == POSITION_TYPE_BUY) ? 1 : 2;
         entryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
         currentSL  = PositionGetDouble(POSITION_SL);
         currentTP  = PositionGetDouble(POSITION_TP);
         datetime pTime = (datetime)PositionGetInteger(POSITION_TIME);
         if(pTime > 0)
            stepsInTrade = (int)((TimeCurrent() - pTime) / PeriodSeconds(PERIOD_M5));
        }
     }
   if(myPositions > 1)
      PrintFormat("[AI Terminal] %s: %d posiciones abiertas con MI magic (debería ser 1) — "
                  "la política de una por par está violada; el backend también la "
                  "aplica (tope por par = 1)", Symbol(), myPositions);
   if(foreignPositions > 0)
      PrintFormat("[AI Terminal] %s: %d posiciones del símbolo con OTRO magic — el EA "
                  "no las cuenta (su contrato es su magic); el backend las ve en MT5 "
                  "y bloquea el duplicado", Symbol(), foreignPositions);

     // Construcción del payload extendido (v14.0: magic + recuentos visibles; v15.0: quote para slippage)
     double mt5_now = (double)TimeCurrent();
     MqlTick tick;
     SymbolInfoTick(Symbol(), tick);
     string body = StringFormat(
        "{\"symbol\":\"%s\",\"timeframe\":\"M5\",\"position\":%d,\"entry_price\":%.5f,\"sl_price\":%.5f,\"tp_price\":%.5f,\"steps_in_trade\":%d,\"mt5_time\":%.3f,\"magic\":%d,\"my_positions\":%d,\"foreign_positions\":%d,\"bid\":%.5f,\"ask\":%.5f,\"quote_time\":%.3f}",
        Symbol(), currentPos, entryPrice, currentSL, currentTP, stepsInTrade, mt5_now,
        (int)MagicNumber, myPositions, foreignPositions,
        tick.bid, tick.ask, mt5_now
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
       res = WebRequest("POST", FastAPI_URL + "/api/v1/ai/predict", headers, 60000, postData, resultArr, responseHeaders);
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

    string cycle_id = GetJsonValue(r, "cycle_id");
    if(cycle_id == "") cycle_id = IntegerToString((long)TimeCurrent());

    // 3. EJECUCIÓN PURA DE LA ORDEN (Actuador)
    string decision = GetJsonValue(r, "decision");

     // A. Si la decisión es cerrar
     if(decision == "CLOSE")
       {
        double totalEAVolume = 0.0;
        for(int i = PositionsTotal() - 1; i >= 0; i--)
          {
           ulong t = PositionGetTicket(i);
           if(t <= 0) continue;
           if(PositionGetString(POSITION_SYMBOL) == Symbol() && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
              totalEAVolume += PositionGetDouble(POSITION_VOLUME);
          }
        if(totalEAVolume < MIN_CLOSE_VOLUME)
          {
           PrintFormat("[AI Terminal] CLOSE bloqueado: volumen EA %.2f < minimo %.2f",
                       totalEAVolume, MIN_CLOSE_VOLUME);
           ReportEADecision(cycle_id, decision, "hold", r);
           return;
          }
        Print("[AI Terminal] Orden de CIERRE ejecutada por el Cerebro.");
        CloseAllPositions();
        ReportEADecision(cycle_id, decision, "close", r);
        return;
       }

    // B. Si la decisión es mantener
    if(decision == "HOLD")
      {
       Print("[AI Terminal] Decisión HOLD. Sin cambios en mercado.");
       ReportEADecision(cycle_id, decision, "hold", r);
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
           ReportEADecision(cycle_id, decision, "rejected_invalid_params", r);
           return;
          }

        // Solo se permite ejecutar si no hay posición abierta.
        // Mientras haya posición activa, la salida debe ser exclusivamente por SL/TP.
        if(currentPos != 0)
          {
           PrintFormat("[AI Terminal] Decisión %s descartada: ya hay posición activa currentPos=%d", decision, currentPos);
           ReportEADecision(cycle_id, decision, "rejected_position_open", r);
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
        double spread  = SymbolInfoDouble(Symbol(), SYMBOL_ASK) - SymbolInfoDouble(Symbol(), SYMBOL_BID);

        double slDist  = slPips * pipSize;
        double tpDist  = tpPips * pipSize;

         long minStopLevel = SymbolInfoInteger(Symbol(), SYMBOL_TRADE_STOPS_LEVEL);
         double spreadDist = 3.0 * pipSize;
         double minStopDist = MathMax(minStopLevel * point, spreadDist);
         if(slDist < minStopDist) slDist = minStopDist;
         if(tpDist < minStopDist) tpDist = minStopDist;

         double buffer = MathMax(spread, 2.0 * pipSize);

         // Ejecutar BUY
         if(decision == "BUY")
           {
            if(currentPos != 0)
              {
               PrintFormat("[AI Terminal] Decisión BUY descartada: ya hay posición activa currentPos=%d", currentPos);
               ReportEADecision(cycle_id, decision, "rejected_position_open", r);
               return;
              }
            double ask = SymbolInfoDouble(Symbol(), SYMBOL_ASK);
            double slPrice = NormalizeDouble(ask - slDist - buffer, digits);
            double tpPrice = NormalizeDouble(ask + tpDist, digits);
            if(ask - slPrice < minStopDist) slPrice = NormalizeDouble(ask - minStopDist, digits);
            if(tpPrice - ask < minStopDist) tpPrice = NormalizeDouble(ask + minStopDist, digits);

            // P0-3: gate del servidor antes de colocar (fail-closed)
            string gateReason = "";
            long orderDbId = 0;
            if(!RequestOrderPermission(cycle_id, "BUY", ask, slPrice, tpPrice, lot, orderDbId, gateReason))
              {
               ReportEADecision(cycle_id, decision, "rejected_by_gate", r);
               return;
              }
            string orderComment = StringFormat("AIQ:%I64d", orderDbId);
            bool placed = trade.Buy(lot, Symbol(), ask, slPrice, tpPrice, orderComment);
            if(placed)
              {
               ReportEADecision(cycle_id, decision, "buy", r);
               PrintFormat("[AI Terminal] BUY placed lot=%.2f ticket=%I64u", lot, trade.ResultOrder());
              }
            else
              {
               ReportEADecision(cycle_id, decision, "error_buy", r);
               PrintFormat("[AI Terminal] ERROR BUY retcode=%d comment=%s | ask=%.5f sl=%.5f tp=%.5f",
                           (int)trade.ResultRetcode(), trade.ResultComment(), ask, slPrice, tpPrice);
              }
           }
         // Ejecutar SELL
         else if(decision == "SELL")
           {
            if(currentPos != 0)
              {
               PrintFormat("[AI Terminal] Decisión SELL descartada: ya hay posición activa currentPos=%d", currentPos);
               ReportEADecision(cycle_id, decision, "rejected_position_open", r);
               return;
              }
            double bid = SymbolInfoDouble(Symbol(), SYMBOL_BID);
            double slPrice = NormalizeDouble(bid + slDist + buffer, digits);
            double tpPrice = NormalizeDouble(bid - tpDist, digits);
            if(slPrice - bid < minStopDist) slPrice = NormalizeDouble(bid + minStopDist, digits);
            if(bid - tpPrice < minStopDist) tpPrice = NormalizeDouble(bid - minStopDist, digits);

            // P0-3: gate del servidor antes de colocar (fail-closed)
            string gateReason = "";
            long orderDbId = 0;
            if(!RequestOrderPermission(cycle_id, "SELL", bid, slPrice, tpPrice, lot, orderDbId, gateReason))
              {
               ReportEADecision(cycle_id, decision, "rejected_by_gate", r);
               return;
              }
            string orderComment = StringFormat("AIQ:%I64d", orderDbId);
            bool placed = trade.Sell(lot, Symbol(), bid, slPrice, tpPrice, orderComment);
            if(placed)
              {
               ReportEADecision(cycle_id, decision, "sell", r);
               PrintFormat("[AI Terminal] SELL placed lot=%.2f ticket=%I64u", lot, trade.ResultOrder());
              }
            else
              {
               ReportEADecision(cycle_id, decision, "error_sell", r);
               PrintFormat("[AI Terminal] ERROR SELL retcode=%d comment=%s | bid=%.5f sl=%.5f tp=%.5f",
                           (int)trade.ResultRetcode(), trade.ResultComment(), bid, slPrice, tpPrice);
              }
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

    long stopsLevel = (long)SymbolInfoInteger(Symbol(), SYMBOL_TRADE_STOPS_LEVEL);
    if(stopsLevel <= 0) stopsLevel = 10;
    double minStopDist = stopsLevel * point;

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
      double surgTickValue = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_VALUE);
      double surgTickSize = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_SIZE);
      double surgLot = PositionGetDouble(POSITION_VOLUME);
      double surgPipMoney = (surgTickSize > 0) ? (surgTickValue/surgTickSize) * pipSize * surgLot : 0.0;
      double riskMoney = (riskDist / pipSize) * surgPipMoney;
      if (EnableSafeLock && riskMoney < SL_LockSafetyNetEur) return;
      double minTrailPrice = minTrailPips * pipSize;
      double surgLockPips = (surgPipMoney > 0) ? (SL_LockSafetyNetEur / surgPipMoney) : 0.0;
      double surgLockDist = surgLockPips * pipSize;
      double newSL = NormalizeDouble(openPrice + MathMax(MathMax(trailDistPrice, minTrailPrice), surgLockDist), digits);

       double currentPr = SymbolInfoDouble(Symbol(), SYMBOL_ASK);
       if(currentPr >= (openPrice + riskDist) && newSL > currentSL && newSL <= (currentPr - minStopDist))
         {
          if(trade.PositionModify(ticket, newSL, currentTP))
            {
             modified = true;
             PrintFormat("[TRAILING] BUY ticket=%I64u SL movido %.5f -> %.5f (risk=%.1f pips, trail=%.1f pips)",
                         ticket, currentSL, newSL, riskDist/pipSize, (newSL-openPrice)/pipSize);
            }
          else
            {
             PrintFormat("[TRAILING] BUY modify failed ret=%d SL=%.5f TP=%.5f", trade.ResultRetcode(), newSL, currentTP);
            }
         }
      }
    else if(posType == POSITION_TYPE_SELL)
      {
       double riskDist = currentSL - openPrice;
       if(riskDist <= 0) return;
       double trailDistPrice = riskDist * TrailingPercent;
       double minTrailPrice = minTrailPips * pipSize;
       double surgTickValue = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_VALUE);
       double surgTickSize = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_SIZE);
       double surgLot = PositionGetDouble(POSITION_VOLUME);
       double surgPipMoney = (surgTickSize > 0) ? (surgTickValue/surgTickSize) * pipSize * surgLot : 0.0;
       double surgRiskMoney = (riskDist / pipSize) * surgPipMoney;
       if (EnableSafeLock && surgRiskMoney < SL_LockSafetyNetEur) return;
       double surgLockPips = (surgPipMoney > 0) ? (SL_LockSafetyNetEur / surgPipMoney) : 0.0;
       double surgLockDist = surgLockPips * pipSize;
       double newSL = NormalizeDouble(openPrice - MathMax(MathMax(trailDistPrice, minTrailPrice), surgLockDist), digits);

       double currentPr = SymbolInfoDouble(Symbol(), SYMBOL_BID);
       if(currentPr <= (openPrice - riskDist) && newSL < currentSL && newSL >= (currentPr + minStopDist))
         {
          if(trade.PositionModify(ticket, newSL, currentTP))
            {
             modified = true;
             PrintFormat("[TRAILING] SELL ticket=%I64u SL movido %.5f -> %.5f (risk=%.1f pips, trail=%.1f pips)",
                         ticket, currentSL, newSL, riskDist/pipSize, (openPrice-newSL)/pipSize);
            }
          else
            {
             PrintFormat("[TRAILING] SELL modify failed ret=%d SL=%.5f TP=%.5f", trade.ResultRetcode(), newSL, currentTP);
            }
         }
      }
  }


//+------------------------------------------------------------------+
//| Breakeven a 1R (P1): SL → entrada cuando el precio recupera el    |
//| riesgo inicial. Se ejecuta DESPUÉS de ManageTrailingStop (OnTick)  |
//| con los mismos guards que el env (`_apply_breakeven`, paridad D3): |
//| riskDist ≤ 0 → return (SL ya ≥ entrada: trailing o breakeven       |
//| previo) y guard de stops-level sobre el precio de cierre del       |
//| lado (BID para BUY / ASK para SELL, el precio que cierra).        |
//+------------------------------------------------------------------+
void ManageBreakeven()
  {
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
      return;   // sin SL colocado por el EA → nada que mover (igual que el trailing)

   long stopsLevel = (long)SymbolInfoInteger(Symbol(), SYMBOL_TRADE_STOPS_LEVEL);
   if(stopsLevel <= 0) stopsLevel = 10;
   double minStopDist = stopsLevel * point;

   if(posType == POSITION_TYPE_BUY)
     {
      double riskDist = openPrice - currentSL;
      if(riskDist <= 0) return;   // SL ya en beneficio (trailing/breakeven previo)
      double bid = SymbolInfoDouble(Symbol(), SYMBOL_BID);
      // Disparo: beneficio ≥ 1R (bid − entrada ≥ riesgo). Guard: SL a la
      // entrada exige al menos stopsLevel de distancia bajo el bid.
      if((bid - openPrice) >= riskDist && (openPrice + minStopDist) <= bid)
        {
         double _tv = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_VALUE);
         double _ts = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_SIZE);
         double _lot = PositionGetDouble(POSITION_VOLUME);
         double _pm = (_ts > 0)? (_tv/_ts) * pipSize * _lot : 0.0;
         double _lp = (_pm > 0)? (SL_LockSafetyNetEur / _pm) : 0.0;
         double newSL = NormalizeDouble(openPrice + _lp * pipSize, digits);
         if(trade.PositionModify(ticket, newSL, currentTP))
           {
            PrintFormat("[BREAKEVEN] BUY ticket=%I64u SL -> entrada %.5f (risk=%.1f pips, bid=%.5f)",
                        ticket, newSL, riskDist / pipSize, bid);
           }
         else
           {
            PrintFormat("[BREAKEVEN] BUY modify failed ret=%d SL=%.5f TP=%.5f", trade.ResultRetcode(), newSL, currentTP);
           }
        }
     }
   else if(posType == POSITION_TYPE_SELL)
     {
      double riskDist = currentSL - openPrice;
      if(riskDist <= 0) return;   // SL ya en beneficio (trailing/breakeven previo)
      double ask = SymbolInfoDouble(Symbol(), SYMBOL_ASK);
      // Disparo: beneficio ≥ 1R (entrada − ask ≥ riesgo). Guard: SL a la
      // entrada exige al menos stopsLevel de distancia sobre el ask.
      if((openPrice - ask) >= riskDist && (openPrice - minStopDist) >= ask)
        {
         double _tv = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_VALUE);
         double _ts = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_SIZE);
         double _lot = PositionGetDouble(POSITION_VOLUME);
         double _pm = (_ts > 0)? (_tv/_ts) * pipSize * _lot : 0.0;
         double _lp = (_pm > 0)? (SL_LockSafetyNetEur / _pm) : 0.0;
         double newSL = NormalizeDouble(openPrice - _lp * pipSize, digits);
         if(trade.PositionModify(ticket, newSL, currentTP))
           {
            PrintFormat("[BREAKEVEN] SELL ticket=%I64u SL -> entrada %.5f (risk=%.1f pips, ask=%.5f)",
                        ticket, newSL, riskDist / pipSize, ask);
           }
         else
           {
            PrintFormat("[BREAKEVEN] SELL modify failed ret=%d SL=%.5f TP=%.5f", trade.ResultRetcode(), newSL, currentTP);
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
      long currentEntry = -1;
      if(HistorySelectByPosition(positionId))
        {
         for(int i = 0; i < HistoryDealsTotal(); i++)
           {
            ulong hTicket = HistoryDealGetTicket(i);
            if(hTicket <= 0) continue;
            if(hTicket == dealTicket)
              {
               currentEntry = HistoryDealGetInteger(hTicket, DEAL_ENTRY);
               continue;
              }
            long hEntry = HistoryDealGetInteger(hTicket, DEAL_ENTRY);
            long hType = HistoryDealGetInteger(hTicket, DEAL_TYPE);
            if(hEntry == DEAL_ENTRY_IN && (hType == DEAL_TYPE_BUY || hType == DEAL_TYPE_SELL))
              {
               openingDeal = hTicket;
               break;
              }
           }
        }

      // v13.2: solo se reportan cierres. El deal de apertura (DEAL_ENTRY_IN,
      // beneficio 0 y entry==exit) duplicaba cada posición en trade_outcomes.
      // El backend también lo ignora (guard en POST /trade/filled).
      if(currentEntry == DEAL_ENTRY_IN)
        {
         PrintFormat("[AI Terminal] OnTradeTransaction descartado: deal=%I64u es apertura (DEAL_ENTRY_IN)", dealTicket);
         return;
        }

     if(openingDeal <= 0)
       {
        PrintFormat("[AI Terminal] OnTradeTransaction: openingDeal=0 positionId=%I64d deal=%I64u - registrando de todas formas",
                     positionId, dealTicket);
       }

     long openingMagic = HistoryDealGetInteger(openingDeal, DEAL_MAGIC);
     if(openingDeal > 0 && openingMagic != (long)MagicNumber)
       {
        PrintFormat("[AI Terminal] OnTradeTransaction: magic=%d esperado=%d - registrando de todas formas",
                     (int)openingMagic, (int)MagicNumber);
       }

      // Si llegamos aquí, procesamos el evento independientemente del magic/origin
      PrintFormat("[AI Terminal] Procesando OnTradeTransaction: positionId=%I64d deal=%I64u openingDeal=%I64u",
                  positionId, dealTicket, openingDeal);

     double entryPrice = 0.0;
     double exitPrice  = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
     double pnl        = HistoryDealGetDouble(dealTicket, DEAL_PROFIT);
     // v14.1 (2026-10-21): el SWAP (financiacion por mantener la posicion
     // abierta) es un coste CIERTO: sin el el backend registraba el pnl BRUTO
     // y el ledger/win-rate/expectativa perdian ese coste. Medido: -0,16 EUR
     // en la operacion EURUSD 76 (7,3 h) y -1,12 EUR acumulados en el
     // historico. El backend suma el swap al pnl (resultado real).
     double dealSwap   = HistoryDealGetDouble(dealTicket, DEAL_SWAP);
     long openingType  = DEAL_TYPE_SELL;
     datetime entryTime= 0;
     datetime closeTime= (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME);
     long orderDbId = 0;
     long openingOrderTicket = 0;

     if(openingDeal > 0)
      {
       entryPrice = HistoryDealGetDouble(openingDeal, DEAL_PRICE);
       openingType = HistoryDealGetInteger(openingDeal, DEAL_TYPE);
       entryTime = (datetime)HistoryDealGetInteger(openingDeal, DEAL_TIME);
       openingMagic = HistoryDealGetInteger(openingDeal, DEAL_MAGIC);
       openingOrderTicket = HistoryDealGetInteger(openingDeal, DEAL_ORDER);
       string openingComment = HistoryDealGetString(openingDeal, DEAL_COMMENT);
       if(StringFind(openingComment, "AIQ:") == 0)
         {
          string orderDbIdText = StringSubstr(openingComment, 4);
          long parsedOrderDbId = StringToInteger(orderDbIdText);
          if(parsedOrderDbId > 0 && orderDbIdText == StringFormat("%I64d", parsedOrderDbId))
             orderDbId = parsedOrderDbId;
         }
      }
    else
      {
       entryPrice = exitPrice;
       entryTime = closeTime;
      }

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
     else if(dealReason == DEAL_REASON_EXPERT || StringFind(comment, "AI-Predict") >= 0)
       {
        exitReason = "ea";
       }

    // PnL porcentual exacto sobre variación del precio subyacente
    double pnlPct = 0.0;
    if(entryPrice > 0.0)
      {
       double dir = isBuy ? 1.0 : -1.0;
       pnlPct = ((exitPrice - entryPrice) / entryPrice) * dir * 100.0;
      }

     string orderDbIdJson = orderDbId > 0 ? StringFormat("%I64d", orderDbId) : "null";
     if(orderDbId <= 0)
       {
        PrintFormat("[AI Terminal] No se pudo recuperar order_db_id del deal de apertura=%I64u positionId=%I64d; outcome quedará sin enlace exacto",
                    openingDeal, positionId);
       }

     // Enviar webhook de telemetría a FastAPI
     // P1.2b: costes de ejecución (commission del apertura, fee del cierre)
     double dealCommission = openingDeal > 0 ? HistoryDealDouble(openingDeal, DEAL_COMMISSION) : 0.0;
     double dealFee        = HistoryDealDouble(dealTicket, DEAL_FEE);
     string cycle_id       = IntegerToString((long)closeTime);
     string body = "";
    StringConcatenate(body, "{",
       "\"symbol\":\"", Symbol(), "\",",
       "\"entry_time\":\"", IntegerToString(entryTime), "\",",
       "\"exit_time\":\"", IntegerToString(closeTime), "\",",
       "\"pnl\":", DoubleToString(pnl, 2), ",",
       "\"swap\":", DoubleToString(dealSwap, 2), ",",
       "\"pnl_pct\":", DoubleToString(pnlPct, 4), ",",
       "\"direction\":\"", direction, "\",",
       "\"sl_hit\":", slHit ? "true" : "false", ",",
       "\"tp_hit\":", tpHit ? "true" : "false", ",",
       "\"exit_reason\":\"", exitReason, "\",",
       "\"deal_ticket\":", IntegerToString((long)dealTicket), ",",
       "\"position_id\":", IntegerToString(positionId), ",",
       "\"order_db_id\":", orderDbIdJson, ",",
       "\"opening_order_ticket\":", IntegerToString(openingOrderTicket), ",",
       "\"opening_deal\":", IntegerToString((long)openingDeal), ",",
       "\"opening_magic\":", IntegerToString(openingMagic), ",",
       "\"opening_deal_found\":", openingDeal > 0 ? "true" : "false", ",",
       "\"magic_match\":", (openingDeal > 0 && openingMagic == (long)MagicNumber) ? "true" : "false", ",
       "\"cycle_id\":\"", cycle_id, "\",",
       "\"entry_price\":", DoubleToString(entryPrice, 5), ",",
       "\"commission\":", DoubleToString(dealCommission, 2), ",",
       "\"fee\":", DoubleToString(dealFee, 2),
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
       SaveToOutbox(body);
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