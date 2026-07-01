//+------------------------------------------------------------------+
//| LiquidityGrabBridge.mq5                                          |
//| Estrategia: captura de liquidez (stop hunt + reversión)          |
//| Par objetivo: XAUUSD                                             |
//|                                                                  |
//| Un "liquidity grab" ocurre cuando el precio hace un spike por    |
//| encima/debajo de un swing previo (barriendo stops) y luego       |
//| revierte cerrando en el lado opuesto. Indica que los             |
//| institucionales recogieron liquidez y empujarán en contra.       |
//|                                                                  |
//| Señal SELL (bearish grab):                                       |
//|   high[1] > max_high de N velas previas   (spike sobre swing)   |
//|   close[1] < max_high                     (cierra de vuelta)     |
//|   (high[1] - close[1]) / range >= WickRatio  (mecha larga)      |
//|   (high[1] - max_high) >= MinSpikePips × pip  (spike mínimo)    |
//|   volumen[1] > promedio × VolumeMult                             |
//|   ADX entre AdxMinLevel y AdxMaxLevel                            |
//|                                                                  |
//| Señal BUY (bullish grab): lógica simétrica                       |
//|                                                                  |
//| SL anchor: high[1] (SELL) o low[1] (BUY) — el extremo del spike |
//+------------------------------------------------------------------+
#property copyright "Trading System"
#property version   "1.2"
#property strict

//--- Parámetros configurables
input string FastAPI_URL      = "http://YOUR_FASTAPI_IP:8090";
input string InternalToken    = "YOUR_INTERNAL_TOKEN";
input int    SendIntervalSec  = 10;
input string Timeframe        = "H1";
input int    EmaPeriod        = 50;    // solo para salida por EMA
input int    AdxPeriod        = 14;
input double AdxMinLevel      = 15.0;  // más permisivo para XAUUSD
input double AdxMaxLevel      = 65.0;
input int    LiquidityPeriod  = 10;    // velas para determinar swing previo
input double WickRatio        = 0.5;   // mínimo: mecha / rango total
input int    MinSpikePips     = 20;    // mínimo de pips que debe superar el swing
input int    VolumePeriod     = 20;
input double VolumeMult       = 1.5;   // volumen mínimo = promedio × VolumeMult
input int    EmaExitBuffer    = 20;    // pips (XAUUSD: 1 pip = 0.10)
input bool   EmaExitConfirm2  = true;
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
      PrintFormat("LiquidityGrabBridge: ERROR handle EMA(%d) code=%d", EmaPeriod, GetLastError());
      return INIT_FAILED;
     }

   adxHandle = iADX(Symbol(), Period(), AdxPeriod);
   if(adxHandle == INVALID_HANDLE)
     {
      PrintFormat("LiquidityGrabBridge: ERROR handle ADX(%d) code=%d", AdxPeriod, GetLastError());
      return INIT_FAILED;
     }

   EventSetTimer(SendIntervalSec);
   PrintFormat("LiquidityGrabBridge v1.2 en %s TF=%s | period=%d wick=%.1f spike=%dpips vol=%.1fx ADX[%.0f-%.0f]",
               Symbol(), Timeframe, LiquidityPeriod, WickRatio, MinSpikePips,
               VolumeMult, AdxMinLevel, AdxMaxLevel);
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
      PrintFormat("LiquidityGrabBridge: posición %s cerrada externamente — reset estado", activeDir);
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
               PrintFormat("LiquidityGrabBridge: TRAIL BUY close=%.2f sl: %.2f → %.2f",
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
               PrintFormat("LiquidityGrabBridge: TRAIL SELL close=%.2f sl: %.2f → %.2f",
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
         PrintFormat("LiquidityGrabBridge: NEWS-CHECK cerró posiciones — reset estado");
         activeDir = ""; activeSymbol = ""; closeRequestSent = false;
         activeEntry = 0; activeSlBase = 0; lastTrailBar = 0; emaCrossedOnce = false;
        }
      else if(DiagMode)
         PrintFormat("LiquidityGrabBridge: NEWS-CHECK ok — %s", CharArrayToString(result));
     }
   else if(res == -1)
      PrintFormat("LiquidityGrabBridge: NEWS-CHECK falló code=%d url=%s", GetLastError(), url);
   else if(DiagMode)
      PrintFormat("LiquidityGrabBridge: NEWS-CHECK HTTP %d", res);
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
               PrintFormat("LiquidityGrabBridge: EMA cruce 1/2 close=%.2f ema=%.2f buffer=%.2f",
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
            PrintFormat("LiquidityGrabBridge: EMA cruce cancelado close=%.2f ema=%.2f",
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

   PrintFormat("LiquidityGrabBridge: EXIT %s reason=%s close=%.2f ema=%.2f",
               activeDir, exitReason, closeVal, emaVal[0]);

   int httpCode = PostClose(Symbol(), exitReason);
   if(httpCode == 200)
     {
      closeRequestSent = true;
      activeDir = "";
      emaCrossedOnce = false;
      PrintFormat("LiquidityGrabBridge: cierre OK %s reason=%s", Symbol(), exitReason);
     }
   else
      PrintFormat("LiquidityGrabBridge: cierre ERROR HTTP %d", httpCode);
  }

//+------------------------------------------------------------------+
//| Detección de liquidity grab y envío de señal                     |
//+------------------------------------------------------------------+
void SendCurrentSignal()
  {
   //--- Datos de la última vela cerrada (índice 1)
   double signalOpen  = iOpen(Symbol(),  Period(), 1);
   double signalHigh  = iHigh(Symbol(),  Period(), 1);
   double signalLow   = iLow(Symbol(),   Period(), 1);
   double signalClose = iClose(Symbol(), Period(), 1);
   datetime barTime   = iTime(Symbol(),  Period(), 1);

   double range = signalHigh - signalLow;
   if(range <= 0) return;

   //--- Swing high/low de las N velas anteriores (índices 2..N+1)
   double prevHigh = -DBL_MAX;
   double prevLow  =  DBL_MAX;
   for(int i = 2; i <= LiquidityPeriod + 1; i++)
     {
      double h = iHigh(Symbol(), Period(), i);
      double l = iLow(Symbol(), Period(), i);
      if(h > prevHigh) prevHigh = h;
      if(l < prevLow)  prevLow  = l;
     }
   if(prevHigh <= 0 || prevLow >= DBL_MAX) return;

   double pip         = SymbolInfoDouble(Symbol(), SYMBOL_POINT) * 10;
   double minSpikeDist = MinSpikePips * pip;

   //--- Bearish grab: spike sobre el máximo previo, cierra de vuelta por debajo
   bool bearishGrab = (signalHigh > prevHigh)                                 // spike
                   && (signalClose < prevHigh)                                 // cierra debajo
                   && ((signalHigh - signalClose) / range >= WickRatio)        // mecha larga
                   && (signalHigh - prevHigh >= minSpikeDist);                 // spike mínimo

   //--- Bullish grab: spike bajo el mínimo previo, cierra de vuelta por encima
   bool bullishGrab  = (signalLow < prevLow)                                  // spike
                   && (signalClose > prevLow)                                  // cierra encima
                   && ((signalClose - signalLow) / range >= WickRatio)         // mecha larga
                   && (prevLow - signalLow >= minSpikeDist);                   // spike mínimo

   if(!bearishGrab && !bullishGrab)
     {
      if(DiagMode)
         PrintFormat("LiquidityGrabBridge: sin grab (H=%.2f L=%.2f C=%.2f prevH=%.2f prevL=%.2f)",
                     signalHigh, signalLow, signalClose, prevHigh, prevLow);
      return;
     }

   string dir      = bullishGrab ? "buy" : "sell";
   double slAnchor = bullishGrab ? signalLow : signalHigh;  // extremo del spike = SL natural
   double grabLevel = bullishGrab ? prevLow : prevHigh;     // nivel de liquidez barrido

   //--- Filtro de volumen: un grab real tiene volumen elevado
   long signalVol = iVolume(Symbol(), Period(), 1);
   long sumVol    = 0;
   for(int i = 2; i <= VolumePeriod + 1; i++)
      sumVol += iVolume(Symbol(), Period(), i);
   long avgVol = sumVol / VolumePeriod;
   if(signalVol < (long)(avgVol * VolumeMult))
     {
      if(DiagMode)
         PrintFormat("LiquidityGrabBridge: %s grab descartado — volumen insuficiente (%d < %d)",
                     dir, (int)signalVol, (int)(avgVol * VolumeMult));
      return;
     }

   //--- Filtro ADX: confirma que hay dinámica de mercado
   double adxVal[1];
   if(CopyBuffer(adxHandle, 0, 1, 1, adxVal) <= 0)
     {
      if(DiagMode) Print("LiquidityGrabBridge: sin datos ADX — señal descartada");
      return;
     }
   if(adxVal[0] < AdxMinLevel)
     {
      if(DiagMode) PrintFormat("LiquidityGrabBridge: ADX bajo %.1f < %.1f — mercado sin dinámica",
                                adxVal[0], AdxMinLevel);
      return;
     }
   if(adxVal[0] > AdxMaxLevel)
     {
      if(DiagMode) PrintFormat("LiquidityGrabBridge: ADX alto %.1f > %.1f — sobreextendido",
                                adxVal[0], AdxMaxLevel);
      return;
     }

   //--- No re-entrar en la misma dirección con posición activa
   if(activeDir != "" && activeDir == dir)
     {
      if(DiagMode) PrintFormat("LiquidityGrabBridge: señal %s ignorada — posición %s activa",
                                dir, activeDir);
      return;
     }

   //--- Deduplicación por vela
   string signalId = StringFormat("LG_%s_%s_%d", Symbol(), dir, (long)barTime);
   if(signalId == lastSentSignalId)
     {
      if(DiagMode) PrintFormat("LiquidityGrabBridge: sin cambio (%s)", lastSentSignalId);
      return;
     }

   //--- Precio de entrada actual
   double entryPrice = (dir == "buy")
                       ? SymbolInfoDouble(Symbol(), SYMBOL_ASK)
                       : SymbolInfoDouble(Symbol(), SYMBOL_BID);

   //--- Métricas del grab para diagnóstico
   double spikeSize  = bullishGrab ? (prevLow - signalLow) : (signalHigh - prevHigh);
   double wickSize   = bullishGrab ? (signalClose - signalLow) : (signalHigh - signalClose);
   double wickRatioActual = wickSize / range;

   string body = StringFormat(
     "{\"symbol\":\"%s\",\"entry_zone\":true,\"direction\":\"%s\","
      "\"zone_high\":%.5f,\"zone_low\":%.5f,"
      "\"timeframe\":\"%s\",\"source\":\"liquidity_grab\","
      "\"signal_id\":\"%s\"}",
     Symbol(), dir, entryPrice, slAnchor, Timeframe, signalId
   );

   PrintFormat("LiquidityGrabBridge: %s %s grab | level=%.2f spike=%.2f wick=%.0f%% | entry=%.2f slAnchor=%.2f | adx=%.1f vol=%d(avg=%d)",
               Symbol(), dir, grabLevel, spikeSize / pip, wickRatioActual * 100,
               entryPrice, slAnchor, adxVal[0], (int)signalVol, (int)avgVol);

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
      PrintFormat("LiquidityGrabBridge: OK — %s", signalId);
     }
   else
      PrintFormat("LiquidityGrabBridge: ERROR HTTP %d — señal no registrada", httpCode);
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
      PrintFormat("LiquidityGrabBridge ERROR: WebRequest falló code=%d", GetLastError());
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
      PrintFormat("LiquidityGrabBridge ERROR: WebRequest close falló code=%d", GetLastError());
   return res;
  }
//+------------------------------------------------------------------+
