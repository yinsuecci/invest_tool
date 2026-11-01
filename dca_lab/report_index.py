"""Static index of every research report under reports/.

The index is generated from the saved report.json files, never from memory:
each row shows the frozen protocol time, the default policy, the full-period
portfolio result and the comparison against ordinary DCA.  It is a navigation
and audit aid, not a new experiment and not an investment recommendation.
"""

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path

CACHE_VERSION = 5  # bump whenever summarize() changes its row shape or labels

FAMILY_LABELS = {
    "vwap": "VWAP 折扣档位",
    "smart": "固定时钟反比/阶梯",
    "signal": "纯机会买入",
    "signal_v2": "纯机会 + 现金加码",
    "indicator": "指标确认组合",
    "opportunity": "机会单",
    "discount": "折扣门槛",
    "portfolio": "组合资金流",
    "ma_tiers": "均线偏离分档",
}

CSS = """
:root{--paper:#f5f8fc;--white:#fff;--ink:#172f50;--muted:#536b86;--accent:#175bd4;
--soft:#edf4ff;--line:#dce5f0;--danger:#a43838;--good:#1c7a46;
--numeric:"Bahnschrift","DIN Alternate",Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.7 "Microsoft YaHei UI","PingFang SC",sans-serif;
background-image:radial-gradient(ellipse at 90% 0,#dcecff80,transparent 40%);background-attachment:fixed}
main{max-width:1320px;margin:auto;padding:24px 32px 56px}
header{border-top:3px solid var(--accent);padding:18px 0 14px}
.eyebrow{font-size:13px;font-weight:600;color:var(--accent)}
h1{font:600 28px/1.35 "Microsoft YaHei UI","PingFang SC",sans-serif;margin:8px 0}
header p{color:var(--muted);margin:6px 0;max-width:78ch}
.stats{display:grid;grid-template-columns:repeat(4,1fr);background:var(--white);
border:1px solid var(--line);border-radius:12px;margin:22px 0 8px;overflow:hidden}
.stat{padding:16px 20px;border-right:1px solid var(--line);min-width:0}
.stat:last-child{border:0}
.stat span{display:block;font-size:12px;color:var(--muted)}
.stat b{display:block;font:500 22px/1.5 var(--numeric);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.note{font-size:13px;color:var(--muted);margin:10px 0 0}
.toolbar{display:flex;flex-wrap:wrap;gap:12px 18px;align-items:flex-end;margin:18px 0 0;
padding:14px 16px;background:var(--white);border:1px solid var(--line);border-radius:12px}
.field{display:flex;flex-direction:column;gap:5px;min-width:0}
.field label{font-size:12px;font-weight:600;color:var(--muted)}
.field input,.field select{min-height:44px;padding:8px 12px;border:1px solid #7b8da6;border-radius:7px;
background:var(--white);color:var(--ink);font:inherit;font-size:14px}
.field input{min-width:280px}
.field input:focus-visible,.field select:focus-visible,.sort:focus-visible{
outline:3px solid var(--accent);outline-offset:2px}
.count{font-size:13px;color:var(--muted);margin-left:auto;padding-bottom:12px}
.table-wrap{overflow:auto;background:var(--white);border:1px solid var(--line);
border-radius:12px;margin-top:14px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap}
th{font-size:12px;font-weight:600;background:#f0f5fc;color:#3e5979;padding:0;text-align:left;
position:sticky;top:0;z-index:1}
th .sort{display:block;width:100%;padding:11px 12px;border:0;background:transparent;
font:inherit;color:inherit;text-align:left;cursor:pointer;white-space:nowrap}
th .sort:hover{background:#e6effb}
th[aria-sort="descending"] .sort::after{content:" ↓";color:var(--accent)}
th[aria-sort="ascending"] .sort::after{content:" ↑";color:var(--accent)}
th.num .sort{text-align:right}
td{padding:10px 12px;border-top:1px solid var(--line);vertical-align:top}
td.links{white-space:normal;min-width:128px}
td.links a{display:inline-block;margin:0 8px 0 0}
tbody tr:hover{background:#f7faff}
tr.current{background:#eef5ff}
td.name{font-weight:600}
td.name small{display:block;font-weight:400;color:var(--muted);font-size:12px}
td.status{white-space:normal;min-width:200px;max-width:300px;line-height:1.5;color:#3c5878}
.num{text-align:right;font-family:var(--numeric)}
.pos{color:var(--good)}.neg{color:var(--danger)}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid #b9cdf0}
a:hover{border-bottom-color:var(--accent)}
a:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;background:var(--soft);
color:#375674;font-size:12px;white-space:nowrap}
.tag.warn{background:#fdecec;color:var(--danger)}
.empty{padding:24px;text-align:center;color:var(--muted)}
footer{margin-top:26px;color:var(--muted);font-size:12px;max-width:90ch}
@media(max-width:900px){main{padding:16px}.stats{grid-template-columns:repeat(2,1fr)}
.stat:nth-child(2){border-right:0}.stat:nth-child(-n+2){border-bottom:1px solid var(--line)}
.field input{min-width:0;width:100%}.field{flex:1 1 100%}.count{margin-left:0;padding:0}}
"""

SCRIPT = """
(function () {
  var table = document.getElementById('reports');
  if (!table || !table.tBodies.length) { return; }
  var body = table.tBodies[0];
  var rows = Array.prototype.slice.call(body.rows);
  var search = document.getElementById('filter-text');
  var family = document.getElementById('filter-family');
  var count = document.getElementById('filter-count');
  var empty = document.getElementById('filter-empty');
  var state = { key: null, dir: -1 };

  function value(row, key) {
    var raw = row.dataset[key];
    if (key === 'name') { return raw || ''; }
    if (raw === '' || raw === undefined) { return null; }
    return Number(raw);
  }

  function apply() {
    var query = (search.value || '').trim().toLowerCase();
    var chosen = family.value;
    var shown = 0;
    rows.forEach(function (row) {
      var okFamily = chosen === 'all' || row.dataset.family === chosen;
      var okQuery = !query || row.dataset.search.indexOf(query) >= 0;
      var visible = okFamily && okQuery;
      row.hidden = !visible;
      if (visible) { shown += 1; }
    });
    count.textContent = '显示 ' + shown + ' / ' + rows.length + ' 份';
    empty.hidden = shown !== 0;
    table.hidden = shown === 0;
  }

  function sortBy(key) {
    if (state.key === key) { state.dir = -state.dir; }
    else { state.key = key; state.dir = key === 'name' ? 1 : -1; }
    rows.sort(function (a, b) {
      var x = value(a, key), y = value(b, key);
      if (x === null && y === null) { return a.dataset.name.localeCompare(b.dataset.name); }
      if (x === null) { return 1; }
      if (y === null) { return -1; }
      if (typeof x === 'string') { return state.dir * x.localeCompare(y, 'zh-CN'); }
      return state.dir * (x - y);
    });
    rows.forEach(function (row) { body.appendChild(row); });
    document.querySelectorAll('th[data-key]').forEach(function (th) {
      th.setAttribute('aria-sort', th.dataset.key === key
        ? (state.dir > 0 ? 'ascending' : 'descending') : 'none');
    });
  }

  document.querySelectorAll('.sort').forEach(function (button) {
    button.addEventListener('click', function () { sortBy(button.dataset.key); });
  });
  search.addEventListener('input', apply);
  family.addEventListener('change', apply);
  sortBy('when');
  apply();
})();
"""


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fmt(value, digits=2, signed=False):
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number != number or number in (float("inf"), float("-inf")):
        return "—"
    text = f"{number:+,.{digits}f}" if signed else f"{number:,.{digits}f}"
    return text.replace("-", "−") if text.startswith("-") else text


def _pct(value, digits=2):
    text = _fmt(value, digits)
    return "—" if text == "—" else f"{text}%"


def _direction_class(value):
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return " pos" if number > 0 else " neg" if number < 0 else ""


def _attr(value):
    if value is None:
        return ""
    return escape(str(value), quote=True)


def _full_record(data):
    default = data.get("default_policy")
    results = data.get("results") or {}
    if not isinstance(results, dict) or "full" not in results:
        return None, None
    period = results.get("full") or {}
    if default and default in period:
        record = period[default]
    elif len(period) == 1:
        record = next(iter(period.values()))
    else:
        return None, None
    if not isinstance(record, dict):
        return None, None
    return record.get("current") or record, record


def _status(data):
    selection = data.get("selection") or {}
    status = selection.get("status")
    if status:
        return status
    decisions = data.get("decisions") or {}
    if decisions:
        text = next(iter(decisions.values()), {}).get("status")
        if text:
            return text
    return "历史实验（无状态字段）"


def summarize(directory):
    data = _load(directory / "report.json")
    if data is None:
        return None
    protocol = data.get("protocol") or {}
    frozen = protocol.get("frozen_at")
    try:
        stamp = datetime.fromisoformat(frozen).astimezone() if frozen else None
    except ValueError:
        stamp = None
    if stamp is None:
        try:
            stamp = datetime.fromtimestamp((directory / "report.json").stat().st_mtime, timezone.utc).astimezone()
        except OSError:
            stamp = None
    current, record = _full_record(data)
    current = current or {}
    versus = (record or {}).get("vs_four") or {}
    default = data.get("default_policy")
    family = data.get("family")
    selection = data.get("selection") or {}
    decisions = data.get("decisions") or {}
    live = any(bool(item.get("live")) for item in decisions.values() if isinstance(item, dict))
    manifest = data.get("manifest") or {}
    row = {
        "name": directory.name,
        "family": family or "legacy",
        "title": FAMILY_LABELS.get(family) or ("早期单标的/组合实验" if not family else family),
        "version": data.get("version") or "—",
        "default": default or "—",
        "selected": selection.get("selected_policy") or "—",
        "eligible": selection.get("eligible_count"),
        "when": stamp.strftime("%Y-%m-%d %H:%M") if stamp else "—",
        "stamp": int(stamp.timestamp() * 1000) if stamp else 0,
        "cutoff": (manifest.get("end_exclusive") or "")[:10],
        "equity": current.get("equity"),
        "twr": (current.get("twr_return") * 100) if current.get("twr_return") is not None else None,
        "execution": (current.get("execution_rate") * 100) if current.get("execution_rate") is not None else None,
        "drawdown": (current.get("max_drawdown") * 100) if current.get("max_drawdown") is not None else None,
        "delta": versus.get("equity_delta"),
        "bps": versus.get("daily_return_gain_bps"),
        "status": _status(data),
        "live": live,
        "baseline_label": data.get("baseline_label") or ("同预算" if family == "smart" else "普通4次"),
        "coverage_gap": bool(versus.get("asset_cost_gain_pct"))
        and any(value is None for value in versus["asset_cost_gain_pct"].values()),
        "has_page": (directory / "index.html").is_file(),
    }
    return row


def _cache_key(report):
    try:
        stat = report.stat()
    except OSError:
        return None
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _read_cache(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    reports = data.get("reports")
    return reports if isinstance(reports, dict) else {}


def build_index(reports, output, use_cache=True):
    reports = Path(reports)
    cache_path = reports / ".index-cache.json"
    cache = _read_cache(cache_path) if use_cache else {}
    rows, fresh = [], {}
    for directory in sorted(path for path in reports.iterdir() if path.is_dir()):
        report = directory / "report.json"
        if not report.is_file():
            continue
        key = _cache_key(report)
        cached = cache.get(directory.name)
        row = None
        if isinstance(cached, dict) and cached.get("key") == key and isinstance(cached.get("row"), dict):
            row = cached["row"]
            if not row.get("title") or "baseline_label" not in row:
                row = None
        if not row:
            row = summarize(directory)
        if row is None:
            continue
        rows.append(row)
        if key:
            fresh[directory.name] = {"key": key, "row": row}
    if use_cache:
        try:
            payload = {"version": CACHE_VERSION, "reports": fresh}
            cache_path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        except OSError:
            pass
    rows.sort(key=lambda row: (row["stamp"], row["name"]), reverse=True)
    latest = rows[0] if rows else None
    cutoff = next((row["cutoff"] for row in rows if row["cutoff"]), None)
    total_live = sum(row["live"] for row in rows)
    families = []
    family_counts = {}
    for row in rows:
        family_counts[row["family"]] = family_counts.get(row["family"], 0) + 1
        if row["family"] not in [item[0] for item in families]:
            families.append((row["family"], row["title"]))
    options = f'<option value="all">全部系列（{len(rows)}）</option>' + "".join(
        f'<option value="{_attr(key)}">{escape(label or key)}（{family_counts.get(key, 0)}）</option>'
        for key, label in families
    )
    body = []
    for row in rows:
        current = " current" if latest and row["name"] == latest["name"] else ""
        links = []
        if row["has_page"]:
            links.append(f'<a href="{_attr(row["name"])}/index.html">报告</a>')
        links.append(f'<a href="{_attr(row["name"])}/report.json">JSON</a>')
        if (reports / row["name"] / "adaptive-protocol.json").is_file():
            links.append(f'<a href="{_attr(row["name"])}/adaptive-protocol.json">协议</a>')
        tags = ""
        if row["baseline_label"] != "普通4次":
            tags += f' <span class="tag">{escape(row["baseline_label"])}</span>'
        if row["coverage_gap"]:
            tags += ' <span class="tag warn">覆盖不全</span>'
        search_text = " ".join(
            str(part) for part in (row["name"], row["title"], row["default"], row["status"])
        ).lower()
        body.append(
            f'<tr class="{current.strip()}" data-family="{_attr(row["family"])}"'
            f' data-name="{_attr(row["name"])}" data-search="{_attr(search_text)}"'
            f' data-when="{row["stamp"]}" data-equity="{_attr(row["equity"])}"'
            f' data-delta="{_attr(row["delta"])}" data-bps="{_attr(row["bps"])}"'
            f' data-twr="{_attr(row["twr"])}" data-dd="{_attr(row["drawdown"])}"'
            f' data-exec="{_attr(row["execution"])}">'
            f'<td class="name">{escape(row["name"])}'
            f'<small>{escape(row["title"])} · v{escape(str(row["version"]))}</small></td>'
            f'<td>{escape(str(row["default"]))}</td>'
            f'<td class="num">{escape(row["when"])}</td>'
            f'<td class="num">{_fmt(row["equity"])}</td>'
            f'<td class="num{_direction_class(row["delta"])}">{_fmt(row["delta"], signed=True)}{tags}</td>'
            f'<td class="num">{_fmt(row["bps"], signed=True)}</td>'
            f'<td class="num">{_pct(row["twr"])}</td>'
            f'<td class="num">{_pct(row["drawdown"])}</td>'
            f'<td class="num">{_pct(row["execution"], 1)}</td>'
            f'<td class="status">{escape(row["status"])}</td>'
            f'<td class="links">{"".join(links)}</td></tr>'
        )
    latest_line = escape(latest["name"]) if latest else "—"
    table = "\n".join(body) if body else '<tr><td colspan="11" class="empty">没有找到报告</td></tr>'
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';script-src 'unsafe-inline';style-src 'unsafe-inline';img-src data:;base-uri 'none';form-action 'none'">
<title>定投实验室 | 全部报告索引</title><style>{CSS}</style></head>
<body><main>
<header>
<div class="eyebrow">定投实验室 · 只读研究版</div>
<h1>全部回测报告索引</h1>
<p>从各目录的 <code>report.json</code> 自动生成。每行只展示该报告自己的默认策略与全期结果；
“较基准”为各报告自身对照口径的期末净值差，标签写明口径，正数不代表已验证的优势。</p>
</header>
<section class="stats" aria-label="总览">
<div class="stat"><span>报告数量</span><b>{len(rows)}</b></div>
<div class="stat"><span>最新实验</span><b>{latest_line}</b></div>
<div class="stat"><span>启用实盘</span><b>{total_live} 个</b></div>
<div class="stat"><span>行情截止</span><b>{escape(cutoff or '—')}</b></div>
</section>
<p class="note">标签写明对照口径：普通4次为相同资金流的普通定投，普通每日1次为同一预算的每日单次定投，同预算为逐标的事后相同最终预算。
所有结果均为已看过历史的回顾性探索，未获得独立前向证据。</p>
<div class="toolbar" role="search">
<div class="field"><label for="filter-text">筛选</label>
<input id="filter-text" type="search" placeholder="报告名、策略或结论关键词" autocomplete="off" spellcheck="false"></div>
<div class="field"><label for="filter-family">系列</label>
<select id="filter-family">{options}</select></div>
<span id="filter-count" class="count" aria-live="polite">显示 {len(rows)} / {len(rows)} 份</span>
</div>
<div class="table-wrap"><table id="reports">
<thead><tr><th data-key="name"><button class="sort" data-key="name" type="button">报告</button></th>
<th>默认策略</th>
<th class="num" data-key="when"><button class="sort" data-key="when" type="button">冻结时间</button></th>
<th class="num" data-key="equity"><button class="sort" data-key="equity" type="button">期末净值</button></th>
<th class="num" data-key="delta"><button class="sort" data-key="delta" type="button">较基准</button></th>
<th class="num" data-key="bps"><button class="sort" data-key="bps" type="button">日均 bps</button></th>
<th class="num" data-key="twr"><button class="sort" data-key="twr" type="button">TWR</button></th>
<th class="num" data-key="dd"><button class="sort" data-key="dd" type="button">最大回撤</button></th>
<th class="num" data-key="exec"><button class="sort" data-key="exec" type="button">动用率</button></th>
<th>结论</th><th>打开</th></tr></thead>
<tbody>{table}</tbody></table>
<p id="filter-empty" class="empty" hidden>没有匹配的报告。</p></div>
<noscript><style>.toolbar{{display:none}}</style></noscript>
<footer>本页由 <code>python -m dca_lab index</code> 生成，不含外部资源，可离线打开。
净值、TWR 与回撤均来自各自报告的 <code>results.full</code>；缺失字段显示为“—”。
点击带箭头的表头可排序，支持名称/策略/结论关键词筛选。
索引缓存 <code>reports/.index-cache.json</code> 按文件大小与修改时间复用，可随时删除。</footer>
<script>{SCRIPT}</script>
</main></body></html>"""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    return output
