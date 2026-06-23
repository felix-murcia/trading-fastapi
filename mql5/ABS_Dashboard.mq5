//+------------------------------------------------------------------+
//| ABS_Dashboard.mq5                                                |
//| Muestra EMA(50) en el gráfico + ADX(14) en subventana,           |
//| con los mismos valores que AccurateBuySellBridge.mq5              |
//+------------------------------------------------------------------+
#property copyright "Trading System"
#property version   "1.0"
#property indicator_separate_window
#property indicator_buffers 4
#property indicator_plots   4

//--- EMA en el chart principal (plot 0)
#property indicator_label1  "EMA 50"
#property indicator_type1   DRAW_LINE
#property indicator_color1  clrDodgerBlue
#property indicator_style1  STYLE_SOLID
#property indicator_width1  2

//--- ADX main (plot 1)
#property indicator_label2  "ADX"
#property indicator_type2   DRAW_LINE
#property indicator_color2  clrWhite
#property indicator_style2  STYLE_SOLID
#property indicator_width2  2

//--- +DI (plot 2)
#property indicator_label3  "+DI"
#property indicator_type3   DRAW_LINE
#property indicator_color3  clrLime
#property indicator_style3  STYLE_DOT
#property indicator_width3  1

//--- -DI (plot 3)
#property indicator_label4  "-DI"
#property indicator_type4   DRAW_LINE
#property indicator_color4  clrRed
#property indicator_style4  STYLE_DOT
#property indicator_width4  1

//--- Parámetros (mismos que AccurateBuySellBridge)
input int    EmaPeriod   = 50;
input int    AdxPeriod   = 14;
input double AdxMinLevel = 20.0;

//--- Buffers
double bufEMA[];
double bufADX[];
double bufDIplus[];
double bufDIminus[];

//--- Handles
int emaHandle  = INVALID_HANDLE;
int adxHandle  = INVALID_HANDLE;

//+------------------------------------------------------------------+
int OnInit()
  {
   SetIndexBuffer(0, bufEMA,     INDICATOR_DATA);
   SetIndexBuffer(1, bufADX,     INDICATOR_DATA);
   SetIndexBuffer(2, bufDIplus,  INDICATOR_DATA);
   SetIndexBuffer(3, bufDIminus, INDICATOR_DATA);

   //--- La EMA se dibuja en la subventana pero con escala propia;
   //    para verla en el chart principal usamos un objeto separado.
   //    Aquí solo la calculamos para el label de datos.
   PlotIndexSetInteger(0, PLOT_DRAW_TYPE, DRAW_NONE);

   IndicatorSetString(INDICATOR_SHORTNAME, StringFormat("ABS_Dashboard EMA(%d) ADX(%d)>%.0f",
                      EmaPeriod, AdxPeriod, AdxMinLevel));
   IndicatorSetInteger(INDICATOR_DIGITS, 1);
   IndicatorSetDouble(INDICATOR_LEVELVALUE, 0, AdxMinLevel);
   IndicatorSetInteger(INDICATOR_LEVELCOLOR, 0, clrYellow);
   IndicatorSetInteger(INDICATOR_LEVELSTYLE, 0, STYLE_DASH);

   emaHandle = iMA(Symbol(), Period(), EmaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   adxHandle = iADX(Symbol(), Period(), AdxPeriod);

   if(emaHandle == INVALID_HANDLE || adxHandle == INVALID_HANDLE)
     {
      Print("ABS_Dashboard: ERROR creando handles");
      return INIT_FAILED;
     }
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(emaHandle != INVALID_HANDLE) IndicatorRelease(emaHandle);
   if(adxHandle != INVALID_HANDLE) IndicatorRelease(adxHandle);
   ObjectDelete(0, "ABS_EMA_LINE");
  }

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {
   int toCopy = rates_total - prev_calculated;
   if(toCopy <= 0) toCopy = 1;

   CopyBuffer(emaHandle, 0, 0, toCopy, bufEMA);
   CopyBuffer(adxHandle, 0, 0, toCopy, bufADX);
   CopyBuffer(adxHandle, 1, 0, toCopy, bufDIplus);
   CopyBuffer(adxHandle, 2, 0, toCopy, bufDIminus);

   //--- Dibujar la EMA como objeto en el chart principal
   DrawEmaOnChart(rates_total, time);

   return rates_total;
  }

//+------------------------------------------------------------------+
void DrawEmaOnChart(int rates_total, const datetime &time[])
  {
   string name = "ABS_EMA_LINE";
   int bars = MathMin(rates_total, 500);

   //--- Usar una polyline de objetos trend line por segmentos no es eficiente.
   //    Mejor: crear un indicador auxiliar. Pero como solución directa,
   //    dibujamos con OBJ_TREND segmentos visibles.

   //--- Limpiar segmentos anteriores
   int total = ObjectsTotal(0, 0, OBJ_TREND);
   for(int i = total - 1; i >= 0; i--)
     {
      string objName = ObjectName(0, i, 0, OBJ_TREND);
      if(StringFind(objName, "ABS_EMA_") == 0)
         ObjectDelete(0, objName);
     }

   //--- Dibujar segmentos EMA en chart principal
   double emaData[];
   ArraySetAsSeries(emaData, true);
   datetime timeData[];
   ArraySetAsSeries(timeData, true);

   int copied = CopyBuffer(emaHandle, 0, 0, bars, emaData);
   if(copied <= 1) return;
   CopyTime(Symbol(), Period(), 0, bars, timeData);

   for(int i = 0; i < copied - 1; i++)
     {
      string segName = StringFormat("ABS_EMA_%d", i);
      ObjectCreate(0, segName, OBJ_TREND, 0,
                   timeData[i+1], emaData[i+1],
                   timeData[i],   emaData[i]);
      ObjectSetInteger(0, segName, OBJPROP_COLOR, clrDodgerBlue);
      ObjectSetInteger(0, segName, OBJPROP_WIDTH, 2);
      ObjectSetInteger(0, segName, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, segName, OBJPROP_RAY_LEFT,  false);
      ObjectSetInteger(0, segName, OBJPROP_BACK, true);
      ObjectSetInteger(0, segName, OBJPROP_SELECTABLE, false);
     }
  }
//+------------------------------------------------------------------+
