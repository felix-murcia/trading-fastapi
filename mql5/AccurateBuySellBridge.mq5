//+------------------------------------------------------------------+
//| AccurateBuySellBridge.mq5                                        |
//| Lee señales de "Accurate Buy Sell System" (buffers 2=BUY,         |
//| 3=SELL) y las envía a FastAPI, igual que SignalBridge.mq5.        |
//+------------------------------------------------------------------+
#property copyright "Trading System"
#property version   "1.0"
#property strict

//--- Parámetros configurables
input string FastAPI_URL      = "http://100.91.167.17:8090";
input string InternalToken    = "90c42a9448defc1f57K8WGdyb3FYaXgTw00gCVKY9JhfFG1A9tfji";
input int    SendIntervalSec  = 10;
input string Timeframe        = "H1";
input string IndicatorName    = "Accurate Buy Sell System";
input int    EmaPeriod        = 50;
input int    AdxPeriod        = 14;
input double AdxMinLevel      = 20.0;
input bool   DiagMode         = false;

//--- Buffers del indicador (descubiertos por diagnóstico)
#define BUF_BUY  2
#define BUF_SELL 3

//--- Estado interno
int    indicatorHandle = INVALID_HANDLE;
int    emaHandle       = INVALID_HANDLE;
int    emaH4Handle     = INVALID_HANDLE;
int    adxHandle       = INVALID_HANDLE;
string lastSentSignalId = "";
string activeDir       = "";   // "buy" o "sell" si hay posición abierta por este EA
string activeSymbol    = "";
bool   closeRequestSent = false;

//+------------------------------------------------------------------+
int OnInit()
  {
   indicatorHandle = iCustom(Symbol(), Period(), IndicatorName);
   if(indicatorHandle == INVALID_HANDLE)
     {
      PrintFormat("AccurateBuySellBridge: ERROR no se pudo crear handle para '%s' — code=%d",
                  IndicatorName, GetLastError());
      return INIT_FAILED;
     }

   emaHandle = iMA(Symbol(), Period(), EmaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(emaHandle == INVALID_HANDLE)
     {
      PrintFormat("AccurateBuySellBridge: ERROR no se pudo crear handle EMA(%d) — code=%d",
                  EmaPeriod, GetLastError());
      return INIT_FAILED;
     }

   emaH4Handle = iMA(Symbol(), PERIOD_H4, EmaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(emaH4Handle == INVALID_HANDLE)
     {
      PrintFormat("AccurateBuySellBridge: ERROR no se pudo crear handle EMA H4(%d) — code=%d",
                  EmaPeriod, GetLastError());
      return INIT_FAILED;
     }

   adxHandle = iADX(Symbol(), Period(), AdxPeriod);
   if(adxHandle == INVALID_HANDLE)
     {
      PrintFormat("AccurateBuySellBridge: ERROR no se pudo crear handle ADX(%d) — code=%d",
                  AdxPeriod, GetLastError());
      return INIT_FAILED;
     }

   EventSetTimer(SendIntervalSec);
   Print("AccurateBuySellBridge v2.0 iniciado en ", Symbol(), " TF=", Timeframe,
         " EMA=", EmaPeriod, " +H4 +ADX(", AdxPeriod, ")>", AdxMinLevel);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(indicatorHandle != INVALID_HANDLE) IndicatorRelease(indicatorHandle);
   if(emaHandle       != INVALID_HANDLE) IndicatorRelease(emaHandle);
   if(emaH4Handle     != INVALID_HANDLE) IndicatorRelease(emaH4Handle);
   if(adxHandle       != INVALID_HANDLE) IndicatorRelease(adxHandle);
  }

void OnTimer() { CheckEmaExit(); SendCurrentSignal(); }

//+------------------------------------------------------------------+
void CheckEmaExit()
  {
   if(activeDir == "" || closeRequestSent) return;

   double emaVal[1];
   if(CopyBuffer(emaHandle, 0, 1, 1, emaVal) <= 0) return;
   double closeVal = iClose(Symbol(), Period(), 1);

   bool exitBuy  = (activeDir == "buy"  && closeVal < emaVal[0]);
   bool exitSell = (activeDir == "sell" && closeVal > emaVal[0]);

   if(!exitBuy && !exitSell) return;

   PrintFormat("AccurateBuySellBridge: EXIT %s — close=%.5f ema=%.5f (cruce contra-EMA)",
               activeDir, closeVal, emaVal[0]);

   int httpCode = PostClose(Symbol(), "ema_cross");
   if(httpCode == 200)
     {
      closeRequestSent = true;
      activeDir = "";
      PrintFormat("AccurateBuySellBridge: cierre enviado OK para %s", Symbol());
     }
   else
      PrintFormat("AccurateBuySellBridge: cierre ERROR HTTP %d", httpCode);
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
      PrintFormat("AccurateBuySellBridge ERROR: WebRequest close falló code=%d", GetLastError());
   return res;
  }


//+------------------------------------------------------------------+
void SendCurrentSignal()
  {
   //--- Solo se evalúa la última vela CERRADA (índice 1) para evitar repintado,
   //    siguiendo la recomendación del propio indicador.
   double buyVal[1], sellVal[1];

   if(CopyBuffer(indicatorHandle, BUF_BUY, 1, 1, buyVal) <= 0)
     {
      if(DiagMode) Print("AccurateBuySellBridge: sin datos en buffer BUY");
      return;
     }
   if(CopyBuffer(indicatorHandle, BUF_SELL, 1, 1, sellVal) <= 0)
     {
      if(DiagMode) Print("AccurateBuySellBridge: sin datos en buffer SELL");
      return;
     }

   bool hasBuy  = (buyVal[0]  != EMPTY_VALUE && buyVal[0]  != 0.0);
   bool hasSell = (sellVal[0] != EMPTY_VALUE && sellVal[0] != 0.0);

   if(!hasBuy && !hasSell)
     {
      if(DiagMode) Print("AccurateBuySellBridge: vela cerrada sin señal");
      return;
     }

   string dir       = hasBuy ? "buy" : "sell";
   double signalVal = hasBuy ? buyVal[0] : sellVal[0];

   //--- Filtro de tendencia EMA: BUY solo si close > EMA, SELL solo si close < EMA
   //    (recomendación de uso de la descripción del indicador), evaluado en la
   //    misma vela cerrada (índice 1).
   double emaVal[1];
   if(CopyBuffer(emaHandle, 0, 1, 1, emaVal) <= 0)
     {
      if(DiagMode) Print("AccurateBuySellBridge: sin datos de EMA, señal descartada");
      return;
     }
   double closeVal = iClose(Symbol(), Period(), 1);

   if(dir == "buy" && closeVal <= emaVal[0])
     {
      if(DiagMode) PrintFormat("AccurateBuySellBridge: BUY descartado por filtro EMA (close=%.5f <= ema=%.5f)",
                                closeVal, emaVal[0]);
      return;
     }
   if(dir == "sell" && closeVal >= emaVal[0])
     {
      if(DiagMode) PrintFormat("AccurateBuySellBridge: SELL descartado por filtro EMA H1 (close=%.5f >= ema=%.5f)",
                                closeVal, emaVal[0]);
      return;
     }

   //--- Filtro EMA H4: la tendencia del timeframe superior debe confirmar la dirección
   double emaH4Val[1];
   if(CopyBuffer(emaH4Handle, 0, 0, 1, emaH4Val) <= 0)
     {
      if(DiagMode) Print("AccurateBuySellBridge: sin datos de EMA H4, señal descartada");
      return;
     }
   double priceNow = SymbolInfoDouble(Symbol(), SYMBOL_BID);
   if(dir == "buy" && priceNow <= emaH4Val[0])
     {
      if(DiagMode) PrintFormat("AccurateBuySellBridge: BUY descartado por filtro EMA H4 (price=%.5f <= emaH4=%.5f)",
                                priceNow, emaH4Val[0]);
      return;
     }
   if(dir == "sell" && priceNow >= emaH4Val[0])
     {
      if(DiagMode) PrintFormat("AccurateBuySellBridge: SELL descartado por filtro EMA H4 (price=%.5f >= emaH4=%.5f)",
                                priceNow, emaH4Val[0]);
      return;
     }

   //--- Filtro ADX: no operar en mercado lateral
   double adxVal[1];
   if(CopyBuffer(adxHandle, 0, 1, 1, adxVal) <= 0)
     {
      if(DiagMode) Print("AccurateBuySellBridge: sin datos de ADX, señal descartada");
      return;
     }
   if(adxVal[0] < AdxMinLevel)
     {
      if(DiagMode) PrintFormat("AccurateBuySellBridge: señal descartada por ADX bajo (%.1f < %.1f) — mercado lateral",
                                adxVal[0], AdxMinLevel);
      return;
     }

   datetime barTime  = iTime(Symbol(), Period(), 1);

   //--- signal_id único por vela+dirección (equivalente al nombre de objeto en Crystal)
   string signalId = StringFormat("ABS_%s_%s_%d", Symbol(), dir, (long)barTime);

   //--- Deduplicación: no reenviar la misma vela
   if(signalId == lastSentSignalId)
     {
      if(DiagMode) PrintFormat("AccurateBuySellBridge: sin cambio (último=%s)", lastSentSignalId);
      return;
     }

   //--- Precio actual de mercado como entrada
   double entryPrice = (dir == "buy")
                       ? SymbolInfoDouble(Symbol(), SYMBOL_ASK)
                       : SymbolInfoDouble(Symbol(), SYMBOL_BID);

   //--- zone_high = precio de entrada, zone_low = precio de la flecha (nivel SL natural)
   string body = StringFormat(
     "{\"symbol\":\"%s\",\"entry_zone\":true,\"direction\":\"%s\","
      "\"zone_high\":%.5f,\"zone_low\":%.5f,"
      "\"timeframe\":\"%s\",\"source\":\"accurate_buy_sell\","
      "\"signal_id\":\"%s\"}",
     Symbol(),
     dir,
     entryPrice,
     signalVal,
     Timeframe,
     signalId
   );

   PrintFormat("AccurateBuySellBridge: enviando %s dir=%s signal_val=%.5f entry_price=%.5f",
               signalId, dir, signalVal, entryPrice);
   int httpCode = PostToFastAPI(body);

   if(httpCode == 200)
     {
      lastSentSignalId = signalId;
      activeDir = dir;
      activeSymbol = Symbol();
      closeRequestSent = false;
      PrintFormat("AccurateBuySellBridge: OK — signal_id=%s", signalId);
     }
   else
      PrintFormat("AccurateBuySellBridge: ERROR HTTP %d — no se actualiza lastSentSignalId", httpCode);
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
      PrintFormat("AccurateBuySellBridge ERROR: WebRequest falló code=%d", GetLastError());

   return res;
  }
//+------------------------------------------------------------------+
