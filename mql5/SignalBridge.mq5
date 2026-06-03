//+------------------------------------------------------------------+
//| SignalBridge.mq5                                                  |
//| Lee objetos de Brain SMC Ultimate y los envía a FastAPI           |
//+------------------------------------------------------------------+
#property copyright "Trading System"
#property version   "1.0"
#property strict

#include <Trade\Trade.mqh>

//--- Parámetros configurables
input string FastAPI_URL      = "http://100.91.167.17:8090";  // IP del servidor FastAPI (ajustar)
input string InternalToken    = "90c42a9448defc1f57K8WGdyb3FYaXgTw00gCVKY9JhfFG1A9tfji"; // X-Internal-Token
input int    SendIntervalSec  = 10;                          // Cada cuántos segundos enviar señal
input string ObjPrefix_Bull   = "";  // Prefijo de objetos alcistas de Brain SMC (dejar vacío = detectar todo)
input string ObjPrefix_Bear   = "";  // Prefijo de objetos bajistas
input color  BullZoneColor    = clrGreen;   // Color de zona alcista en el chart
input color  BearZoneColor    = clrRed;     // Color de zona bajista en el chart
input string Timeframe        = "H1";

//--- Estado interno
datetime lastSendTime = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(SendIntervalSec);
   Print("SignalBridge iniciado en ", Symbol(), " TF=", Timeframe);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTimer()
  {
   SendCurrentSignal();
  }

//+------------------------------------------------------------------+
//| Lógica principal: escanea objetos y determina si hay Entry Zone   |
//+------------------------------------------------------------------+
void SendCurrentSignal()
  {
   double currentPrice = SymbolInfoDouble(Symbol(), SYMBOL_BID);

   bool   entryZone  = false;
   string direction  = "";
   double zoneHigh   = 0;
   double zoneLow    = 0;

   int total = ObjectsTotal(0, 0, -1);

   for(int i = 0; i < total; i++)
     {
      string name = ObjectName(0, i, 0, -1);
      ENUM_OBJECT type = (ENUM_OBJECT)ObjectGetInteger(0, name, OBJPROP_TYPE);

      // Solo rectángulos (zonas de precio)
      if(type != OBJ_RECTANGLE) continue;

      double p1 = ObjectGetDouble(0, name, OBJPROP_PRICE, 0);
      double p2 = ObjectGetDouble(0, name, OBJPROP_PRICE, 1);
      double high = MathMax(p1, p2);
      double low  = MathMin(p1, p2);

      // ¿El precio actual está dentro de la zona?
      if(currentPrice >= low && currentPrice <= high)
        {
         color  objColor = (color)ObjectGetInteger(0, name, OBJPROP_COLOR);
         string zoneName = name;
         StringToLower(zoneName);

         // Determinar dirección por color o nombre
         bool isBull = (objColor == BullZoneColor)
                    || StringFind(zoneName, "bull")  >= 0
                    || StringFind(zoneName, "buy")   >= 0
                    || StringFind(zoneName, "green") >= 0
                    || StringFind(zoneName, "support") >= 0
                    || StringFind(zoneName, "demand") >= 0;

         bool isBear = (objColor == BearZoneColor)
                    || StringFind(zoneName, "bear")   >= 0
                    || StringFind(zoneName, "sell")   >= 0
                    || StringFind(zoneName, "red")    >= 0
                    || StringFind(zoneName, "resist") >= 0
                    || StringFind(zoneName, "supply") >= 0;

         if(isBull || isBear)
           {
            entryZone = true;
            direction = isBull ? "buy" : "sell";
            zoneHigh  = high;
            zoneLow   = low;

            PrintFormat("Zona detectada: %s dir=%s high=%.5f low=%.5f",
                        name, direction, zoneHigh, zoneLow);
            break;
           }
        }
     }

   // Construir JSON
   string body = StringFormat(
     "{\"symbol\":\"%s\",\"entry_zone\":%s,\"direction\":%s,"
      "\"zone_high\":%s,\"zone_low\":%s,\"timeframe\":\"%s\"}",
     Symbol(),
     entryZone ? "true" : "false",
     direction != "" ? ("\"" + direction + "\"") : "null",
     zoneHigh > 0 ? DoubleToString(zoneHigh, 5) : "null",
     zoneLow  > 0 ? DoubleToString(zoneLow,  5) : "null",
     Timeframe
   );

   PostToFastAPI(body);
  }

//+------------------------------------------------------------------+
//| Envía POST a FastAPI                                               |
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
      PrintFormat("SignalBridge ERROR: WebRequest falló (¿URL permitida en MT5?) code=%d", GetLastError());
   else if(res != 200)
      PrintFormat("SignalBridge HTTP %d para %s", res, url);
  }
