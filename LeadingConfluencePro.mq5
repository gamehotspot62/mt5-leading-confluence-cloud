//+------------------------------------------------------------------+
//|                                       LeadingConfluencePro.mq5   |
//|     Universal Leading-Indicator Confluence & Cloud ML System     |
//|  Non-Repainting Closed-Bar Signals | Order Flow BVC/CLV + VSA +  |
//|  Liquidity Sweeps + Volatility Squeeze + VWAP + KER + Causal MTF |
//|  FVG/Displacement + Cloud Adaptive Optimization & ML Meta-Filter |
//+------------------------------------------------------------------+
#property copyright "LeadingConfluencePro Quantitative Architecture"
#property link      "https://github.com/gamehotspot62/mt5-leading-confluence-cloud"
#property version   "3.00"
#property description "Universal non-repainting leading confluence indicator v2.01:"
#property description "1) BVC + CLV Order Flow Delta & CVD Divergence (microstructure)"
#property description "2) Wyckoff Liquidity Sweep / SFP Reversal (behavioral stop hunts)"
#property description "3) Bollinger-Keltner Volatility Squeeze & OLS Momentum"
#property description "4) Adaptive VWAP Fair Value Bands + VSA Relative Volume + FVG/Disp"
#property description "5) Kaufman Efficiency Ratio Regime, ATR Percentile & Causal MTF"
#property description "6) 24/7 Cloud Macro/On-Chain/ETF/COT Bias, Adaptive Params & ML Filter"

#property indicator_chart_window
#property indicator_buffers 11
#property indicator_plots   5

//--- Plot 0: Confirmed BUY Arrow
#property indicator_label1  "Confirmed BUY"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrLime
#property indicator_style1  STYLE_SOLID
#property indicator_width1  2

//--- Plot 1: Confirmed SELL Arrow
#property indicator_label2  "Confirmed SELL"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrTomato
#property indicator_style2  STYLE_SOLID
#property indicator_width2  2

//--- Plot 2: Institutional VWAP Fair Value
#property indicator_label3  "Institutional VWAP"
#property indicator_type3   DRAW_LINE
#property indicator_color3  clrDeepSkyBlue
#property indicator_style3  STYLE_SOLID
#property indicator_width3  2

//--- Plot 3: VWAP Upper Band (+Sigma)
#property indicator_label4  "VWAP Upper Band"
#property indicator_type4   DRAW_LINE
#property indicator_color4  clrCrimson
#property indicator_style4  STYLE_DOT
#property indicator_width4  1

//--- Plot 4: VWAP Lower Band (-Sigma)
#property indicator_label5  "VWAP Lower Band"
#property indicator_type5   DRAW_LINE
#property indicator_color5  clrMediumSeaGreen
#property indicator_style5  STYLE_DOT
#property indicator_width5  1

//--- Enumerations
enum ENUM_STRICTNESS
{
   MODE_ACTIVE   = 60, // Active (Score >= 60)
   MODE_BALANCED = 68, // Balanced (Score >= 68)
   MODE_STRICT   = 75, // Strict Institutional (Score >= 75)
   MODE_CUSTOM   = 0   // Custom Minimum Score
};

enum ENUM_VWAP_MODE
{
   VWAP_ROLLING      = 0, // Rolling Adaptive Window (All Timeframes)
   VWAP_SESSION_AUTO = 1  // Session Anchored (D1/W1/MN1 Auto)
};

enum ENUM_VOL_SOURCE
{
   VOL_AUTO = 0, // Auto (Real Volume if available, else Tick Volume)
   VOL_TICK = 1, // Force Tick Volume
   VOL_REAL = 2  // Force Real Volume
};

//--- Input Parameters
input group "=== 1. Confluence & Signal Confirmation ==="
input ENUM_STRICTNESS InpStrictnessMode           = MODE_BALANCED; // Signal Strictness Preset (Manual Fallback)
input int             InpCustomMinScore           = 68;            // Custom Min Score (0-100, used if MODE_CUSTOM)
input int             InpMinBarsBetweenSignals    = 6;             // Minimum Cooldown Bars Between Signals
input bool            InpAllowConsecutiveSameDir  = false;         // Allow Same-Direction Signals Without VWAP Reset
input bool            InpEnableSweepPlaybook      = true;          // Enable Playbook 1: Liquidity Sweep / SFP
input bool            InpEnableSqueezePlaybook    = true;          // Enable Playbook 2: Volatility Squeeze Breakout
input bool            InpEnableVWAPPullbackPlaybook = true;        // Enable Playbook 3: VWAP Pullback Continuation
input bool            InpEnableFVGDisplacement    = true;          // Enable Fair Value Gap & Displacement Urgency Bonus

input group "=== 2. Institutional Fair Value (VWAP) & Volume ==="
input ENUM_VWAP_MODE  InpVWAPMode                 = VWAP_ROLLING;  // VWAP Anchor Mode
input int             InpVWAPPeriod               = 50;            // Rolling VWAP Period (Manual Fallback)
input double          InpVWAPBandMult             = 2.0;           // VWAP Band StdDev Multiplier
input ENUM_VOL_SOURCE InpVolumeSource             = VOL_AUTO;      // Volume Data Source
input int             InpVolPeriod                = 20;            // RVOL Baseline Lookback (Bars)
input bool            InpShowVWAPBands            = true;          // Display VWAP & Deviation Bands on Chart

input group "=== 3. Volatility Squeeze, Structure & Regime ==="
input int             InpATRPeriod                = 14;            // ATR Volatility Period
input int             InpSqzPeriod                = 20;            // Squeeze (BB/KC) Period
input double          InpBBMult                   = 2.0;           // Bollinger Bands Multiplier
input double          InpKCMult                   = 1.5;           // Keltner Channel ATR Multiplier
input int             InpMinSqueezeBars           = 4;             // Minimum Compression Bars Before Release
input int             InpKERPeriod                = 14;            // Kaufman Efficiency Ratio Period
input int             InpPivotLeft                = 5;             // Swing Pivot Left Bars
input int             InpPivotRight               = 3;             // Swing Pivot Right Confirmation Bars
input bool            InpUseHTFFilter             = true;          // Enable Causal Higher-Timeframe Macro Filter
input ENUM_TIMEFRAMES InpHTFTimeframe             = PERIOD_CURRENT;// HTF Timeframe (PERIOD_CURRENT = Auto Next TF)

input group "=== 4. Risk Levels (SL / TP Projections) ==="
input double          InpSLBufferATR              = 0.25;          // Stop Loss Structural Buffer (Manual Fallback)
input double          InpMinSLATR                 = 1.10;          // Minimum Stop Loss Distance (ATR Mult)
input double          InpMaxSLATR                 = 2.50;          // Maximum Stop Loss Distance (ATR Mult)
input double          InpTP1RiskMult              = 1.8;           // Take Profit 1 R-Multiple (Manual Fallback)
input double          InpTP2RiskMult              = 3.0;           // Take Profit 2 R-Multiple (Manual Fallback)
input bool            InpShowLastTradeLines       = true;          // Draw Latest Signal Entry / SL / TP1 / TP2 Lines

input group "=== 5. External Macro & Cloud ML Adaptive Bridge ==="
input bool            InpUseExternalMacroBias     = true;          // Enable External Macro/On-Chain/ETF/COT Bias Bridge
input string          InpMacroCSVFile             = "MacroFlowBias.csv"; // Macro CSV File
input bool            InpMacroHardVeto            = false;         // Hard Veto Signals Opposed by Strong Bias (|Bias|>=50)
input bool            InpUseCloudAdaptiveSettings = true;          // Auto-Apply Cloud Walk-Forward Optimized Params
input string          InpOptConfigCSVFile         = "OptimizedConfig.csv"; // Optimized Config CSV File
input bool            InpUseCloudMLFilter         = true;          // Apply Cloud Trained ML Meta-Label Probability Gate
input string          InpMLWeightsCSVFile         = "MLModelWeights.csv"; // ML Model Weights CSV File
input bool            InpUseCommonFiles           = false;         // Read CSVs from Terminal\\Common\\Files

input group "=== 6. Dashboard & Alerts ==="
input bool            InpShowDashboard            = true;          // Show On-Chart Quant Telemetry Dashboard
input ENUM_BASE_CORNER InpDashCorner              = CORNER_LEFT_UPPER; // Dashboard Corner
input int             InpDashX                    = 14;            // Dashboard X Offset (Pixels)
input int             InpDashY                    = 24;            // Dashboard Y Offset (Pixels)
input int             InpMaxHistoryBars           = 3000;          // Max History Bars to Process (0 = All Bars)
input bool            InpEnableAlert              = true;          // Popup Terminal Alert on Confirmed Closed Bar
input bool            InpEnablePush               = false;         // Mobile Push Notification
input bool            InpEnableEmail              = false;         // Email Notification
input bool            InpEnableSound              = false;         // Play Sound Alert
input string          InpSoundFile                = "alert2.wav";  // Sound File Name

//--- Indicator Plot & Calculation Buffers
double BuySignalBuffer[];
double SellSignalBuffer[];
double VWAPBuffer[];
double VWAPUpperBuffer[];
double VWAPLowerBuffer[];
double ScoreBuffer[];
double SLBuffer[];
double TP1Buffer[];
double TP2Buffer[];
double RegimeBuffer[];
double MLProbBuffer[];

//--- Private Bar-Aligned State Arrays (Non-Series: 0 = Oldest, rates_total - 1 = Live)
double g_effVol[];
double g_rvol[];
double g_atr[];
double g_atrPct[];
double g_ofi[];
double g_delta[];
double g_cvd[];
double g_vwapVal[];
double g_vwapStd[];
double g_vwapZ[];
double g_emaFast[];
double g_emaSlow[];
double g_kcUpper[];
double g_kcLower[];
int    g_sqzOn[];
int    g_sqzCount[];
double g_sqzMom[];
double g_kerRaw[];
int    g_htfBias[];
long   g_sessStart[];
int    g_fvgDispState[]; // 1 = Bull FVG/Disp, -1 = Bear FVG/Disp, 0 = None

double g_swHigh[];
int    g_swHighBar[];
double g_swHighCVD[];
double g_prevSwHigh[];
double g_prevSwHighCVD[];

double g_swLow[];
int    g_swLowBar[];
double g_swLowCVD[];
double g_prevSwLow[];
double g_prevSwLowCVD[];

int    g_lastSigBar[];
int    g_lastSigDir[];
int    g_buyReset[];
int    g_sellReset[];
int    g_sigPlaybook[];

//--- Effective Runtime Parameters (Overridden by Cloud Adaptive Config when enabled & matched)
int    g_effMinScore       = 68;
int    g_effVWAPPeriod     = 50;
double g_effSLBufferATR    = 0.25;
double g_effTP1Mult        = 1.8;
double g_effTP2Mult        = 3.0;
bool   g_effEnSweep        = true;
bool   g_effEnSqueeze      = true;
bool   g_effEnPullback     = true;
double g_effMLMinProb      = 0.50;
bool   g_cloudConfigLoaded = false;
string g_cloudConfigSource = "Manual";
bool   g_cloudConfigDirty  = false;

//--- Cloud ML Logistic Meta-Model State
bool   g_mlLoaded          = false;
string g_mlModelSym        = "OFF";
double g_mlIntercept       = 0.0;
double g_mlMinProb         = 0.50;
double g_mlMean[10];
double g_mlStd[10];
double g_mlWeight[10];

//--- Runtime Lifecycle Globals
string          g_prefix               = "";
int             g_prevRatesTotal       = 0;
int             g_calcStart            = 0;
datetime        g_lastAlertBarTime     = 0;
datetime        g_lastMacroCheck       = 0;
datetime        g_lastCloudCheck       = 0;
int             g_cachedMacroBias      = 0;
string          g_cachedMacroNote      = "OFF";
bool            g_htfPendingSync       = false;
ENUM_TIMEFRAMES g_resolvedHTF          = PERIOD_CURRENT;
int             g_lastVisualRatesTotal = 0;
int             g_lastVisualMacroBias  = 999999;

//--- Playbook IDs
#define PB_NONE         0
#define PB_SWEEP        1
#define PB_SQUEEZE      2
#define PB_VWAP_PULL    3
#define PB_MULTI        4

//+------------------------------------------------------------------+
//| Helper: Standard Normal CDF (Abramowitz & Stegun 7.1.26)         |
//+------------------------------------------------------------------+
double NormCDF(const double z)
{
   double zc = z;
   if(zc > 8.0)  return 1.0;
   if(zc < -8.0) return 0.0;

   double x = MathAbs(zc) / 1.4142135623730951;
   double t = 1.0 / (1.0 + 0.3275911 * x);
   double poly = (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t;
   double erfVal = 1.0 - poly * MathExp(-x * x);
   return 0.5 * (1.0 + ((zc >= 0.0) ? erfVal : -erfVal));
}

//+------------------------------------------------------------------+
//| Helper: Resolve Higher Timeframe                                 |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES ResolveHTF(const ENUM_TIMEFRAMES requested, const ENUM_TIMEFRAMES baseTF)
{
   if(requested != PERIOD_CURRENT && PeriodSeconds(requested) > PeriodSeconds(baseTF))
      return requested;

   switch(baseTF)
   {
      case PERIOD_M1:  return PERIOD_M15;
      case PERIOD_M2:  return PERIOD_M15;
      case PERIOD_M3:  return PERIOD_M15;
      case PERIOD_M4:  return PERIOD_M30;
      case PERIOD_M5:  return PERIOD_M30;
      case PERIOD_M6:  return PERIOD_M30;
      case PERIOD_M10: return PERIOD_H1;
      case PERIOD_M12: return PERIOD_H1;
      case PERIOD_M15: return PERIOD_H1;
      case PERIOD_M20: return PERIOD_H2;
      case PERIOD_M30: return PERIOD_H4;
      case PERIOD_H1:  return PERIOD_H4;
      case PERIOD_H2:  return PERIOD_H8;
      case PERIOD_H3:  return PERIOD_D1;
      case PERIOD_H4:  return PERIOD_D1;
      case PERIOD_H6:  return PERIOD_D1;
      case PERIOD_H8:  return PERIOD_D1;
      case PERIOD_H12: return PERIOD_W1;
      case PERIOD_D1:  return PERIOD_W1;
      case PERIOD_W1:  return PERIOD_MN1;
      default:         return baseTF;
   }
}

//+------------------------------------------------------------------+
//| Helper: Exact Causal Bar Close Timestamp                         |
//+------------------------------------------------------------------+
long GetBarCloseTime(const datetime barOpenTime, const ENUM_TIMEFRAMES tf)
{
   if(tf == PERIOD_MN1)
   {
      MqlDateTime dt;
      TimeToStruct(barOpenTime, dt);
      dt.mon += 1;
      if(dt.mon > 12)
      {
         dt.mon = 1;
         dt.year += 1;
      }
      dt.day  = 1;
      dt.hour = 0;
      dt.min  = 0;
      dt.sec  = 0;
      return (long)StructToTime(dt);
   }
   return (long)barOpenTime + (long)PeriodSeconds(tf);
}

//+------------------------------------------------------------------+
//| Helper: Session Key for Session-Anchored VWAP                    |
//+------------------------------------------------------------------+
long GetSessionKey(const datetime t)
{
   if(_Period < PERIOD_D1)
      return (long)t / 86400;

   if(_Period == PERIOD_D1)
      return ((long)t + 3 * 86400) / (7 * 86400);

   MqlDateTime dt;
   TimeToStruct(t, dt);
   return (long)dt.year * 12 + (long)dt.mon;
}

//+------------------------------------------------------------------+
//| Helper: Effective Bar Volume (Real or Tick Fallback)             |
//+------------------------------------------------------------------+
double GetEffectiveVolume(const int idx, const long &tickVol[], const long &realVol[])
{
   double v = 0.0;
   if(InpVolumeSource == VOL_TICK)
      v = (double)tickVol[idx];
   else if(InpVolumeSource == VOL_REAL)
      v = (double)realVol[idx];
   else
      v = (realVol[idx] > 0) ? (double)realVol[idx] : (double)tickVol[idx];

   return (v > 0.0) ? v : 1.0;
}

//+------------------------------------------------------------------+
//| Helper: Determine Manual Minimum Score Threshold                 |
//+------------------------------------------------------------------+
int GetManualMinScoreThreshold()
{
   if(InpStrictnessMode == MODE_CUSTOM)
      return MathMax(40, MathMin(95, InpCustomMinScore));
   return (int)InpStrictnessMode;
}

//+------------------------------------------------------------------+
//| Helper: Ensure Private Working State Arrays Match rates_total    |
//+------------------------------------------------------------------+
void ResizeStateArrays(const int sz)
{
   int reserve = 256;
   ArrayResize(g_effVol,       sz, reserve);
   ArrayResize(g_rvol,         sz, reserve);
   ArrayResize(g_atr,          sz, reserve);
   ArrayResize(g_atrPct,       sz, reserve);
   ArrayResize(g_ofi,          sz, reserve);
   ArrayResize(g_delta,        sz, reserve);
   ArrayResize(g_cvd,          sz, reserve);
   ArrayResize(g_vwapVal,      sz, reserve);
   ArrayResize(g_vwapStd,      sz, reserve);
   ArrayResize(g_vwapZ,        sz, reserve);
   ArrayResize(g_emaFast,      sz, reserve);
   ArrayResize(g_emaSlow,      sz, reserve);
   ArrayResize(g_kcUpper,      sz, reserve);
   ArrayResize(g_kcLower,      sz, reserve);
   ArrayResize(g_sqzOn,        sz, reserve);
   ArrayResize(g_sqzCount,     sz, reserve);
   ArrayResize(g_sqzMom,       sz, reserve);
   ArrayResize(g_kerRaw,       sz, reserve);
   ArrayResize(g_htfBias,      sz, reserve);
   ArrayResize(g_sessStart,    sz, reserve);
   ArrayResize(g_fvgDispState, sz, reserve);

   ArrayResize(g_swHigh,       sz, reserve);
   ArrayResize(g_swHighBar,    sz, reserve);
   ArrayResize(g_swHighCVD,    sz, reserve);
   ArrayResize(g_prevSwHigh,   sz, reserve);
   ArrayResize(g_prevSwHighCVD,sz, reserve);

   ArrayResize(g_swLow,        sz, reserve);
   ArrayResize(g_swLowBar,     sz, reserve);
   ArrayResize(g_swLowCVD,     sz, reserve);
   ArrayResize(g_prevSwLow,    sz, reserve);
   ArrayResize(g_prevSwLowCVD, sz, reserve);

   ArrayResize(g_lastSigBar,   sz, reserve);
   ArrayResize(g_lastSigDir,   sz, reserve);
   ArrayResize(g_buyReset,     sz, reserve);
   ArrayResize(g_sellReset,    sz, reserve);
   ArrayResize(g_sigPlaybook,  sz, reserve);
}

//+------------------------------------------------------------------+
//| Helper: Strip Broker Suffix & Map Common Aliases                 |
//+------------------------------------------------------------------+
string NormalizeSymbolKey(const string sym)
{
   string orig = sym;
   StringTrimLeft(orig);
   StringTrimRight(orig);

   string upper = orig;
   StringToUpper(upper);

   int sepPos = StringFind(upper, ".");
   if(sepPos > 0)
   {
      upper = StringSubstr(upper, 0, sepPos);
      orig  = StringSubstr(orig, 0, sepPos);
   }
   sepPos = StringFind(upper, "#");
   if(sepPos > 0)
   {
      upper = StringSubstr(upper, 0, sepPos);
      orig  = StringSubstr(orig, 0, sepPos);
   }
   sepPos = StringFind(upper, "_");
   if(sepPos > 0)
   {
      upper = StringSubstr(upper, 0, sepPos);
      orig  = StringSubstr(orig, 0, sepPos);
   }
   sepPos = StringFind(upper, "-");
   if(sepPos > 0)
   {
      upper = StringSubstr(upper, 0, sepPos);
      orig  = StringSubstr(orig, 0, sepPos);
   }

   // Strip common single lowercase broker suffix on >=7 char symbols (e.g. EURUSDm, XAUUSDc)
   int len = StringLen(orig);
   if(len >= 7)
   {
      ushort lastCh = StringGetCharacter(orig, len - 1);
      if(lastCh == 'm' || lastCh == 'c' || lastCh == 'i' || lastCh == 'x')
         upper = StringSubstr(upper, 0, len - 1);
   }

   // Map common broker aliases & substrings to canonical cloud universe keys
   if(StringFind(upper, "XAUUSD") >= 0 || StringFind(upper, "GOLD") >= 0)   return "XAUUSD";
   if(StringFind(upper, "XAGUSD") >= 0 || StringFind(upper, "SILVER") >= 0) return "XAGUSD";
   if(StringFind(upper, "BTCUSD") >= 0 || upper == "BITCOIN")               return "BTCUSD";
   if(StringFind(upper, "ETHUSD") >= 0 || upper == "ETHEREUM")              return "ETHUSD";
   if(StringFind(upper, "EURUSD") >= 0)                                     return "EURUSD";
   if(StringFind(upper, "GBPUSD") >= 0)                                     return "GBPUSD";
   if(StringFind(upper, "USDJPY") >= 0)                                     return "USDJPY";
   if(StringFind(upper, "WTI") >= 0 || StringFind(upper, "XTIUSD") >= 0 ||
      StringFind(upper, "USOUSD") >= 0 || upper == "CL" || upper == "USOIL") return "USOIL";
   if(StringFind(upper, "USTEC") >= 0 || StringFind(upper, "NAS100") >= 0 ||
      StringFind(upper, "NDX") >= 0 || StringFind(upper, "US100") >= 0)    return "US100";
   if(StringFind(upper, "SPX500") >= 0 || StringFind(upper, "SP500") >= 0 ||
      StringFind(upper, "US500") >= 0)                                     return "US500";

   return upper;
}

//+------------------------------------------------------------------+
//| Helper: Refresh Optional External Macro / On-Chain / ETF Bias    |
//+------------------------------------------------------------------+
void RefreshExternalMacroBias(const bool forceNow)
{
   if(!InpUseExternalMacroBias)
   {
      g_cachedMacroBias = 0;
      g_cachedMacroNote = "OFF";
      return;
   }

   datetime nowTime = TimeLocal();
   if(!forceNow && g_lastMacroCheck > 0 && (nowTime - g_lastMacroCheck) < 60)
      return;

   g_lastMacroCheck = nowTime;

   string normSym    = NormalizeSymbolKey(_Symbol);
   string symKey     = "MACRO_BIAS_" + _Symbol;
   string symNormKey = "MACRO_BIAS_" + normSym;

   if(GlobalVariableCheck(symKey))
   {
      int val = (int)MathRound(GlobalVariableGet(symKey));
      g_cachedMacroBias = MathMax(-100, MathMin(100, val));
      g_cachedMacroNote = "GV(" + _Symbol + ")";
      return;
   }
   if(GlobalVariableCheck(symNormKey))
   {
      int val = (int)MathRound(GlobalVariableGet(symNormKey));
      g_cachedMacroBias = MathMax(-100, MathMin(100, val));
      g_cachedMacroNote = "GV(" + normSym + ")";
      return;
   }
   if(GlobalVariableCheck("MACRO_BIAS_GLOBAL"))
   {
      int val = (int)MathRound(GlobalVariableGet("MACRO_BIAS_GLOBAL"));
      g_cachedMacroBias = MathMax(-100, MathMin(100, val));
      g_cachedMacroNote = "GV(GLOBAL)";
      return;
   }

   int commonFlag = InpUseCommonFiles ? FILE_COMMON : 0;
   if(StringLen(InpMacroCSVFile) > 0 && FileIsExist(InpMacroCSVFile, commonFlag))
   {
      int fh = FileOpen(InpMacroCSVFile, FILE_READ | FILE_TXT | FILE_ANSI | commonFlag);
      if(fh != INVALID_HANDLE)
      {
         int exactScore  = 0;
         int normScore   = 0;
         int globalScore = 0;
         bool hasExact   = false;
         bool hasNorm    = false;
         bool hasGlobal  = false;
         string exactNote  = "";
         string normNote   = "";
         string globalNote = "";

         string symUpper = _Symbol;
         StringToUpper(symUpper);

         while(!FileIsEnding(fh))
         {
            string line = FileReadString(fh);
            StringReplace(line, "\r", "");
            StringTrimLeft(line);
            StringTrimRight(line);
            if(StringLen(line) == 0 || StringGetCharacter(line, 0) == '#')
               continue;

            string parts[];
            int count = StringSplit(line, ',', parts);
            if(count < 2)
               continue;

            StringTrimLeft(parts[0]);
            StringTrimRight(parts[0]);
            StringToUpper(parts[0]);
            if(parts[0] == "SYMBOL")
               continue;

            StringTrimLeft(parts[1]);
            StringTrimRight(parts[1]);
            int parsed = (int)MathRound(StringToDouble(parts[1]));
            parsed = MathMax(-100, MathMin(100, parsed));

            string note = (count >= 3) ? parts[2] : "CSV";
            StringTrimLeft(note);
            StringTrimRight(note);

            if(parts[0] == symUpper)
            {
               exactScore = parsed;
               exactNote  = note;
               hasExact   = true;
            }
            else if(parts[0] == normSym)
            {
               normScore = parsed;
               normNote  = note;
               hasNorm   = true;
            }
            else if(parts[0] == "GLOBAL")
            {
               globalScore = parsed;
               globalNote  = note;
               hasGlobal   = true;
            }
         }
         FileClose(fh);

         if(hasExact)  { g_cachedMacroBias = exactScore;  g_cachedMacroNote = exactNote;  return; }
         if(hasNorm)   { g_cachedMacroBias = normScore;   g_cachedMacroNote = normNote;   return; }
         if(hasGlobal) { g_cachedMacroBias = globalScore; g_cachedMacroNote = globalNote; return; }
      }
   }

   g_cachedMacroBias = 0;
   g_cachedMacroNote = "NoFeed(0)";
}

//+------------------------------------------------------------------+
//| Helper: Refresh Cloud Adaptive Settings & ML Weights (60s)       |
//+------------------------------------------------------------------+
void RefreshCloudAdaptiveAndML(const bool forceNow)
{
   datetime nowTime = TimeLocal();
   if(!forceNow && g_lastCloudCheck > 0 && (nowTime - g_lastCloudCheck) < 60)
      return;

   g_lastCloudCheck = nowTime;

   // Snapshot previous effective config to detect real parameter changes
   int    prevMinScore = g_effMinScore;
   int    prevVwapP    = g_effVWAPPeriod;
   double prevSLBuf    = g_effSLBufferATR;
   double prevTP1      = g_effTP1Mult;
   double prevTP2      = g_effTP2Mult;
   bool   prevSw       = g_effEnSweep;
   bool   prevSq       = g_effEnSqueeze;
   bool   prevPl       = g_effEnPullback;
   double prevMLProb   = g_effMLMinProb;
   bool   prevMLLoaded = g_mlLoaded;
   double prevMLInt    = g_mlIntercept;
   double prevMLW0     = g_mlWeight[0];

   // Defaults from user inputs
   g_effMinScore   = GetManualMinScoreThreshold();
   g_effVWAPPeriod = MathMax(5, InpVWAPPeriod);
   g_effSLBufferATR= InpSLBufferATR;
   g_effTP1Mult    = InpTP1RiskMult;
   g_effTP2Mult    = InpTP2RiskMult;
   g_effEnSweep    = InpEnableSweepPlaybook;
   g_effEnSqueeze  = InpEnableSqueezePlaybook;
   g_effEnPullback = InpEnableVWAPPullbackPlaybook;
   g_effMLMinProb  = 0.50;
   g_cloudConfigLoaded = false;
   g_cloudConfigSource = "Manual";

   string symUpper = _Symbol;
   StringToUpper(symUpper);
   string normSym  = NormalizeSymbolKey(_Symbol);
   string tfStr    = EnumToString(_Period);
   StringReplace(tfStr, "PERIOD_", "");
   StringToUpper(tfStr);

   int commonFlag = InpUseCommonFiles ? FILE_COMMON : 0;

   // 1) Read OptimizedConfig.csv (priority: EXACT/TF -> NORM/TF -> NORM/ALL -> GLOBAL/ALL)
   bool cfgHadMLProb = false;
   if(InpUseCloudAdaptiveSettings && StringLen(InpOptConfigCSVFile) > 0 && FileIsExist(InpOptConfigCSVFile, commonFlag))
   {
      int fh = FileOpen(InpOptConfigCSVFile, FILE_READ | FILE_TXT | FILE_ANSI | commonFlag);
      if(fh != INVALID_HANDLE)
      {
         int bestRank = 99;
         int    bMinScore = g_effMinScore;
         int    bVwapP    = g_effVWAPPeriod;
         double bSLBuf    = g_effSLBufferATR;
         double bTP1      = g_effTP1Mult;
         double bTP2      = g_effTP2Mult;
         bool   bSw       = g_effEnSweep;
         bool   bSq       = g_effEnSqueeze;
         bool   bPl       = g_effEnPullback;
         double bMLProb   = 0.50;
         string bSrc      = "Manual";

         while(!FileIsEnding(fh))
         {
            string line = FileReadString(fh);
            StringReplace(line, "\r", "");
            StringTrimLeft(line);
            StringTrimRight(line);
            if(StringLen(line) == 0 || StringGetCharacter(line, 0) == '#')
               continue;

            string p[];
            int cnt = StringSplit(line, ',', p);
            if(cnt < 11)
               continue;

            for(int k = 0; k < cnt; k++)
            {
               StringTrimLeft(p[k]);
               StringTrimRight(p[k]);
            }
            StringToUpper(p[0]);
            StringToUpper(p[1]);
            if(p[0] == "SYMBOL")
               continue;

            int rank = 99;
            string srcLabel = "";
            if(p[0] == symUpper && p[1] == tfStr)      { rank = 1; srcLabel = symUpper + "/" + tfStr; }
            else if(p[0] == normSym && p[1] == tfStr)  { rank = 2; srcLabel = normSym + "/" + tfStr; }
            else if(p[0] == normSym && p[1] == "ALL")  { rank = 3; srcLabel = normSym + "/ALL"; }
            else if(p[0] == "GLOBAL" && p[1] == "ALL") { rank = 4; srcLabel = "GLOBAL"; }

            if(rank < bestRank)
            {
               bestRank  = rank;
               bMinScore = MathMax(40, MathMin(95, (int)StringToInteger(p[2])));
               bVwapP    = MathMax(10, MathMin(300, (int)StringToInteger(p[3])));
               bSLBuf    = MathMax(0.05, MathMin(1.50, StringToDouble(p[4])));
               bTP1      = MathMax(0.50, MathMin(5.00, StringToDouble(p[5])));
               bTP2      = MathMax(bTP1, MathMin(8.00, StringToDouble(p[6])));
               bSw       = (StringToInteger(p[7]) != 0);
               bSq       = (StringToInteger(p[8]) != 0);
               bPl       = (StringToInteger(p[9]) != 0);
               bMLProb   = MathMax(0.05, MathMin(0.95, StringToDouble(p[10])));
               bSrc      = srcLabel;
            }
         }
         FileClose(fh);

         if(bestRank < 99)
         {
            g_effMinScore       = bMinScore;
            g_effVWAPPeriod     = bVwapP;
            g_effSLBufferATR    = bSLBuf;
            g_effTP1Mult        = bTP1;
            g_effTP2Mult        = bTP2;
            g_effEnSweep        = bSw;
            g_effEnSqueeze      = bSq;
            g_effEnPullback     = bPl;
            g_effMLMinProb      = bMLProb;
            g_cloudConfigLoaded = true;
            g_cloudConfigSource = bSrc;
            cfgHadMLProb        = true;
         }
      }
   }

   // 2) Read MLModelWeights.csv (priority: EXACT -> NORM -> GLOBAL)
   g_mlLoaded    = false;
   g_mlModelSym  = "OFF";
   g_mlIntercept = 0.0;
   g_mlMinProb   = 0.50;
   for(int k = 0; k < 10; k++) { g_mlMean[k] = 0.0; g_mlStd[k] = 1.0; g_mlWeight[k] = 0.0; }

   if(InpUseCloudMLFilter && StringLen(InpMLWeightsCSVFile) > 0 && FileIsExist(InpMLWeightsCSVFile, commonFlag))
   {
      int fh = FileOpen(InpMLWeightsCSVFile, FILE_READ | FILE_TXT | FILE_ANSI | commonFlag);
      if(fh != INVALID_HANDLE)
      {
         int bestRank = 99;
         while(!FileIsEnding(fh))
         {
            string line = FileReadString(fh);
            StringReplace(line, "\r", "");
            StringTrimLeft(line);
            StringTrimRight(line);
            if(StringLen(line) == 0 || StringGetCharacter(line, 0) == '#')
               continue;

            string p[];
            int cnt = StringSplit(line, ',', p);
            if(cnt < 33)
               continue;

            StringTrimLeft(p[0]);
            StringTrimRight(p[0]);
            StringToUpper(p[0]);
            if(p[0] == "SYMBOL")
               continue;

            int rank = 99;
            if(p[0] == symUpper)      rank = 1;
            else if(p[0] == normSym)  rank = 2;
            else if(p[0] == "GLOBAL") rank = 3;

            if(rank < bestRank)
            {
               bestRank      = rank;
               g_mlModelSym  = p[0];
               g_mlIntercept = StringToDouble(p[1]);
               g_mlMinProb   = MathMax(0.05, MathMin(0.95, StringToDouble(p[2])));
               for(int k = 0; k < 10; k++)
               {
                  g_mlMean[k]   = StringToDouble(p[3 + k]);
                  double sVal   = StringToDouble(p[13 + k]);
                  g_mlStd[k]    = (MathAbs(sVal) > 1e-9) ? sVal : 1.0;
                  g_mlWeight[k] = StringToDouble(p[23 + k]);
               }
               g_mlLoaded = true;
            }
         }
         FileClose(fh);

         if(g_mlLoaded && !cfgHadMLProb)
            g_effMLMinProb = g_mlMinProb;
      }
   }

   if(!forceNow)
   {
      if(prevMinScore != g_effMinScore ||
         prevVwapP    != g_effVWAPPeriod ||
         MathAbs(prevSLBuf - g_effSLBufferATR) > 1e-6 ||
         MathAbs(prevTP1   - g_effTP1Mult) > 1e-6 ||
         MathAbs(prevTP2   - g_effTP2Mult) > 1e-6 ||
         prevSw       != g_effEnSweep ||
         prevSq       != g_effEnSqueeze ||
         prevPl       != g_effEnPullback ||
         prevMLLoaded != g_mlLoaded ||
         MathAbs(prevMLProb - g_effMLMinProb) > 1e-6 ||
         MathAbs(prevMLInt - g_mlIntercept) > 1e-6 ||
         MathAbs(prevMLW0 - g_mlWeight[0]) > 1e-6)
      {
         g_cloudConfigDirty = true;
      }
   }
}

//+------------------------------------------------------------------+
//| Helper: Evaluate Native Logistic ML Meta-Label Win Probability   |
//+------------------------------------------------------------------+
double EvaluateMLWinProb(const int dir,
                         const double ofi,
                         const double netDelta3,
                         const double rvol,
                         const double vwapZ,
                         const double ker,
                         const int htfBias,
                         const double clv,
                         const double atrPct,
                         const double fvgDispAligned,
                         const int macroBias)
{
   if(!g_mlLoaded)
      return 1.0;

   double x[10];
   x[0] = (double)dir * ofi;
   x[1] = (double)dir * netDelta3;
   x[2] = MathMin(rvol, 5.0);
   x[3] = (double)dir * vwapZ;
   x[4] = ker;
   x[5] = (double)dir * (double)htfBias;
   x[6] = (double)dir * clv;
   x[7] = atrPct / 100.0;
   x[8] = fvgDispAligned;
   x[9] = ((double)dir * (double)macroBias) / 100.0;

   double z = g_mlIntercept;
   for(int k = 0; k < 10; k++)
   {
      double xNorm = (x[k] - g_mlMean[k]) / g_mlStd[k];
      z += g_mlWeight[k] * xNorm;
   }
   z = MathMax(-20.0, MathMin(20.0, z));
   return 1.0 / (1.0 + MathExp(-z));
}

//+------------------------------------------------------------------+
//| Helper: Compute Causal Higher-Timeframe Macro Bias Array         |
//+------------------------------------------------------------------+
void ComputeCausalHTFBias(const int rates_total,
                          const int startIdx,
                          const datetime &time[])
{
   if(!InpUseHTFFilter || g_resolvedHTF == _Period)
   {
      g_htfPendingSync = false;
      for(int i = startIdx; i < rates_total; i++)
         g_htfBias[i] = 0;
      return;
   }

   int chartSec = MathMax(1, PeriodSeconds(_Period));
   int htfSec   = MathMax(chartSec, PeriodSeconds(g_resolvedHTF));
   int ratio    = MathMax(1, htfSec / chartSec);
   int spanBars = rates_total - g_calcStart;
   int needed   = (spanBars / ratio) + 120;
   if(needed < 150) needed = 150;

   int avail = Bars(_Symbol, g_resolvedHTF);
   if(avail <= 0)
   {
      g_htfPendingSync = true;
      for(int i = startIdx; i < rates_total; i++)
         g_htfBias[i] = 0;
      return;
   }
   if(needed > avail) needed = avail;

   MqlRates htfRates[];
   ArraySetAsSeries(htfRates, false);
   int htfCount = CopyRates(_Symbol, g_resolvedHTF, 0, needed, htfRates);
   if(htfCount <= 0)
   {
      g_htfPendingSync = true;
      for(int i = startIdx; i < rates_total; i++)
         g_htfBias[i] = 0;
      return;
   }

   g_htfPendingSync = false;

   double ema20 = htfRates[0].close;
   double ema50 = htfRates[0].close;
   const double a20 = 2.0 / 21.0;
   const double a50 = 2.0 / 51.0;

   int htfBiasArr[];
   long htfCloseTime[];
   ArrayResize(htfBiasArr, htfCount);
   ArrayResize(htfCloseTime, htfCount);

   for(int h = 0; h < htfCount; h++)
   {
      if(h > 0)
      {
         ema20 = a20 * htfRates[h].close + (1.0 - a20) * ema20;
         ema50 = a50 * htfRates[h].close + (1.0 - a50) * ema50;
      }

      int wStart = MathMax(0, h - 29);
      double sumPV = 0.0;
      double sumV  = 0.0;
      for(int k = wStart; k <= h; k++)
      {
         double tp = (htfRates[k].high + htfRates[k].low + htfRates[k].close) / 3.0;
         double v  = (htfRates[k].real_volume > 0) ? (double)htfRates[k].real_volume : (double)htfRates[k].tick_volume;
         if(v <= 0.0) v = 1.0;
         sumPV += tp * v;
         sumV  += v;
      }
      double htfVwap = (sumV > 0.0) ? (sumPV / sumV) : htfRates[h].close;

      int bias = 0;
      if(htfRates[h].close > htfVwap && ema20 >= ema50)
         bias = 1;
      else if(htfRates[h].close < htfVwap && ema20 <= ema50)
         bias = -1;

      htfBiasArr[h]   = bias;
      htfCloseTime[h] = GetBarCloseTime(htfRates[h].time, g_resolvedHTF);
   }

   int hPtr = 0;
   for(int i = startIdx; i < rates_total; i++)
   {
      long barClose = GetBarCloseTime(time[i], _Period);
      while(hPtr + 1 < htfCount && htfCloseTime[hPtr + 1] <= barClose)
         hPtr++;

      if(htfCloseTime[hPtr] <= barClose)
         g_htfBias[i] = htfBiasArr[hPtr];
      else
         g_htfBias[i] = 0;
   }
}

//+------------------------------------------------------------------+
//| Helper: Playbook Name Text                                       |
//+------------------------------------------------------------------+
string PlaybookName(const int pb)
{
   switch(pb)
   {
      case PB_SWEEP:     return "Sweep/SFP";
      case PB_SQUEEZE:   return "SqzBreak";
      case PB_VWAP_PULL: return "VWAPPull";
      case PB_MULTI:     return "MultiConf";
      default:           return "None";
   }
}

//+------------------------------------------------------------------+
//| Helper: Create or Update Chart Label                             |
//+------------------------------------------------------------------+
void SetDashLabel(const string name,
                  const string text,
                  const int xDist,
                  const int yDist,
                  const color clr,
                  const int fontSize = 9,
                  const string fontName = "Consolas")
{
   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, name, OBJPROP_CORNER, InpDashCorner);
      ObjectSetInteger(0, name, OBJPROP_BACK, false);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   }
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, xDist);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, yDist);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, fontName);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, fontSize);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
}

//+------------------------------------------------------------------+
//| Helper: Create or Update Horizontal Signal Level Line            |
//+------------------------------------------------------------------+
void SetLevelLine(const string objBase,
                  const datetime tStart,
                  const datetime tEnd,
                  const double price,
                  const color clr,
                  const ENUM_LINE_STYLE style,
                  const string labelPrefix)
{
   string lineName = g_prefix + objBase + "_Line";
   if(ObjectFind(0, lineName) < 0)
   {
      ObjectCreate(0, lineName, OBJ_TREND, 0, tStart, price, tEnd, price);
      ObjectSetInteger(0, lineName, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, lineName, OBJPROP_RAY_LEFT, false);
      ObjectSetInteger(0, lineName, OBJPROP_BACK, true);
      ObjectSetInteger(0, lineName, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, lineName, OBJPROP_HIDDEN, true);
   }
   ObjectMove(0, lineName, 0, tStart, price);
   ObjectMove(0, lineName, 1, tEnd, price);
   ObjectSetInteger(0, lineName, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, lineName, OBJPROP_STYLE, style);
   ObjectSetInteger(0, lineName, OBJPROP_WIDTH, 1);

   string txtName = g_prefix + objBase + "_Txt";
   string caption = labelPrefix + " " + DoubleToString(price, _Digits);
   if(ObjectFind(0, txtName) < 0)
   {
      ObjectCreate(0, txtName, OBJ_TEXT, 0, tEnd, price);
      ObjectSetInteger(0, txtName, OBJPROP_ANCHOR, ANCHOR_LEFT);
      ObjectSetInteger(0, txtName, OBJPROP_BACK, false);
      ObjectSetInteger(0, txtName, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, txtName, OBJPROP_HIDDEN, true);
   }
   ObjectMove(0, txtName, 0, tEnd, price);
   ObjectSetString(0, txtName, OBJPROP_TEXT, caption);
   ObjectSetString(0, txtName, OBJPROP_FONT, "Consolas");
   ObjectSetInteger(0, txtName, OBJPROP_FONTSIZE, 8);
   ObjectSetInteger(0, txtName, OBJPROP_COLOR, clr);
}

//+------------------------------------------------------------------+
//| Helper: Delete Level Projection Lines                            |
//+------------------------------------------------------------------+
void ClearTradeLines()
{
   string keys[4] = {"ENT", "SL", "TP1", "TP2"};
   for(int k = 0; k < 4; k++)
   {
      ObjectDelete(0, g_prefix + keys[k] + "_Line");
      ObjectDelete(0, g_prefix + keys[k] + "_Txt");
   }
}

//+------------------------------------------------------------------+
//| Helper: Fire Once-Per-Closed-Bar Signal Alerts                   |
//+------------------------------------------------------------------+
void FireSignalAlerts(const int barIdx,
                      const datetime &time[],
                      const double &close[])
{
   bool isBuy  = (BuySignalBuffer[barIdx] != EMPTY_VALUE);
   bool isSell = (SellSignalBuffer[barIdx] != EMPTY_VALUE);
   if(!isBuy && !isSell)
      return;

   string dirStr   = isBuy ? "BUY" : "SELL";
   int    scoreVal = (int)MathAbs(ScoreBuffer[barIdx]);
   string pbStr    = PlaybookName(g_sigPlaybook[barIdx]);
   string mlStr    = (MLProbBuffer[barIdx] > 0.0) ? StringFormat(" | ML: %.0f%%", MLProbBuffer[barIdx] * 100.0) : "";

   string msg = StringFormat("LeadingConfluencePro v2 [%s %s] CONFIRMED %s (%s) | Score: %d/100%s | Entry: %s | SL: %s | TP1: %s | TP2: %s | Bar: %s",
                             _Symbol,
                             EnumToString(_Period),
                             dirStr,
                             pbStr,
                             scoreVal,
                             mlStr,
                             DoubleToString(close[barIdx], _Digits),
                             DoubleToString(SLBuffer[barIdx], _Digits),
                             DoubleToString(TP1Buffer[barIdx], _Digits),
                             DoubleToString(TP2Buffer[barIdx], _Digits),
                             TimeToString(time[barIdx], TIME_DATE | TIME_MINUTES));

   if(InpEnableAlert)
      Alert(msg);
   if(InpEnablePush)
      SendNotification(msg);
   if(InpEnableEmail)
      SendMail(StringFormat("LCPv2 %s %s %s (%d)", _Symbol, EnumToString(_Period), dirStr, scoreVal), msg);
   if(InpEnableSound && StringLen(InpSoundFile) > 0)
      PlaySound(InpSoundFile);
}

//+------------------------------------------------------------------+
//| Helper: Evaluate Historical Stats & Render Dashboard/Trade Lines |
//+------------------------------------------------------------------+
void UpdateVisualsAndStats(const int rates_total,
                           const datetime &time[],
                           const double &high[],
                           const double &low[],
                           const double &close[],
                           const long &realVol[])
{
   int lastClosed = rates_total - 2;
   if(lastClosed < g_calcStart)
      return;

   int totalSignals = 0;
   int closedTrades = 0;
   int tp1Wins      = 0;
   int tp2Wins      = 0;
   int losses       = 0;
   int openTrades   = 0;
   int lastSigIdx   = -1;

   // Causal forward-bar evaluation using strictly closed bars (conservative SL-first tie-breaking)
   for(int s = g_calcStart; s <= lastClosed; s++)
   {
      int dir = 0;
      if(BuySignalBuffer[s] != EMPTY_VALUE)       dir = 1;
      else if(SellSignalBuffer[s] != EMPTY_VALUE) dir = -1;
      else continue;

      totalSignals++;
      lastSigIdx = s;

      double entry = close[s];
      double sl    = SLBuffer[s];
      double tp1   = TP1Buffer[s];
      double tp2   = TP2Buffer[s];
      if(sl == EMPTY_VALUE || tp1 == EMPTY_VALUE || tp2 == EMPTY_VALUE)
         continue;

      bool resolved = false;
      bool hitTP1   = false;

      for(int k = s + 1; k <= lastClosed; k++)
      {
         if(dir == 1)
         {
            bool slHit  = (low[k] <= sl);
            bool tp1Hit = (high[k] >= tp1);
            bool tp2Hit = (high[k] >= tp2);

            if(slHit)
            {
               if(!hitTP1) losses++;
               resolved = true;
               break;
            }
            if(tp2Hit)
            {
               if(!hitTP1) tp1Wins++;
               tp2Wins++;
               hitTP1   = true;
               resolved = true;
               break;
            }
            if(tp1Hit && !hitTP1)
            {
               tp1Wins++;
               hitTP1 = true;
               sl = entry;
            }
         }
         else
         {
            bool slHit  = (high[k] >= sl);
            bool tp1Hit = (low[k] <= tp1);
            bool tp2Hit = (low[k] <= tp2);

            if(slHit)
            {
               if(!hitTP1) losses++;
               resolved = true;
               break;
            }
            if(tp2Hit)
            {
               if(!hitTP1) tp1Wins++;
               tp2Wins++;
               hitTP1   = true;
               resolved = true;
               break;
            }
            if(tp1Hit && !hitTP1)
            {
               tp1Wins++;
               hitTP1 = true;
               sl = entry;
            }
         }
      }

      if(resolved || hitTP1)
         closedTrades++;
      else
         openTrades++;
   }

   // Update Entry / SL / TP1 / TP2 Projection Lines for Latest Confirmed Signal
   if(InpShowLastTradeLines && lastSigIdx >= 0)
   {
      int barSec = MathMax(60, PeriodSeconds(_Period));
      datetime tStart = time[lastSigIdx];
      datetime tEnd   = time[rates_total - 1] + (datetime)(barSec * 10);

      SetLevelLine("ENT", tStart, tEnd, close[lastSigIdx], clrSilver, STYLE_DOT, "ENTRY");
      SetLevelLine("SL",  tStart, tEnd, SLBuffer[lastSigIdx], clrTomato, STYLE_DASH, "SL");
      SetLevelLine("TP1", tStart, tEnd, TP1Buffer[lastSigIdx], clrMediumSeaGreen, STYLE_DASH, "TP1");
      SetLevelLine("TP2", tStart, tEnd, TP2Buffer[lastSigIdx], clrLime, STYLE_SOLID, "TP2");
   }
   else
   {
      ClearTradeLines();
   }

   if(!InpShowDashboard)
      return;

   int panelW = 650;
   int rowCount = 7 + (InpUseExternalMacroBias ? 1 : 0);
   int panelH = 22 + rowCount * 17;

   string bgName = g_prefix + "BG";
   if(ObjectFind(0, bgName) < 0)
   {
      ObjectCreate(0, bgName, OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, bgName, OBJPROP_CORNER, InpDashCorner);
      ObjectSetInteger(0, bgName, OBJPROP_BORDER_TYPE, BORDER_FLAT);
      ObjectSetInteger(0, bgName, OBJPROP_BACK, false);
      ObjectSetInteger(0, bgName, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, bgName, OBJPROP_HIDDEN, true);
   }
   ObjectSetInteger(0, bgName, OBJPROP_XDISTANCE, InpDashX);
   ObjectSetInteger(0, bgName, OBJPROP_YDISTANCE, InpDashY);
   ObjectSetInteger(0, bgName, OBJPROP_XSIZE, panelW);
   ObjectSetInteger(0, bgName, OBJPROP_YSIZE, panelH);
   ObjectSetInteger(0, bgName, OBJPROP_BGCOLOR, C'18,22,30');
   ObjectSetInteger(0, bgName, OBJPROP_COLOR, C'62,78,104');

   int xTxt = InpDashX + 10;
   int yTxt = InpDashY + 8;
   int dy   = 17;

   // Row 1: Title
   string tfName = EnumToString(_Period);
   StringReplace(tfName, "PERIOD_", "");
   string titleStr = StringFormat("LEADING CONFLUENCE PRO v3.0 [%s %s] | Regime-Matched Causal", _Symbol, tfName);
   SetDashLabel(g_prefix + "R1", titleStr, xTxt, yTxt, clrWhite, 9);
   yTxt += dy;

   // Row 2: Active Gates + Volume Source + VWAP
   string volMode = "Tick";
   if(InpVolumeSource == VOL_REAL)
      volMode = "Real";
   else if(InpVolumeSource == VOL_AUTO && realVol[lastClosed] > 0)
      volMode = "Real(Auto)";
   else if(InpVolumeSource == VOL_AUTO)
      volMode = "Tick(Auto)";

   string row2 = StringFormat("Gate: %d+ (%s) | VWAP: %s(%d) | R:R 1:%.1f / 1:%.1f | Vol: %s",
                              g_effMinScore,
                              g_cloudConfigLoaded ? "Cloud" : "Manual",
                              (InpVWAPMode == VWAP_ROLLING) ? "Roll" : "Sess",
                              g_effVWAPPeriod,
                              g_effTP1Mult,
                              g_effTP2Mult,
                              volMode);
   SetDashLabel(g_prefix + "R2", row2, xTxt, yTxt, clrLightSteelBlue, 9);
   yTxt += dy;

   // Row 3: Cloud ML + ATR Percentile + FVG/Displacement Status
   bool cloudSvcOk = (GlobalVariableCheck("LCP_CLOUD_STATUS") && GlobalVariableGet("LCP_CLOUD_STATUS") > 0.5);
   string svcTxt   = cloudSvcOk ? "SYNCED" : ((g_cloudConfigLoaded || g_mlLoaded) ? "LOCAL_CSV" : "DEFAULT");
   string mlTxt    = "OFF";
   if(g_mlLoaded && lastClosed >= g_calcStart + 2)
   {
      double rngLC = high[lastClosed] - low[lastClosed];
      double clvLC = (rngLC > _Point * 0.1) ? (((close[lastClosed] - low[lastClosed]) - (high[lastClosed] - close[lastClosed])) / rngLC) : 0.0;
      double sd3LC = g_delta[lastClosed] + g_delta[lastClosed - 1] + g_delta[lastClosed - 2];
      double sv3LC = MathMax(1.0, g_effVol[lastClosed] + g_effVol[lastClosed - 1] + g_effVol[lastClosed - 2]);
      double nd3LC = sd3LC / sv3LC;
      double bFvgLC = (g_fvgDispState[lastClosed] > 0) ? 1.0 : 0.0;
      double sFvgLC = (g_fvgDispState[lastClosed] < 0) ? 1.0 : 0.0;
      double pBuyLC  = EvaluateMLWinProb(1, g_ofi[lastClosed], nd3LC, g_rvol[lastClosed], g_vwapZ[lastClosed], RegimeBuffer[lastClosed], g_htfBias[lastClosed], clvLC, g_atrPct[lastClosed], bFvgLC, g_cachedMacroBias);
      double pSellLC = EvaluateMLWinProb(-1, g_ofi[lastClosed], nd3LC, g_rvol[lastClosed], g_vwapZ[lastClosed], RegimeBuffer[lastClosed], g_htfBias[lastClosed], clvLC, g_atrPct[lastClosed], sFvgLC, g_cachedMacroBias);
      mlTxt = StringFormat("ON(%s >=%.0f%% | B:%.0f%% S:%.0f%%)", g_mlModelSym, g_effMLMinProb * 100.0, pBuyLC * 100.0, pSellLC * 100.0);
   }
   else if(g_mlLoaded)
   {
      mlTxt = StringFormat("ON(%s >=%.0f%%)", g_mlModelSym, g_effMLMinProb * 100.0);
   }
   string fvgTxt   = (g_fvgDispState[lastClosed] > 0) ? "BULL" : ((g_fvgDispState[lastClosed] < 0) ? "BEAR" : "NONE");
   color cloudClr  = (g_mlLoaded || cloudSvcOk) ? clrAqua : clrSilver;
   string rowCloud = StringFormat("Cloud: %s (Opt:%s) | ML: %s | ATR-Pct: %.0f%% | FVG/Disp: %s",
                                  svcTxt, g_cloudConfigSource, mlTxt, g_atrPct[lastClosed], fvgTxt);
   SetDashLabel(g_prefix + "RCloud", rowCloud, xTxt, yTxt, cloudClr, 9);
   yTxt += dy;

   // Row 4: Live Closed-Bar Microstructure
   double ofiVal  = g_ofi[lastClosed];
   double rvolVal = g_rvol[lastClosed];
   double zVal    = g_vwapZ[lastClosed];
   color microClr = (ofiVal >= 0.20) ? clrLimeGreen : ((ofiVal <= -0.20) ? clrTomato : clrSilver);
   string row3 = StringFormat("Micro[Closed]: OFI %+.2f | RVOL %.2fx | VWAP-Z %+.2f",
                              ofiVal, rvolVal, zVal);
   SetDashLabel(g_prefix + "R3", row3, xTxt, yTxt, microClr, 9);
   yTxt += dy;

   // Row 5: Regime & Causal HTF & Squeeze
   double kerVal = RegimeBuffer[lastClosed];
   string kerTxt = (kerVal >= 0.38) ? "Trending" : ((kerVal < 0.22) ? "Chop/Range" : "Balanced");
   string htfTxt = "OFF";
   if(InpUseHTFFilter && g_resolvedHTF != _Period)
   {
      string htfShort = EnumToString(g_resolvedHTF);
      StringReplace(htfShort, "PERIOD_", "");
      string biasLabel = (g_htfBias[lastClosed] > 0) ? "BULL" : ((g_htfBias[lastClosed] < 0) ? "BEAR" : "NEUT");
      htfTxt = htfShort + ":" + biasLabel;
   }
   string sqzTxt = (g_sqzOn[lastClosed] == 1) ? StringFormat("ON(%d)", g_sqzCount[lastClosed]) : "OFF";
   color regClr  = (g_sqzOn[lastClosed] == 1) ? clrGold : clrDeepSkyBlue;
   string row4 = StringFormat("Regime: KER %.2f (%s) | HTF %s | Sqz %s",
                              kerVal, kerTxt, htfTxt, sqzTxt);
   SetDashLabel(g_prefix + "R4", row4, xTxt, yTxt, regClr, 9);
   yTxt += dy;

   // Row 6 (Optional): External Macro / On-Chain / ETF Bridge
   if(InpUseExternalMacroBias)
   {
      color extClr = (g_cachedMacroBias > 15) ? clrLimeGreen : ((g_cachedMacroBias < -15) ? clrTomato : clrSilver);
      string rowExt = StringFormat("Macro/OnChain/ETF Bias: %+d (%s)%s",
                                   g_cachedMacroBias,
                                   g_cachedMacroNote,
                                   InpMacroHardVeto ? " [VETO ON]" : "");
      SetDashLabel(g_prefix + "RExt", rowExt, xTxt, yTxt, extClr, 9);
      yTxt += dy;
   }
   else
   {
      ObjectDelete(0, g_prefix + "RExt");
   }

   // Row 7: Causal Historical Performance Stats
   double winPct = (closedTrades > 0) ? (100.0 * (double)tp1Wins / (double)closedTrades) : 0.0;
   double totalR = 0.5 * g_effTP1Mult * (double)tp1Wins + 0.5 * g_effTP2Mult * (double)tp2Wins - 1.0 * (double)losses;
   double expR   = (closedTrades > 0) ? (totalR / (double)closedTrades) : 0.0;
   color statClr = (expR > 0.0) ? clrMediumSpringGreen : ((closedTrades == 0) ? clrSilver : clrLightSalmon);
   string row5 = StringFormat("Causal Stats: %d sig (%d closed, %d open) | TP1 Win %.1f%% | TP2 %d | Exp %+.2fR",
                              totalSignals, closedTrades, openTrades, winPct, tp2Wins, expR);
   SetDashLabel(g_prefix + "R5", row5, xTxt, yTxt, statClr, 9);
   yTxt += dy;

   // Row 8: Latest Confirmed Signal Plan
   if(lastSigIdx >= 0)
   {
      bool lastIsBuy = (BuySignalBuffer[lastSigIdx] != EMPTY_VALUE);
      int  lastScore = (int)MathAbs(ScoreBuffer[lastSigIdx]);
      string mlTag   = (MLProbBuffer[lastSigIdx] > 0.0) ? StringFormat(" ML %.0f%%", MLProbBuffer[lastSigIdx] * 100.0) : "";
      string row6 = StringFormat("Last: %s [%s] Score %d%s | @ %s | SL %s | TP1 %s",
                                 lastIsBuy ? "BUY" : "SELL",
                                 PlaybookName(g_sigPlaybook[lastSigIdx]),
                                 lastScore,
                                 mlTag,
                                 DoubleToString(close[lastSigIdx], _Digits),
                                 DoubleToString(SLBuffer[lastSigIdx], _Digits),
                                 DoubleToString(TP1Buffer[lastSigIdx], _Digits));
      SetDashLabel(g_prefix + "R6", row6, xTxt, yTxt, lastIsBuy ? clrLime : clrTomato, 9);
   }
   else
   {
      SetDashLabel(g_prefix + "R6", "Last: Waiting for confirmed closed-bar confluence signal...", xTxt, yTxt, clrSilver, 9);
   }
}

//+------------------------------------------------------------------+
//| Custom indicator initialization function                         |
//+------------------------------------------------------------------+
int OnInit()
{
   g_prefix      = StringFormat("LCP_%I64d_%u_", ChartID(), GetTickCount());
   g_resolvedHTF = ResolveHTF(InpHTFTimeframe, _Period);

   SetIndexBuffer(0,  BuySignalBuffer,  INDICATOR_DATA);
   SetIndexBuffer(1,  SellSignalBuffer, INDICATOR_DATA);
   SetIndexBuffer(2,  VWAPBuffer,       INDICATOR_DATA);
   SetIndexBuffer(3,  VWAPUpperBuffer,  INDICATOR_DATA);
   SetIndexBuffer(4,  VWAPLowerBuffer,  INDICATOR_DATA);
   SetIndexBuffer(5,  ScoreBuffer,      INDICATOR_CALCULATIONS);
   SetIndexBuffer(6,  SLBuffer,         INDICATOR_CALCULATIONS);
   SetIndexBuffer(7,  TP1Buffer,        INDICATOR_CALCULATIONS);
   SetIndexBuffer(8,  TP2Buffer,        INDICATOR_CALCULATIONS);
   SetIndexBuffer(9,  RegimeBuffer,     INDICATOR_CALCULATIONS);
   SetIndexBuffer(10, MLProbBuffer,     INDICATOR_CALCULATIONS);

   ArraySetAsSeries(BuySignalBuffer,  false);
   ArraySetAsSeries(SellSignalBuffer, false);
   ArraySetAsSeries(VWAPBuffer,       false);
   ArraySetAsSeries(VWAPUpperBuffer,  false);
   ArraySetAsSeries(VWAPLowerBuffer,  false);
   ArraySetAsSeries(ScoreBuffer,      false);
   ArraySetAsSeries(SLBuffer,         false);
   ArraySetAsSeries(TP1Buffer,        false);
   ArraySetAsSeries(TP2Buffer,        false);
   ArraySetAsSeries(RegimeBuffer,     false);
   ArraySetAsSeries(MLProbBuffer,     false);

   PlotIndexSetInteger(0, PLOT_ARROW, 233);
   PlotIndexSetInteger(1, PLOT_ARROW, 234);
   PlotIndexSetInteger(0, PLOT_ARROW_SHIFT, 10);
   PlotIndexSetInteger(1, PLOT_ARROW_SHIFT, -10);

   for(int p = 0; p < 5; p++)
      PlotIndexSetDouble(p, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   g_prevRatesTotal       = 0;
   g_calcStart            = 0;
   g_lastAlertBarTime     = 0;
   g_lastMacroCheck       = 0;
   g_lastCloudCheck       = 0;
   g_htfPendingSync       = false;
   g_cloudConfigDirty     = false;
   g_lastVisualRatesTotal = 0;
   g_lastVisualMacroBias  = 999999;

   RefreshExternalMacroBias(true);
   RefreshCloudAdaptiveAndML(true);

   IndicatorSetInteger(INDICATOR_DIGITS, _Digits);
   IndicatorSetString(INDICATOR_SHORTNAME,
                      StringFormat("LeadingConfluencePro v2 (%d+)", g_effMinScore));

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Custom indicator deinitialization function                       |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   ObjectsDeleteAll(0, g_prefix);
   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| Custom indicator iteration function                              |
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
   ArraySetAsSeries(time,        false);
   ArraySetAsSeries(open,        false);
   ArraySetAsSeries(high,        false);
   ArraySetAsSeries(low,         false);
   ArraySetAsSeries(close,       false);
   ArraySetAsSeries(tick_volume, false);
   ArraySetAsSeries(volume,      false);

   // Refresh Cloud Feeds & Adaptive Params/ML Weights (throttled every 60s)
   RefreshExternalMacroBias(prev_calculated == 0);
   RefreshCloudAdaptiveAndML(prev_calculated == 0);

   int warmup = MathMax(g_effVWAPPeriod, MathMax(InpSqzPeriod, MathMax(InpVolPeriod, MathMax(InpATRPeriod, InpKERPeriod))))
                + InpPivotLeft + InpPivotRight + 10;
   if(warmup < 65) warmup = 65;

   if(rates_total < warmup + 5)
      return 0;

   bool htfJustSynced = false;
   if(g_htfPendingSync && InpUseHTFFilter && g_resolvedHTF != _Period)
   {
      if(Bars(_Symbol, g_resolvedHTF) > 0)
         htfJustSynced = true;
   }

   bool fullRecalc = (prev_calculated == 0 ||
                      prev_calculated > rates_total ||
                      g_prevRatesTotal == 0 ||
                      rates_total < g_prevRatesTotal ||
                      rates_total > g_prevRatesTotal + 2 ||
                      htfJustSynced ||
                      g_cloudConfigDirty);
   g_cloudConfigDirty = false;

   if(fullRecalc)
   {
      ResizeStateArrays(rates_total);

      ArrayInitialize(BuySignalBuffer,  EMPTY_VALUE);
      ArrayInitialize(SellSignalBuffer, EMPTY_VALUE);
      ArrayInitialize(VWAPBuffer,       EMPTY_VALUE);
      ArrayInitialize(VWAPUpperBuffer,  EMPTY_VALUE);
      ArrayInitialize(VWAPLowerBuffer,  EMPTY_VALUE);
      ArrayInitialize(ScoreBuffer,      0.0);
      ArrayInitialize(SLBuffer,         EMPTY_VALUE);
      ArrayInitialize(TP1Buffer,        EMPTY_VALUE);
      ArrayInitialize(TP2Buffer,        EMPTY_VALUE);
      ArrayInitialize(RegimeBuffer,     0.0);
      ArrayInitialize(MLProbBuffer,     0.0);

      if(InpMaxHistoryBars > 0 && rates_total > InpMaxHistoryBars + warmup)
         g_calcStart = rates_total - InpMaxHistoryBars - warmup;
      else
         g_calcStart = 0;
   }
   else
   {
      if(rates_total > ArraySize(g_effVol))
         ResizeStateArrays(rates_total);
   }

   int start = fullRecalc ? g_calcStart : MathMax(g_calcStart, prev_calculated - 1);
   if(start < g_calcStart)
      start = g_calcStart;

   ComputeCausalHTFBias(rates_total, start, time);

   const double alphaFast = 2.0 / (MathMax(2, InpSqzPeriod) + 1.0);
   const double alphaSlow = 2.0 / 51.0;
   const int    atrLen    = MathMax(2, InpATRPeriod);
   const int    volLen    = MathMax(2, InpVolPeriod);
   const int    sqzLen    = MathMax(5, InpSqzPeriod);
   const int    kerLen    = MathMax(2, InpKERPeriod);
   const int    minScore  = g_effMinScore;

   //--- Pass 1: Continuous Microstructure, Fair Value, Squeeze, Regime & Swings [start .. rates_total - 1]
   for(int i = start; i < rates_total; i++)
   {
      g_effVol[i] = GetEffectiveVolume(i, tick_volume, volume);

      if(i > g_calcStart)
      {
         int vStart = MathMax(g_calcStart, i - volLen);
         int vCount = i - vStart;
         double sumV = 0.0;
         for(int k = vStart; k < i; k++)
            sumV += g_effVol[k];
         double avgV = (vCount > 0) ? (sumV / (double)vCount) : g_effVol[i];
         g_rvol[i] = (avgV > 0.0) ? (g_effVol[i] / avgV) : 1.0;
      }
      else
      {
         g_rvol[i] = 1.0;
      }

      // Wilder ATR
      double tr = high[i] - low[i];
      if(i > g_calcStart)
      {
         double tr2 = MathAbs(high[i] - close[i - 1]);
         double tr3 = MathAbs(low[i]  - close[i - 1]);
         if(tr2 > tr) tr = tr2;
         if(tr3 > tr) tr = tr3;
      }
      if(i == g_calcStart)
         g_atr[i] = tr;
      else if(i < g_calcStart + atrLen)
         g_atr[i] = (g_atr[i - 1] * (double)(i - g_calcStart) + tr) / (double)(i - g_calcStart + 1);
      else
         g_atr[i] = (g_atr[i - 1] * (double)(atrLen - 1) + tr) / (double)atrLen;

      // 100-Bar Rolling ATR Percentile Rank [0..100]
      if(i >= g_calcStart + 20)
      {
         int aStart = MathMax(g_calcStart, i - 99);
         int aCnt   = i - aStart + 1;
         int leCnt  = 0;
         for(int k = aStart; k <= i; k++)
            if(g_atr[k] <= g_atr[i]) leCnt++;
         g_atrPct[i] = 100.0 * (double)leCnt / (double)aCnt;
      }
      else
      {
         g_atrPct[i] = 50.0;
      }

      // EMAs
      if(i == g_calcStart)
      {
         g_emaFast[i] = close[i];
         g_emaSlow[i] = close[i];
      }
      else
      {
         g_emaFast[i] = alphaFast * close[i] + (1.0 - alphaFast) * g_emaFast[i - 1];
         g_emaSlow[i] = alphaSlow * close[i] + (1.0 - alphaSlow) * g_emaSlow[i - 1];
      }

      // Bulk Volume Classification (BVC) + CLV Order Flow Imbalance
      double bvcNet = 0.0;
      if(i > g_calcStart + 1)
      {
         int dpStart = MathMax(g_calcStart + 1, i - 19);
         int dpCount = i - dpStart + 1;
         double sumDP = 0.0;
         for(int k = dpStart; k <= i; k++)
            sumDP += (close[k] - close[k - 1]);
         double meanDP = sumDP / (double)dpCount;

         double sumVarDP = 0.0;
         for(int k = dpStart; k <= i; k++)
         {
            double diff = (close[k] - close[k - 1]) - meanDP;
            sumVarDP += diff * diff;
         }
         double sigmaDP = MathSqrt(sumVarDP / (double)dpCount);
         if(sigmaDP > 1e-12)
            bvcNet = 2.0 * NormCDF((close[i] - close[i - 1]) / sigmaDP) - 1.0;
      }

      double barRange = high[i] - low[i];
      double clv = (barRange > _Point * 0.1) ? (((close[i] - low[i]) - (high[i] - close[i])) / barRange) : 0.0;
      g_ofi[i]   = 0.60 * bvcNet + 0.40 * clv;
      g_delta[i] = g_effVol[i] * g_ofi[i];
      g_cvd[i]   = (i > g_calcStart) ? (g_cvd[i - 1] + g_delta[i]) : g_delta[i];

      // Track FVG / Displacement state for HUD display
      bool bFvg = (i >= g_calcStart + 2 && low[i] > high[i - 2]);
      bool sFvg = (i >= g_calcStart + 2 && high[i] < low[i - 2]);
      bool bDsp = ((close[i] - open[i]) >= 0.65 * g_atr[i] && g_rvol[i] >= 1.25 && clv >= 0.50);
      bool sDsp = ((open[i] - close[i]) >= 0.65 * g_atr[i] && g_rvol[i] >= 1.25 && clv <= -0.50);
      if((bFvg || bDsp) && !(sFvg || sDsp))      g_fvgDispState[i] = 1;
      else if((sFvg || sDsp) && !(bFvg || bDsp)) g_fvgDispState[i] = -1;
      else                                       g_fvgDispState[i] = 0;

      // Session Anchor Tracking
      if(i == g_calcStart || GetSessionKey(time[i]) != GetSessionKey(time[i - 1]))
         g_sessStart[i] = i;
      else
         g_sessStart[i] = g_sessStart[i - 1];

      // Two-Pass Numerically Stable VWAP & Deviation Bands
      int vwapStart = g_calcStart;
      if(InpVWAPMode == VWAP_ROLLING)
      {
         vwapStart = MathMax(g_calcStart, i - MathMax(5, g_effVWAPPeriod) + 1);
      }
      else
      {
         int sessIdx = (int)g_sessStart[i];
         int floor8  = i - 7;
         vwapStart = MathMax(g_calcStart, MathMin(sessIdx, floor8));
         if(i - vwapStart + 1 > 1440)
            vwapStart = i - 1439;
      }

      double sumPV = 0.0;
      double sumVW = 0.0;
      for(int k = vwapStart; k <= i; k++)
      {
         double tp = (high[k] + low[k] + close[k]) / 3.0;
         double vk = g_effVol[k];
         sumPV += tp * vk;
         sumVW += vk;
      }
      double vwap = (sumVW > 0.0) ? (sumPV / sumVW) : ((high[i] + low[i] + close[i]) / 3.0);

      double sumSqVW = 0.0;
      for(int k = vwapStart; k <= i; k++)
      {
         double tp   = (high[k] + low[k] + close[k]) / 3.0;
         double diff = tp - vwap;
         sumSqVW += g_effVol[k] * diff * diff;
      }
      double vwapStd = (sumVW > 0.0) ? MathSqrt(sumSqVW / sumVW) : 0.0;

      g_vwapVal[i] = vwap;
      g_vwapStd[i] = vwapStd;
      double zDenom = MathMax(vwapStd, 0.25 * g_atr[i]);
      g_vwapZ[i] = (zDenom > 1e-12) ? ((close[i] - vwap) / zDenom) : 0.0;

      if(InpShowVWAPBands)
      {
         VWAPBuffer[i]      = vwap;
         VWAPUpperBuffer[i] = vwap + InpVWAPBandMult * vwapStd;
         VWAPLowerBuffer[i] = vwap - InpVWAPBandMult * vwapStd;
      }
      else
      {
         VWAPBuffer[i]      = EMPTY_VALUE;
         VWAPUpperBuffer[i] = EMPTY_VALUE;
         VWAPLowerBuffer[i] = EMPTY_VALUE;
      }

      // Volatility Squeeze (BB inside KC) + OLS Momentum
      int sqzStart = MathMax(g_calcStart, i - sqzLen + 1);
      int sqzN     = i - sqzStart + 1;
      double sumClose = 0.0;
      double hh = high[sqzStart];
      double ll = low[sqzStart];
      for(int k = sqzStart; k <= i; k++)
      {
         sumClose += close[k];
         if(high[k] > hh) hh = high[k];
         if(low[k]  < ll) ll = low[k];
      }
      double smaSqz = sumClose / (double)sqzN;

      double sumSqBB = 0.0;
      for(int k = sqzStart; k <= i; k++)
      {
         double dc = close[k] - smaSqz;
         sumSqBB += dc * dc;
      }
      double bbStd   = MathSqrt(sumSqBB / (double)sqzN);
      double bbUpper = smaSqz + InpBBMult * bbStd;
      double bbLower = smaSqz - InpBBMult * bbStd;

      double kcUpper = g_emaFast[i] + InpKCMult * g_atr[i];
      double kcLower = g_emaFast[i] - InpKCMult * g_atr[i];
      g_kcUpper[i]   = kcUpper;
      g_kcLower[i]   = kcLower;

      g_sqzOn[i]    = (sqzN >= sqzLen && bbUpper < kcUpper && bbLower > kcLower) ? 1 : 0;
      g_sqzCount[i] = (g_sqzOn[i] == 1) ? ((i > g_calcStart ? g_sqzCount[i - 1] : 0) + 1) : 0;

      if(sqzN >= 2)
      {
         double midBase = 0.5 * (0.5 * (hh + ll) + smaSqz);
         double xBar    = 0.5 * (double)(sqzN - 1);
         double sumXX   = (double)sqzN * ((double)sqzN * (double)sqzN - 1.0) / 12.0;
         double sumY    = 0.0;
         double sumXY   = 0.0;
         for(int k = 0; k < sqzN; k++)
         {
            double yVal = close[sqzStart + k] - midBase;
            sumY  += yVal;
            sumXY += ((double)k - xBar) * yVal;
         }
         if(sumXX > 1e-12)
         {
            double slope     = sumXY / sumXX;
            double intercept = (sumY / (double)sqzN) - slope * xBar;
            g_sqzMom[i] = intercept + slope * (double)(sqzN - 1);
         }
         else
         {
            g_sqzMom[i] = 0.0;
         }
      }
      else
      {
         g_sqzMom[i] = 0.0;
      }

      // Kaufman Efficiency Ratio (KER) & 3-Bar EMA RegimeBuffer
      if(i >= g_calcStart + kerLen)
      {
         double netDir = MathAbs(close[i] - close[i - kerLen]);
         double pathVol = 0.0;
         for(int k = i - kerLen + 1; k <= i; k++)
            pathVol += MathAbs(close[k] - close[k - 1]);
         g_kerRaw[i] = (pathVol > 1e-12) ? (netDir / pathVol) : 0.0;
      }
      else
      {
         g_kerRaw[i] = 0.0;
      }
      RegimeBuffer[i] = (i > g_calcStart) ? (0.5 * g_kerRaw[i] + 0.5 * RegimeBuffer[i - 1]) : g_kerRaw[i];

      // Causal Swing Pivot Detection (Confirmed at i = p + InpPivotRight)
      if(i == g_calcStart)
      {
         g_swHigh[i]        = 0.0;
         g_swHighBar[i]     = -1;
         g_swHighCVD[i]     = 0.0;
         g_prevSwHigh[i]    = 0.0;
         g_prevSwHighCVD[i] = 0.0;

         g_swLow[i]         = 0.0;
         g_swLowBar[i]      = -1;
         g_swLowCVD[i]      = 0.0;
         g_prevSwLow[i]     = 0.0;
         g_prevSwLowCVD[i]  = 0.0;
      }
      else
      {
         g_swHigh[i]        = g_swHigh[i - 1];
         g_swHighBar[i]     = g_swHighBar[i - 1];
         g_swHighCVD[i]     = g_swHighCVD[i - 1];
         g_prevSwHigh[i]    = g_prevSwHigh[i - 1];
         g_prevSwHighCVD[i] = g_prevSwHighCVD[i - 1];

         g_swLow[i]         = g_swLow[i - 1];
         g_swLowBar[i]      = g_swLowBar[i - 1];
         g_swLowCVD[i]      = g_swLowCVD[i - 1];
         g_prevSwLow[i]     = g_prevSwLow[i - 1];
         g_prevSwLowCVD[i]  = g_prevSwLowCVD[i - 1];

         if(i >= g_calcStart + InpPivotLeft + InpPivotRight)
         {
            int p = i - InpPivotRight;
            bool isPivotH = true;
            bool isPivotL = true;

            for(int k = p - InpPivotLeft; k < p; k++)
            {
               if(high[k] > high[p]) isPivotH = false;
               if(low[k]  < low[p])  isPivotL = false;
               if(!isPivotH && !isPivotL) break;
            }
            if(isPivotH || isPivotL)
            {
               for(int k = p + 1; k <= i; k++)
               {
                  if(high[k] >= high[p]) isPivotH = false;
                  if(low[k]  <= low[p])  isPivotL = false;
                  if(!isPivotH && !isPivotL) break;
               }
            }

            if(isPivotH)
            {
               g_prevSwHigh[i]    = g_swHigh[i];
               g_prevSwHighCVD[i] = g_swHighCVD[i];
               g_swHigh[i]        = high[p];
               g_swHighBar[i]     = p;
               g_swHighCVD[i]     = g_cvd[p];
            }
            if(isPivotL)
            {
               g_prevSwLow[i]     = g_swLow[i];
               g_prevSwLowCVD[i]  = g_swLowCVD[i];
               g_swLow[i]         = low[p];
               g_swLowBar[i]      = p;
               g_swLowCVD[i]      = g_cvd[p];
            }
         }
      }
   }

   //--- Pass 2: Strictly Closed-Bar Confluence Signal Generation [start .. rates_total - 2]
   int liveIdx = rates_total - 1;
   BuySignalBuffer[liveIdx]  = EMPTY_VALUE;
   SellSignalBuffer[liveIdx] = EMPTY_VALUE;
   ScoreBuffer[liveIdx]      = 0.0;
   SLBuffer[liveIdx]         = EMPTY_VALUE;
   TP1Buffer[liveIdx]        = EMPTY_VALUE;
   TP2Buffer[liveIdx]        = EMPTY_VALUE;
   MLProbBuffer[liveIdx]     = 0.0;
   g_sigPlaybook[liveIdx]    = PB_NONE;

   int sigLoopEnd = rates_total - 2;
   for(int i = start; i <= sigLoopEnd; i++)
   {
      BuySignalBuffer[i]  = EMPTY_VALUE;
      SellSignalBuffer[i] = EMPTY_VALUE;
      ScoreBuffer[i]      = 0.0;
      SLBuffer[i]         = EMPTY_VALUE;
      TP1Buffer[i]        = EMPTY_VALUE;
      TP2Buffer[i]        = EMPTY_VALUE;
      MLProbBuffer[i]     = 0.0;
      g_sigPlaybook[i]    = PB_NONE;

      if(i == g_calcStart)
      {
         g_lastSigBar[i] = -10000;
         g_lastSigDir[i] = 0;
         g_buyReset[i]   = 1;
         g_sellReset[i]  = 1;
         continue;
      }

      g_lastSigBar[i] = g_lastSigBar[i - 1];
      g_lastSigDir[i] = g_lastSigDir[i - 1];
      g_buyReset[i]   = g_buyReset[i - 1];
      g_sellReset[i]  = g_sellReset[i - 1];

      if(close[i] < g_vwapVal[i]) g_buyReset[i]  = 1;
      if(close[i] > g_vwapVal[i]) g_sellReset[i] = 1;

      if(i < g_calcStart + warmup)
         continue;

      double atr = g_atr[i];
      double barRange = high[i] - low[i];
      if(atr <= _Point * 2.0 || barRange < 0.25 * atr)
         continue;

      bool recentSqz = (g_sqzCount[i - 1] >= InpMinSqueezeBars ||
                        g_sqzCount[i - 2] >= InpMinSqueezeBars ||
                        g_sqzCount[i - 3] >= InpMinSqueezeBars);
      if(g_atrPct[i] < 8.0 && !recentSqz)
         continue;

      if((i - g_lastSigBar[i - 1]) < InpMinBarsBetweenSignals)
         continue;

      double clv = (barRange > _Point * 0.1) ? (((close[i] - low[i]) - (high[i] - close[i])) / barRange) : 0.0;
      double bodyOpenClose = close[i] - open[i];
      double lowerWick = MathMin(open[i], close[i]) - low[i];
      double upperWick = high[i] - MathMax(open[i], close[i]);

      // VSA Absorption Signatures
      bool bullVSA = (g_rvol[i] >= 1.30 && clv >= 0.35 &&
                      (lowerWick >= 0.35 * barRange || bodyOpenClose >= 0.50 * atr));
      bool bearVSA = (g_rvol[i] >= 1.30 && clv <= -0.35 &&
                      (upperWick >= 0.35 * barRange || (-bodyOpenClose) >= 0.50 * atr));

      // Fair Value Gap & Displacement Urgency Flags
      bool bullFVG  = (i >= g_calcStart + 2 && low[i] > high[i - 2]);
      bool bearFVG  = (i >= g_calcStart + 2 && high[i] < low[i - 2]);
      bool bullDisp = (bodyOpenClose >= 0.65 * atr && g_rvol[i] >= 1.25 && clv >= 0.50);
      bool bearDisp = ((-bodyOpenClose) >= 0.65 * atr && g_rvol[i] >= 1.25 && clv <= -0.50);
      double bullFvgDisp = (bullFVG || bullDisp) ? 1.0 : 0.0;
      double bearFvgDisp = (bearFVG || bearDisp) ? 1.0 : 0.0;

      double sumDelta3 = g_delta[i] + g_delta[i - 1] + g_delta[i - 2];
      double sumVol3   = g_effVol[i] + g_effVol[i - 1] + g_effVol[i - 2];
      double netDelta3 = (sumVol3 > 0.0) ? (sumDelta3 / sumVol3) : 0.0;

      double vwapSlope = g_vwapVal[i] - g_vwapVal[i - 3];
      double ker       = RegimeBuffer[i];
      int    htfBias   = g_htfBias[i];
      double z         = g_vwapZ[i];

      //--- Check Playbooks (Bullish — v3.0 Regime-Matched Routing)
      bool   rawBullSweep  = false;
      double bullSweepExt  = low[i];
      bool   trigBullSweep = false;
      bool   trigBullSqz   = false;
      bool   trigBullPull  = false;
      double bullStructExtreme = low[i];

      if(g_effEnSweep && g_swLow[i - 1] > 0.0 && (i - g_swLowBar[i - 1]) <= 80)
      {
         double swL = g_swLow[i - 1];
         bool sweptNow  = (low[i] < swL - 0.05 * atr);
         bool sweptPrev = (low[i - 1] < swL - 0.05 * atr && low[i - 1] > swL - 2.5 * atr);
         if((sweptNow || sweptPrev) &&
            close[i] > swL &&
            (close[i] > open[i] || clv >= 0.45) &&
            g_ofi[i] >= 0.15 &&
            z <= 1.2 &&
            !(htfBias == -1 && ker >= 0.45))
         {
            rawBullSweep = true;
            bullSweepExt = sweptPrev ? MathMin(low[i], low[i - 1]) : low[i];
         }
      }

      if(g_effEnSqueeze)
      {
         double maxHigh5 = high[i - 1];
         for(int k = i - 5; k <= i - 1; k++)
            if(high[k] > maxHigh5) maxHigh5 = high[k];

         if(recentSqz &&
            g_sqzOn[i] == 0 &&
            close[i] > g_kcUpper[i] &&
            close[i] > maxHigh5 &&
            g_sqzMom[i] > 0.0 && g_sqzMom[i] > g_sqzMom[i - 1] &&
            g_rvol[i] >= 1.20 &&
            clv >= 0.35 &&
            g_ofi[i] >= 0.25 &&
            z <= 2.2 &&
            (!InpUseHTFFilter || g_resolvedHTF == _Period || htfBias == 1))
         {
            trigBullSqz = true;
         }
      }

      if(g_effEnPullback)
      {
         bool htfAlignedBull = (!InpUseHTFFilter || g_resolvedHTF == _Period || htfBias == 1);
         bool trendSupport   = htfAlignedBull && (ker >= 0.20 || vwapSlope > 0.0 || g_emaFast[i] > g_emaSlow[i]);
         bool touchedVWAP    = (low[i] <= g_vwapVal[i] + 0.25 * atr) ||
                               (low[i - 1] <= g_vwapVal[i - 1] + 0.25 * atr);
         bool heldSupport    = (low[i] >= g_vwapVal[i] - 2.2 * g_vwapStd[i]);
         bool confirmClose   = (close[i] > open[i] && close[i] > g_vwapVal[i] && clv >= 0.35 &&
                                (close[i] > high[i - 1] || bodyOpenClose >= 0.40 * atr));

         if(trendSupport && touchedVWAP && heldSupport && confirmClose &&
            g_ofi[i] >= 0.20 && g_rvol[i] >= 1.00 && z <= 1.8)
         {
            trigBullPull = true;
         }
      }

      // Standalone Sweep in balanced HTF (0), or confluence Sweep + Sqz/Pullback with HTF (+1)
      trigBullSweep = rawBullSweep && (!InpUseHTFFilter || g_resolvedHTF == _Period || htfBias == 0 || trigBullSqz || trigBullPull);
      if(trigBullSweep)
      {
         bullStructExtreme = bullSweepExt;
         if(trigBullSqz || trigBullPull)
            bullStructExtreme = MathMin(bullStructExtreme, MathMin(low[i], low[i - 1]));
      }
      else if(trigBullSqz || trigBullPull)
      {
         bullStructExtreme = MathMin(low[i], low[i - 1]);
      }

      //--- Check Playbooks (Bearish — v3.0 Regime-Matched Routing)
      bool   rawBearSweep  = false;
      double bearSweepExt  = high[i];
      bool   trigBearSweep = false;
      bool   trigBearSqz   = false;
      bool   trigBearPull  = false;
      double bearStructExtreme = high[i];

      if(g_effEnSweep && g_swHigh[i - 1] > 0.0 && (i - g_swHighBar[i - 1]) <= 80)
      {
         double swH = g_swHigh[i - 1];
         bool sweptNow  = (high[i] > swH + 0.05 * atr);
         bool sweptPrev = (high[i - 1] > swH + 0.05 * atr && high[i - 1] < swH + 2.5 * atr);
         if((sweptNow || sweptPrev) &&
            close[i] < swH &&
            (close[i] < open[i] || clv <= -0.45) &&
            g_ofi[i] <= -0.15 &&
            z >= -1.2 &&
            !(htfBias == 1 && ker >= 0.45))
         {
            rawBearSweep = true;
            bearSweepExt = sweptPrev ? MathMax(high[i], high[i - 1]) : high[i];
         }
      }

      if(g_effEnSqueeze)
      {
         double minLow5 = low[i - 1];
         for(int k = i - 5; k <= i - 1; k++)
            if(low[k] < minLow5) minLow5 = low[k];

         if(recentSqz &&
            g_sqzOn[i] == 0 &&
            close[i] < g_kcLower[i] &&
            close[i] < minLow5 &&
            g_sqzMom[i] < 0.0 && g_sqzMom[i] < g_sqzMom[i - 1] &&
            g_rvol[i] >= 1.20 &&
            clv <= -0.35 &&
            g_ofi[i] <= -0.25 &&
            z >= -2.2 &&
            (!InpUseHTFFilter || g_resolvedHTF == _Period || htfBias == -1))
         {
            trigBearSqz = true;
         }
      }

      if(g_effEnPullback)
      {
         bool htfAlignedBear = (!InpUseHTFFilter || g_resolvedHTF == _Period || htfBias == -1);
         bool trendSupport   = htfAlignedBear && (ker >= 0.20 || vwapSlope < 0.0 || g_emaFast[i] < g_emaSlow[i]);
         bool touchedVWAP    = (high[i] >= g_vwapVal[i] - 0.25 * atr) ||
                               (high[i - 1] >= g_vwapVal[i - 1] - 0.25 * atr);
         bool heldResist     = (high[i] <= g_vwapVal[i] + 2.2 * g_vwapStd[i]);
         bool confirmClose   = (close[i] < open[i] && close[i] < g_vwapVal[i] && clv <= -0.35 &&
                                (close[i] < low[i - 1] || (-bodyOpenClose) >= 0.40 * atr));

         if(trendSupport && touchedVWAP && heldResist && confirmClose &&
            g_ofi[i] <= -0.20 && g_rvol[i] >= 1.00 && z >= -1.8)
         {
            trigBearPull = true;
         }
      }

      trigBearSweep = rawBearSweep && (!InpUseHTFFilter || g_resolvedHTF == _Period || htfBias == 0 || trigBearSqz || trigBearPull);
      if(trigBearSweep)
      {
         bearStructExtreme = bearSweepExt;
         if(trigBearSqz || trigBearPull)
            bearStructExtreme = MathMax(bearStructExtreme, MathMax(high[i], high[i - 1]));
      }
      else if(trigBearSqz || trigBearPull)
      {
         bearStructExtreme = MathMax(high[i], high[i - 1]);
      }

      //--- Calculate 100-Point Confluence Score (Bullish)
      int bullScore = 0;
      int bullPB    = PB_NONE;
      int bullCount = (trigBullSweep ? 1 : 0) + (trigBullSqz ? 1 : 0) + (trigBullPull ? 1 : 0);
      if(bullCount > 0)
      {
         if(bullCount > 1)        bullPB = PB_MULTI;
         else if(trigBullSweep)   bullPB = PB_SWEEP;
         else if(trigBullSqz)     bullPB = PB_SQUEEZE;
         else                     bullPB = PB_VWAP_PULL;

         bullScore += 25;

         if(g_ofi[i] >= 0.35)      bullScore += 10;
         else if(g_ofi[i] >= 0.15) bullScore += 6;

         if(netDelta3 >= 0.12)     bullScore += 5;
         else if(netDelta3 > 0.0)  bullScore += 2;

         bool bullCVDDiv = (trigBullSweep && g_swLow[i - 1] > 0.0 && g_cvd[i] > g_swLowCVD[i - 1]);
         if(bullCVDDiv || bullVSA) bullScore += 5;

         if(g_rvol[i] >= 1.50)      bullScore += 10;
         else if(g_rvol[i] >= 1.20) bullScore += 7;
         else if(g_rvol[i] >= 1.00) bullScore += 4;
         if(bullVSA)                bullScore += 5;

         if(trigBullSweep)
         {
            if(z <= 0.25)      bullScore += 15;
            else if(z <= 0.85) bullScore += 10;
            else               bullScore += 4;
         }
         else
         {
            if(z >= -0.25 && z <= 1.25) bullScore += 15;
            else if(z <= 1.85)          bullScore += 10;
            else                        bullScore += 4;
         }

         if(!InpUseHTFFilter || g_resolvedHTF == _Period) bullScore += 5;
         else if(htfBias == 1)                            bullScore += 10;
         else if(htfBias == 0)                            bullScore += 5;

         if(trigBullSweep && ker <= 0.38)                     bullScore += 5;
         else if((trigBullSqz || trigBullPull) && ker >= 0.28) bullScore += 5;
         else                                                 bullScore += 3;

         if(clv >= 0.55)      bullScore += 5;
         else if(clv >= 0.35) bullScore += 3;
         if(close[i] > high[i - 1]) bullScore += 5;

         if(InpEnableFVGDisplacement && bullFvgDisp > 0.5)
            bullScore += 4;

         if(InpUseExternalMacroBias)
         {
            if(InpMacroHardVeto && g_cachedMacroBias <= -50)
               bullScore = 0;
            else
               bullScore += (int)MathRound(0.15 * (double)g_cachedMacroBias);
         }
         bullScore = MathMax(0, MathMin(100, bullScore));
      }

      //--- Calculate 100-Point Confluence Score (Bearish)
      int bearScore = 0;
      int bearPB    = PB_NONE;
      int bearCount = (trigBearSweep ? 1 : 0) + (trigBearSqz ? 1 : 0) + (trigBearPull ? 1 : 0);
      if(bearCount > 0)
      {
         if(bearCount > 1)        bearPB = PB_MULTI;
         else if(trigBearSweep)   bearPB = PB_SWEEP;
         else if(trigBearSqz)     bearPB = PB_SQUEEZE;
         else                     bearPB = PB_VWAP_PULL;

         bearScore += 25;

         if(g_ofi[i] <= -0.35)      bearScore += 10;
         else if(g_ofi[i] <= -0.15) bearScore += 6;

         if(netDelta3 <= -0.12)     bearScore += 5;
         else if(netDelta3 < 0.0)   bearScore += 2;

         bool bearCVDDiv = (trigBearSweep && g_swHigh[i - 1] > 0.0 && g_cvd[i] < g_swHighCVD[i - 1]);
         if(bearCVDDiv || bearVSA)  bearScore += 5;

         if(g_rvol[i] >= 1.50)      bearScore += 10;
         else if(g_rvol[i] >= 1.20) bearScore += 7;
         else if(g_rvol[i] >= 1.00) bearScore += 4;
         if(bearVSA)                bearScore += 5;

         if(trigBearSweep)
         {
            if(z >= -0.25)      bearScore += 15;
            else if(z >= -0.85) bearScore += 10;
            else                bearScore += 4;
         }
         else
         {
            if(z <= 0.25 && z >= -1.25) bearScore += 15;
            else if(z >= -1.85)         bearScore += 10;
            else                        bearScore += 4;
         }

         if(!InpUseHTFFilter || g_resolvedHTF == _Period) bearScore += 5;
         else if(htfBias == -1)                           bearScore += 10;
         else if(htfBias == 0)                            bearScore += 5;

         if(trigBearSweep && ker <= 0.38)                     bearScore += 5;
         else if((trigBearSqz || trigBearPull) && ker >= 0.28) bearScore += 5;
         else                                                 bearScore += 3;

         if(clv <= -0.55)      bearScore += 5;
         else if(clv <= -0.35) bearScore += 3;
         if(close[i] < low[i - 1]) bearScore += 5;

         if(InpEnableFVGDisplacement && bearFvgDisp > 0.5)
            bearScore += 4;

         if(InpUseExternalMacroBias)
         {
            if(InpMacroHardVeto && g_cachedMacroBias >= 50)
               bearScore = 0;
            else
               bearScore -= (int)MathRound(0.15 * (double)g_cachedMacroBias);
         }
         bearScore = MathMax(0, MathMin(100, bearScore));
      }

      bool allowBuy  = InpAllowConsecutiveSameDir || (g_lastSigDir[i - 1] != 1)  || (g_buyReset[i - 1] == 1);
      bool allowSell = InpAllowConsecutiveSameDir || (g_lastSigDir[i - 1] != -1) || (g_sellReset[i - 1] == 1);

      bool buyValid  = (bullScore >= minScore && allowBuy);
      bool sellValid = (bearScore >= minScore && allowSell);

      double bullProb = 0.0;
      double bearProb = 0.0;
      if(buyValid && InpUseCloudMLFilter && g_mlLoaded)
      {
         bullProb = EvaluateMLWinProb(1, g_ofi[i], netDelta3, g_rvol[i], z, ker, htfBias, clv, g_atrPct[i], bullFvgDisp, g_cachedMacroBias);
         if(bullProb < g_effMLMinProb)
            buyValid = false;
      }
      if(sellValid && InpUseCloudMLFilter && g_mlLoaded)
      {
         bearProb = EvaluateMLWinProb(-1, g_ofi[i], netDelta3, g_rvol[i], z, ker, htfBias, clv, g_atrPct[i], bearFvgDisp, g_cachedMacroBias);
         if(bearProb < g_effMLMinProb)
            sellValid = false;
      }

      int finalDir = 0;
      if(buyValid && !sellValid)
         finalDir = 1;
      else if(sellValid && !buyValid)
         finalDir = -1;
      else if(buyValid && sellValid)
      {
         if(bullScore > bearScore)      finalDir = 1;
         else if(bearScore > bullScore) finalDir = -1;
      }

      if(finalDir == 1)
      {
         double entry   = close[i];
         double rawRisk = MathAbs(entry - (bullStructExtreme - g_effSLBufferATR * atr));
         double minRisk = InpMinSLATR * atr;
         double maxRisk = InpMaxSLATR * atr;
         double risk    = MathMax(minRisk, MathMin(maxRisk, rawRisk));

         BuySignalBuffer[i] = low[i] - 0.35 * atr;
         ScoreBuffer[i]     = (double)bullScore;
         SLBuffer[i]        = NormalizeDouble(entry - risk, _Digits);
         TP1Buffer[i]       = NormalizeDouble(entry + g_effTP1Mult * risk, _Digits);
         TP2Buffer[i]       = NormalizeDouble(entry + g_effTP2Mult * risk, _Digits);
         MLProbBuffer[i]    = (InpUseCloudMLFilter && g_mlLoaded) ? bullProb : 0.0;
         g_sigPlaybook[i]   = bullPB;

         g_lastSigBar[i]    = i;
         g_lastSigDir[i]    = 1;
         g_buyReset[i]      = 0;
         g_sellReset[i]     = 1;
      }
      else if(finalDir == -1)
      {
         double entry   = close[i];
         double rawRisk = MathAbs((bearStructExtreme + g_effSLBufferATR * atr) - entry);
         double minRisk = InpMinSLATR * atr;
         double maxRisk = InpMaxSLATR * atr;
         double risk    = MathMax(minRisk, MathMin(maxRisk, rawRisk));

         SellSignalBuffer[i] = high[i] + 0.35 * atr;
         ScoreBuffer[i]      = -(double)bearScore;
         SLBuffer[i]         = NormalizeDouble(entry + risk, _Digits);
         TP1Buffer[i]        = NormalizeDouble(entry - g_effTP1Mult * risk, _Digits);
         TP2Buffer[i]        = NormalizeDouble(entry - g_effTP2Mult * risk, _Digits);
         MLProbBuffer[i]     = (InpUseCloudMLFilter && g_mlLoaded) ? bearProb : 0.0;
         g_sigPlaybook[i]    = bearPB;

         g_lastSigBar[i]     = i;
         g_lastSigDir[i]     = -1;
         g_sellReset[i]      = 0;
         g_buyReset[i]       = 1;
      }
   }

   //--- Alert Dispatch (Strictly Once Per Newly Closed Bar, Never on Initial Load)
   datetime latestClosedTime = time[rates_total - 2];
   if(fullRecalc || g_prevRatesTotal == 0)
   {
      g_lastAlertBarTime = latestClosedTime;
   }
   else if(latestClosedTime > g_lastAlertBarTime)
   {
      FireSignalAlerts(rates_total - 2, time, close);
      g_lastAlertBarTime = latestClosedTime;
   }

   //--- Update Causal Historical Stats & Dashboard HUD
   if(fullRecalc || rates_total != g_lastVisualRatesTotal || g_cachedMacroBias != g_lastVisualMacroBias)
   {
      UpdateVisualsAndStats(rates_total, time, high, low, close, volume);
      g_lastVisualRatesTotal = rates_total;
      g_lastVisualMacroBias  = g_cachedMacroBias;
   }

   g_prevRatesTotal = rates_total;
   return rates_total;
}
//+------------------------------------------------------------------+
