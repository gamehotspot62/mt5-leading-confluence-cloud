//+------------------------------------------------------------------+
//|                                LeadingConfluenceCloudBridge.mq5  |
//|     24/7 Cloud Macro / On-Chain / ETF / COT & ML Sync Service    |
//|  Fetches live cloud CSV feeds via WebRequest in a background     |
//|  service thread (no chart blocking) and updates MT5 Files + GVs  |
//+------------------------------------------------------------------+
#property copyright "LeadingConfluencePro Cloud Architecture"
#property link      "https://github.com/gamehotspot62/mt5-leading-confluence-cloud"
#property version   "2.00"
#property service

//--- Input Parameters
input group "=== 1. Cloud Feed Endpoints (GitHub Raw HTTPS) ==="
input string InpMacroBiasURL        = "https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/MacroFlowBias.csv";
input string InpOptConfigURL        = "https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/OptimizedConfig.csv";
input string InpMLWeightsURL        = "https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/MLModelWeights.csv";

input group "=== 2. Sync Schedule & Local Storage ==="
input int    InpPollIntervalSeconds = 300;   // Poll Interval in Seconds (Default: 5 min)
input int    InpHttpTimeoutMs       = 10000; // HTTP Request Timeout (ms)
input bool   InpUseCommonFiles      = false; // Write to Terminal\\Common\\Files (Share Across MT5 Terminals)
input bool   InpVerboseLog          = true;  // Print Sync Summary to Terminal Experts Log

//+------------------------------------------------------------------+
//| Helper: Fetch HTTPS Text Payload via MQL5 WebRequest             |
//+------------------------------------------------------------------+
bool FetchCloudText(const string url, string &outBody, int &outStatus)
{
   outBody   = "";
   outStatus = -1;
   if(StringLen(url) < 8)
      return false;

   string headers = "Accept: text/plain,text/csv,*/*\r\nUser-Agent: MetaTrader5-LeadingConfluenceBridge/2.0\r\n";
   char   post[];
   char   result[];
   string resultHeaders = "";
   ArrayResize(post, 0);

   ResetLastError();
   int res = WebRequest("GET", url, headers, InpHttpTimeoutMs, post, result, resultHeaders);
   outStatus = res;

   if(res == -1)
   {
      int err = GetLastError();
      if(err == 4014)
      {
         Print("LeadingConfluenceCloudBridge [ERROR 4014]: URL not allowed! ",
               "In MT5 go to Tools -> Options -> Expert Advisors -> check 'Allow WebRequest for listed URL' and add: https://raw.githubusercontent.com");
      }
      else
      {
         PrintFormat("LeadingConfluenceCloudBridge: WebRequest failed for %s (error=%d)", url, err);
      }
      return false;
   }

   if(res != 200)
   {
      PrintFormat("LeadingConfluenceCloudBridge: HTTP %d from %s", res, url);
      return false;
   }

   outBody = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   return (StringLen(outBody) > 0);
}

//+------------------------------------------------------------------+
//| Helper: Write Text Safely to MQL5\\Files or Common\\Files         |
//+------------------------------------------------------------------+
bool WriteLocalCSV(const string fileName, const string content)
{
   int commonFlag = InpUseCommonFiles ? FILE_COMMON : 0;
   int fh = FileOpen(fileName, FILE_WRITE | FILE_TXT | FILE_ANSI | commonFlag);
   if(fh == INVALID_HANDLE)
   {
      PrintFormat("LeadingConfluenceCloudBridge: Failed writing %s (err=%d)", fileName, GetLastError());
      return false;
   }
   FileWriteString(fh, content);
   FileClose(fh);
   return true;
}

//+------------------------------------------------------------------+
//| Helper: Publish Macro Bias CSV Entries to Terminal Global Vars   |
//+------------------------------------------------------------------+
int PublishMacroBiasGlobals(const string csvText)
{
   string lines[];
   int nLines = StringSplit(csvText, '\n', lines);
   int updated = 0;

   for(int i = 0; i < nLines; i++)
   {
      string line = lines[i];
      StringReplace(line, "\r", "");
      StringTrimLeft(line);
      StringTrimRight(line);
      if(StringLen(line) == 0 || StringGetCharacter(line, 0) == '#')
         continue;

      string cols[];
      int nCols = StringSplit(line, ',', cols);
      if(nCols < 2)
         continue;

      string sym = cols[0];
      StringTrimLeft(sym);
      StringTrimRight(sym);
      StringToUpper(sym);
      if(sym == "SYMBOL" || StringLen(sym) == 0)
         continue;

      string valStr = cols[1];
      StringTrimLeft(valStr);
      StringTrimRight(valStr);
      double biasVal = StringToDouble(valStr);
      biasVal = MathMax(-100.0, MathMin(100.0, biasVal));

      GlobalVariableSet("MACRO_BIAS_" + sym, biasVal);
      updated++;
   }
   return updated;
}

//+------------------------------------------------------------------+
//| Sync All Cloud Datasets Once                                     |
//+------------------------------------------------------------------+
void SyncAllCloudFeeds()
{
   string body   = "";
   int    status = -1;
   bool   okAll  = true;
   int    gvCnt  = 0;

   // 1. Macro / On-Chain / ETF / COT Bias CSV
   if(FetchCloudText(InpMacroBiasURL, body, status) && StringFind(body, "SYMBOL,") >= 0)
   {
      if(WriteLocalCSV("MacroFlowBias.csv", body))
         gvCnt = PublishMacroBiasGlobals(body);
      else
         okAll = false;
   }
   else
   {
      okAll = false;
   }

   // 2. Cloud ML Walk-Forward Optimized Parameters CSV
   if(FetchCloudText(InpOptConfigURL, body, status) && StringFind(body, "SYMBOL,") >= 0)
   {
      if(!WriteLocalCSV("OptimizedConfig.csv", body))
         okAll = false;
   }
   else
   {
      okAll = false;
   }

   // 3. Cloud Trained ML Meta-Model Weights CSV
   if(FetchCloudText(InpMLWeightsURL, body, status) && StringFind(body, "SYMBOL,") >= 0)
   {
      if(!WriteLocalCSV("MLModelWeights.csv", body))
         okAll = false;
   }
   else
   {
      okAll = false;
   }

   GlobalVariableSet("LCP_CLOUD_STATUS", okAll ? 1.0 : 0.0);
   if(okAll)
      GlobalVariableSet("LCP_CLOUD_LAST_SYNC", (double)TimeGMT());

   if(InpVerboseLog)
   {
      PrintFormat("LeadingConfluenceCloudBridge: Sync %s | Macro GVs=%d | UTC=%s",
                  okAll ? "OK" : "PARTIAL/FAILED",
                  gvCnt,
                  TimeToString(TimeGMT(), TIME_DATE | TIME_MINUTES));
   }
}

//+------------------------------------------------------------------+
//| Service Program Entry Point                                      |
//+------------------------------------------------------------------+
void OnStart()
{
   Print("LeadingConfluenceCloudBridge v2.00 service started.");

   int intervalSec = MathMax(30, InpPollIntervalSeconds);
   while(!IsStopped())
   {
      SyncAllCloudFeeds();

      int elapsedMs = 0;
      int targetMs  = intervalSec * 1000;
      while(elapsedMs < targetMs && !IsStopped())
      {
         Sleep(500);
         elapsedMs += 500;
      }
   }

   Print("LeadingConfluenceCloudBridge service stopped.");
}
//+------------------------------------------------------------------+
