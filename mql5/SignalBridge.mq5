//+------------------------------------------------------------------+
//| SignalBridge.mq5                                                  |
//| Lee señales de Brain SMC Ultimate y las envía a FastAPI           |
//+------------------------------------------------------------------+
#property copyright "Trading System"
#property version   "1.2"
#property strict

//--- Parámetros configurables
input string FastAPI_URL      = "http://100.91.167.17:8090";
input string InternalToken    = "90c42a9448defc1f57K8WGdyb3FYaXgTw00gCVKY9JhfFG1A9tfji";
input int    SendIntervalSec  = 10;
input string Timeframe        = "M5";
input double MaxDistATR       = 3.0;   // Zona ignorada si precio está a más de N×ATR
input bool   DiagMode         = false;

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(SendIntervalSec);
   Print("SignalBridge v1.2 iniciado en ", Symbol(), " TF=", Timeframe,
         " | MaxDistATR=", MaxDistATR);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { EventKillTimer(); }
void OnTimer()                  { SendCurrentSignal(); }

//+------------------------------------------------------------------+
double GetATR(int period = 14)
  {
   double atr = 0;
   for(int i = 1; i <= period; i++)
     {
      double h = iHigh(Symbol(), PERIOD_CURRENT, i);
      double l = iLow (Symbol(), PERIOD_CURRENT, i);
      double c = iClose(Symbol(), PERIOD_CURRENT, i + 1);
      double tr = MathMax(h - l, MathMax(MathAbs(h - c), MathAbs(l - c)));
      atr += tr;
     }
   return atr / period;
  }

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

   double currentPrice = SymbolInfoDouble(Symbol(), SYMBOL_BID);
   double atr          = GetATR(14);
   double maxDist      = atr * MaxDistATR;

   bool   entryZone = false;
   string direction = "";
   double zonePrice = 0;

   int total = ObjectsTotal(0, 0, -1);

   for(int i = 0; i < total; i++)
     {
      string name    = ObjectName(0, i, 0, -1);
      string text    = ObjectGetString(0, name, OBJPROP_TEXT);
      string nameLow = name; StringToLower(nameLow);
      string textLow = text; StringToLower(textLow);

      // Patrón Brain SMC Ultimate: TB_C_B_<fecha>_txt o TB_C_U_<fecha>_txt
      // También acepta cualquier objeto cuyo texto contenga "entry zone"
      bool isBrainSMC = StringFind(nameLow, "tb_c_") >= 0;
      bool hasEntryText = StringFind(textLow, "entry") >= 0
                       || StringFind(textLow, "zone")  >= 0;

      if(!isBrainSMC && !hasEntryText) continue;

      double price = ObjectGetDouble(0, name, OBJPROP_PRICE, 0);
      if(price <= 0) continue;

      // Filtro de proximidad: ignorar zonas muy alejadas del precio actual
      if(MathAbs(currentPrice - price) > maxDist)
        {
         PrintFormat("Zona ignorada (lejos): %s price=%.5f dist=%.5f maxDist=%.5f",
                     name, price, MathAbs(currentPrice - price), maxDist);
         continue;
        }

      // Determinar dirección
      // Patrón nombre: TB_C_B_ = Bearish (SELL), TB_C_U_ / TB_C_L_ = Bullish (BUY)
      string dir = "";
      if(StringFind(nameLow, "_b_") >= 0 || StringFind(nameLow, "_bear") >= 0
         || StringFind(textLow, "bear") >= 0 || StringFind(textLow, "sell") >= 0)
         dir = "sell";
      else if(StringFind(nameLow, "_u_") >= 0 || StringFind(nameLow, "_l_") >= 0
              || StringFind(nameLow, "_bull") >= 0 || StringFind(nameLow, "_buy") >= 0
              || StringFind(textLow, "bull") >= 0 || StringFind(textLow, "buy") >= 0)
         dir = "buy";
      else
         dir = (price <= currentPrice) ? "buy" : "sell";   // fallback por posición

      entryZone = true;
      direction = dir;
      zonePrice = price;

      PrintFormat("Entry Zone activa: obj=%s price=%.5f dir=%s dist=%.5f",
                  name, price, direction, MathAbs(currentPrice - price));
      break;
     }

   // JSON
   string body = StringFormat(
     "{\"symbol\":\"%s\",\"entry_zone\":%s,\"direction\":%s,"
      "\"zone_high\":%s,\"zone_low\":%s,\"timeframe\":\"%s\"}",
     Symbol(),
     entryZone ? "true" : "false",
     direction != "" ? ("\"" + direction + "\"") : "null",
     zonePrice > 0 ? DoubleToString(zonePrice, 5) : "null",
     zonePrice > 0 ? DoubleToString(zonePrice, 5) : "null",
     Timeframe
   );

   PostToFastAPI(body);
  }

//+------------------------------------------------------------------+
void PostToFastAPI(string body)
  {
   char   postData[];
   char   result[];
   string headers = "Content-Type: application/json\r\n"
                    "X-Internal-Token: " + InternalToken + "\r\n";

   StringToCharArray(body, postData, 0, StringLen(body));
   ArrayResize(postData, StringLen(body));

   string url = FastAPI_URL + "/v1/smc/signal";
   int    timeout = 5000;
   string responseHeaders;

   int res = WebRequest("POST", url, headers, timeout, postData, result, responseHeaders);

   if(res == -1)
      PrintFormat("SignalBridge ERROR: WebRequest falló code=%d", GetLastError());
   else if(res != 200)
      PrintFormat("SignalBridge HTTP %d", res);
  }
