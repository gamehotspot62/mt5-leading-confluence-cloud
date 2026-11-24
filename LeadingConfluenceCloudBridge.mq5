//+------------------------------------------------------------------+
//|                                LeadingConfluenceCloudBridge.mq5  |
//|     24/7 Cloud Macro / On-Chain / ETF / COT & ML Sync Service    |
//|  Fetches live cloud CSV feeds via WebRequest in a background     |
//|  service thread (no chart blocking) and updates MT5 Files + GVs  |
//+------------------------------------------------------------------+
#property copyright "LeadingConfluencePro Cloud Architecture"
#property link      "https://github.com/gamehotspot62/mt5-leading-confluence-cloud"
#property version   "2.10"
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

input group "=== 3. Optional Instant MT5 -> Telegram Relay ==="
input bool   InpEnableTelegramRelay = true;  // Relay Queued MT5 Signals to Telegram (Needs Token & ChatID)
input string InpTelegramBotToken    = "";    // Telegram Bot Token (Leave Blank to Disable)
input string InpTelegramChatID      = "";    // Telegram Chat ID (Leave Blank to Disable)
input int    InpOutboxPollSeconds   = 3;     // Outbox Poll Interval in Seconds

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
//| Helper: Percent-Encode UTF-8 Text for Form Body                  |
//+------------------------------------------------------------------+
string UrlEncodeUTF8(const string text)
{
   uchar bytes[];
   int n = StringToCharArray(text, bytes, 0, WHOLE_ARRAY, CP_UTF8);
   string out = "";
   for(int i = 0; i < n; i++)
   {
      uchar ch = bytes[i];
      if(ch == 0) break;
      if((ch >= '0' && ch <= '9') || (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') ||
         ch == '-' || ch == '_' || ch == '.' || ch == '~')
      {
         out += CharToString(ch);
      }
      else if(ch == ' ')
      {
         out += "+";
      }
      else
      {
         out += StringFormat("%%%02X", ch);
      }
   }
   return out;
}

//+------------------------------------------------------------------+
//| Helper: Send Single Message to Telegram Bot API via WebRequest   |
//+------------------------------------------------------------------+
bool SendTelegramText(const string msg)
{
   if(StringLen(InpTelegramBotToken) <= 10 || StringLen(InpTelegramChatID) == 0 || StringLen(msg) == 0)
      return false;

   string url = "https://api.telegram.org/bot" + InpTelegramBotToken + "/sendMessage";
   string bodyStr = "chat_id=" + UrlEncodeUTF8(InpTelegramChatID) +
                    "&disable_web_page_preview=true&text=" + UrlEncodeUTF8(msg);
   char post[];
   int nBytes = StringToCharArray(bodyStr, post, 0, WHOLE_ARRAY, CP_UTF8);
   if(nBytes > 0 && post[nBytes - 1] == 0)
      ArrayResize(post, nBytes - 1);

   char   result[];
   string resultHeaders = "";
   string headers = "Content-Type: application/x-www-form-urlencoded\r\nUser-Agent: MetaTrader5-LeadingConfluenceBridge/2.10\r\n";

   ResetLastError();
   int res = WebRequest("POST", url, headers, InpHttpTimeoutMs, post, result, resultHeaders);
   if(res == -1)
   {
      int err = GetLastError();
      if(err == 4014)
      {
         Print("LeadingConfluenceCloudBridge [ERROR 4014]: Telegram URL not allowed! ",
               "In MT5 go to Tools -> Options -> Expert Advisors -> add: https://api.telegram.org");
      }
      else
      {
         PrintFormat("LeadingConfluenceCloudBridge: Telegram WebRequest failed (error=%d)", err);
      }
      return false;
   }

   string resp = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(res == 200 && StringFind(resp, "\"ok\":true") >= 0)
      return true;

   PrintFormat("LeadingConfluenceCloudBridge: Telegram HTTP %d (%s)", res, StringSubstr(resp, 0, 120));
   return false;
}

//+------------------------------------------------------------------+
//| Process Queued MT5 Signals in LCP_TelegramOutbox.txt              |
//+------------------------------------------------------------------+
void ProcessTelegramOutbox()
{
   if(!InpEnableTelegramRelay || StringLen(InpTelegramBotToken) <= 10 || StringLen(InpTelegramChatID) == 0)
      return;

   int commonFlag = InpUseCommonFiles ? FILE_COMMON : 0;
   if(!FileIsExist("LCP_TelegramOutbox.txt", commonFlag))
      return;

   int fh = FileOpen("LCP_TelegramOutbox.txt", FILE_READ | FILE_TXT | FILE_ANSI | commonFlag);
   if(fh == INVALID_HANDLE)
      return;

   string queue[];
   while(!FileIsEnding(fh))
   {
      string ln = FileReadString(fh);
      StringReplace(ln, "\r", "");
      StringTrimLeft(ln);
      StringTrimRight(ln);
      if(StringLen(ln) > 0)
      {
         int k = ArraySize(queue);
         ArrayResize(queue, k + 1);
         queue[k] = ln;
      }
   }
   FileClose(fh);

   int total = ArraySize(queue);
   if(total == 0)
   {
      FileDelete("LCP_TelegramOutbox.txt", commonFlag);
      return;
   }

   string failed[];
   int sentCnt = 0;
   for(int i = 0; i < total; i++)
   {
      if(IsStopped())
      {
         for(int r = i; r < total; r++)
         {
            int fk = ArraySize(failed);
            ArrayResize(failed, fk + 1);
            failed[fk] = queue[r];
         }
         break;
      }

      if(SendTelegramText(queue[i]))
         sentCnt++;
      else
      {
         int fk = ArraySize(failed);
         ArrayResize(failed, fk + 1);
         failed[fk] = queue[i];
      }
   }

   if(ArraySize(failed) == 0)
   {
      FileDelete("LCP_TelegramOutbox.txt", commonFlag);
   }
   else
   {
      int wfh = FileOpen("LCP_TelegramOutbox.txt", FILE_WRITE | FILE_TXT | FILE_ANSI | commonFlag);
      if(wfh != INVALID_HANDLE)
      {
         for(int i = 0; i < ArraySize(failed); i++)
            FileWriteString(wfh, failed[i] + "\r\n");
         FileClose(wfh);
      }
   }

   if(sentCnt > 0 && InpVerboseLog)
      PrintFormat("LeadingConfluenceCloudBridge: Relayed %d MT5 alert(s) to Telegram (remaining=%d).", sentCnt, ArraySize(failed));
}

//+------------------------------------------------------------------+
//| Service Program Entry Point                                      |
//+------------------------------------------------------------------+
void OnStart()
{
   Print("LeadingConfluenceCloudBridge v2.10 service started.");

   int syncTargetMs   = MathMax(30, InpPollIntervalSeconds) * 1000;
   int outboxTargetMs = MathMax(1, InpOutboxPollSeconds) * 1000;
   int syncElapsedMs   = syncTargetMs;
   int outboxElapsedMs = outboxTargetMs;

   while(!IsStopped())
   {
      if(syncElapsedMs >= syncTargetMs)
      {
         SyncAllCloudFeeds();
         syncElapsedMs = 0;
      }
      if(outboxElapsedMs >= outboxTargetMs)
      {
         ProcessTelegramOutbox();
         outboxElapsedMs = 0;
      }

      Sleep(500);
      syncElapsedMs   += 500;
      outboxElapsedMs += 500;
   }

   Print("LeadingConfluenceCloudBridge service stopped.");
}
//+------------------------------------------------------------------+
