//+------------------------------------------------------------------+
//|                              LeadingConfluenceAutoEntry_M15.mq5  |
//|     Optional M15 Auto-Entry Expert Advisor for XAUUSD & Any Sym  |
//|  Reads confirmed closed-bar signals (shift 1) from               |
//|  LeadingConfluencePro.ex5, sizes positions by % risk, executes   |
//|  with structural SL/TP2, and manages TP1 partial close + BE.     |
//+------------------------------------------------------------------+
#property copyright "LeadingConfluencePro Quantitative Architecture"
#property link      "https://github.com/gamehotspot62/mt5-leading-confluence-cloud"
#property version   "1.10"
#property description "Optional M15 Auto-Entry EA paired with LeadingConfluencePro.ex5 v3.50."
#property description "Evaluates strictly shift=1 (closed M15 bar) and manages TP1 partial + Protected-Runner SL."

//--- Input Parameters
input group "=== 1. Signal Source & Entry Timeframe ==="
input string          InpIndicatorName        = "LeadingConfluencePro"; // Indicator Name in MQL5\\Indicators
input ENUM_TIMEFRAMES InpEntryTimeframe       = PERIOD_M15;             // Chart Timeframe to Take Entry On (Default: M15)
input int             InpMinScoreOverride     = 0;                      // Extra Score Floor (0 = Use Indicator/Cloud Gate)
input ulong           InpMagicNumber          = 202615;                 // EA Magic Number
input bool            InpDryRunLogOnly        = false;                  // Dry-Run Mode (Log Only, No Live Orders)

input group "=== 2. Risk & Trade Execution ==="
input double          InpRiskPercent          = 0.50;                   // Equity Risk % Per Trade (0 = Fixed Lot)
input double          InpFixedLotFallback     = 0.01;                   // Fixed Lot Size if Risk% <= 0
input int             InpMaxSpreadPoints      = 60;                     // Maximum Allowed Spread (Points)
input int             InpMaxPositionsPerSymbol= 1;                      // Max Open EA Positions on This Symbol
input bool            InpCloseOppositeOnSignal= false;                  // Close Opposing Position Before Opening New Signal
input int             InpSlippagePoints       = 20;                     // Max Order Slippage (Points)

input group "=== 3. TP1 Partial Close & Protected-Runner Management ==="
input bool            InpMoveSLToBEAtTP1      = true;                   // Move Stop Loss to Protected-Runner Level at TP1
input double          InpPartialClosePctAtTP1 = 65.0;                   // % Volume to Close at TP1 (0 = Disable Partial)
input double          InpRunnerSLRiskMultAtTP1= -0.50;                  // Protected Runner SL in R (-0.50 = Half SL, 0 = BE)

//--- Runtime Globals
int      g_indHandle        = INVALID_HANDLE;
datetime g_lastProcessedBar = 0;

//+------------------------------------------------------------------+
//| Helper: Determine Broker Supported Filling Mode                  |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING GetOrderFilling()
{
   long filling = SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
   if((filling & SYMBOL_FILLING_FOK) == SYMBOL_FILLING_FOK)
      return ORDER_FILLING_FOK;
   if((filling & SYMBOL_FILLING_IOC) == SYMBOL_FILLING_IOC)
      return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}

//+------------------------------------------------------------------+
//| Helper: Normalize Volume to Broker Step & Limits                 |
//+------------------------------------------------------------------+
double NormalizeLots(const double rawLots)
{
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(stepLot <= 0.0) stepLot = 0.01;

   double lots = MathFloor(rawLots / stepLot + 1e-9) * stepLot;
   if(lots < minLot) lots = minLot;
   if(lots > maxLot) lots = maxLot;

   int stepDigits = 2;
   if(stepLot >= 1.0)       stepDigits = 0;
   else if(stepLot >= 0.1)  stepDigits = 1;
   else if(stepLot >= 0.01) stepDigits = 2;
   else                     stepDigits = 3;

   return NormalizeDouble(lots, stepDigits);
}

//+------------------------------------------------------------------+
//| Helper: Calculate Risk-Based Lot Size from SL Distance           |
//+------------------------------------------------------------------+
double ComputeLotSize(const double slDist)
{
   if(InpRiskPercent <= 0.0 || slDist <= 0.0)
      return NormalizeLots(InpFixedLotFallback);

   double tickVal = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSz  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickVal <= 0.0 || tickSz <= 0.0)
      return NormalizeLots(InpFixedLotFallback);

   double lossPerLot = (slDist / tickSz) * tickVal;
   if(lossPerLot <= 0.0)
      return NormalizeLots(InpFixedLotFallback);

   double equity    = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney = equity * (InpRiskPercent / 100.0);
   double rawLots   = riskMoney / lossPerLot;
   return NormalizeLots(rawLots);
}

//+------------------------------------------------------------------+
//| Helper: Count Open Positions Managed by This EA on _Symbol       |
//+------------------------------------------------------------------+
int CountMyPositions()
{
   int cnt = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagicNumber)
      {
         cnt++;
      }
   }
   return cnt;
}

//+------------------------------------------------------------------+
//| Helper: Close Opposing Positions on Confirmed Reversal           |
//+------------------------------------------------------------------+
void CloseOpposingPositions(const ENUM_POSITION_TYPE targetCloseType)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;

      ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      if(posType != targetCloseType) continue;

      double vol = PositionGetDouble(POSITION_VOLUME);
      MqlTradeRequest req;
      MqlTradeResult  res;
      ZeroMemory(req);
      ZeroMemory(res);

      req.action       = TRADE_ACTION_DEAL;
      req.position     = ticket;
      req.symbol       = _Symbol;
      req.volume       = vol;
      req.type         = (posType == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      req.price        = (posType == POSITION_TYPE_BUY) ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      req.deviation    = (ulong)MathMax(0, InpSlippagePoints);
      req.magic        = InpMagicNumber;
      req.type_filling = GetOrderFilling();
      req.comment      = "LCP_M15_FlipClose";

      if(!OrderSend(req, res))
         PrintFormat("LeadingConfluenceAutoEntry_M15: Close opposing failed (ticket=%I64u, ret=%u, err=%d)",
                     ticket, res.retcode, GetLastError());
      else
      {
         GlobalVariableDel("LCP_EA_TP1_"  + IntegerToString((long)ticket));
         GlobalVariableDel("LCP_EA_RISK_" + IntegerToString((long)ticket));
         GlobalVariableDel("LCP_EA_BE_"   + IntegerToString((long)ticket));
      }
   }
}

//+------------------------------------------------------------------+
//| Helper: Manage Open Positions (TP1 Partial Close + Breakeven)    |
//+------------------------------------------------------------------+
void ManageOpenPositions()
{
   if(!InpMoveSLToBEAtTP1 && InpPartialClosePctAtTP1 <= 0.0)
      return;

   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(stepLot <= 0.0) stepLot = 0.01;
   double minStopDist = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;

      string beKey   = "LCP_EA_BE_"   + IntegerToString((long)ticket);
      string tp1Key  = "LCP_EA_TP1_"  + IntegerToString((long)ticket);
      string riskKey = "LCP_EA_RISK_" + IntegerToString((long)ticket);
      if(GlobalVariableCheck(beKey) && GlobalVariableGet(beKey) > 0.5)
         continue;

      ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double curSL     = PositionGetDouble(POSITION_SL);
      double curTP     = PositionGetDouble(POSITION_TP);
      double curVol    = PositionGetDouble(POSITION_VOLUME);

      double initRisk = GlobalVariableCheck(riskKey) ? GlobalVariableGet(riskKey) : MathAbs(openPrice - curSL);
      double tp1Price = 0.0;
      if(GlobalVariableCheck(tp1Key))
      {
         tp1Price = GlobalVariableGet(tp1Key);
      }
      else if(initRisk > 0.0)
      {
         tp1Price = (posType == POSITION_TYPE_BUY) ? (openPrice + 0.50 * initRisk) : (openPrice - 0.50 * initRisk);
      }
      else if(curTP > 0.0)
      {
         tp1Price = openPrice + (curTP - openPrice) * 0.20;
      }
      if(tp1Price <= 0.0)
         continue;

      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      bool reachedTP1 = (posType == POSITION_TYPE_BUY) ? (bid >= tp1Price) : (ask <= tp1Price);
      if(!reachedTP1)
         continue;

      // 1) Partial close at TP1 if configured and volume permits
      if(InpPartialClosePctAtTP1 > 0.0 && InpPartialClosePctAtTP1 < 99.0)
      {
         double rawClose = curVol * (InpPartialClosePctAtTP1 / 100.0);
         double closeVol = MathFloor(rawClose / stepLot + 1e-9) * stepLot;
         double remVol   = NormalizeDouble(curVol - closeVol, 3);
         if(closeVol >= minLot && remVol >= minLot)
         {
            MqlTradeRequest pReq;
            MqlTradeResult  pRes;
            ZeroMemory(pReq);
            ZeroMemory(pRes);
            pReq.action       = TRADE_ACTION_DEAL;
            pReq.position     = ticket;
            pReq.symbol       = _Symbol;
            pReq.volume       = NormalizeLots(closeVol);
            pReq.type         = (posType == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
            pReq.price        = (posType == POSITION_TYPE_BUY) ? bid : ask;
            pReq.deviation    = (ulong)MathMax(0, InpSlippagePoints);
            pReq.magic        = InpMagicNumber;
            pReq.type_filling = GetOrderFilling();
            pReq.comment      = "LCP_M15_TP1_Partial";
            if(!OrderSend(pReq, pRes))
               PrintFormat("LeadingConfluenceAutoEntry_M15: Partial close failed (ticket=%I64u, ret=%u)", ticket, pRes.retcode);
            else
               PrintFormat("LeadingConfluenceAutoEntry_M15: TP1 partial closed %.2f lots on #%I64u", pReq.volume, ticket);
         }
      }

      // 2) Move Stop Loss to Protected-Runner level (default -0.50R, or BE+2pts when 0.0)
      if(InpMoveSLToBEAtTP1 && initRisk > 0.0)
      {
         double bePad = (InpRunnerSLRiskMultAtTP1 >= 0.0) ? (2.0 * _Point) : 0.0;
         double protSL = (posType == POSITION_TYPE_BUY)
                         ? NormalizeDouble(openPrice + InpRunnerSLRiskMultAtTP1 * initRisk + bePad, _Digits)
                         : NormalizeDouble(openPrice - InpRunnerSLRiskMultAtTP1 * initRisk - bePad, _Digits);

         bool validMove = (posType == POSITION_TYPE_BUY)
                          ? (protSL > curSL && (bid - protSL) >= minStopDist)
                          : ((curSL == 0.0 || protSL < curSL) && (protSL - ask) >= minStopDist);

         if(validMove && PositionSelectByTicket(ticket))
         {
            MqlTradeRequest mReq;
            MqlTradeResult  mRes;
            ZeroMemory(mReq);
            ZeroMemory(mRes);
            mReq.action   = TRADE_ACTION_SLTP;
            mReq.position = ticket;
            mReq.symbol   = _Symbol;
            mReq.sl       = protSL;
            mReq.tp       = curTP;
            if(!OrderSend(mReq, mRes))
               PrintFormat("LeadingConfluenceAutoEntry_M15: Protected SL update failed (ticket=%I64u, ret=%u)", ticket, mRes.retcode);
            else
               PrintFormat("LeadingConfluenceAutoEntry_M15: Moved SL to Protected Level (%s) on #%I64u", DoubleToString(protSL, _Digits), ticket);
         }
      }

      GlobalVariableSet(beKey, 1.0);
   }
}

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   g_indHandle = iCustom(_Symbol, InpEntryTimeframe, InpIndicatorName);
   if(g_indHandle == INVALID_HANDLE)
   {
      PrintFormat("LeadingConfluenceAutoEntry_M15: Failed to load indicator '%s' (err=%d). Ensure %s.ex5 is in MQL5\\Indicators\\.",
                  InpIndicatorName, GetLastError(), InpIndicatorName);
      return INIT_FAILED;
   }

   // Record latest closed bar so attaching mid-bar waits cleanly for the next closed signal
   g_lastProcessedBar = iTime(_Symbol, InpEntryTimeframe, 1);

   PrintFormat("LeadingConfluenceAutoEntry_M15 initialized on %s [%s] | DryRun=%s | Risk=%.2f%%",
               _Symbol,
               EnumToString(InpEntryTimeframe),
               InpDryRunLogOnly ? "TRUE" : "FALSE",
               InpRiskPercent);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(g_indHandle != INVALID_HANDLE)
   {
      IndicatorRelease(g_indHandle);
      g_indHandle = INVALID_HANDLE;
   }
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
   ManageOpenPositions();

   if(g_indHandle == INVALID_HANDLE)
      return;
   if(BarsCalculated(g_indHandle) < 80)
      return;

   // Evaluate strictly once per newly closed M15 candle (shift = 1)
   datetime closedBarTime = iTime(_Symbol, InpEntryTimeframe, 1);
   if(closedBarTime <= 0 || closedBarTime == g_lastProcessedBar)
      return;
   g_lastProcessedBar = closedBarTime;

   double buyArr[1], sellArr[1], scoreArr[1], slArr[1], tp1Arr[1], tp2Arr[1], mlArr[1];
   if(CopyBuffer(g_indHandle, 0,  1, 1, buyArr)   != 1) return;
   if(CopyBuffer(g_indHandle, 1,  1, 1, sellArr)  != 1) return;
   if(CopyBuffer(g_indHandle, 5,  1, 1, scoreArr) != 1) return;
   if(CopyBuffer(g_indHandle, 6,  1, 1, slArr)    != 1) return;
   if(CopyBuffer(g_indHandle, 7,  1, 1, tp1Arr)   != 1) return;
   if(CopyBuffer(g_indHandle, 8,  1, 1, tp2Arr)   != 1) return;
   if(CopyBuffer(g_indHandle, 10, 1, 1, mlArr)    != 1) return;

   bool isBuy  = (buyArr[0]  != EMPTY_VALUE && buyArr[0]  > 0.0);
   bool isSell = (sellArr[0] != EMPTY_VALUE && sellArr[0] > 0.0);
   if(!isBuy && !isSell)
      return;

   int absScore = (int)MathAbs(scoreArr[0]);
   if(InpMinScoreOverride > 0 && absScore < InpMinScoreOverride)
   {
      PrintFormat("LeadingConfluenceAutoEntry_M15: Signal skipped (Score %d < Override %d)", absScore, InpMinScoreOverride);
      return;
   }

   double slPrice  = NormalizeDouble(slArr[0], _Digits);
   double tp1Price = NormalizeDouble(tp1Arr[0], _Digits);
   double tp2Price = NormalizeDouble(tp2Arr[0], _Digits);
   if(slPrice <= 0.0 || tp2Price <= 0.0 || slPrice == EMPTY_VALUE || tp2Price == EMPTY_VALUE)
      return;

   int spreadPts = (int)SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(InpMaxSpreadPoints > 0 && spreadPts > InpMaxSpreadPoints)
   {
      PrintFormat("LeadingConfluenceAutoEntry_M15: Entry skipped on %s — Spread %d pts > Max %d pts",
                  _Symbol, spreadPts, InpMaxSpreadPoints);
      return;
   }

   if(InpCloseOppositeOnSignal && !InpDryRunLogOnly)
      CloseOpposingPositions(isBuy ? POSITION_TYPE_SELL : POSITION_TYPE_BUY);

   if(CountMyPositions() >= InpMaxPositionsPerSymbol)
   {
      PrintFormat("LeadingConfluenceAutoEntry_M15: Signal detected on %s (%s Score=%d), max positions (%d) reached.",
                  _Symbol, isBuy ? "BUY" : "SELL", absScore, InpMaxPositionsPerSymbol);
      return;
   }

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double entryPrice = isBuy ? ask : bid;
   double slDist     = isBuy ? (entryPrice - slPrice) : (slPrice - entryPrice);
   double minStopDist = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;

   if(slDist <= minStopDist || slDist <= 0.0)
   {
      PrintFormat("LeadingConfluenceAutoEntry_M15: Entry skipped — SL distance invalid at live price (entry=%s, sl=%s)",
                  DoubleToString(entryPrice, _Digits), DoubleToString(slPrice, _Digits));
      return;
   }

   double lots = ComputeLotSize(slDist);

   if(InpDryRunLogOnly)
   {
      PrintFormat("[DRY-RUN] %s M15 %s | Lots=%.2f | Score=%d | ML=%.0f%% | Entry=%s | SL=%s | TP1=%s | TP2=%s",
                  _Symbol, isBuy ? "BUY" : "SELL", lots, absScore, mlArr[0] * 100.0,
                  DoubleToString(entryPrice, _Digits), DoubleToString(slPrice, _Digits),
                  DoubleToString(tp1Price, _Digits), DoubleToString(tp2Price, _Digits));
      return;
   }

   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = _Symbol;
   req.volume       = lots;
   req.type         = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price        = entryPrice;
   req.sl           = slPrice;
   req.tp           = tp2Price;
   req.deviation    = (ulong)MathMax(0, InpSlippagePoints);
   req.magic        = InpMagicNumber;
   req.type_filling = GetOrderFilling();
   req.comment      = StringFormat("LCP_M15_S%d", absScore);

   if(!OrderSend(req, res))
   {
      PrintFormat("LeadingConfluenceAutoEntry_M15: OrderSend FAILED (ret=%u, err=%d, %s)",
                  res.retcode, GetLastError(), res.comment);
      return;
   }

   PrintFormat("LeadingConfluenceAutoEntry_M15: EXECUTED %s %s %.2f lots @ %s | SL=%s | TP1=%s | TP2=%s | Deal=#%I64u",
               _Symbol, isBuy ? "BUY" : "SELL", lots,
               DoubleToString(res.price > 0.0 ? res.price : entryPrice, _Digits),
               DoubleToString(slPrice, _Digits),
               DoubleToString(tp1Price, _Digits),
               DoubleToString(tp2Price, _Digits),
               res.deal);

   // Store TP1 target and initial risk distance per open position ticket
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagicNumber)
      {
         string tp1Key  = "LCP_EA_TP1_"  + IntegerToString((long)ticket);
         string riskKey = "LCP_EA_RISK_" + IntegerToString((long)ticket);
         if(!GlobalVariableCheck(tp1Key))
            GlobalVariableSet(tp1Key, tp1Price);
         if(!GlobalVariableCheck(riskKey))
            GlobalVariableSet(riskKey, slDist);
      }
   }
}
//+------------------------------------------------------------------+
