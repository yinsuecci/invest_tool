/* Local adapter for the pinned, unmodified TradingView Lightweight Charts library. */
(function () {
  'use strict';
  let active = null;
  const offset = 8 * 3600;
  function destroy() {
    if (active) { active.chart.remove(); active = null; }
  }
  function render(host, asset, candles, orders, unit, start, end, onSelect) {
    destroy();
    const LC = window.LightweightCharts;
    if (!LC || LC.version() !== '5.2.1') throw new Error('TradingView library version mismatch');
    host.innerHTML = '<div class="tv-price-title">价格（USDT / 单位） · EMA72 / EMA168</div><div class="tv-surface" role="img" aria-label="TradingView K线、均线、RSI及模拟买入标记"></div><p id="tv-hover" class="caption">移动十字光标查看OHLC及RSI；点击K线或下方逐笔记录查看买入依据。</p><p id="tv-status" class="caption"></p>';
    host.classList.add('tv-host');
    const surface = host.querySelector('.tv-surface');
    const chart = LC.createChart(surface, {
      autoSize: true, layout: { background: { type: 'solid', color: '#ffffff' }, textColor: '#57718f', attributionLogo: true, fontFamily: 'Microsoft YaHei, sans-serif', fontSize: 11 },
      grid: { vertLines: { color: '#edf3fa' }, horzLines: { color: '#edf3fa' } },
      rightPriceScale: { borderColor: '#d5e3f2', minimumWidth: 70 },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#d5e3f2', rightOffset: 2 },
      localization: { locale: 'zh-CN', timeFormatter: t => new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ') },
    });
    active = { chart };
    const price = chart.addSeries(LC.CandlestickSeries, { upColor: '#b65a4b', downColor: '#4d8069', borderVisible: false, wickUpColor: '#b65a4b', wickDownColor: '#4d8069', lastValueVisible: false, priceLineVisible: false });
    const stamp = t => Math.floor(t / 1000) + offset;
    price.setData(candles.map(b => ({ time: stamp(b[0]), open: b[1], high: b[2], low: b[3], close: b[4] })));
    const indicatorBuckets = new Map();
    for (const row of asset.indicators || []) {
      if (row[0] < start || row[0] >= end) continue;
      const bucket = Math.floor((row[0] + offset * 1000) / unit) * unit - offset * 1000;
      indicatorBuckets.set(bucket, row);
    }
    const plotLine = (field, color, title, pane = 0) => {
      const series = chart.addSeries(LC.LineSeries, { color, lineWidth: 1, title, lastValueVisible: false, priceLineVisible: false,
        ...(pane ? { autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) } : {}) }, pane);
      series.setData([...indicatorBuckets.entries()].filter(([, r]) => Number.isFinite(r[field])).map(([t, r]) => ({ time: stamp(t), value: r[field] })));
      return series;
    };
    plotLine(1, '#2f91bd', 'EMA72');
    plotLine(2, '#526b9b', 'EMA168');
    const currentPolicy = host.ownerDocument.getElementById('policy')?.value || '';
    if(currentPolicy.startsWith('inverse_')){
      const anchor=orders.find(p=>Number.isFinite(p.o[24])&&p.o[24]>0)?.o[24];
      if(anchor) price.createPriceLine({price:anchor,color:'#1764d8',lineWidth:1,lineStyle:LC.LineStyle.Dashed,axisLabelVisible:true,title:'固定P0'});
      host.querySelector('.tv-price-title').textContent+=currentPolicy==='inverse_1'||currentPolicy==='inverse_2'?' · 固定参考价P0；均线/RSI仅展示，不参与反比买入':' · 固定参考价P0；买入过滤按前一闭合小时指标，详见实验规则';
    }
    if(currentPolicy==='ladder_2pct'||currentPolicy.startsWith('opportunity_')){
      const caps=new Map();
      for(const p of orders){
        if(!Number.isFinite(p.o[17]))continue;
        caps.set(Math.floor((p.t+offset*1000)/unit)*unit-offset*1000,p.o[17]);
      }
      const ladder=chart.addSeries(LC.LineSeries,{color:'#1764d8',lineWidth:1,lineStyle:LC.LineStyle.Dashed,title:currentPolicy.startsWith('opportunity_')?'折价分批报价上限':'阶梯报价上限',lastValueVisible:false,priceLineVisible:false});
      ladder.setData([...caps].map(([t,v])=>({time:stamp(t),value:v})));
      host.querySelector('.tv-price-title').textContent+=currentPolicy.startsWith('opportunity_')?' · 折价分批上限（候选事件时刻；可能整天不买）':' · 阶梯报价上限（事件时刻；首次机会后须比上次成交再低2%）';
    }
    if (asset.vwap_reference && currentPolicy.startsWith('vwap_')) {
      const profile = asset.vwap_profiles[currentPolicy === 'vwap_fixed' ? 'fixed' : 'fitted'];
      const buckets = new Map();
      for (const [time, value] of asset.vwap_reference) {
        if (time < start || time >= end || !Number.isFinite(value)) continue;
        buckets.set(Math.floor((time + offset * 1000) / unit) * unit - offset * 1000, value);
      }
      const reference = chart.addSeries(LC.LineSeries,{color:'#0e7490',lineWidth:2,title:'滚动VWAP20d',lastValueVisible:false,priceLineVisible:false});
      reference.setData([...buckets].map(([time,value])=>({time:stamp(time),value})));
      profile.discounts.slice(1).forEach((discount,index)=>{
        const band=chart.addSeries(LC.LineSeries,{color:['#7295c2','#527cab','#2c5b90'][index],lineWidth:1,lineStyle:LC.LineStyle.Dashed,title:'VWAP -'+(discount*100).toFixed(1)+'%',lastValueVisible:false,priceLineVisible:false});
        band.setData([...buckets].map(([time,value])=>({time:stamp(time),value:value*(1-discount)})));
      });
      host.querySelector('.tv-price-title').textContent+=' · VWAP20d与分档线（上一闭合小时HLC3加权；20日非图表20根）';
    }
    if(asset.indicators?.some(r=>r.length>4)){
      plotLine(4, '#8b9db5', 'BB20中轨');plotLine(5, '#84acd3', 'BB20下轨');plotLine(6, '#84acd3', 'BB20上轨');
      host.querySelector('.tv-price-title').textContent+=' · 布林20小时 ±2σ（非图表周期重算）';
    }
    const policy = host.ownerDocument.getElementById('policy')?.value || '';
    const showDiscount = asset.discount_caps && (policy.startsWith('mix_') || ['core_discount', 'discount_only', 'signal_scan', 'signal_reserve', 'signal_ladder'].includes(policy));
    if (showDiscount) {
      const caps = new Map();
      for (const [time, ceiling] of asset.discount_caps) {
        if (time < start || time >= end || ceiling === null) continue;
        const bucket = Math.floor((time + offset * 1000) / unit) * unit - offset * 1000;
        caps.set(bucket, ceiling);
      }
      const limit = chart.addSeries(LC.LineSeries, { color: '#1764d8', lineWidth: 1, lineStyle: LC.LineStyle.Dashed,
        title: 'EMA折扣参考', lastValueVisible: false, priceLineVisible: false });
      limit.setData([...caps].map(([time, value]) => ({ time: stamp(time), value })));
      host.querySelector('.tv-price-title').textContent += ' · 虚线：EMA折扣参考线（上一闭合小时）；单笔上限见明细';
    }
    const rsi = plotLine(3, '#1764d8', 'RSI14 (0–100)', 1);
    rsi.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 } });
    for (const level of [30, 70]) rsi.createPriceLine({ price: level, color: '#a5b6cc', lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: String(level) });
    if (chart.panes()[1]) chart.panes()[1].setHeight(140);
    const groups = new Map(), hitMap = new Map();
    let currentFills = 0, baseFills = 0;
    for (const pair of orders) {
      const bucket = Math.floor((pair.t + offset * 1000) / unit) * unit - offset * 1000;
      const time = stamp(bucket);
      for (const [record, base] of [[pair.o, false], [pair.b, true]]) {
        if (record[7] || !record[4]) continue;
        if (base) baseFills++; else currentFills++;
        const key = `${time}/${base}`;
        const group = groups.get(key) || { time, base, quantity: 0, notional: 0, count: 0, index: pair.i };
        group.quantity += record[4]; group.notional += record[2]; group.count++;
        groups.set(key, group);
        if (!hitMap.has(time) || !base) hitMap.set(time, pair.i);
      }
    }
    const markers = [...groups.values()].sort((a, b) => a.time - b.time || Number(a.base) - Number(b.base)).map(g => ({
      time: g.time, position: g.base ? 'atPriceTop' : 'atPriceBottom', price: g.notional / g.quantity,
      color: g.base ? '#667f9b' : '#1764d8', shape: g.base ? 'circle' : 'arrowUp', size: .8,
      id: `fill-${g.index}`, text: unit >= 86400000 ? `${g.base ? 'B' : 'S'}×${g.count}` : '',
    }));
    LC.createSeriesMarkers(price, markers);
    chart.timeScale().fitContent();
    const text = (value, digits = 2) => Number(value).toFixed(digits);
    chart.subscribeCrosshairMove(param => {
      const p = param.seriesData.get(price), r = param.seriesData.get(rsi);
      if (!param.time || !p) return;
      host.querySelector('#tv-hover').textContent = `${new Date(param.time * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC+8 · 开 ${p.open} 高 ${p.high} 低 ${p.low} 收 ${p.close} USDT · RSI14 ${r ? text(r.value) : '无'}（闭合小时聚合显示）`;
    });
    chart.subscribeClick(param => {
      if (typeof param.hoveredObjectId === 'string' && param.hoveredObjectId.startsWith('fill-')) onSelect(Number(param.hoveredObjectId.slice(5)));
      else if (hitMap.has(param.time)) onSelect(hitMap.get(param.time));
    });
    host.dataset.libraryVersion = LC.version();
    host.dataset.markerCount = String(markers.length);
    host.querySelector('#tv-status').textContent = `TradingView Lightweight Charts ${LC.version()} · 策略实际买入 ${currentFills} 笔 / 基准 ${baseFills} 笔 · ${markers.length} 个图上标记 · 时间 UTC+8；较大周期内同类买单按实际数量加权成交价合并，笔数显示为S/B×n。标记不代表真实订单。`;
    if (host.ownerDocument.getElementById('daily')) {
      const details = document.createElement('div');
      details.className = 'table-wrap scroll';
      details.tabIndex = 0;
      details.setAttribute('role', 'region');
      details.setAttribute('aria-label', '模拟买单记录表格，可左右滚动');
      details.style.marginTop = '12px';
      const timeLabel = t => new Date(t + offset * 1000).toISOString().slice(0, 16).replace('T', ' ');
      details.innerHTML = '<table><thead><tr><th>本标的买入时间 UTC+8</th><th>候选本金 / 对照本金 U</th><th>候选数量 / 对照数量</th></tr></thead><tbody>' + orders.map(p => `<tr role="button" tabindex="0" data-fill-detail="${p.i}" aria-label="查看${timeLabel(p.t)}本标的买单"><td>${timeLabel(p.t)}</td><td>${text(p.o[2],4)} / ${text(p.b[2],4)}</td><td>${text(p.o[4],8)} / ${text(p.b[4],8)}</td></tr>`).join('') + '</tbody></table>';
      details.querySelectorAll('[data-fill-detail]').forEach(row => {
        row.onclick = () => onSelect(Number(row.dataset.fillDetail));
        row.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(Number(row.dataset.fillDetail)); } };
      });
      host.append(details);
    }
  }
  window.DcaTradingView = { render, destroy };
})();
