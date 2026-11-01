"""Causal technical-discount entries; volume signals are not investor identities."""

from collections import deque
from math import floor

from .market import HOUR
from .seven_smart import evaluate

CANDIDATES = {
    'inverse_2':(2,None,'旧rho=2（仅参照，可能追高）'),
    'opportunity_value':(2,None,'折价门控 + 等额分批'),
    'opportunity_tiers':(2,None,'折价门控 + ATR深度分档'),
    'opportunity_flow_boost':(2,None,'折价分档 + 量价吸筹代理加权'),
    'opportunity_flow_only':(2,None,'折价分档 + 必须量价确认'),
}

STUDY = {
    'version':'discount-accumulation-1.0',
    'rules':'技术参考价R=min(20d VWAP, EMA168)，均只取上一闭合小时；首次买入上限R−0.25×ATR14。前一闭合价和当前含5bp滑点报价均不超过上限才可入场。高于上限可全天零买入，无基础单。',
    'batching':'同轮后续成交价还须≤上次成交价−0.5×当前ATR14且间隔≥24小时；只有成功成交推进批次。前一闭合价>R+0.5×ATR才重置该标的折价轮次，不因普通反弹重置。每日合计四个轮转窗口，不因跳过补单。',
    'sizing':'等额版7.5U/笔；分档版7.5×(1+floor((R−max(闭合价,可执行报价))/ATR))U，折价更深加大，无金额上限；量价加权版仅在代理成立时再乘1.5。全部按需追加、无预算截断。',
    'flow':'24根连续闭合小时CMF=sum(((2C−H−L)/(H−L))×V)/sum(V)，零振幅项记0；CMF≥0.05、24小时OBV净变化>0、最新小时量≥此前24小时平均量，三项同时成立。只作本交易对买压代理，不识别真实机构。',
    'selection':'四个机会单候选仅在开发段排名；旧rho2只作参照，不可获选，避免默认策略违反高估可不买要求。沿用每标的至少5笔等原覆盖门槛；无人合格时明确研究回退，不放宽门槛。',
    'sources':['https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/accumulation-distribution'],
    'limitations':[
        '这里的低估/高估仅是相对VWAP/EMA的技术折价/溢价，不是企业、黄金或代币基本面公允价值；低于均线不保证便宜。',
        '量价吸筹仅为币安该现货交易对的CMF/OBV/放量代理，没有机构身份、全市场资金流或真实买卖方向数据。SPYB/QQQB成交量不代表美股ETF全市场机构行为。',
        '候选包含只折价等额、折价分档、量价加权、必须量价确认四个增量模块。没有信号可完全不买，最大4个全组合候选窗口/日，不保证每天至少成交1笔。',
        '默认只从机会单中按开发段选择，旧rho2保留参照但不参与获选。若覆盖不足则是未达标研究回退，不自动恢复基础单，不宣称最优。',
        '同一折价轮次要求后续更低报价且24小时冷却；上涨途中可能长期不买，存在错过上涨与持仓不足风险；没有止损或出售规则。',
    ],
}


def volume_features(bars):
    window = deque(maxlen=24)
    output,previous = {},None
    for bar in bars:
        if previous and bar.time!=previous.time+HOUR:
            window.clear()
            previous=None
        multiplier = (2*bar.close-bar.high-bar.low)/(bar.high-bar.low) if bar.high>bar.low else 0.
        signed = bar.volume*(1 if previous and bar.close>previous.close else -1 if previous and bar.close<previous.close else 0)
        relative = bar.volume/(sum(x[1] for x in window)/24) if len(window)==24 and sum(x[1] for x in window)>0 else None
        window.append((multiplier*bar.volume,bar.volume,signed))
        volume = sum(x[1] for x in window)
        cmf = sum(x[0] for x in window)/volume if len(window)==24 and volume>0 else None
        obv = sum(x[2] for x in window) if len(window)==24 else None
        output[bar.time] = {'cmf24':cmf,'obv24':obv,'relative_volume':relative,
            'accumulation_proxy':bool(cmf is not None and cmf>=.05 and obv>0 and relative is not None and relative>=1)}
        previous=bar
    return output


def decide(bar, feature, quote, flow, last, time, policy):
    refs = (feature.vwap20,feature.ema168,feature.atr14)
    valid = all(v is not None and v>0 for v in refs)
    reference = min(refs[:2]) if valid else None
    atr = feature.atr14 if valid else None
    cap = reference-.25*atr if valid else None
    reset = bool(valid and bar.close>reference+.5*atr)
    if reset:
        last=None
    depth = max(0.,(reference-max(bar.close,quote))/atr) if valid else 0.
    stage = floor(depth)
    proxy = flow['accumulation_proxy']
    gross = 7.5 if policy=='opportunity_value' else 7.5*(1+stage)
    if policy=='opportunity_flow_boost' and proxy:
        gross *= 1.5
    reason = None
    if not valid:
        reason='indicator_warmup'
    elif max(bar.close,quote)>cap:
        reason='above_discount_entry_cap'
    elif policy=='opportunity_flow_only' and not proxy:
        reason='no_accumulation_proxy'
    elif last and (time-last['time']<24*HOUR or quote>last['price']-.5*atr):
        reason='wait_next_lower_batch'
    if last and valid:
        cap=min(cap,last['price']-.5*atr)
    return {'hit':reason is None,'gross':gross,'cap':cap,'reference':reference,
        'atr14':atr,'depth_atr':depth,'tier':stage,'batch':0 if last is None else last['level']+1,
        'reset_episode':reset,'reason':reason,**flow}


def factory():
    cached={}
    def build(data,market,policy):
        if not cached:
            cached.update({k:volume_features(bars) for k,bars in data.items()})
        def signal(key,time,quote,last):
            bar,feature = market[key][time-HOUR]
            return decide(bar,feature,quote,cached[key][time-HOUR],last,time,policy)
        return signal
    return build


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--data',default='data/seven-assets-2026-11-01')
    parser.add_argument('--output',default='reports/accumulation-002')
    args=parser.parse_args()
    evaluate(args.data,args.output,candidates=CANDIDATES,opportunity_builder=factory(),study=STUDY)
