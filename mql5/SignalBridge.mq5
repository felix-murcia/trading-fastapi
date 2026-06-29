//+------------------------------------------------------------------+
//| SignalBridge.mq5                                                  |
//| Lee señales de Crystal Buy Sell Liquidity y las envía a FastAPI  |
//+------------------------------------------------------------------+
#property copyright "Trading System"
#property version   "2.0"
#property strict

//--- Parámetros configurables
input string FastAPI_URL      = "http://YOUR_FASTAPI_IP:8090";
input string InternalToken    = "YOUR_INTERNAL_TOKEN";
input int    SendIntervalSec  = 10;
input string Timeframe        = "M5";
input int    MaxSignalAgeSec  = 120;   // Señal ignorada si el candle tiene más de N segundos
input bool   DiagMode         = false;

//--- Estado interno: último signal_id enviado (evita reenviar el mismo objeto)
string lastSentSignalId = "";

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(SendIntervalSec);
   Print("SignalBridge v2.0 iniciado en ", Symbol(), " TF=", Timeframe);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { EventKillTimer(); }
void OnTimer()                  { SendCurrentSignal(); }

//+------------------------------------------------------------------+
void PrintAllObjects()
  {
   int total = ObjectsTotal(0, 0, -1);
   Print("=== Objetos en el chart: ", total, " ===");
   for(int i = 0; i < total; i++)
     {
      string name  = ObjectName(0, i, 0, -1);
      int    type  = (int)ObjectGetInteger(0, name, OBJPROP_TYPE);
      string text  = ObjectGetString(0, name, OBJPROP_TEXT);
      double price = ObjectGetDouble(0, name, OBJPROP_PRICE, 0);
      PrintFormat("  [%d] type=%d name=%s text=%s price=%.5f", i, type, name, text, price);
     }
   Print("=== Fin listado ===");
  }

//+------------------------------------------------------------------+
void SendCurrentSignal()
  {
   if(DiagMode) { PrintAllObjects(); return; }

   //--- Buscar el objeto Crystal más reciente (mayor timestamp en el nombre)
   //    Nombres: QT_L_B_<unix>  (BUY)  |  QT_L_S_<unix>  (SELL)
   string bestName  = "";
   string bestDir   = "";
   double bestPrice = 0;
   long   bestTs    = 0;

   int total = ObjectsTotal(0, 0, -1);
   for(int i = 0; i < total; i++)
     {
      string name = ObjectName(0, i, 0, -1);
      string nameLow = name; StringToLower(nameLow);

      // Orden: más específico primero (SB/SS antes que B/S)
      bool isBuy  = (StringFind(nameLow, "qt_l_sb_") == 0)
                 || (StringFind(nameLow, "qt_l_b_")  == 0);
      bool isSell = (StringFind(nameLow, "qt_l_ss_") == 0)
                 || (StringFind(nameLow, "qt_l_s_")  == 0
                     && StringFind(nameLow, "qt_l_sb_") < 0
                     && StringFind(nameLow, "qt_l_ss_") < 0);
      if(!isBuy && !isSell) continue;

      double price = ObjectGetDouble(0, name, OBJPROP_PRICE, 0);
      if(price <= 0) continue;

      //--- Extraer timestamp: buscar el último '_' y coger lo que viene después
      int    lastUnder = StringLen(nameLow) - 1;
      while(lastUnder > 0 && StringGetCharacter(nameLow, lastUnder) != '_') lastUnder--;
      string tsStr = StringSubstr(nameLow, lastUnder + 1);
      long   ts    = (long)StringToInteger(tsStr);

      if(ts > bestTs)
        {
         bestTs    = ts;
         bestName  = name;
         bestPrice = price;
         bestDir   = isBuy ? "buy" : "sell";
        }
     }

   if(bestName == "")
     {
      if(DiagMode) Print("SignalBridge: no hay objetos Crystal activos");
      return;
     }

   //--- Filtro de antigüedad: señal de candle abierto hace más de MaxSignalAgeSec → ignorar
   long signalAgeSec = (long)TimeCurrent() - bestTs;
   if(signalAgeSec > MaxSignalAgeSec)
     {
      PrintFormat("SignalBridge: señal descartada por antigüedad — %s age=%ds (max=%ds)",
                  bestName, (int)signalAgeSec, MaxSignalAgeSec);
      return;
     }

   //--- Deduplicación: no reenviar el mismo objeto
   if(bestName == lastSentSignalId)
     {
      if(DiagMode) PrintFormat("SignalBridge: sin cambio (último=%s)", lastSentSignalId);
      return;
     }

   //--- Precio actual de mercado como entrada (el objeto guarda el precio de inicio de vela)
   double entryPrice = (bestDir == "buy")
                       ? SymbolInfoDouble(Symbol(), SYMBOL_ASK)
                       : SymbolInfoDouble(Symbol(), SYMBOL_BID);

   //--- Construir JSON
   string body = StringFormat(
     "{\"symbol\":\"%s\",\"entry_zone\":true,\"direction\":\"%s\","
      "\"zone_high\":%.5f,\"zone_low\":%.5f,"
      "\"timeframe\":\"%s\",\"source\":\"crystal_liquidity\","
      "\"signal_id\":\"%s\"}",
     Symbol(),
     bestDir,
     entryPrice,
     entryPrice,
     Timeframe,
     bestName
   );

   PrintFormat("SignalBridge: enviando %s dir=%s signal_price=%.5f entry_price=%.5f",
               bestName, bestDir, bestPrice, entryPrice);
   int httpCode = PostToFastAPI(body);

   if(httpCode == 200)
     {
      lastSentSignalId = bestName;
      PrintFormat("SignalBridge: OK — signal_id=%s", bestName);
     }
   else
      PrintFormat("SignalBridge: ERROR HTTP %d — no se actualiza lastSentSignalId", httpCode);
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
      PrintFormat("SignalBridge ERROR: WebRequest falló code=%d", GetLastError());

   return res;
  }
