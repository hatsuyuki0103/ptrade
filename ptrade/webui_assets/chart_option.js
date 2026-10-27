/* ===========================================================================
 * chart_option.js —— **K 线 ECharts option 构建器的唯一真源**（V3-W1 / A6）
 *
 * 谁用它：
 *   1) `ptrade/chart.py` 的 `_render_html`：**read_text() 内联**本文件（剥掉 `export`），
 *      所以 `render_chart` 产物仍是**单文件自包含、零外链**；
 *   2) 前端（`ptrade/webui.py` 主页面，W2）：用**同一份文件**（webapp 静态路由 `/static/chart_option.js`），
 *      调同一个 `buildOption(...)`。
 *   ⇒ **不允许**在别处再写一份 K 线 option 构建器（`tests/test_perf_analysis.py::T-V3-19` 会扫全仓把它抓出来）。
 *
 * ★★ 迷你 K 线（用户显式需求：BOLL/SAR 面板里也要有蜡烛；2026-10-16 曾因 `yAxis[5]` 缺
 *   `scale:true` 而**看不见**，已修）**必须由调用方通过 `opts.miniCandlestick` 传入**：
 *   原来那两个 `__MINI_BOLL__` / `__MINI_SAR__` 占位符是在 Python 侧 `.replace()` 注入的，
 *   **前端没有替换机制** —— 抽函数时若把它们留在体内，迷你蜡烛会在主页面路径上**静默消失**（Critic C3）。
 *
 * 签名（★ W2 请按这个用）：
 *   buildOption(payload, period, opts) -> ECharts option
 *     payload : `{periods:{daily,weekly,monthly}, legend:[[name...]], initial_period, code}`
 *               （= `/api/chart/data` 的 data，或 `render_chart` 内联的 `CHART_DATA`）
 *     period  : "daily" | "weekly" | "monthly"
 *     opts    : { miniCandlestick?: {boll?: <series spec>, sar?: <series spec>},
 *                 getActive?: () => string }
 *               `miniCandlestick` 的两段用本文件的 `miniCandlestick(axisIndex)` 产物
 *               （axisIndex=1 → BOLL 面板，5 → SAR 面板）；**省略则不画迷你蜡烛**。
 *   miniCandlestick(axisIndex) -> series spec（不含 `data`；`buildOption` 会按周期填 `d.ohlc`）
 * ========================================================================= */
const UP = "#ef232a", DOWN = "#14b143";
const LABEL = { daily: "日线", weekly: "周线", monthly: "月线" };

/** BOLL / SAR 面板共用的**背景迷你 K 线** series 规格（唯一工厂）。
 *  ★ axisIndex 必须与面板号一致（1=BOLL, 5=SAR）；该面板的 yAxis 必须有 `scale:true`，
 *    否则价格轴从 0 起 → 蜡烛被压成 <1 像素（完全不可见）。 */
export function miniCandlestick(axisIndex) {
  const idx = Number(axisIndex) | 0;
  return {
    name: "微型K线", type: "candlestick",
    xAxisIndex: idx, yAxisIndex: idx,
    barWidth: "62%", silent: true, legendHoverLink: false,
    itemStyle: {
      color: "rgba(239,35,42,0.45)", color0: "rgba(20,177,67,0.45)",
      borderColor: "rgba(239,35,42,0.6)", borderColor0: "rgba(20,177,67,0.6)",
    },
    z: 1,
  };
}

function num(v) { return (v === null || v === undefined) ? "-" : v; }

export function buildOption(payload, period, opts) {
  const o = opts || {};
  const d = payload.periods[period];
  const d0 = d.dates.length;
  /* ★★ v6：**按周期**的默认可见根数（用户拍板：日线近半年 · 周线/月线近 5 年）。
     旧实现是写死 `120` 并用**百分比**（`startPct`）。为什么改成**索引**：
       ① 6023 根时 1% ≈ 60 根 ⇒ 百分比表达不出「≈120 根」；
       ② 用日期字符串又会在 `dates` 有重复时按名字匹配、行为依赖实现；
       ③ 用索引用确定：`startIndex = max(0, d0 - visible)`。
     ★ 注意 `dataZoom` 两个组件（inside/slider）**必须用同一对索引** —— 只设一处会不同步。
     ★ `filterMode:"filter"` 显式写：ECharts 5.5.1 的默认确实是 filter，但**显式才叫契约**。 */
  const DEFAULT_VISIBLE_BARS = { daily: 120, weekly: 260, monthly: 60 };
  const visible = DEFAULT_VISIBLE_BARS[period] || d0;
  const startIndex = Math.max(0, d0 - visible);
  const endIndex = Math.max(0, d0 - 1);
  /* 图例项（分组 → 扁平）：分组真源在 Python 的 `legend_groups()`，这里只展平 */
  /* ★ V4（用户诉求 4）：图例只留 6 条 —— MA12/24/48/96 + VOL MA5/MA10。
     **series 一条都不删**（只过滤图例条目）；面板标题里的说明（②⑤⑥）也不动，
     那是用户认线/认指标的**唯一依据**。legendselectchanged 照旧可点。 */
  const LEGEND_KEEP = ["MA12", "MA24", "MA48", "MA96", "VOL MA5", "VOL MA10"];
  const legendData = (function () {
    const out = [];
    (payload.legend || []).forEach(function (g) {
      g.forEach(function (n) { if (LEGEND_KEEP.indexOf(n) >= 0) out.push(n); });
    });
    return out;
  })();
  /* 迷你 K 线：**由调用方传入**（见文件头 C3 说明）；没传就不画。 */
  const mini = o.miniCandlestick || {};
  function miniSeries(spec) {
    if (!spec) return null;
    const item = Object.assign({}, spec);
    item.data = d.ohlc;
    return item;
  }
  const activePeriod = function () {
    return (typeof o.getActive === "function" ? o.getActive() : period);
  };
  /* 6 个图表面板：K线 / BOLL / 成交量 / 成交额 / MACD / SAR（第 7 格是下方 HTML 分析区） */
  const grids = [
    {left:66, right:26, top:"2%",  height:"28%"},
    {left:66, right:26, top:"32%", height:"11%"},
    {left:66, right:26, top:"44.5%", height:"9%"},
    {left:66, right:26, top:"55%", height:"9%"},
    {left:66, right:26, top:"65.5%", height:"9%"},
    {left:66, right:26, top:"76%", height:"10%"}
  ];
  const xAxis = grids.map(function (g, i) {
    return {
      type:"category", gridIndex:i, data:d.dates, boundaryGap:true,
      axisLine:{onZero:false, lineStyle:{color:"#c9cdd4"}},
      axisTick:{show:i === 5}, axisLabel:{show:i === 5, color:"#86909c"},
      splitLine:{show:false}, min:"dataMin", max:"dataMax",
      axisPointer:{label:{show:i === 0}}
    };
  });
  const yAxis = [
    {scale:true, gridIndex:0, splitNumber:5, axisLabel:{color:"#86909c"}, splitLine:{lineStyle:{color:"#f2f3f5"}}},
    {scale:true, gridIndex:1, splitNumber:3, axisLabel:{color:"#86909c"}, splitLine:{lineStyle:{color:"#f2f3f5"}}},
    {gridIndex:2, splitNumber:2, axisLabel:{show:false}, splitLine:{show:false}},
    /* 成交额独立面板：亿元刻度 */
    {gridIndex:3, splitNumber:3, splitLine:{show:false},
     axisLabel:{color:"#1f6feb", formatter:function (v) { return (v / 1e8).toFixed(0) + "亿"; }}},
    {gridIndex:4, splitNumber:2, axisLabel:{show:false}, splitLine:{show:false}},
    /* ⑥ SAR 面板：与 BOLL 面板一样铺了迷你 K 线，而它的值是**价格单位** ——
       缺 `scale:true` 时 ECharts 会让轴**从 0 起**，价格区间被压进面板顶部约 20%，
       蜡烛没有最小尺寸 → 被压成 <1 像素 → **完全不可见**（散点靠固定 symbolSize:4 才勉强可见）。
       ⚠️ 凡是共享迷你 K 线的面板都必须有 scale:true（tests/test_chart_v2.py 有判据钉住）。 */
    {scale:true, gridIndex:5, splitNumber:2, axisLabel:{show:false}, splitLine:{show:false}}
  ];
  const series = [
    /* ① K线面板：只放 K线 + MA12/24/48/96（BOLL 已移到独立面板） */
    {name:"K线", type:"candlestick", data:d.ohlc, xAxisIndex:0, yAxisIndex:0,
     itemStyle:{color:UP, color0:DOWN, borderColor:UP, borderColor0:DOWN}},
    {name:"MA12", type:"line", data:d.ma["12"], xAxisIndex:0, yAxisIndex:0, smooth:true,
     showSymbol:false, lineStyle:{width:1.2, color:"#1f2329"}},
    {name:"MA24", type:"line", data:d.ma["24"], xAxisIndex:0, yAxisIndex:0, smooth:true,
     showSymbol:false, lineStyle:{width:1.2, color:"#f2c200"}},
    {name:"MA48", type:"line", data:d.ma["48"], xAxisIndex:0, yAxisIndex:0, smooth:true,
     showSymbol:false, lineStyle:{width:1.2, color:"#ff7ac6"}},
    {name:"MA96", type:"line", data:d.ma["96"], xAxisIndex:0, yAxisIndex:0, smooth:true,
     showSymbol:false, lineStyle:{width:1.2, color:"#14b143"}},
    /* ② BOLL(20,2) 独立面板（背景铺一层迷你 K 线，与 SAR 面板共用同一工厂） */
    miniSeries(mini.boll),
    {name:"BOLL上轨", type:"line", data:d.boll.upper, xAxisIndex:1, yAxisIndex:1, showSymbol:false,
     lineStyle:{width:1, color:"#8c8c8c", type:"dashed"}},
    {name:"BOLL中轨", type:"line", data:d.boll.mid, xAxisIndex:1, yAxisIndex:1, showSymbol:false,
     lineStyle:{width:1.2, color:"#ff9f43"}},
    {name:"BOLL下轨", type:"line", data:d.boll.lower, xAxisIndex:1, yAxisIndex:1, showSymbol:false,
     lineStyle:{width:1, color:"#8c8c8c", type:"dashed"}},
    /* ③ 成交量面板：只放量 */
    {name:"成交量", type:"bar", data:d.volume.map(function (v, i) {
       return {value:v, itemStyle:{color: d.bar_up[i] ? UP : DOWN}};
     }), xAxisIndex:2, yAxisIndex:2, barWidth:"60%"},
    {name:"VOL MA5", type:"line", data:d.vol_ma["5"], xAxisIndex:2, yAxisIndex:2, showSymbol:false,
     lineStyle:{width:1, color:"#f2c200"}},
    {name:"VOL MA10", type:"line", data:d.vol_ma["10"], xAxisIndex:2, yAxisIndex:2, showSymbol:false,
     lineStyle:{width:1, color:"#1f6feb"}},
    /* ④ 成交额独立面板：**柱状**（涨跌红/绿），亿元刻度，与成交量彻底分家 */
    {name:"成交额", type:"bar", data:d.amount.map(function (v, i) {
       return {value:v, itemStyle:{color: d.bar_up[i] ? UP : DOWN}};
     }), xAxisIndex:3, yAxisIndex:3, barWidth:"70%"},
    /* ⑤ MACD 面板 */
    {name:"MACD柱", type:"bar", data:d.macd.hist.map(function (v) {
       return {value:v, itemStyle:{color: (v !== null && v >= 0) ? UP : DOWN}};
     }), xAxisIndex:4, yAxisIndex:4, barWidth:"50%"},
    {name:"DIF", type:"line", data:d.macd.dif, xAxisIndex:4, yAxisIndex:4, showSymbol:false,
     lineStyle:{width:1.1, color:"#1f2329"}},
    {name:"DEA", type:"line", data:d.macd.dea, xAxisIndex:4, yAxisIndex:4, showSymbol:false,
     lineStyle:{width:1.1, color:"#f2c200"}},
    /* ⑥ SAR 面板（同样铺背景迷你 K 线；SAR 本身仍是红绿散点，**不动**） */
    miniSeries(mini.sar),
    {name:"SAR", type:"scatter", data:d.sar.value.map(function (v, i) {
       return {value:v, itemStyle:{color: d.sar.trend[i] > 0 ? UP : DOWN}};
     }), xAxisIndex:5, yAxisIndex:5, symbolSize:4}
  ].filter(Boolean);
  return {
    animation:false,
    backgroundColor:"#fff",
    /* 7 个面板标题全部显式（第 7 格为下方 HTML 分析区） */
    title:[
      {text:"① K线 + MA12/24/48/96", left:70, top:"0.5%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"② BOLL(20,2) 上/中/下轨", left:70, top:"30.5%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"③ 成交量（股）+ VOL MA5/MA10", left:70, top:"43%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"④ 成交额（亿元）", left:70, top:"53.5%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"⑤ MACD(12,26,9)", left:70, top:"64%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"⑥ SAR(0.02/0.02/0.20)", left:70, top:"74.5%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}}
    ],
    legend:{top:"0.5%", right:26, itemWidth:14, itemHeight:8, textStyle:{fontSize:11},
            data:legendData},
    tooltip:{trigger:"axis", axisPointer:{type:"cross", link:[{xAxisIndex:"all"}]},
      backgroundColor:"rgba(255,255,255,0.96)", borderColor:"#e5e6eb", textStyle:{color:"#1f2329", fontSize:12},
      formatter:function (ps) {
        if (!ps || !ps.length) return "";
        const i = ps[0].dataIndex, dd = payload.periods[activePeriod()];
        const rows = ["<b>" + dd.dates[i] + "</b>"];
        const ohlc = dd.ohlc[i] || [];
        if (ohlc.length === 4) {
          const up = ohlc[1] >= ohlc[0];
          rows.push("开 " + num(ohlc[0]) + " 收 <b style='color:" + (up ? UP : DOWN) + "'>" + num(ohlc[1]) + "</b>");
          rows.push("高 " + num(ohlc[3]) + " 低 " + num(ohlc[2]));
        }
        if (dd.pct_chg && dd.pct_chg[i] !== null) rows.push("涨跌幅 " + num(dd.pct_chg[i]) + "%");
        const vol = dd.volume[i], amt = dd.amount[i];
        if (vol !== null) rows.push("量 " + (vol / 1e4).toFixed(2) + " 万  MA5 " + ((dd.vol_ma["5"][i] || 0) / 1e4).toFixed(2) + " 万  MA10 " + ((dd.vol_ma["10"][i] || 0) / 1e4).toFixed(2) + " 万");
        if (amt !== null) rows.push("成交额 " + (amt / 1e8).toFixed(3) + " 亿");
        rows.push("MA12 " + num(dd.ma["12"][i]) + " / MA24 " + num(dd.ma["24"][i]) + " / MA48 " + num(dd.ma["48"][i]) + " / MA96 " + num(dd.ma["96"][i]));
        rows.push("BOLL " + num(dd.boll.lower[i]) + " / " + num(dd.boll.mid[i]) + " / " + num(dd.boll.upper[i]));
        rows.push("DIF " + num(dd.macd.dif[i]) + " DEA " + num(dd.macd.dea[i]) + " 柱 " + num(dd.macd.hist[i]));
        rows.push("SAR " + num(dd.sar.value[i]) + (dd.sar.trend[i] > 0 ? " (多)" : " (空)"));
        return rows.join("<br>");
      }},
    axisPointer:{link:[{xAxisIndex:"all"}], label:{backgroundColor:"#1f6feb"}},
    grid:grids, xAxis:xAxis, yAxis:yAxis, series:series,
    dataZoom:[
      /* ★ v6：**索引** + 显式 filterMode；两个组件同一对 startValue/endValue ⇒ 天然同步 */
      {type:"inside", filterMode:"filter", xAxisIndex:[0,1,2,3,4,5],
       startValue:startIndex, endValue:endIndex},
      {type:"slider", filterMode:"filter", xAxisIndex:[0,1,2,3,4,5], bottom:2, height:16,
       startValue:startIndex, endValue:endIndex}
    ]
  };
}

/* ===========================================================================
 * 分时（period === "minute"）option 构建器 —— **唯一真源**
 * （`chart.py` 的 `render_intraday` HTML 路径与 Web 主页面 W2 共用同一份）
 *
 * payload 形状（= `chart._intraday_payload()` 的产物，`/api/chart/data?period=minute` 的 data）：
 *   { kind:"intraday", code, name, day, asof, source, complete, fetched_at,
 *     times:[ "09:30", ... ], price:[num|null], avg:[num|null], vol:[num|null],
 *     amount:[num|null], bar_up:[bool], meta:{points, window, label} }
 * ★ 注意：分时 payload **没有 `periods` 字段** —— 它**不能**走 `buildOption()`
 *   （那会 `payload.periods[period]` 取到 undefined → TypeError，X2 真缺陷就是这么来的）。
 *
 * 面板形态与 `render_intraday` 现有形态一致：2 个 grid（① 价格+均价线 / ② 分时量）。
 * ========================================================================= */
export function buildIntradayOption(payload) {
  const d = payload || {};
  const times = d.times || [];
  const price = d.price || [];
  const avg = d.avg || [];
  const vol = d.vol || [];
  const barUp = d.bar_up || [];
  return {
    animation: false,
    backgroundColor: "#fff",
    title: [
      {text:"① 分时价格 + 均价线", left:60, top:6,
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}},
      {text:"② 分时量", left:60, top:"66%",
       textStyle:{fontSize:11, color:"#86909c", fontWeight:"normal"}}
    ],
    grid: [
      {left:60, right:24, top:"9%", height:"52%"},
      {left:60, right:24, top:"70%", height:"22%"}
    ],
    tooltip: {trigger:"axis", axisPointer:{type:"cross"}},
    axisPointer: {link:[{xAxisIndex:"all"}]},
    xAxis: [
      {type:"category", gridIndex:0, data:times, boundaryGap:false,
       axisLabel:{show:false}, axisTick:{show:false}},
      {type:"category", gridIndex:1, data:times, boundaryGap:false,
       axisLabel:{color:"#86909c"}}
    ],
    yAxis: [
      {scale:true, gridIndex:0, splitNumber:4, axisLabel:{color:"#86909c"},
       splitLine:{lineStyle:{color:"#f2f3f5"}}},
      {gridIndex:1, splitNumber:2, axisLabel:{show:false}, splitLine:{show:false}}
    ],
    series: [
      {name:"价格", type:"line", data:price, xAxisIndex:0, yAxisIndex:0, showSymbol:false,
       lineStyle:{width:1.4, color:"#1f6feb"}},
      {name:"均价", type:"line", data:avg, xAxisIndex:0, yAxisIndex:0, showSymbol:false,
       lineStyle:{width:1.2, color:"#f2c200"}},
      {name:"分时量", type:"bar", data:vol.map(function (v, i) {
         return {value:v, itemStyle:{color: barUp[i] ? UP : DOWN}};
       }), xAxisIndex:1, yAxisIndex:1, barWidth:"60%"}
    ]
  };
}
