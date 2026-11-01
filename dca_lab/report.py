"""An offline, self-contained interactive research report."""

import json
import hashlib
import html
from pathlib import Path


def render_report(report, output):
    template = Path(__file__).with_name("portfolio_template.html" if report.get("scope") == "portfolio" else "report_template.html").read_text(encoding="utf-8")
    if report.get("scope") == "portfolio":
        template = template.replace("__PORTFOLIO_UI_CSS__", Path(__file__).with_name("portfolio_dashboard.css").read_text(encoding="utf-8"))
        template = template.replace("__PORTFOLIO_UI_JS__", Path(__file__).with_name("portfolio_dashboard.js").read_text(encoding="utf-8"))
        keys = tuple(report["assets"])
        count = {3: "三", 5: "五"}.get(len(keys), str(len(keys)))
        template = template.replace("三个标的", count + "个标的").replace("三标的", count + "标的").replace("三等权", count + "标的等权")
        for suffix in ("成本改善 %", "支出"):
            old = "".join(f"<th>{k}{suffix}</th>" for k in ("GOOG", "XAU", "SOL"))
            new = "".join(f"<th>{html.escape(k)}{suffix}</th>" for k in keys)
            template = template.replace(old, new)
        template = template.replace("后面三列", f"后面{count}列")
        if report.get("selection"):
            size = len(report["selection"]["ranking"])
            template = template.replace("8个固定模板 + 4个旧纯机会版", f"{size-4}个固定模板 + 4个旧纯机会版").replace("${s.eligible_count}/12", "${s.eligible_count}/" + str(size))
            if size > 12:
                template = template.replace("三者AND共8版。", "三者AND共8版。另增3版：RSI或布林互补；4小时下降趋势或趋势未知时要求两者同时确认；再按clip(1+0.3×折扣深度−0.3×下降趋势,0.7,1.3)调整机会额度。只在信号成立时调额，不增加基础单。")
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    result = template
    library = adapter = notice = ""
    if report.get("chart_engine"):
        vendor = Path(__file__).with_name("vendor")
        source = json.loads((vendor / "source.json").read_text(encoding="utf-8"))
        bundle = (vendor / "lightweight-charts.js").read_bytes()
        if hashlib.sha256(bundle).hexdigest() != source["js_sha256"]:
            raise ValueError("TradingView library hash mismatch")
        library = bundle.decode("utf-8").replace("</script", "<\\/script")
        adapter = Path(__file__).with_name("tradingview_adapter.js").read_text(encoding="utf-8")
        notice = html.escape((vendor / "NOTICE").read_text(encoding="utf-8"))
    result = result.replace("__CHART_LIBRARY__", library).replace("__CHART_ADAPTER__", adapter).replace("__CHART_NOTICE__", notice)
    result = result.replace("__REPORT_PAYLOAD__", payload)
    path = Path(output) / "index.html"
    path.write_text(result, encoding="utf-8")
    return path
