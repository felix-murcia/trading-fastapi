//+------------------------------------------------------------------+
//| StructureBreakBridge.mq5                                         |
//| Estrategia: rotura de estructura de N velas + volumen            |
//| Pares objetivo: EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, NZDUSD |
//|                                                                  |
//| Señal BUY:  close[1] > máximo de las N velas anteriores         |
//|             + volumen[1] > promedio × VolumeMult                 |
//|             + close[1] > EMA50[1]                                |
//|             + ADX entre AdxMinLevel y AdxMaxLevel                |
//|                                                                  |
//| Señal SELL: close[1] < mínimo de las N velas anteriores         |
//|             + mismos filtros                                      |
//|                                                                  |
//| SL anchor: low[1] (BUY) o high[1] (SELL) de la vela de rotura   |
//| FastAPI calcula SL = anchor ± base_dist × SL_MULT               |
//+------------------------------------------------------------------+
#property copyright "Trading System"
#property version   "1.0"
#property strict

//--- Parámetros configurables
input string FastAPI_URL      = "http://YOUR_FASTAPI_IP:8090";
input string InternalToken    = "YOUR_INTERNAL_TOKEN";
input int    SendIntervalSec  = 10;
input string Timeframe        = "H1";
input int    EmaPeriod        = 50;
input int    AdxPeriod        = 14;
input double AdxMinLevel      = 20.0;
input double AdxMaxLevel      = 50.0;
input int    StructurePeriod  = 20;    // velas para determinar swing high/low previo
input int    VolumePeriod     = 20;    // velas para calcular volumen medio
input double VolumeMult       = 1.5;   // volumen mínimo = promedio × VolumeMult
input int    EmaExitBuffer    = 10;    // pips al otro lado de la EMA para salir
input bool   EmaExitConfirm2  = true;  // exigir 2 velas consecutivas para cerrar por EMA
input bool   DiagMode         = false;

//--- Handles
int emaHandle = INVALID_HANDLE;
int adxHandle = INVALID_HANDLE;

//--- Estado interno
string lastSentSignalId = "";
string activeDir        = "";
string activeSymbol     = "";
bool   closeRequestSent = false;
double activeEntry      = 0;
double activeSlBase     = 0;
datetime lastTrailBar   = 0;
bool   emaCrossedOnce   = false;
int    newsCheckCounter = 0;
#define NEWS_CHECK_INTERVAL 30   // 30 ticks × 10s = 5 min

//+------------------------------------------------------------------+
int OnInit()
  {
   emaHandle = iMA(Symbol(), Period(), EmaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(emaHandle == INVALID_HANDLE)
     {
      PrintFormat("StructureBreakBridge: ERROR handle EMA(%d) code=%d", EmaPeriod, GetLastError());
      return INIT_FAILED;
     }

   adxHandle = iADX(Symbol(), Period(), AdxPeriod);
   if(adxHandle == INVALID_HANDLE)
     {
      PrintFormat("StructureBreakBridge: ERROR handle ADX(%d) code=%d", AdxPeriod, GetLastError());
      return INIT_FAILED;
     }

   EventSetTimer(SendIntervalSec);
   PrintFormat("StructureBreakBridge v1.0 en %s TF=%s EMA=%d ADX(%d)[%.0f-%.0f] struct=%d vol=%.1fx",
               Symbol(), Timeframe, EmaPeriod, AdxPeriod, AdxMinLevel, AdxMaxLevel,
               StructurePeriod, VolumeMult);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(emaHandle != INVALID_HANDLE) IndicatorRelease(emaHandle);
   if(adxHandle != INVALID_HANDLE) IndicatorRelease(adxHandle);
  }

void OnTimer() { ManageTrailing(); CheckNewsExit(); CheckEmaExit(); SendCurrentSignal(); }

//+------------------------------------------------------------------+
//| Trailing stop proporcional al cierre de vela                     |
//+------------------------------------------------------------------+
void ManageTrailing()
  {
   if(activeDir == "") return;

   //--- Detectar cierre externo (SL hit, cierre manual en MT5)
   bool posFound = false;
   for(int k = PositionsTotal() - 1; k >= 0; k--)
      if(PositionGetSymbol(k) == Symbol()) { posFound = true; break; }
   if(!posFound)
     {
      PrintFormat("StructureBreakBridge: posición %s cerrada externamente — reset estado", activeDir);
      activeDir = ""; activeSymbol = ""; activeSlBase = 0;
      lastTrailBar = 0; emaCrossedOnce = false; closeRequestSent = false;
      return;
     }

   //--- Solo ejecutar una vez por vela cerrada
   datetime barTime = iTime(Symbol(), Period(), 1);
   if(barTime == lastTrailBar) return;
   lastTrailBar = barTime;

   double closeVal = iClose(Symbol(), Period(), 1);
   int    digits   = (int)SymbolInfoInteger(Symbol(), SYMBOL_DIGITS);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(PositionGetSymbol(i) != Symbol()) continue;
      ulong  ticket  = PositionGetInteger(POSITION_TICKET);
      double posSL   = PositionGetDouble(POSITION_SL);
      double posTP   = PositionGetDouble(POSITION_TP);
      long   posType = PositionGetInteger(POSITION_TYPE);

      if(activeSlBase == 0) activeSlBase = posSL;

      if(activeDir == "buy" && posType == POSITION_TYPE_BUY)
        {
         if(closeVal > activeEntry)
           {
            double newSL = NormalizeDouble(activeSlBase + (closeVal - activeEntry), digits);
            if(newSL <= posSL) break;
            MqlTradeRequest req = {}; MqlTradeResult res = {};
            req.action = TRADE_ACTION_SLTP; req.position = ticket;
            req.symbol = Symbol(); req.sl = newSL; req.tp = posTP;
            if(OrderSend(req, res))
               PrintFormat("StructureBreakBridge: TRAIL BUY close=%.5f sl: %.5f → %.5f",
                           closeVal, posSL, newSL);
           }
         break;
        }
      else if(activeDir == "sell" && posType == POSITION_TYPE_SELL)
        {
         if(closeVal < activeEntry)
           {
            double newSL = NormalizeDouble(activeSlBase - (activeEntry - closeVal), digits);
            if(newSL >= posSL) break;
            MqlTradeRequest req = {}; MqlTradeResult res = {};
            req.action = TRADE_ACTION_SLTP; req.position = ticket;
            req.symbol = Symbol(); req.sl = newSL; req.tp = posTP;
            if(OrderSend(req, res))
               PrintFormat("StructureBreakBridge: TRAIL SELL close=%.5f sl: %.5f → %.5f",
                           closeVal, posSL, newSL);
           }
         break;
        }
      else break;
     }
  }

//+------------------------------------------------------------------+
//| Cierre proactivo por noticias fundamentales                      |
//+------------------------------------------------------------------+
void CheckNewsExit()
  {
   newsCheckCounter++;
   if(newsCheckCounter < NEWS_CHECK_INTERVAL) return;
   newsCheckCounter = 0;

   char   postData[];
   char   result[];
   string headers = "Content-Type: application/json\r\n"
                    "X-Internal-Token: " + InternalToken + "\r\n";
   string body = "{}";
   StringToCharArray(body, postData, 0, StringLen(body));
   ArrayResize(postData, StringLen(body));

   string url = FastAPI_URL + "/v1/smc/news-check";
   string responseHeaders;
   int res = WebRequest("POST", url, headers, 5000, postData, result, responseHeaders);

   if(res == 200)
     {
      string response = CharArrayToString(result);
      if(StringFind(response, "\"action\":\"closed\"") >= 0)
        {
         PrintFormat("StructureBreakBridge: NEWS-CHECK cerró posiciones — reset estado");
         activeDir = ""; activeSymbol = ""; closeRequestSent = false;
         activeEntry = 0; activeSlBase = 0; lastTrailBar = 0; emaCrossedOnce = false;
        }
      else if(DiagMode)
         PrintFormat("StructureBreakBridge: NEWS-CHECK ok — %s", CharArrayToString(result));
     }
   else if(res == -1)
      PrintFormat("StructureBreakBridge: NEWS-CHECK falló code=%d url=%s", GetLastError(), url);
   else if(DiagMode)
      PrintFormat("StructureBreakBridge: NEWS-CHECK HTTP %d", res);
  }

//+------------------------------------------------------------------+
//| Salida por cruce de EMA o ADX exhausto/sobreextendido           |
//+------------------------------------------------------------------+
void CheckEmaExit()
  {
   if(activeDir == "" || closeRequestSent) return;

   double emaVal[1];
   if(CopyBuffer(emaHandle, 0, 1, 1, emaVal) <= 0) return;

   double closeVal   = iClose(Symbol(), Period(), 1);
   double pip        = SymbolInfoDouble(Symbol(), SYMBOL_POINT) * 10;
   double bufferDist = EmaExitBuffer * pip;

   bool emaCross = false;
   if(activeDir == "buy")  emaCross = (closeVal < emaVal[0] - bufferDist);
   else if(activeDir == "sell") emaCross = (closeVal > emaVal[0] + bufferDist);

   string exitReason = "";

   if(emaCross)
     {
      if(EmaExitConfirm2)
        {
         if(!emaCrossedOnce)
           {
            emaCrossedOnce = true;
            if(DiagMode)
               PrintFormat("StructureBreakBridge: EMA cruce 1/2 close=%.5f ema=%.5f buffer=%.5f",
                           closeVal, emaVal[0], bufferDist);
           }
         else
            exitReason = "ema_cross";
        }
      else
         exitReason = "ema_cross";
     }
   else
     {
      if(emaCrossedOnce)
        {
         emaCrossedOnce = false;
         if(DiagMode)
            PrintFormat("StructureBreakBridge: EMA cruce cancelado close=%.5f ema=%.5f",
                        closeVal, emaVal[0]);
        }
     }

   //--- ADX: salida si la tendencia se agota o sobreextiende
   if(exitReason == "")
     {
      double adxVal[1];
      if(CopyBuffer(adxHandle, 0, 1, 1, adxVal) > 0)
        {
         if(adxVal[0] < AdxMinLevel)
            exitReason = StringFormat("adx_low_%.1f", adxVal[0]);
         else if(adxVal[0] > AdxMaxLevel)
            exitReason = StringFormat("adx_high_%.1f", adxVal[0]);
        }
     }

   if(exitReason == "") return;

   PrintFormat("StructureBreakBridge: EXIT %s reason=%s close=%.5f ema=%.5f",
               activeDir, exitReason, closeVal, emaVal[0]);

   int httpCode = PostClose(Symbol(), exitReason);
   if(httpCode == 200)
     {
      closeRequestSent = true;
      activeDir = "";
      emaCrossedOnce = false;
      PrintFormat("StructureBreakBridge: cierre OK %s reason=%s", Symbol(), exitReason);
     }
   else
      PrintFormat("StructureBreakBridge: cierre ERROR HTTP %d", httpCode);
  }

//+------------------------------------------------------------------+
//| Detección de rotura de estructura y envío de señal               |
//+------------------------------------------------------------------+
void SendCurrentSignal()
  {
   //--- Datos de la última vela cerrada (índice 1)
   double signalClose = iClose(Symbol(), Period(), 1);
   double signalHigh  = iHigh(Symbol(), Period(), 1);
   double signalLow   = iLow(Symbol(), Period(), 1);
   datetime barTime   = iTime(Symbol(), Period(), 1);

   if(signalClose <= 0) return;

   //--- Swing high/low de las N velas anteriores a la vela de señal (índices 2..N+1)
   double prevHigh = -DBL_MAX;
   double prevLow  =  DBL_MAX;
   for(int i = 2; i <= StructurePeriod + 1; i++)
     {
      double h = iHigh(Symbol(), Period(), i);
      double l = iLow(Symbol(), Period(), i);
      if(h > prevHigh) prevHigh = h;
      if(l < prevLow)  prevLow  = l;
     }
   if(prevHigh <= 0 || prevLow >= DBL_MAX) return;

   //--- Detección: ¿cerró por encima del máximo previo o por debajo del mínimo?
   bool isBuy  = (signalClose > prevHigh);
   bool isSell = (signalClose < prevLow);
   if(!isBuy && !isSell)
     {
      if(DiagMode) PrintFormat("StructureBreakBridge: sin rotura (close=%.5f prevH=%.5f prevL=%.5f)",
                                signalClose, prevHigh, prevLow);
      return;
     }

   string dir      = isBuy ? "buy" : "sell";
   double breakLevel = isBuy ? prevHigh : prevLow;
   double slAnchor   = isBuy ? signalLow : signalHigh;

   //--- Filtro de volumen
   long signalVol = iVolume(Symbol(), Period(), 1);
   long sumVol    = 0;
   for(int i = 2; i <= VolumePeriod + 1; i++)
      sumVol += iVolume(Symbol(), Period(), i);
   long avgVol = sumVol / VolumePeriod;
   if(signalVol < (long)(avgVol * VolumeMult))
     {
      if(DiagMode) PrintFormat("StructureBreakBridge: %s descartada — volumen insuficiente (%d < %d×%.1f=%d)",
                                dir, (int)signalVol, (int)avgVol, VolumeMult, (int)(avgVol * VolumeMult));
      return;
     }

   //--- Filtro EMA: vela cerrada en el lado correcto de la tendencia
   double emaVal[1];
   if(CopyBuffer(emaHandle, 0, 1, 1, emaVal) <= 0)
     {
      if(DiagMode) Print("StructureBreakBridge: sin datos EMA — señal descartada");
      return;
     }
   if(dir == "buy"  && signalClose <= emaVal[0])
     {
      if(DiagMode) PrintFormat("StructureBreakBridge: BUY descartado por EMA (close=%.5f <= ema=%.5f)",
                                signalClose, emaVal[0]);
      return;
     }
   if(dir == "sell" && signalClose >= emaVal[0])
     {
      if(DiagMode) PrintFormat("StructureBreakBridge: SELL descartado por EMA (close=%.5f >= ema=%.5f)",
                                signalClose, emaVal[0]);
      return;
     }

   //--- Filtro ADX
   double adxVal[1];
   if(CopyBuffer(adxHandle, 0, 1, 1, adxVal) <= 0)
     {
      if(DiagMode) Print("StructureBreakBridge: sin datos ADX — señal descartada");
      return;
     }
   if(adxVal[0] < AdxMinLevel)
     {
      if(DiagMode) PrintFormat("StructureBreakBridge: ADX bajo %.1f < %.1f — mercado lateral",
                                adxVal[0], AdxMinLevel);
      return;
     }
   if(adxVal[0] > AdxMaxLevel)
     {
      if(DiagMode) PrintFormat("StructureBreakBridge: ADX alto %.1f > %.1f — sobreextendido",
                                adxVal[0], AdxMaxLevel);
      return;
     }

   //--- No re-entrar en la misma dirección con posición activa
   if(activeDir != "" && activeDir == dir)
     {
      if(DiagMode) PrintFormat("StructureBreakBridge: señal %s ignorada — posición %s ya activa",
                                dir, activeDir);
      return;
     }

   //--- Deduplicación por vela
   string signalId = StringFormat("SB_%s_%s_%d", Symbol(), dir, (long)barTime);
   if(signalId == lastSentSignalId)
     {
      if(DiagMode) PrintFormat("StructureBreakBridge: sin cambio (%s)", lastSentSignalId);
      return;
     }

   //--- Precio de entrada actual
   double entryPrice = (dir == "buy")
                       ? SymbolInfoDouble(Symbol(), SYMBOL_ASK)
                       : SymbolInfoDouble(Symbol(), SYMBOL_BID);

   string body = StringFormat(
     "{\"symbol\":\"%s\",\"entry_zone\":true,\"direction\":\"%s\","
      "\"zone_high\":%.5f,\"zone_low\":%.5f,"
      "\"timeframe\":\"%s\",\"source\":\"structure_break\","
      "\"signal_id\":\"%s\"}",
     Symbol(), dir, entryPrice, slAnchor, Timeframe, signalId
   );

   PrintFormat("StructureBreakBridge: %s %s | break=%.5f entry=%.5f slAnchor=%.5f | adx=%.1f vol=%d(avg=%d×%.1f)",
               Symbol(), dir, breakLevel, entryPrice, slAnchor,
               adxVal[0], (int)signalVol, (int)avgVol, VolumeMult);

   int httpCode = PostToFastAPI(body);
   if(httpCode == 200)
     {
      lastSentSignalId = signalId;
      activeDir        = dir;
      activeSymbol     = Symbol();
      closeRequestSent = false;
      activeEntry      = entryPrice;
      activeSlBase     = 0;
      lastTrailBar     = 0;
      emaCrossedOnce   = false;
      PrintFormat("StructureBreakBridge: OK — %s", signalId);
     }
   else
      PrintFormat("StructureBreakBridge: ERROR HTTP %d — señal no registrada", httpCode);
  }

//+------------------------------------------------------------------+
int PostToFastAPI(string body)
  {
   char   postData[];
   char   result[];
   string headers = "Content-Type: application/json\r\n"
                    "X-Internal-Token: " + InternalToken + "\r\n";
   StringToCharArray(body, postData, 0, StringLen(body));
   ArrayResize(postData, StringLen(body));
   string url = FastAPI_URL + "/v1/smc/signal";
   string responseHeaders;
   int res = WebRequest("POST", url, headers, 5000, postData, result, responseHeaders);
   if(res == -1)
      PrintFormat("StructureBreakBridge ERROR: WebRequest falló code=%d", GetLastError());
   return res;
  }

//+------------------------------------------------------------------+
int PostClose(string symbol, string reason)
  {
   char   postData[];
   char   result[];
   string headers = "Content-Type: application/json\r\n"
                    "X-Internal-Token: " + InternalToken + "\r\n";
   string body = StringFormat("{\"symbol\":\"%s\",\"reason\":\"%s\"}", symbol, reason);
   StringToCharArray(body, postData, 0, StringLen(body));
   ArrayResize(postData, StringLen(body));
   string url = FastAPI_URL + "/v1/smc/close";
   string responseHeaders;
   int res = WebRequest("POST", url, headers, 5000, postData, result, responseHeaders);
   if(res == -1)
      PrintFormat("StructureBreakBridge ERROR: WebRequest close falló code=%d", GetLastError());
   return res;
  }
//+------------------------------------------------------------------+
