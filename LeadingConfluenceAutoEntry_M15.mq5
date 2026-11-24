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
#property description "Optional M15 Auto-Entry EA paired with LeadingConfluencePro.ex5 (v2.10)."
#property description "Evaluates strictly shift=1 (closed bar) and syncs TP1 partial, BE, indicator trail & exit markers."

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
input double          InpMaxLots              = 10.00;                  // Maximum Lots Cap
input int             InpMaxSpreadPoints      = 60;                     // Maximum Allowed Spread (Points)
input int             InpMaxPositionsPerSymbol= 1;                      // Max Open EA Positions on This Symbol
input bool            InpCloseOppositeOnSignal= false;                  // Close Opposing Position Before Opening New Signal
input int             InpSlippagePoints       = 20;                     // Max Order Slippage (Points)

input group "=== 3. TP1 Partial Close, Breakeven & Indicator Exits ==="
input bool            InpMoveSLToBEAtTP1      = true;                   // Move Stop Loss to Breakeven When TP1 Reached
input double          InpPartialClosePctAtTP1 = 50.0;                   // % Volume to Close at TP1 (0 = Disable Partial)
input bool            InpFollowIndicatorExits = true;                   // Close Position When Indicator Plots Exit Marker (Buf 6)
input bool            InpFollowIndicatorTrail = true;                   // Sync Position SL Tightening With Indicator Trail (Buf 7)

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
//| Helper: Floor Raw Lots to Broker Step, Min & Cap (SSRN Core)     |
//+------------------------------------------------------------------+
double CoreFloorLots(const double raw, const double minimum, const double step, const double maximum)
{
   if(raw <= 0.0 || minimum <= 0.0 || step <= 0.0 || maximum < minimum) return 0.0;
   double capped = MathMin(raw, maximum);
   double lots   = MathFloor((capped + step * 1.0e-9) / step) * step;
   if(lots + step * 1.0e-8 < minimum) return 0.0;
   return NormalizeDouble(lots, 8);
}

//+------------------------------------------------------------------+
//| Helper: Normalize Volume to Broker Step & Limits                 |
//+------------------------------------------------------------------+
double NormalizeLots(const double rawLots)
{
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = MathMin(InpMaxLots, SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX));
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(stepLot <= 0.0) stepLot = 0.01;
   if(maxLot < minLot) maxLot = minLot;

   double floored = CoreFloorLots(rawLots, minLot, stepLot, maxLot);
   if(floored <= 0.0) floored = minLot;

   int stepDigits = 2;
   if(stepLot >= 1.0)       stepDigits = 0;
   else if(stepLot >= 0.1)  stepDigits = 1;
   else if(stepLot >= 0.01) stepDigits = 2;
   else                     stepDigits = 3;

   return NormalizeDouble(floored, stepDigits);
}

//+------------------------------------------------------------------+
//| Helper: Calculate Risk-Based Lot Size via OrderCalcProfit        |
//+------------------------------------------------------------------+
double ComputeLotSize(const bool isBuy, const double entryPrice, const double slPrice)
{
   double slDist = MathAbs(entryPrice - slPrice);
   if(InpRiskPercent <= 0.0 || slDist <= 0.0 || entryPrice <= 0.0 || slPrice <= 0.0)
      return NormalizeLots(InpFixedLotFallback);

   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney  = equity * (InpRiskPercent / 100.0);
   double minLot     = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double lossPerLot = 0.0;

   double lossRef = 0.0;
   ENUM_ORDER_TYPE oType = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(minLot > 0.0 && OrderCalcProfit(oType, _Symbol, minLot, entryPrice, slPrice, lossRef))
      lossPerLot = MathAbs(lossRef) / minLot;

   if(lossPerLot <= 0.0)
   {
      double tickVal = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
      double tickSz  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
      if(tickVal <= 0.0 || tickSz <= 0.0)
         return NormalizeLots(InpFixedLotFallback);
      lossPerLot = (slDist / tickSz) * tickVal;
   }

   if(lossPerLot <= 0.0)
      return NormalizeLots(InpFixedLotFallback);

   double rawLots = riskMoney / lossPerLot;
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
         GlobalVariableDel("LCP_EA_TP1_" + IntegerToString((long)ticket));
         GlobalVariableDel("LCP_EA_BE_"  + IntegerToString((long)ticket));
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

      string beKey  = "LCP_EA_BE_"  + IntegerToString((long)ticket);
      string tp1Key = "LCP_EA_TP1_" + IntegerToString((long)ticket);
      if(GlobalVariableCheck(beKey) && GlobalVariableGet(beKey) > 0.5)
         continue;

      ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double curSL     = PositionGetDouble(POSITION_SL);
      double curTP     = PositionGetDouble(POSITION_TP);
      double curVol    = PositionGetDouble(POSITION_VOLUME);

      double tp1Price = 0.0;
      if(GlobalVariableCheck(tp1Key))
      {
         tp1Price = GlobalVariableGet(tp1Key);
      }
      else if(curTP > 0.0)
      {
         tp1Price = openPrice + (curTP - openPrice) * 0.60;
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

      // 2) Move Stop Loss to Breakeven (+2 points buffer)
      if(InpMoveSLToBEAtTP1)
      {
         double beSL = (posType == POSITION_TYPE_BUY)
                       ? NormalizeDouble(openPrice + 2.0 * _Point, _Digits)
                       : NormalizeDouble(openPrice - 2.0 * _Point, _Digits);

         bool validMove = (posType == POSITION_TYPE_BUY)
                          ? (beSL > curSL && (bid - beSL) >= minStopDist)
                          : ((curSL == 0.0 || beSL < curSL) && (beSL - ask) >= minStopDist);

         if(validMove && PositionSelectByTicket(ticket))
         {
            MqlTradeRequest mReq;
            MqlTradeResult  mRes;
            ZeroMemory(mReq);
            ZeroMemory(mRes);
            mReq.action   = TRADE_ACTION_SLTP;
            mReq.position = ticket;
            mReq.symbol   = _Symbol;
            mReq.sl       = beSL;
            mReq.tp       = curTP;
            if(!OrderSend(mReq, mRes))
               PrintFormat("LeadingConfluenceAutoEntry_M15: BE SL update failed (ticket=%I64u, ret=%u)", ticket, mRes.retcode);
            else
               PrintFormat("LeadingConfluenceAutoEntry_M15: Moved SL to BE (%s) on #%I64u", DoubleToString(beSL, _Digits), ticket);
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
//| Helper: Sync Open Positions With Indicator Exits & Trail (Buf 6/7)|
//+------------------------------------------------------------------+
void SyncIndicatorExitAndTrail(const datetime closedBarTime)
{
   if((!InpFollowIndicatorExits && !InpFollowIndicatorTrail) || InpDryRunLogOnly || g_indHandle == INVALID_HANDLE)
      return;

   double exitArr[1], trailArr[1];
   bool hasExit  = (InpFollowIndicatorExits && CopyBuffer(g_indHandle, 6, 1, 1, exitArr)  == 1 && exitArr[0]  != EMPTY_VALUE && exitArr[0]  > 0.0);
   bool hasTrail = (InpFollowIndicatorTrail && CopyBuffer(g_indHandle, 7, 1, 1, trailArr) == 1 && trailArr[0] != EMPTY_VALUE && trailArr[0] > 0.0);
   if(!hasExit && !hasTrail)
      return;

   long stopPts   = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   long freezePts = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL);
   double minDist = (double)MathMax(stopPts, freezePts) * _Point + 2.0 * SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double tickSz  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSz <= 0.0) tickSz = _Point;

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;

      datetime openedAt = (datetime)PositionGetInteger(POSITION_TIME);
      if(openedAt >= closedBarTime)
         continue;

      ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

      if(hasExit)
      {
         MqlTradeRequest cReq;
         MqlTradeResult  cRes;
         ZeroMemory(cReq);
         ZeroMemory(cRes);
         cReq.action       = TRADE_ACTION_DEAL;
         cReq.position     = ticket;
         cReq.symbol       = _Symbol;
         cReq.volume       = PositionGetDouble(POSITION_VOLUME);
         cReq.type         = (posType == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
         cReq.price        = (posType == POSITION_TYPE_BUY) ? bid : ask;
         cReq.deviation    = (ulong)MathMax(0, InpSlippagePoints);
         cReq.magic        = InpMagicNumber;
         cReq.type_filling = GetOrderFilling();
         cReq.comment      = "LCP_v210_IndExit";

         if(!OrderSend(cReq, cRes))
            PrintFormat("LeadingConfluenceAutoEntry_M15: Indicator exit close failed (ticket=%I64u, ret=%u)", ticket, cRes.retcode);
         else
         {
            GlobalVariableDel("LCP_EA_TP1_" + IntegerToString((long)ticket));
            GlobalVariableDel("LCP_EA_BE_"  + IntegerToString((long)ticket));
            PrintFormat("LeadingConfluenceAutoEntry_M15: Closed #%I64u on v2.10 indicator Exit marker", ticket);
         }
         continue;
      }

      if(hasTrail)
      {
         double curSL = PositionGetDouble(POSITION_SL);
         double curTP = PositionGetDouble(POSITION_TP);
         double rawT  = trailArr[0];
         double tSL   = (posType == POSITION_TYPE_BUY)
                        ? NormalizeDouble(MathFloor(rawT / tickSz + 1e-9) * tickSz, _Digits)
                        : NormalizeDouble(MathCeil(rawT / tickSz - 1e-9) * tickSz, _Digits);

         bool tighter = (posType == POSITION_TYPE_BUY)
                        ? (tSL > curSL + tickSz * 0.5 && (bid - tSL) >= minDist && (curSL <= 0.0 || (bid - curSL) >= minDist))
                        : ((curSL <= 0.0 || tSL < curSL - tickSz * 0.5) && (tSL - ask) >= minDist && (curSL <= 0.0 || (curSL - ask) >= minDist));

         if(tighter && PositionSelectByTicket(ticket))
         {
            MqlTradeRequest mReq;
            MqlTradeResult  mRes;
            ZeroMemory(mReq);
            ZeroMemory(mRes);
            mReq.action   = TRADE_ACTION_SLTP;
            mReq.position = ticket;
            mReq.symbol   = _Symbol;
            mReq.sl       = tSL;
            mReq.tp       = curTP;
            if(!OrderSend(mReq, mRes))
               PrintFormat("LeadingConfluenceAutoEntry_M15: Indicator trail SL sync failed (ticket=%I64u, ret=%u)", ticket, mRes.retcode);
            else
               PrintFormat("LeadingConfluenceAutoEntry_M15: Synced #%I64u SL to indicator trail %s", ticket, DoubleToString(tSL, _Digits));
         }
      }
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

   // Evaluate strictly once per newly closed candle (shift = 1)
   datetime closedBarTime = iTime(_Symbol, InpEntryTimeframe, 1);
   if(closedBarTime <= 0 || closedBarTime == g_lastProcessedBar)
      return;
   g_lastProcessedBar = closedBarTime;

   SyncIndicatorExitAndTrail(closedBarTime);

   // Read LeadingConfluencePro v2.10 buffer layout (16 buffers)
   double buyArr[1], sellArr[1], scoreArr[1], slArr[1], tp1Arr[1], tp2Arr[1], mlArr[1];
   if(CopyBuffer(g_indHandle, 0,  1, 1, buyArr)   != 1) return;
   if(CopyBuffer(g_indHandle, 1,  1, 1, sellArr)  != 1) return;
   if(CopyBuffer(g_indHandle, 8,  1, 1, scoreArr) != 1) return;
   if(CopyBuffer(g_indHandle, 9,  1, 1, slArr)    != 1) return;
   if(CopyBuffer(g_indHandle, 10, 1, 1, tp1Arr)   != 1) return;
   if(CopyBuffer(g_indHandle, 11, 1, 1, tp2Arr)   != 1) return;
   if(CopyBuffer(g_indHandle, 13, 1, 1, mlArr)    != 1) return;

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

   double lots = ComputeLotSize(isBuy, entryPrice, slPrice);

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

   // Store TP1 target per open position ticket
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagicNumber)
      {
         string tp1Key = "LCP_EA_TP1_" + IntegerToString((long)ticket);
         if(!GlobalVariableCheck(tp1Key))
            GlobalVariableSet(tp1Key, tp1Price);
      }
   }
}
//+------------------------------------------------------------------+
