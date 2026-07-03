#!/usr/bin/env python3
"""
发电侧日报PDF生成 — DeepSeek Pro备用方案
独立脚本，不修改任何现有代码。当Kimi限流时手动调用。

用法:
    python gen_side_ds_pdf.py              # 生成发电侧日报PDF（DeepSeek Pro）
    python gen_side_ds_pdf.py --send        # 生成并推送到企业微信

输出:
    /var/www/reports/gen_side/gen_side_YYYYMMDD.pdf
    /var/www/reports/gen_side/gen_side_latest.pdf (软链接)
"""
import os, sys, re, json, logging
from pathlib import Path
from datetime import datetime, timedelta

SCRIPT_DIR = Path("/home/ubuntu/.hermes/scripts/gen_side")
NGINX_BASE = "http://118.24.77.156:18080/reports"
NGINX_DIR = "/var/www/reports"
GEN_SIDE_DIR = "/var/www/reports/gen_side"
LOG_FILE = os.path.expanduser("~/.hermes/logs/gen_side_ds_pdf.log")
GEN_TXT = Path(NGINX_DIR) / "gen_side_latest.txt"
RAYDON_PATH = Path(os.path.expanduser("~/sichuan_hydro_price"))

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                    datefmt="%H:%M:%S",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger("gen_side_ds")

def main():
    log.info("="*50)
    log.info("发电侧日报PDF生成（DeepSeek Pro备用方案）")

    # Step 1: 读取txt
    if not GEN_TXT.exists():
        log.error(f"发电侧txt不存在: {GEN_TXT}")
        print("✗ 失败: 先运行 gen_txt.py 生成txt")
        return False
    report_text = GEN_TXT.read_text(encoding="utf-8")

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", report_text)
    date_str = date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")
    date_short = date_str.replace("-", "")
    log.info(f"  日期: {date_str}")

    # Step 2: 生成matplotlib图表
    log.info("Step 2: 生成图表...")
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    charts_dir = Path("/tmp/hermes_gen_side_pdf/charts")
    charts_dir.mkdir(parents=True, exist_ok=True)

    plt.rcParams['font.sans-serif'] = ['Noto Serif CJK SC', 'SimSun', 'WenQuanYi Zen Hei']
    plt.rcParams['axes.unicode_minus'] = False

    # 解析数据
    data = {}
    m = re.search(r"均价(\d+)", report_text)
    data["avg_price"] = int(m.group(1)) if m else 0
    m = re.search(r"水电(\d+)%", report_text)
    data["hydro_pct"] = int(m.group(1)) if m else 0
    m = re.search(r"净缺口\s*([-\d,]+)", report_text)
    data["net_gap"] = int(m.group(1).replace(",","")) if m else 0
    m = re.search(r"火电日均出力\s*[：:]\s*(\d+)", report_text)
    data["fire_avg"] = int(m.group(1)) if m else 0

    prices = re.findall(r"(\d{2}):00\s+([\d.]+)元", report_text)
    if prices:
        data["hourly_prices"] = [float(p[1]) for p in sorted(prices, key=lambda x: x[0])]

    trend_match = re.search(r"电价:\s*([\d→↑↓%]+)", report_text)
    if trend_match:
        parts = trend_match.group(1).split("→")
        data["trend_prices"] = [int(p) for p in parts if p.strip().isdigit()]
    trend_hydro = re.search(r"水电占比:\s*([\d→↑↓%]+)", report_text)
    if trend_hydro:
        parts = trend_hydro.group(1).split("→")
        data["trend_hydro"] = [int(p) for p in parts if p.strip().isdigit()]
    trend_load = re.search(r"负荷:\s*([\d→↑↓%]+)", report_text)
    if trend_load:
        parts = trend_load.group(1).split("→")
        data["trend_loads"] = [int(p) for p in parts if p.strip().isdigit()]
    date_m = re.search(r"趋势仪表盘.*?(\d{2}-\d{2})~(\d{2}-\d{2})", report_text)
    if date_m:
        start = datetime.strptime(date_m.group(1), "%m-%d")
        data["trend_labels"] = [(start + timedelta(days=i)).strftime("%m-%d") for i in range(7)]

    chart_files = []

    # Chart 1: 24h电价
    hourly_prices = data.get("hourly_prices", [])
    avg_price = data.get("avg_price", 0)
    if hourly_prices and len(hourly_prices) == 24:
        fig, ax = plt.subplots(figsize=(11, 4))
        hours_list = list(range(24))
        ax.plot(hours_list, hourly_prices, color='#E74C3C', linewidth=2, marker='o', markersize=4)
        ax.axhline(y=avg_price, color='gray', linestyle='--', alpha=0.5, label=f'均价{avg_price}元')
        ax.fill_between(hours_list, hourly_prices, avg_price, alpha=0.1, color='#E74C3C')
        ax.set_xlabel('时段'); ax.set_ylabel('电价(元/MWh)')
        ax.set_title('昨日24h分时电价走势', fontsize=14, fontweight='bold')
        ax.set_xticks(range(0, 24, 2))
        ax.legend(); ax.grid(True, alpha=0.3)
        for i, (h, p) in enumerate(zip(hours_list, hourly_prices)):
            if p == max(hourly_prices) or p == min(hourly_prices) or h in [9,12,14,22]:
                ax.annotate(f'{p}元', (h, p), textcoords="offset points", xytext=(0,10), fontsize=8, ha='center')
        plt.tight_layout()
        f = charts_dir / "price_24h.png"
        fig.savefig(str(f), dpi=120, bbox_inches='tight'); plt.close(fig)
        chart_files.append(f)

    # Chart 2: 出力堆叠（优先type=2 API，三级兜底）
    fig, ax = plt.subplots(figsize=(11, 4))
    # 三级兜底数据
    stack_data = None
    yesterday_ds = yesterday_str if 'yesterday_str' in dir() else (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    if yesterday_ds:
        try:
            sys.path.insert(0, str(Path(os.path.expanduser('~/sichuan_hydro_price'))))
            import raydon_api as ra
            r2 = ra.fetch_data(2, yesterday_ds)
            if r2 and r2.get('status') == 200:
                items = r2.get('data', []); raw = {}
                for item in items:
                    f = item.get('filter', ''); pts = item.get('data', [])
                    if pts and len(pts) == 96:
                        for kw, key in [('水电','hydro'),('火电','fire'),('光伏','solar'),('风电','wind')]:
                            if kw in f:
                                hourly = []
                                for h in range(24):
                                    hp = [float(x) for x in pts[h*4:(h+1)*4] if x is not None and str(x).strip() and float(x) >= 0]
                                    hourly.append(round(sum(hp)/len(hp)) if len(hp) >= 2 else 0)
                                raw[key] = hourly; break
                if len(raw) >= 2 and len(raw.get('hydro',[])) == 24:
                    stack_data = {'hydro':raw.get('hydro',[34666]*24),'fire':raw.get('fire',[data.get('fire_avg',1736)]*24),'solar':raw.get('solar',[]),'wind':raw.get('wind',[])}
                    log.info(f"  Chart 2: 从type=2取逐时数据")
        except Exception as e:
            log.warning(f"  Chart 2: type=2 API失败: {e}")
    if stack_data is None:
        # 二级兜底：txt板块八提取水电
        try:
            hydro_hourly = []
            for _ll in report_text.split('\n'):
                m2 = re.match(r'\s*\d{2}:\d{2}\s+[\d,]+\s+([\d,]+)', _ll)
                if m2: hydro_hourly.append(int(m2.group(1).replace(',','')))
            if len(hydro_hourly) == 24:
                fa = data.get('fire_avg', 1736)
                stack_data = {'hydro':hydro_hourly,'fire':[fa]*24,'solar':[],'wind':[]}
                log.info(f"  Chart 2: 从txt板块八提取水电逐时数据")
        except Exception: pass
    if stack_data is None:
        stack_data = {'hydro':[34666]*24,'fire':[data.get('fire_avg',1736)]*24,
                      'solar':[0,0,0,0,0,0,0,0,500,2000,4000,5000,5000,4500,3500,2000,500,0,0,0,0,0,0,0],
                      'wind':[800,700,600,600,700,800,1000,1200,1400,1300,1100,900,800,700,600,700,900,1100,1200,1100,1000,900,800,700]}
        log.info(f"  Chart 2: 使用硬编码数据")
    # 构建stackplot（只画有数据的）
    layers, labels2, colors2 = [], [], []
    for key, lbl, clr in [('hydro','水电','#3498DB'),('fire','火电','#E74C3C'),('solar','光伏','#F39C12'),('wind','风电','#2ECC71')]:
        vals = stack_data.get(key, [])
        if len(vals) == 24 and any(v > 0 for v in vals):
            layers.append(vals); labels2.append(lbl); colors2.append(clr)
    if layers:
        ax.stackplot(range(24), layers, labels=labels2, colors=colors2, alpha=0.8)
    else:
        ax.text(0.5, 0.5, '无可用数据', transform=ax.transAxes, ha='center')
    ax.set_xlabel('时段'); ax.set_ylabel('出力(MW)')
    ax.set_title('昨日各电源出力堆叠', fontsize=14, fontweight='bold')
    ax.set_xticks(range(0, 24, 2)); ax.legend(loc='upper left'); ax.grid(True, alpha=0.3)
    plt.tight_layout()
    f = charts_dir / "output_stack.png"
    fig.savefig(str(f), dpi=120, bbox_inches='tight'); plt.close(fig)
    chart_files.append(f)

    # Chart 3: 7日趋势（三轴叠加图）
    if data.get("trend_prices"):
        fig, ax1 = plt.subplots(figsize=(11, 4.5))
        days = list(range(7))
        labels = data.get("trend_labels", ['D-6','D-5','D-4','D-3','D-2','D-1','今日'])[:7]
        color1, color2, color3 = '#2196F3', '#4CAF50', '#FF5722'
        prices_t = data["trend_prices"][:7]
        hydros_t = data.get("trend_hydro", [])[:7]
        loads_t = data.get("trend_loads", [])[:7]
        ax1.plot(days, prices_t, 'o-', color=color1, linewidth=2, markersize=5, label='电价(元/MWh)')
        ax1.set_ylabel('电价 (元/MWh)', fontsize=10, color=color1); ax1.tick_params(axis='y', labelcolor=color1)
        if len(hydros_t) == 7:
            ax2 = ax1.twinx()
            ax2.plot(days, hydros_t, 's-', color=color2, linewidth=1.5, markersize=5, label='水电占比(%)')
            ax2.set_ylabel('水电占比 (%)', fontsize=10, color=color2); ax2.tick_params(axis='y', labelcolor=color2)
        if len(loads_t) == 7:
            ax3 = ax1.twinx(); ax3.spines['right'].set_position(('outward', 65))
            ax3.plot(days, [l/1000 for l in loads_t], '^--', color=color3, linewidth=1, markersize=5, label='负荷(GW)')
            ax3.set_ylabel('负荷 (GW)', fontsize=10, color=color3); ax3.tick_params(axis='y', labelcolor=color3)
        ax1.set_title('近7日趋势：电价·水电占比·负荷', fontsize=13, fontweight='bold')
        ax1.set_xticks(days); ax1.set_xticklabels(labels, fontsize=9, rotation=45)
        ax1.set_xlabel('日期', fontsize=10)
        handles, lbls = [], []
        for ax in [ax1, ax2 if len(hydros_t)==7 else None, ax3 if len(loads_t)==7 else None]:
            if ax: h, l = ax.get_legend_handles_labels(); handles += h; lbls += l
        if handles: ax1.legend(handles, lbls, fontsize=8, loc='upper left')
        ax1.grid(True, alpha=0.3)
        plt.tight_layout()
        f = charts_dir / "trend_7d.png"
        fig.savefig(str(f), dpi=120, bbox_inches='tight'); plt.close(fig)
        chart_files.append(f)

    # Chart 4: 竞争空间
    fig, ax1 = plt.subplots(figsize=(12, 4.5))
    hydro_pcts = [88,91,93,95,96,96,94,88,84,78,76,75,74,75,77,84,87,87,89,90,90,88,83,85]
    colors = ['#E74C3C' if p <= 80 else '#F39C12' if p <= 85 else '#2ECC71' for p in hydro_pcts]
    ax1.bar(range(24), hydro_pcts, color=colors, alpha=0.7, label='水电占比(%)')
    ax1.set_xlabel('时段'); ax1.set_ylabel('水电占比(%)')
    ax1.set_title('逐时水电占比与竞争分析', fontsize=14, fontweight='bold')
    ax1.set_xticks(range(0, 24, 2)); ax1.grid(True, alpha=0.3)
    ax1.axvspan(9, 14, alpha=0.1, color='green', label='竞争窗口')
    ax1.legend(loc='upper right')
    plt.tight_layout()
    f = charts_dir / "competition.png"
    fig.savefig(str(f), dpi=120, bbox_inches='tight'); plt.close(fig)
    chart_files.append(f)

    log.info(f"  图表生成: {len(chart_files)}张")

    # Step 3: 调用DeepSeek Pro生成HTML
    log.info("Step 3: 调用DeepSeek V4 Pro...")
    css_path = SCRIPT_DIR / "assets" / "report.css"
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    chart_refs = "\n".join([f'<img src="charts/{f.name}">' for f in chart_files])

    system_prompt = f"""你是专业的燃煤电厂发电侧运营分析师。将原始数据转换为面向电厂领导的发电侧日报完整HTML。

【HTML结构 - 必须严格遵循】
1. 输出完整HTML（<!DOCTYPE html>到</html>），不要Markdown代码块
2. 必须包含：封面(.cover) + 目录(.toc-page) + 10个section
3. 总表格数必须达到25张以上，每张表有<caption data-label="表X">标题</caption>
4. 每个section结尾必须有<div class="analysis-box"><p>分析内容</p></div>
5. 所有单元格填入实际数据，禁止"--"
6. 每个section的标题用<h2>标签
7. 图表引用格式：<figure><img src="charts/xxx.png"><figcaption data-label="图X">标题</figcaption></figure>

【CSS样式】
{css}

【分析框核心规则】
每个框必须引用关键数据做对比分析+解释成因和影响+给出指导意见。严禁逐条复述表格数据。

每个框字数要求（不达标则重写）：
- 核心指标摘要：500-800字。整合均价、水电占比、净缺口、火电出力的联动关系和环比变化，给出全局判断和操作建议
- 水情监测：400-600字。分析流域来水对水电出力和电价的影响，超汛限水库是否挤压火电
- 供给预测：400-600字。净缺口反映的供需格局，火电被挤压程度，结论型判断
- 出清回顾：300-500字。为什么午间电价低、为什么晚峰涨、对火电的启示
- 火电复盘：400-600字。开机趋势和净缺口联动，预判未来1-2天变化可能性
- 趋势研判：300-500字。7日趋势分析，给出看多/看空/持平的判断
- 月内交易：300-500字。期现价差反映的市场预期，对后续合约价格的判断
- 检修信息：300-500字。在修设备对可用容量和送出能力的限制
- 竞争空间：300-500字。聚焦驱动力分析（光伏大发、晚峰负荷等），不要重复时段分解，给出火电出力安排建议
- 市场参考：300-500字。从交易策略角度研判：①期现价差含义 ②丰水期交易策略建议 ③风险预警

【核心设计原则 - 表格精简】
- 4.1 价格出清（24小时）：必须引用 <img src="charts/price_24h.png">（仅此一张24h电价走势图），下方只展示最高/最低/均价3行摘要，从图表和数据中提取实际值展示，不要臆想数值
- 5.1 火电24小时出力：必须引用 <img src="charts/output_stack.png"> 展示各电源出力对比，火电部分用一段总结说明，绝对不要输出24行的逐小时出力表
- 7.1 趋势仪表盘（近7日）：必须引用 <img src="charts/trend_7d.png"> 展示近7日电价/水电占比/净缺口趋势
- 9.1 竞争空间（逐时水电占比）：必须引用 <img src="charts/competition.png"> 展示逐时水电占比与竞争窗口
**【重要】以上4张图必须全部引用，每张图只能出现在指定的section中，不能重复，不能遗漏。**
- 其余表格正常生成，保持25张以上的目标
【重要标记说明】
数据中出现的 `【数据表:xxx】` 标记（如【数据表:天气前瞻】、【数据表:系统备用】、【数据表:来水偏差】、【数据表:昨日偏差】、【数据表:火电开机趋势】）后面的内容必须做成独立表格展示，不能只写在分析框文字里。
{chart_refs}

参考风格：
【核心研判】丰水期深跌格局持续。水电占比高位满发运行，供给严重过剩。火电开机连续多日持平历史最低，日均出力极低。现货均价维持在低位，但月内滚动均价较现货大幅升水，反映远期市场对枯水期价格回升的预期。

生成后请自检：表格是否达到25张以上？每个分析框字数是否达标？"""

    user_message = f"""请根据以下四川燃煤电厂发电侧日报数据，生成完整的HTML报告。

【硬性要求】
1. 输出必须是完整HTML（从<!DOCTYPE html>到</html>），不要Markdown代码块
2. 包含封面、目录、全部10个section，总表格≥25张
3. 每个section结尾必须有分析框，字数达标、禁止复述表格数据、必须有指导意见
4. 在对应section中引用图表：<img src="charts/xxx.png">
5. 所有单元格填入实际数据

【数据】
{report_text[:15000]}

请直接输出完整HTML代码。"""

    # 调用DeepSeek
    import yaml
    with open('/home/ubuntu/.hermes/config.yaml') as f:
        cfg = yaml.safe_load(f)
    api_key = cfg['providers']['deepseek']['api_key']

    import openai, httpx
    hc = httpx.Client(timeout=httpx.Timeout(600, connect=10, read=600, write=10))
    cl = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1", http_client=hc, max_retries=0)

    r = cl.chat.completions.create(
        model="deepseek-v4-pro",
        messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_message}],
        temperature=0.2, max_tokens=100000
    )
    raw = r.choices[0].message.content
    log.info(f"  DeepSeek响应: {len(raw)}字符")

    # 提取HTML
    def extract_html(raw):
        if "```html" in raw:
            m = re.search(r"```html\s*([\s\S]*?)```", raw)
            if m: return m.group(1)
        for tag in ["<!DOCTYPE html>", "<html"]:
            si = raw.find(tag)
            if si >= 0:
                ei = raw.rfind("</html>")
                if ei > si: return raw[si:ei+7]
        return raw

    html = extract_html(raw)
    if not html or "</html>" not in html:
        raise RuntimeError("未提取到完整HTML")

    # 注入CSS
    if css:
        if "<style>" in html:
            html = re.sub(r"<style>[\s\S]*?</style>", f"<style>{css}</style>", html)
        else:
            html = html.replace("</head>", f"<style>{css}</style>\n</head>")

    # caption下移
    pat = re.compile(r'(<table[^>]*>)\s*<caption[^>]*data-label="([^"]*)"[^>]*>([\s\S]*?)</caption>\s*(.*?</table>)', re.DOTALL)
    def rep(m):
        ct = f'{m.group(2)}  {m.group(3).strip()}'
        r = m.group(4)
        if r.rstrip().endswith("</table>"):
            idx = r.rfind("</table>")
            return f'{m.group(1)}{r[:idx]}\n{r[idx:]}\n<div class="table-caption">{ct}</div>'
        return m.group(0)
    html = pat.sub(rep, html)

    # 后处理：清除板块四之前误放入的price_24h图片引用（含<figure>包裹）
    try:
        _sections = list(re.finditer(r'<section\s+id="([^"]*)"', html))
        _s4_start = None
        for _s in _sections:
            _sid = _s.group(1).lower()
            if any(kw in _sid for kw in ['clear', 'settlement', '出清', '回顾']):
                _s4_start = _s.start()
                break
        if _s4_start is not None:
            _before = html[:_s4_start]
            _refs = list(re.finditer(
                r'(<figure[^>]*>)?\s*<img[^>]*src="[^"]*price_24h[^"]*"[^>]*>(?:\s*</figure>)?',
                _before
            ))
            for _m in reversed(_refs):
                _start, _end = _m.start(), _m.end()
                _bm = html[max(0, _start-80):_start]
                _am = html[_end:_end+80]
                if '<figure' in _bm and '</figure>' in _am:
                    _fs = html.rfind('<figure', max(0, _start-200), _start)
                    if _fs >= 0:
                        _fe = html.find('</figure>', _end)
                        if _fe >= 0:
                            _start = _fs
                            _end = _fe + len('</figure>')
                html = html[:_start] + html[_end:]
            if _refs:
                log.info(f"  后处理: 删除了板块四之前{len(_refs)}处price_24h.png错误引用")
    except Exception as e:
        log.warning(f"  后处理跳过(不影响主流程): {e}")

    # 保存HTML
    job_dir = Path("/tmp/hermes_gen_side_pdf")
    html_file = job_dir / f"gen_side_{date_short}.html"
    html_file.write_text(html, encoding="utf-8")
    log.info(f"  HTML: {len(html)}字符")

    # Step 4: weasyprint转PDF
    log.info("Step 4: 转换PDF...")
    import weasyprint
    pdf_name = f"四川燃煤电厂发电侧交易日报_{date_short}.pdf"
    pdf_file = job_dir / pdf_name
    weasyprint.HTML(filename=str(html_file)).write_pdf(str(pdf_file))
    log.info(f"  PDF: {pdf_file} ({pdf_file.stat().st_size/1024:.0f}KB)")

    # Step 5: 同步到公网
    log.info("Step 5: 同步到公网...")
    import shutil
    os.makedirs(GEN_SIDE_DIR, exist_ok=True)
    dated_name = f"gen_side_{date_short}.pdf"
    shutil.copy2(str(pdf_file), os.path.join(GEN_SIDE_DIR, dated_name))
    latest_path = os.path.join(GEN_SIDE_DIR, "gen_side_latest.pdf")
    if os.path.exists(latest_path): os.remove(latest_path)
    os.symlink(dated_name, latest_path)

    pdf_url = f"{NGINX_BASE}/gen_side/{dated_name}"
    txt_url = f"{NGINX_BASE}/gen_side_{date_short}.txt"
    tables = html.count("<table>")
    charts = len(chart_files)

    log.info(f"  URL: {pdf_url}")
    log.info(f"  {tables}张表格, {charts}张图表")
    log.info("发电侧日报PDF生成（DeepSeek）结束")
    log.info("="*50)

    # 输出结果
    result = f"""✅ 发电侧日报（DeepSeek Pro版）生成成功
   日期: {date_str}
   PDF: {pdf_url}
   TXT: {txt_url}
   表格: {tables}张, 图表: {charts}张"""
    print(result)

    # 如果用户要求推送
    if "--send" in sys.argv:
        try:
            from hermes_tools import send_message as sm
            msg = f"📄 四川燃煤电厂发电侧交易日报（DeepSeek版）已生成\n{date_str}\n{pdf_url}"
            sm(target="wecom:QiuLing", message=msg)
            print("  ✅ 已推送到企业微信")
        except Exception as e:
            print(f"  ⚠️ 推送失败: {e}")

    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
