/* Dashboard presentation only: never changes strategy results or execution rules. */
(function () {
  'use strict';
  window.DcaDashboard = {
    renderSmart(p,state,report){
      const node=id=>document.getElementById(id),fmt=(x,n=3)=>x===null||x===undefined?'不可比较':Number(x).toLocaleString('zh-CN',{minimumFractionDigits:n,maximumFractionDigits:n});
      node('strategy-survey').hidden=true;
      node('vwap-study').hidden=true;
      node('smart-study').hidden=false;
      const records=report.results[state.period],selected=records[state.policy],details=report.smart[state.period][state.policy];
      node('smart-verdict').textContent='预先固定三种模板：rho=1、rho=2、2%价格阶梯。默认rho=1，不按全期结果选赢家。四个固定窗口、五标的轮转，最多4笔/日；最终投入逐标的配平，未启用实盘。';
      if(report.smart_selection){
        node('smart-choice-label').textContent='候选 / 开发段研究选择';
        node('smart-verdict').textContent='研究首选：'+report.policies[report.selection.selected_policy]+'。仅按开发段覆盖资格、最弱标的成本改善选择，不按全期重新排名。所有策略与回测结果保持原样，非实盘启用、非长期最优证明。';
        node('smart-choice-details').textContent='选择区间 '+report.selection.start+' 至 '+report.selection.end+'：'+report.selection.ranking.map(r=>report.policies[r.policy]+'，资格'+(r.eligible?'通过':'未过')+'，最弱成本改善 '+fmt(r.minimum_cost_gain_pct)+'%，评分 '+fmt(r.score)).join('；')+'。中段、末段与全期均为已看过的历史，不是新的独立证据。';
      }
      node('smart-rules').textContent=state.policy==='ladder_2pct'?'阶梯：首次仅等待原VWAP机会及报价上限；起始含费目标7.5U。此后闭合价和可执行报价都须≤上次实际成交价×98%，目标7.5×1.5^层数U。仅成交推进层级，不重置、不强制基础单。':'反比：每段开始前固定各标的参考价P0；含费额度=7.5U×(P0/当前可执行报价)^rho。固定UTC+8的00/06/12/18四个窗口，五标的轮转；不使用RSI、VWAP或跨标的仓位过滤。当前报价可观察，不能偷看该小时收盘或最低价。';
        if(report.indicator_ablation){
          node('smart-study').querySelector('h2').textContent='七标的：反比额度与指标过滤消融';
          const filter=report.indicator_ablation.confirmations[state.policy];
          node('smart-rules').textContent='含费额度=7.5U×(分段开始前固定参考价P0/当前可执行报价)^rho。七标的轮转，每日合计4个候选窗口；'+(filter?'买入条件：'+report.indicator_ablation.rules[filter]+'。条件不符跳过，不补基础单。':'不加指标过滤，作为额度函数对照。')+' 无资金上限；每个标的开发段至少5次成交才通过覆盖门槛。';
          node('smart-verdict').textContent+=' 本轮六个固定候选：原rho1/2与RSI、布林下轨、EMA168、探底确认，逐一对比。';
        }
        if(report.opportunity_study){
          node('smart-study').querySelector('h2').textContent='技术折价分批与量价吸筹代理';
          node('smart-verdict').textContent=report.selection.status+'；默认查看 '+report.policies[report.selection.selected_policy]+'。旧rho2只作参照，不参与选择。各候选按自身最终预算分别对比普通定投，不能直接比较不同候选的净值差。';
          node('smart-rules').textContent=report.opportunity_study.rules+' '+report.opportunity_study.batching+' '+report.opportunity_study.sizing+' '+report.opportunity_study.flow;
        }
        if(report.opportunity_study?.sol_targeted){
          const fit=report.opportunity_study.sol_targeted;
          node('smart-study').querySelector('h2').textContent='SOL专项优化：其他六标的冻结';
          node('smart-verdict').textContent=report.selection.status+'。'+fit.ranking.length+'组有限候选仅用开发段排名；SOL逐项对比相同最终预算普通定投，其他六标的的实际成交保持不变。首轮结果已查看后扩展目录，非独立验证。';
          node('smart-choice-details').textContent='SOL选择区间 '+report.selection.start+' 至 '+report.selection.end+'，合格 '+fit.eligible_count+'/'+fit.ranking.length+'。锁定参数 '+JSON.stringify(fit.selected.plan)+'。开发段最优SOL成本改善 '+fmt(fit.selected.cost_gain_pct)+'%，成交 '+fit.selected.fills+'笔。后段、全期不参与排名，历史已被查看，非独立证据。';
          let box=node('sol-trial-study');
          if(!box){box=document.createElement('section');box.id='sol-trial-study';node('smart-study').append(box);}
          box.innerHTML='<h3>SOL开发段全部'+fit.ranking.length+'组试验</h3><p class="caption">reference：min为VWAP20d与EMA168较低者，ema只用EMA168，ema72只用EMA72，blend为VWAP20d与EMA168平均；percent为每折价2%加一档。entry_atr=-0.25允许参考上方0.25ATR缓冲。5笔及数量覆盖不达标不能宣称最佳。</p><div class="table-wrap"><table><thead><tr><th>参数</th><th>SOL成本便宜 %</th><th>成交笔数</th><th>数量/普通 %</th><th>资格</th></tr></thead><tbody>'+fit.ranking.map(r=>'<tr><td>'+JSON.stringify(r.plan)+'</td><td>'+fmt(r.cost_gain_pct)+'</td><td>'+r.fills+'</td><td>'+fmt(r.quantity_ratio*100,1)+'</td><td>'+(r.eligible?'通过':'未过')+'</td></tr>').join('')+'</tbody></table></div>';
        }
        if(report.opportunity_study?.independent_assets){
          const study=report.opportunity_study.independent_assets,keys=Object.keys(study.fitting),next=records.opportunity_assets_fitted,old=records.opportunity_assets_previous;
          node('smart-study').querySelector('h2').textContent='七标的分别独立优化';
          node('smart-verdict').textContent=report.selection.status+'。每标的96组固定参数+当前方案，合计'+study.trial_count+'次开发段试验；合并回放仍是每天最多4个全组合候选事件，无基础单。';
          node('smart-choice-details').textContent='选择区间 '+study.start+' 至 '+study.end+'。每个标的只按自身开发段成本和覆盖资格独立锁参，同分保留当前方案；中段、末段与全期只作诊断，不参与排名。679次尝试与历史反复查看存在过拟合风险，非独立样本外验证。';
          let box=node('individual-asset-study');
          const chosen=node('individual-trial-asset')?.value||state.asset;
          if(!box){box=document.createElement('section');box.id='individual-asset-study';node('smart-study').append(box);}
          const incumbentSol=report.results.development.opportunity_assets_previous.current.orders.find(o=>o.asset==='SOL')?.opportunity?.sol_plan;
          const planText=(p,k)=>p.mode==='incumbent'?(k==='SOL'?'保留SOL锁参 '+JSON.stringify(incumbentSol):'保留：VWAP/EMA168较低者−0.25ATR；ATR深度分档；量价成立×1.5；后批0.5ATR'):({min:'VWAP/EMA168较低者',ema:'EMA168',ema72:'EMA72',blend:'VWAP与EMA168平均'}[p.reference])+'；入场偏移 '+p.entry_atr+'ATR；'+(p.sizing==='equal'?'7.5U等额':'每折价2%加档')+'；'+(p.rsi_max===null?'无RSI门控':'RSI≤'+p.rsi_max)+'；后批再跌 '+p.next_atr+'ATR';
          box.innerHTML='<h3>每个标的的独立方案与后段表现</h3><p class="caption">正数更便宜、负数更贵；当前区间旧/新均各自按最终实际投入配平普通定投，跨方案投入不同。末段负数或无成交必须保留展示。参数入场偏移-0.25表示参考上方0.25ATR缓冲，不是基本面低估。</p><div class="table-wrap" role="region" aria-label="逐标的独立优化对照" tabindex="0"><table><thead><tr><th>标的</th><th>锁定方案</th><th>开发成交/资格</th><th>开发成本便宜 %</th><th>当前区间旧→新成本便宜 %</th><th>中段新 %</th><th>末段新 %</th></tr></thead><tbody>'+keys.map(k=>{const f=study.fitting[k],q=f.selected;return '<tr><td>'+k+'</td><td>'+planText(q.plan,k)+'</td><td>'+q.fills+' / '+(q.eligible?'通过':'未过；研究回退')+'</td><td>'+fmt(q.cost_gain_pct)+'</td><td>'+fmt(old.vs_four.asset_cost_gain_pct[k])+' → '+fmt(next.vs_four.asset_cost_gain_pct[k])+'</td><td>'+fmt(report.results.validation.opportunity_assets_fitted.vs_four.asset_cost_gain_pct[k])+'</td><td>'+fmt(report.results.holdout.opportunity_assets_fitted.vs_four.asset_cost_gain_pct[k])+'</td></tr>';}).join('')+'</tbody></table></div><label for="individual-trial-asset">查看标的参数试验</label><select id="individual-trial-asset">'+keys.map(k=>'<option value="'+k+'"'+(k===chosen?' selected':'')+'>'+k+'</option>').join('')+'</select><p id="individual-trial-summary" class="caption"></p><div class="table-wrap" role="region" aria-label="所选标的开发段完整试验" tabindex="0"><table><thead><tr><th>参数</th><th>开发成本便宜 %</th><th>成交</th><th>数量/普通 %</th><th>资格</th></tr></thead><tbody id="individual-trial-rows"></tbody></table></div>';
          const renderTrials=()=>{const key=node('individual-trial-asset').value,f=study.fitting[key];node('individual-trial-summary').textContent=key+'：'+f.eligible_count+'/'+study.catalogue_size+'组通过最低覆盖门槛；以下仅开发段排名。';node('individual-trial-rows').innerHTML=f.ranking.map(r=>'<tr><td>'+planText(r.plan,key)+(r.index===f.selected.index?'（选中）':'')+'</td><td>'+fmt(r.cost_gain_pct)+'</td><td>'+r.fills+'</td><td>'+fmt(r.quantity_ratio*100,1)+'</td><td>'+(r.eligible?'通过':'未过')+'</td></tr>').join('');};
          node('individual-trial-asset').onchange=renderTrials;renderTrials();
        }
      node('smart-result-rows').innerHTML=Object.entries(records).map(([k,r])=>{
        const c=r.current,q=r.target_quality,match=report.budget_matching_by_policy[state.period][k];
        return `<tr><td>${report.policies[k]}${k===state.policy?'（当前）':''}</td><td>${fmt(c.contributed,2)}</td><td>${fmt(match.ordinary_daily,6)}</td><td>${c.orders.filter(o=>o.quantity>0).length} / ${c.orders.length}</td><td>${c.orders.filter(o=>o.skip_reason==='exchange_minimum_or_quantity').length}</td><td>${fmt(q.minimum_cost_gain_pct)}</td><td>${q.eligible?'通过':'未通过；不能宣称最佳'}</td></tr>`;
      }).join('');
      node('smart-cost-rows').innerHTML=Object.entries(selected.current.assets).map(([k,a])=>{
        const events=selected.current.orders.filter(o=>o.asset===k),ideal=details.ideal[k];
          return `<tr><td>${k}</td><td>${state.policy.startsWith('inverse')?fmt(selected.current.anchors[k],6):state.policy.startsWith('opportunity_')?'动态参考与上限，见逐笔':'使用上次成交价'}</td><td>${fmt(a.unit_cost,6)}</td><td>${fmt(selected.vs_four.asset_cost_gain_pct[k])}</td><td>${fmt(selected.vs_matched.asset_cost_gain_pct[k])}</td><td>${ideal?fmt(ideal.ideal_cost_gain_pct):'不适用'}</td><td>${events.filter(o=>o.quantity>0).length} / ${events.length}</td><td>${fmt(selected.four.assets[k].quantity?a.quantity/selected.four.assets[k].quantity*100:null,1)}</td></tr>`;
      }).join('');
      node('smart-caveat').textContent='理想降幅仅使用同一报价序列和可任意分割的成交，不包含最小金额/数量过滤造成的时点选择。实际高降幅可能伴随大量高价小额订单被拒绝，不能说全由额度公式贡献。阶梯可能只成交极少笔。日历普通对照的低于最低金额预算先积累再买；同钟对照在相同事件时刻分配资金，零钱按资产隔离积累。两个对照都包含现金，都是事后最终预算配平。';
        if(report.indicator_ablation) node('smart-caveat').textContent+=' 本轮没有阶梯候选。过滤版不显示无过滤反比公式的理想增益；覆盖资格按当前区间重新诊断，默认选择只用开发段。';
        if(report.opportunity_study) node('smart-caveat').textContent='技术折价不是基本面低估。CMF/OBV/放量只描述该币安交易对，不识别机构，ETF代币成交量也不是美股全市场量。门控、分档、量价加权、必须量价确认逐项比较；覆盖不足的候选不能称最佳。各候选投入不同，仅与其自身等最终预算对照比较，均非独立样本外证据。';
      node('smart-lock').textContent='冻结协议 SHA-256：'+report.protocol.sha256+'\n预设选择锁 SHA-256：'+report.selection.lock_sha256;
    },
    renderVwap(p, state, report) {
      const node = id => document.getElementById(id), study = report.vwap;
      const fmt = (x, digits = 2) => x === null || x === undefined ? '不可比较' : Number(x).toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits});
      node('vwap-study').hidden = false;
      const chosen = report.selection.selected_policy;
      if(study.joint_search){
        node('vwap-amount-description').textContent = '阈值是低于20d VWAP的百分比，达到更深档采用更深档倍数。含费目标=当日组合新增×目标权重×倍数。中心30U时：'+Object.entries(study.fitted_plan).map(([k,p])=>k+' '+p.multipliers.map(m=>fmt(30*report.config.weights[k]*m,0)).join('/')+'U').join('；')+'。目标不是承诺支出，大倍数仍受组合单日最多2倍新增、已有现金、权重与最小金额限制；当日新增30U时全组合最多花60U。';
        node('vwap-fit-description').textContent = study.search_note+' 不是全局最优；全部两轮试验及其当时组合参数保存在 joint-trials.json。';
        node('vwap-trial-summary').textContent = `查看最后一轮档位试验（共${Object.values(study.fitting).reduce((s,f)=>s+f.ranking.length,0)}行；两轮完整记录另存）`;
        node('vwap-score-description').textContent = '先检查动用≥60%、成交≥20笔、每标的数量≥普通最多4次的50%、成本完整。通过后最大化最弱标的成本降幅；未通过时每单位覆盖缺口扣1000分，同分比较平均降幅再比较净值。目标：五个标的全部至少便宜1%。只用开发段选择，未达标不放宽约束，不保证全局最优或未来表现。';
      }
      if(study.targeted){
        node('vwap-score-description').textContent += ' 本轮只调GOOG/XAU/SOL，BTC/ETH参数锁定；合格候选还须BTC/ETH在开发段分别便宜≥1%，不保证后段仍保住优势。';
      }
      node('vwap-verdict').textContent = `开发段 ${study.fit_period.start} 至 ${study.fit_period.end}：${study.joint_search ? `共享账本两轮搜索，${study.catalogue_size}组档位、${study.trial_count}次试验；${study.search_note}` : '逐标的6个固定方案'}；组合确认方式首选 ${report.policies[chosen]}。${report.selection.status}。通过覆盖门槛 ${report.selection.eligible_count}/5，不代表未来最优。`;
      if(report.execution_mode==='full_signal'){
        node('vwap-verdict').textContent = '完整信号额度模式：取消日预算与剩余额度截断；本轮不搜索、不重选信号或档位。'+report.selection.status+'。无实盘交易。';
        node('vwap-rule-description').textContent = '20d=480根闭合小时线的HLC3成交量加权滚动均价。原有VWAP档位、EMA/RSI过滤、权重及4小时成交间隔保持不变。只改变VWAP候选额度执行，旧指标参考版与普通定投不变。';
        node('vwap-amount-description').textContent = '每笔完整含费信号额度=当日组合新增×标的目标权重×档位倍数。不再取剩余日预算与信号额度的较小值，也不再限制为当日新增2倍。现金不足时整笔跳过，不减额、不借款；仅保留交易所数量步长取整和最小金额规则。';
        node('vwap-fit-description').textContent = '以下为沿用的冻结档位；评分重新按当前执行模式计算。本轮没有档位搜索，不能称为重新找到最优。';
        node('vwap-trial-summary').textContent = '查看冻结档位的开发段诊断（非搜索试验）';
        node('vwap-score-description').textContent = '开发段排名只作诊断，不据此重选默认信号；沿用上轮默认。完整成交额度、现金守恒、相同资金流是本轮验证重点。仍检查动用≥60%、成交≥20笔、每标的数量≥普通最多4次50%及全部成本便宜≥1%的目标。';
      }
      node('vwap-profile-rows').innerHTML = Object.entries(study.fitting).map(([k,fit]) => {
        const profile = study.fitted_plan[k], top = fit.ranking[0];
        const filters = [profile.rsi_max!==undefined?'RSI≤'+profile.rsi_max:null,profile.ema_discount!==undefined?'价格≤EMA168×'+fmt(1-profile.ema_discount,3):null,profile.bb_cap?'价格≤布林下轨':null].filter(Boolean).join('；');
        return `<tr><td>${k}${fit.frozen?'（参数锁定）':''}</td><td>${profile.discounts.map(x=>fmt(x*100,2)+'%').join(' / ')}</td><td>${profile.multipliers.map(x=>fmt(x,0)+'倍').join(' / ')}</td><td>${fit.selected_profile}${filters?' · '+filters:''}</td><td>${fit.eligible_count ? '通过覆盖门槛' : '无合格方案，研究回退'}</td><td>${fmt(top.score,3)}</td></tr>`;
      }).join('');
      node('vwap-fit-trials').innerHTML = Object.entries(study.fitting).flatMap(([k,fit]) => fit.ranking.map(r=>`<tr><td>${k}</td><td>${r.profile}${r.profile===fit.selected_profile?'（选中）':''}</td><td>${r.eligible?'通过':'未过：'+Object.entries(r.checks).filter(x=>!x[1]).map(x=>({deployment:'动用率',fills:'笔数',quantity:'数量',cost:'成本',protected:'BTC/ETH保护'}[x[0]])).join('/')}</td><td>${fmt(r.score,3)}</td><td>${fmt(r.execution_rate*100,1)}</td><td>${r.fills}</td><td>${fmt(r.cost_gain_pct[k],3)}</td></tr>`)).join('');
      const all = report.results[state.period];
      if(study.joint_search){
        const q=all[state.policy].target_quality;
        const previous=study.seed_comparison && study.seed_comparison[state.period];
        node('vwap-target-progress').textContent = `当前评估区间：五标的全部便宜1%目标${q.target_met?'达到（仅历史回测）':'未达到'}；最弱成本降幅 ${fmt(q.minimum_cost_gain_pct,3)}%，覆盖门槛${q.eligible?'通过':'未通过'}。`+(previous && state.policy==='vwap_fitted' ? ` 各标的成本降幅，${study.targeted?'上一轮锁定方案':'原六组拟合'} → 本轮：`+Object.keys(q.cost_gain_pct).map(k=>`${k} ${fmt(previous.asset_cost_gain_pct[k],3)}% → ${fmt(q.cost_gain_pct[k],3)}%`).join('；')+'。正数更便宜，负数更贵。' : '');
        const ablation=study.filter_ablation && study.filter_ablation[state.period];
        if(ablation && state.policy==='vwap_fitted') node('vwap-target-progress').textContent += ' 同档位移除过滤的消融对照：新增过滤净值变化 '+fmt(ablation.comparison.equity_delta)+'U；逐标的成本改善 '+Object.entries(ablation.comparison.asset_cost_gain_pct).map(([k,v])=>k+' '+fmt(v,3)+'%').join('；')+'。';
        const execution=report.execution_ablation && report.execution_ablation[state.period];
        if(execution && state.policy==='vwap_fitted') node('vwap-target-progress').textContent += ' 取消截断前 → 后，各标的相对普通定投的成本降幅：'+Object.entries(q.cost_gain_pct).map(([k,v])=>k+' '+fmt(execution.prior_cost_gain_pct[k],3)+'% → '+fmt(v,3)+'%').join('；')+'。现金不足整单跳过 '+execution.prior_cash_rejections+' → '+execution.cash_rejections+' 次；成交 '+execution.prior_fills+' → '+execution.fills+' 笔；较截断版期末资产变化 '+fmt(execution.comparison.equity_delta)+'U。';
      }
      node('vwap-result-rows').innerHTML = Object.entries(all).map(([k,v])=>`<tr><td>${report.policies[k]}${k===state.policy?'（当前）':''}</td><td>${fmt(v.vs_previous.equity_delta)}</td><td>${fmt(v.vs_four.equity_delta)}</td><td>${fmt(v.current.execution_rate*100,1)}</td><td>${fmt(v.current.cash)}</td><td>${v.current.daily.reduce((s,d)=>s+d.fills,0)}</td><td>${fmt(v.vs_matched.equity_delta)}</td></tr>`).join('');
      const confirmationKeys = ['vwap_fitted','vwap_bottom','vwap_rsi','vwap_either'].filter(k=>all[k]);
      node('vwap-cost-rows').innerHTML = confirmationKeys.flatMap(policy=>Object.entries(all[policy].current.assets).map(([k,asset])=>{
        const reference = all.vwap_fitted.current.assets[k], gain = all[policy].vs_unconfirmed.asset_cost_gain_pct[k];
        return `<tr><td>${report.policies[policy]}</td><td>${k}</td><td>${fmt(asset.unit_cost,6)}</td><td>${fmt(gain,3)}</td><td>${fmt(reference.quantity ? asset.quantity/reference.quantity*100 : null,1)}</td><td>${fmt(all[policy].vs_four.asset_cost_gain_pct[k],3)}</td></tr>`;
      })).join('');
      node('vwap-later').textContent = ['validation','holdout'].map((period,i)=>{
        const rows=report.results[period];
        return `${i?'末段':'中段'}${rows.vwap_fitted.current.days}日，确认版相对同档位无确认的资产变化：${confirmationKeys.slice(1).map(k=>report.policies[k]+' '+fmt(rows[k].vs_unconfirmed.equity_delta)+'U').join('；')}`;
      }).join('。')+'。此处后段不参与拟合；各段从零持仓开始。';
      node('vwap-mode-rows').innerHTML = report.selection.ranking.map((r,i)=>`<tr><td>${i+1}. ${report.policies[r.policy]}</td><td>${r.eligible?'通过':'未过：'+Object.entries(r.checks).filter(x=>!x[1]).map(x=>({deployment:'动用率',fills:'笔数',all_assets:'成本',cost:'成本',quantity:'数量',protected:'BTC/ETH保护'}[x[0]])).join('/')}</td><td>${fmt(r.score,3)}</td></tr>`).join('');
      node('vwap-locks').textContent = '档位锁 SHA-256：'+study.fit_lock_sha256+'\n确认选择锁 SHA-256：'+report.selection.lock_sha256;
      if(report.execution_mode==='on_demand'){
        node('vwap-verdict').textContent = '按需外部追加资金：信号满足时按完整额度买入，投入不设预算上限。本轮沿用冻结档位，不重选；只用最终实际含费投入确定普通定投对标预算。';
        node('vwap-rule-description').textContent = '30U中心和21–39U区间只是信号计算基数，不是实际入金计划或投入上限。每笔不足现金在回测中记为外部追加，不计成利润；仓位过滤、价格条件和4小时成交间隔不变。';
        node('vwap-amount-description').textContent = '完整含费信号额度=当日信号基数×标的目标权重×档位倍数。按交易所步长取整，满足其他规则即补足所需现金成交，不因现金或日预算减额/跳过。没有真实转账或实盘交易。';
        node('vwap-fit-description').textContent = '沿用冻结档位；本轮只改变资金供应和普通定投对照口径，不搜索参数。';
        node('vwap-trial-summary').textContent = '查看冻结档位诊断（非新搜索）';
        node('vwap-score-description').textContent = '普通定投对照按最终每标的实际含费支出反算每日预算。不同入金时序、不同最小金额和未投入现金都要同时查看；不再声称同逐日资金流。';
        node('vwap-later').textContent = '本轮不新增确认方式、不重拟合；各历史分段单独从零持仓开始。对标金额事后确定，不能用于先验预测。';
        node('vwap-target-progress').textContent = '相同最终预算下五标的全部便宜1%：'+(all[state.policy].target_quality.target_met?'达到（仅历史结果）':'未达到')+'。资金进入时间不同，不能将追加资金算作盈利。';
      }
      if(state.policy.startsWith('vwap_')){
        node('stats').children[2].querySelector('span').textContent='计划1–4窗口 / 实际成交';
        node('stats').children[2].querySelector('b').textContent='0–4 笔';
      }
    },
    init({ report, getState, onViewChange }) {
      const views = ['overview', 'chart', 'research', 'ledger'];
      const buttons = [...document.querySelectorAll('[data-workspace]')];
      let view = views.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
      const format = (value, digits = 2) => Number(value).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
      const node = id => document.getElementById(id);
      function paint() {
        document.querySelectorAll('[data-view]').forEach(panel => { panel.hidden = panel.dataset.view !== view; });
        buttons.forEach(button => {
          const selected = button.dataset.workspace === view;
          button.setAttribute('aria-selected', String(selected));
          button.tabIndex = selected ? 0 : -1;
        });
      }
      function choose(next, record = true) {
        if (!views.includes(next)) return;
        view = next;
        paint();
        if (record && location.hash !== '#' + view) history.pushState(null, '', '#' + view);
        onViewChange();
      }
      buttons.forEach((button, index) => {
        button.onclick = () => choose(button.dataset.workspace);
        button.onkeydown = event => {
          let next;
          if (event.key === 'ArrowRight') next = (index + 1) % views.length;
          if (event.key === 'ArrowLeft') next = (index + views.length - 1) % views.length;
          if (event.key === 'Home') next = 0;
          if (event.key === 'End') next = views.length - 1;
          if (next === undefined) return;
          event.preventDefault();
          choose(views[next]);
          buttons[next].focus();
        };
      });
      window.addEventListener('popstate', () => choose(views.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview', false));
      document.querySelectorAll('.table-wrap').forEach((wrap, index) => {
        wrap.tabIndex = 0;
        wrap.setAttribute('role', 'region');
        const title = wrap.querySelector('th')?.textContent || '数据';
        wrap.setAttribute('aria-label', title + '表格，可左右滚动，第' + (index + 1) + '组');
      });
      paint();
      return {
        get view() { return view; },
        update(p) {
          const state = getState(), current = p.current, base = p[state.base];
          const delta = current.equity - base.equity;
          node('kpi-equity').textContent = format(current.equity) + ' U';
          node('kpi-contributed').textContent = '累计新增 ' + format(current.contributed) + ' U · 含全部现金';
          node('kpi-delta').textContent = (delta >= 0 ? '+' : '') + format(delta) + ' U';
          node('kpi-delta').dataset.direction = delta < 0 ? 'negative' : 'positive';
          node('kpi-baseline').textContent = state.base === 'fixed30' ? '资金流不同，不计策略优势' : state.base === 'matched' ? '较同动用预算对照（事后配平）' : state.base === 'once' ? '较同资金流普通每天1次' : '较同资金流普通最多4次';
          node('kpi-cash').textContent = format(current.cash) + ' U';
          node('kpi-execution').textContent = '资金动用 ' + format(current.execution_rate * 100, 1) + '% · ' + current.daily.filter(d => d.fills === 0).length + '天未买入';
          node('overview-context').textContent = report.policies[state.policy] + ' · ' + current.start + ' 至 ' + current.end + ' · ' + current.days + '个共同完整日';
          const baseName = report.family==='smart'&&state.base==='matched'?'同一轮转事件时刻、等最终预算对照':report.execution_mode==='on_demand' ? (state.base==='fixed30'?'旧固定30U参照（投入不同）':'等最终逐标的预算 · 每标的普通'+(state.base==='once'?'每天1次':'最多4次')) : state.base === 'four' ? '普通最多4次' : state.base === 'once' ? '普通每天1次' : state.base === 'matched' ? '事后同动用预算对照' : '固定30U参照（资金流不同）';
          const selection = report.selection;
          node('chart-plan').textContent = report.policies[state.policy] + (selection && state.policy === selection.selected_policy ? (selection.eligible_count ? ' · 开发段相对首选，未获独立验证' : ' · 开发段评分相对首选，但无方案通过覆盖门槛，仅研究回退') : ' · 手动查看候选，非最优声明');
          const target = report.results[state.period][state.policy].target_quality;
          if(target) node('chart-plan').textContent += ` · 当前区间全部便宜1%：${target.target_met?'达到（历史结果）':'未达到'}；覆盖门槛${target.eligible?'通过':'未通过'}`;
          if(report.execution_mode==='full_signal' && state.policy.startsWith('vwap_')) node('chart-plan').textContent = '沿用冻结信号与档位 · 完整信号额度、不作预算截断 · 现金不足整笔跳过 · 当前区间全部便宜1%：'+(target.target_met?'达到（历史结果）':'未达到');
          const dailyHeader = node('daily').closest('table').querySelector('thead th:nth-child(3)');
          if(report.execution_mode==='full_signal') dailyHeader.textContent = state.policy.startsWith('vwap_') ? '日初可用现金 U（非日限额）' : '动用预算 USDT';
          node('chart-cost-caption').textContent = `以下含费单位成本统计整个评估区间 ${current.start} 至 ${current.end}，比较${baseName}；下方K线日期只是展示窗口，不会重新拟合或重算此均价。数量和分配不同，均价改善不等于收益提升。`;
          node('chart-cost-rows').innerHTML = Object.entries(current.assets).map(([key, asset]) => {
            const reference = base.assets[key], comparable = reference.unit_cost > 0 && asset.unit_cost > 0;
            const gain = comparable ? (1 - asset.unit_cost / reference.unit_cost) * 100 : null;
            const difference = comparable ? reference.unit_cost - asset.unit_cost : null;
            const cost = value => value === null ? '无成交 / 不可比较' : format(value, 6);
            const description = gain === null ? '无成交 / 不可比较' : gain >= 0 ? '便宜 ' + format(gain, 3) + '%' : '更贵 ' + format(-gain, 3) + '%';
            return `<tr data-selected="${key===state.asset}"><td>${key}${key===state.asset?'（当前K线）':''}</td><td>${cost(reference.unit_cost)}</td><td>${cost(asset.unit_cost)}</td><td>${description}</td><td>${difference===null?'不可比较':(difference>=0?'+':'')+format(difference,6)}</td><td>${format(reference.quantity,8)} / ${format(asset.quantity,8)}</td></tr>`;
          }).join('');
          if(report.execution_mode==='on_demand'){
            const matching=report.budget_matching_by_policy?.[state.period]?.[state.policy]||report.budget_matching[state.period];
            node('final-budget-panel').hidden=false;
            node('final-budget-summary').textContent = `${current.days}日策略最终实际含费投入 ${format(matching.total_budget,6)}U；普通定投${Object.keys(report.assets).length}标的合计每日分配 ${format(matching.ordinary_daily,6)}U（总额÷天数）。以下各标的分别配平，不再用30U/日当作实际投入。`;
            node('final-budget-rows').innerHTML=Object.entries(matching.assets).map(([k,a])=>`<tr><td>${k}</td><td>${format(a.final_budget,6)}</td><td>${format(a.ordinary_daily,6)}</td><td>${format(a.four_actual_spend,6)}</td><td>${format(a.four_cash,6)}</td></tr>`).join('');
            node('stats').children[0].querySelector('span').textContent='信号额度中心（非投入上限）';
            node('stats').children[1].querySelector('span').textContent='普通定投等最终预算 / 日';
            node('stats').children[1].querySelector('b').textContent=format(matching.ordinary_daily)+' U';
            document.querySelector('header > p').textContent='信号触发时按需追加；普通定投按每个标的最终实际含费投入反算每日预算。';
            node('weights').textContent='信号目标权重：'+Object.entries(report.config.weights).map(([k,v])=>k+' '+format(v*100,1)+'%').join(' / ')+'。信号基数21–39U，实际入金不设每日预算上限；仍保留原有仓位过滤。';
            node('summary').textContent=`${current.start} 至 ${current.end} · ${current.days}日 · 策略按需实际追加 ${format(current.contributed)}U；普通定投等最终逐标的预算 ${format(matching.ordinary_daily)}U/日。资金进入时间不同，非同资金流；旧固定30U参照投入不同。`;
            node('funding-caption').textContent='蓝线为实际追加，灰线30U仅为信号尺度参考，不是实际每日预算。资金流调整净值剔除外部追加；对照入金时序不同。';
            node('kpi-baseline').textContent=baseName+'；现金计入净值';
            node('kpi-execution').textContent='按需入金至成交所需 · '+current.daily.filter(d=>d.fills===0).length+'天未买入；100%动用是机制结果，不代表效率优势';
            node('baseline').querySelector('[value="four"]').textContent='等最终逐标的预算 · 每标的最多4次';
            node('baseline').querySelector('[value="once"]').textContent='等最终逐标的预算 · 每标的每天1次';
            node('baseline').querySelector('[value="matched"]').textContent='等最终逐标的预算（同4次对照）';
            node('baseline').querySelector('[value="fixed30"]').textContent='旧固定30U参照（投入不同）';
            dailyHeader.textContent='当日实际追加 U（非限额）';
            node('chart-plan').textContent='完整信号额度、按需追加 · 普通定投按最终各标的投入配平 · 无实盘';
            node('chart-cost-caption').textContent=`${current.days}日含费单位成本，比较${baseName}。对标金额事后确定，资金时序不同；普通未投入零钱也计入净值。下方日期只控制K线窗口。`;
            if(report.family==='smart'){
              node('weights').textContent='预设4个固定窗口、'+Object.keys(report.assets).length+'标的轮转；初始含费尺度7.5U/事件（合计每日30U基准，不是上限）。不使用跨标的仓位门槛；每个候选单独反算最终普通预算。';
              node('baseline').querySelector('[value="matched"]').textContent='同一轮转事件时刻 · 等最终预算';
              node('chart-plan').textContent=report.policies[state.policy]+(report.smart_selection&&state.policy===report.selection.selected_policy?' · 开发段标准研究首选（非长期最优）':' · 固定模板，未拟合')+' · 按需追加 · 日历与同钟对照分开显示';
              node('funding-caption').textContent='蓝线为实际外部追加；灰线30U为四个固定窗口×初始7.5U的尺度，不是实际投入上限。追加不计入收益。';
              if(report.smart_selection) node('summary').textContent+=' 当前'+(state.policy===report.selection.selected_policy?'为开发段标准研究首选':'为手动查看候选')+'；本区间最低金额/数量未成交 '+current.orders.filter(o=>o.skip_reason==='exchange_minimum_or_quantity').length+' / '+current.orders.length+' 个事件。降幅含执行过滤影响，不是纯公式收益。';
              if(report.indicator_ablation){
                node('baseline').querySelector('[value="fixed30"]').disabled=true;
                node('baseline').querySelector('[value="fixed30"]').textContent='本轮不提供固定30U对照';
                node('summary').textContent=node('summary').textContent.replace('旧固定30U参照投入不同。','六个固定候选仅用开发段选优，每标的开发段至少5笔。');
              }
              if(report.opportunity_study){
                node('summary').textContent=`${current.start} 至 ${current.end} · ${current.days}日 · 实际追加 ${format(current.contributed)}U；普通等最终预算 ${format(matching.ordinary_daily)}U/日，入金时序不同。${report.selection.status}。`;
                node('weights').textContent='七标的合计每日最多4个轮转候选时刻；高于折价上限完全不买，无强制基础单，可全天零成交；30U仅为金额尺度。';
                node('chart-plan').textContent=report.policies[state.policy]+' · 技术折价非基本面估值 · 量价代理非机构身份 · '+report.selection.status;
                node('stats').children[2].querySelector('b').textContent='0–4 次';
              }
            }
          }
        },
      };
    },
  };
})();
