//+------------------------------------------------------------------+
//| AccurateBuySellDiag.mq5                                          |
//| Script de diagnóstico: escanea los buffers de "Accurate Buy Sell |
//| System" para descubrir cuál contiene las señales de flecha.      |
//+------------------------------------------------------------------+
#property script_show_inputs

input string IndicatorName = "Accurate Buy Sell System";
input int    BarsToCheck   = 30;
input int    MaxBuffers    = 8;

void OnStart()
  {
   int handle = iCustom(Symbol(), Period(), IndicatorName);
   if(handle == INVALID_HANDLE)
     {
      PrintFormat("AccurateBuySellDiag: ERROR no se pudo crear handle para '%s' — code=%d",
                  IndicatorName, GetLastError());
      return;
     }

   PrintFormat("AccurateBuySellDiag: handle OK para '%s' en %s %s",
               IndicatorName, Symbol(), EnumToString(Period()));

   for(int buf = 0; buf < MaxBuffers; buf++)
     {
      double values[];
      ArraySetAsSeries(values, true);
      int copied = CopyBuffer(handle, buf, 0, BarsToCheck, values);

      if(copied <= 0)
        {
         PrintFormat("  Buffer %d: sin datos (copied=%d, err=%d)", buf, copied, GetLastError());
         continue;
        }

      string line = "";
      int nonEmpty = 0;
      for(int i = 0; i < copied; i++)
        {
         if(values[i] != EMPTY_VALUE && values[i] != 0.0 && values[i] != -1.0)
           {
            nonEmpty++;
            double hi  = iHigh(Symbol(), Period(), i);
            double lo  = iLow(Symbol(), Period(), i);
            string rel = (values[i] > hi) ? "ABOVE_HIGH" : (values[i] < lo) ? "BELOW_LOW" : "INSIDE_RANGE";
            line += StringFormat("[i=%d t=%s val=%.5f hi=%.5f lo=%.5f rel=%s] ", i,
                      TimeToString(iTime(Symbol(), Period(), i), TIME_DATE|TIME_MINUTES), values[i], hi, lo, rel);
           }
        }
      PrintFormat("  Buffer %d: copied=%d nonEmptyCount=%d  %s", buf, copied, nonEmpty, line);
     }

   IndicatorRelease(handle);
   Print("AccurateBuySellDiag: fin del escaneo");
  }
//+------------------------------------------------------------------+
