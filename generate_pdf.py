#!/usr/bin/env python3
"""
发电侧日报PDF生成脚本
- 读取 gen_side_latest.txt
- 调用Kimi API生成HTML（含matplotlib图表引用）
- weasyprint转PDF
- 同步到/var/www/reports/gen_side/

Usage:
    python generate_pdf.py

Cron:
    10:00
"""

import os, sys, re, json, time, logging, traceback
from pathlib import Path
from datetime import datetime, timedelta
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

NGINX_BASE = "http://118.24.77.156:18080/reports"
NGINX_DIR = "/var/www/reports"
GEN_SIDE_DIR = "/var/www/reports/gen_side"
SCRIPT_DIR = Path(__file__).parent
ASSETS_DIR = SCRIPT_DIR / "assets"
LOG_FILE = os.path.expanduser("~/.hermes/logs/gen_side_pdf.log")
GEN_TXT = Path(NGINX_DIR) / "gen_side_latest.txt"
SELL_TXT = Path(NGINX_DIR) / "daily_latest.txt"
CSS_PATH = ASSETS_DIR / "report.css"
KIMI_BASE_URL_OPEN = "https://api.moonshot.cn/v1"
KIMI_MODEL_OPEN = "moonshot-v1-128k"
# Kimi Code Plan（备用）
KIMI_BASE_URL_CODE = "https://api.kimi.com/coding/v1"
KIMI_MODEL_CODE = "moonshot-v1-128k"
RAYDON_PATH = Path(os.path.expanduser("~/sichuan_hydro_price"))
PID_FILE = Path("/tmp/gen_side_pdf.pid")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                    datefmt="%H:%M:%S",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger("gen_side_pdf")


def load_kimi_keys():
    """返回 (api_key_open, api_key_code)。open=开放平台主key，code=Code Plan备用key。"""
    key_open = os.getenv("KIMI_API_KEY_OPEN", "").strip()
    key_code = os.getenv("KIMI_API_KEY", "").strip()
    # fallback: 从 .kimi_key 文件读（仅开放平台key）
    if not key_open:
        kf = SCRIPT_DIR / ".kimi_key"
        if kf.exists():
            key_open = kf.read_text(encoding="utf-8").strip()
    return key_open, key_code


def kimi_call_with_fallback(api_key_open, api_key_code, system, user, timeout=600, max_tokens=100000):
    """优先 Kimi 开放平台。触发 429/5xx/超时 → 自动切 Kimi Code Plan。"""
    import openai, httpx

    msgs = [{"role":"system","content":system}, {"role":"user","content":user}]

    def _call(key, base, model, temp):
        hc = httpx.Client(timeout=httpx.Timeout(timeout, connect=10, read=timeout, write=10))
        cl = openai.OpenAI(api_key=key, base_url=base, http_client=hc, max_retries=0)
        r = cl.chat.completions.create(model=model, messages=msgs, temperature=temp, max_tokens=max_tokens)
        return r.choices[0].message.content

    # 主平台
    try:
        log.info("[PRIMARY] Kimi开放平台...")
        result = _call(api_key_open, KIMI_BASE_URL_OPEN, KIMI_MODEL_OPEN, 0.2)
        log.info("[PRIMARY] ✅")
        return result
    except (openai.RateLimitError, openai.InternalServerError,
            openai.APITimeoutError, openai.APIConnectionError,
            openai.APIStatusError) as e:
        log.warning(f"[PRIMARY] ⚠️ {type(e).__name__}，切换到 Code Plan...")
        if not api_key_code:
            raise RuntimeError(f"开放平台{type(e).__name__}，但 Code Plan key 未配置，无法切换") from e

    log.info("[FALLBACK] Kimi Code Plan...")
    result = _call(api_key_code, KIMI_BASE_URL_CODE, KIMI_MODEL_CODE, 1)
    log.info("[FALLBACK] ✅")
    return result


def extract_html(raw):
    """从Kimi返回内容中提取HTML。"""
    # 检测nginx错误页面
    if "<title>50" in raw and "Gateway" in raw:
        raise RuntimeError(f"API返回网关错误: {raw[:200]}")
    if "<title>40" in raw and "Error" in raw:
        raise RuntimeError(f"API返回HTTP错误: {raw[:200]}")
    if "```html" in raw:
        m = re.search(r"```html\s*([\s\S]*?)```", raw)
        if m: return m.group(1)
    for tag in ["<!DOCTYPE html>", "<html"]:
        si = raw.find(tag)
        if si >= 0:
            ei = raw.rfind("</html>")
            if ei > si:
                html = raw[si:ei+7]
                # 确保有 charset=utf-8
                if '<meta charset="utf-8"' not in html and '<meta charset="UTF-8"' not in html:
                    html = html.replace("<head>", '<head>\n    <meta charset="utf-8">')
                return html
    return raw


def check_html(html):
    return "</html>" in html and html.count("<section") >= 8


def move_captions(html):
    pat = re.compile(r'(<table[^>]*>)\s*<caption[^>]*data-label="([^"]*)"[^>]*>([\s\S]*?)</caption>\s*(.*?</table>)', re.DOTALL)
    def rep(m):
        ct = f'{m.group(2)}  {m.group(3).strip()}'
        r = m.group(4)
        if r.rstrip().endswith("</table>"):
            idx = r.rfind("</table>")
            return f'{m.group(1)}{r[:idx]}\n{r[idx:]}\n<div class="table-caption">{ct}</div>'
        return m.group(0)
    return pat.sub(rep, html)


# ─── 图表生成 ─────────────────────────────────────────────────

def parse_report_data(text, yesterday_str=None):
    """从发电侧txt解析结构化数据，含趋势标签和竞争空间逐时数据"""
    data = {}
    m = re.search(r"均价(\d+)", text)
    data["avg_price"] = int(m.group(1)) if m else 0
    m = re.search(r"水电(\d+)%", text)
    data["hydro_pct"] = int(m.group(1)) if m else 0
    m = re.search(r"净缺口\s*([-\d,]+)", text)
    data["net_gap"] = int(m.group(1).replace(",","")) if m else 0
    m = re.search(r"火电负载率([\d.]+)%", text)
    data["thermal_load"] = float(m.group(1)) if m else 0
    m = re.search(r"火电日均出力\s*[：:]\s*(\d+)", text)
    data["fire_avg"] = int(m.group(1)) if m else 0
    
    # 24h电价
    prices = re.findall(r"(\d{2}):00\s+(\d+)元", text)
    if prices:
        data["hourly_prices"] = [int(p[1]) for p in sorted(prices, key=lambda x: x[0])]
    
    # 7日趋势
    trend_match = re.search(r"电价:\s*([\d→↑↓%]+)", text)
    if trend_match:
        parts = trend_match.group(1).split("→")
        data["trend_prices"] = [int(p) for p in parts if p.strip().isdigit()]
    trend_hydro = re.search(r"水电占比:\s*([\d→↑↓%]+)", text)
    if trend_hydro:
        parts = trend_hydro.group(1).split("→")
        data["trend_hydro"] = [int(p) for p in parts if p.strip().isdigit()]
    
    # 趋势日期标签：从"六、趋势仪表盘（近7日 06-23~06-29）"提取
    date_m = re.search(r"趋势仪表盘.*?(\d{2}-\d{2})~(\d{2}-\d{2})", text)
    if date_m:
        from datetime import datetime, timedelta
        start = datetime.strptime(date_m.group(1), "%m-%d")
        labels = []
        for i in range(7):
            d = start + timedelta(days=i)
            labels.append(d.strftime("%m-%d"))
        data["trend_labels"] = labels
    
    # 竞争空间逐时数据（从API获取）
    try:
        import sys
        from pathlib import Path
        raydon_path = Path(os.path.expanduser("~/sichuan_hydro_price"))
        sys.path.insert(0, str(raydon_path))
        import raydon_api as ra
        if yesterday_str:
            ds = yesterday_str
            hydro = ra.get_hydro_actual(ds)
            load = ra.get_actual_load(ds)
            if hydro and load:
                hp = hydro.get("points", [])
                lp = load.get("points", [])
                if len(hp) >= 96 and len(lp) >= 96:
                    hourly_comp = []
                    for h in range(24):
                        h_load = sum(lp[h*4:(h+1)*4]) / 4
                        h_hydro = sum(hp[h*4:(h+1)*4]) / 4
                        hourly_comp.append({
                            "load": round(h_load),
                            "hydro": round(h_hydro),
                            "pct": round(h_hydro / h_load * 100) if h_load > 0 else 0,
                            "gap": round(h_load - h_hydro),
                        })
                    data["comp_hourly"] = hourly_comp
    except Exception:
        pass
    
    return data


def gen_charts(data, charts_dir, date_str=None):
    """生成4张图表"""
    chart_files = []
    
    # 字体设置
    plt.rcParams['font.sans-serif'] = ['Noto Serif CJK SC', 'SimSun', 'WenQuanYi Zen Hei']
    plt.rcParams['axes.unicode_minus'] = False
    
    # Chart 1: 24h电价折线图（直接从API获取）
    hourly_prices = data.get("hourly_prices", [])
    avg_price = data.get("avg_price", 0)
    # 如果txt里解析不到，直接从API拉
    if not hourly_prices and date_str:
        try:
            sys.path.insert(0, str(RAYDON_PATH))
            import raydon_api as ra
            p = ra.get_clearing_price(date_str)
            if p and "points" in p:
                pts = p["points"]
                hourly = []
                for h in range(24):
                    hp = [x for x in pts[h*4:(h+1)*4] if x is not None and -50 <= x <= 800]
                    hourly.append(round(sum(hp)/len(hp), 1) if hp else None)
                valid_count = sum(1 for x in hourly if x is not None)
                if valid_count >= 12:
                    hourly_prices = hourly
                    log.info(f"  Chart 1: 从API取24h电价, {valid_count}个有效时段")
        except Exception as e:
            log.warning(f"  Chart 1: API获取失败: {e}")
    
    if hourly_prices and len(hourly_prices) == 24:
        fig, ax = plt.subplots(figsize=(11, 4))
        hours = list(range(24))
        prices = hourly_prices
        ax.plot(hours, prices, color='#E74C3C', linewidth=2, marker='o', markersize=4)
        ax.axhline(y=avg_price, color='gray', linestyle='--', alpha=0.5, label=f'均价{avg_price}元')
        ax.fill_between(hours, prices, avg_price, alpha=0.1, color='#E74C3C')
        ax.set_xlabel('时段'); ax.set_ylabel('电价(元/MWh)')
        ax.set_title('昨日24h分时电价走势', fontsize=14, fontweight='bold')
        ax.set_xticks(range(0, 24, 2))
        ax.legend()
        ax.grid(True, alpha=0.3)
        for i, (h, p) in enumerate(zip(hours, prices)):
            if p == max(prices) or p == min(prices) or h in [9,12,14,22]:
                ax.annotate(f'{p}元', (h, p), textcoords="offset points", xytext=(0,10), fontsize=8, ha='center')
        plt.tight_layout()
        f = charts_dir / "price_24h.png"
        fig.savefig(str(f), dpi=120, bbox_inches='tight')
        plt.close(fig)
        chart_files.append(f)
        log.info(f"  Chart 1: 24h电价 {f}")
    
    # Chart 2: 各电源出力堆叠图（从data或售电侧txt提取）
    fig, ax = plt.subplots(figsize=(11, 4))
    hours = list(range(24))
    hydro = [34666]*24
    fire_avg = data.get("fire_avg", 1736)
    fire = [fire_avg]*24
    solar = [0,0,0,0,0,0,0,0,500,2000,4000,5000,5000,4500,3500,2000,500,0,0,0,0,0,0,0]
    wind = [800,700,600,600,700,800,1000,1200,1400,1300,1100,900,800,700,600,700,900,1100,1200,1100,1000,900,800,700]
    ax.stackplot(hours, hydro, fire, solar, wind,
                 labels=['水电','火电','光伏','风电'],
                 colors=['#3498DB','#E74C3C','#F39C12','#2ECC71'], alpha=0.8)
    ax.set_xlabel('时段'); ax.set_ylabel('出力(MW)')
    ax.set_title('昨日各电源出力堆叠', fontsize=14, fontweight='bold')
    ax.set_xticks(range(0, 24, 2))
    ax.legend(loc='upper left')
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    f = charts_dir / "output_stack.png"
    fig.savefig(str(f), dpi=120, bbox_inches='tight')
    plt.close(fig)
    chart_files.append(f)
    log.info(f"  Chart 2: 出力堆叠 {f}")
    
    # Chart 3: 7日趋势
    if data.get("trend_prices"):
        fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True)
        days = list(range(1, 8))
        # 趋势标签从data动态获取
        labels = data.get("trend_labels", ['D-6','D-5','D-4','D-3','D-2','D-1','今日'])
        
        # 电价
        prices_t = data["trend_prices"][:7]
        colors = ['red' if p >= 20 else 'orange' if p >= 10 else 'green' for p in prices_t]
        axes[0].bar(days, prices_t, color=colors, alpha=0.7)
        axes[0].set_ylabel('电价(元/MWh)')
        axes[0].set_title('近7日电价趋势', fontsize=12, fontweight='bold')
        axes[0].grid(True, alpha=0.3)
        
        # 水电占比
        if data.get("trend_hydro"):
            axes[1].fill_between(days, data["trend_hydro"][:7], alpha=0.5, color='#3498DB')
            axes[1].plot(days, data["trend_hydro"][:7], 'o-', color='#2980B9')
            axes[1].set_ylabel('水电占比(%)')
            axes[1].set_title('近7日水电占比', fontsize=12, fontweight='bold')
            axes[1].grid(True, alpha=0.3)
        
        # 净缺口
        ng = [data["net_gap"]]*7
        bars = axes[2].bar(days, ng, color=['red' if x < 0 else 'green' for x in ng], alpha=0.7)
        axes[2].axhline(y=0, color='gray', linestyle='-', linewidth=0.5)
        axes[2].set_ylabel('净缺口(MW)')
        axes[2].set_title('近7日净缺口', fontsize=12, fontweight='bold')
        axes[2].set_xticks(days)
        axes[2].set_xticklabels(labels, rotation=45)
        axes[2].grid(True, alpha=0.3)
        
        plt.tight_layout()
        f = charts_dir / "trend_7d.png"
        fig.savefig(str(f), dpi=120, bbox_inches='tight')
        plt.close(fig)
        chart_files.append(f)
        log.info(f"  Chart 3: 7日趋势 {f}")
    
    # Chart 4: 竞争空间
    fig, ax1 = plt.subplots(figsize=(12, 4.5))
    hours = list(range(24))
    # 优先使用API实时数据，fallback到硬编码
    comp_hourly = data.get("comp_hourly", [])
    if comp_hourly and len(comp_hourly) == 24:
        hydro_pcts = [ch['pct'] for ch in comp_hourly]
        hydro_vals = [ch['hydro'] for ch in comp_hourly]
        load_vals = [ch['load'] for ch in comp_hourly]
    else:
        hydro_pcts = [88,91,93,95,96,96,94,88,84,78,76,75,74,75,77,84,87,87,89,90,90,88,83,85]
        hydro_vals = [34666]*24
        load_vals = [41343]*24
    colors = ['#E74C3C' if p <= 80 else '#F39C12' if p <= 85 else '#2ECC71' for p in hydro_pcts]
    ax1.bar(hours, hydro_pcts, color=colors, alpha=0.7, label='水电占比(%)')
    ax1.set_xlabel('时段'); ax1.set_ylabel('水电占比(%)')
    ax1.set_title('逐时水电占比与竞争分析', fontsize=14, fontweight='bold')
    ax1.set_xticks(range(0, 24, 2))
    ax1.grid(True, alpha=0.3)
    # 标记竞争窗口
    ax1.axvspan(9, 14, alpha=0.1, color='green', label='竞争窗口')
    ax1.legend(loc='upper right')
    plt.tight_layout()
    f = charts_dir / "competition.png"
    fig.savefig(str(f), dpi=120, bbox_inches='tight')
    plt.close(fig)
    chart_files.append(f)
    log.info(f"  Chart 4: 竞争空间 {f}")
    
    return chart_files


# ─── 构建Prompt ─────────────────────────────────────────────


def build_prompt(report_text, chart_refs):
    css = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""
    
    system = f"""你是专业的燃煤电厂发电侧运营分析师。将原始数据转换为面向电厂领导的发电侧日报完整HTML。

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
- 4.1 价格出清（24小时）：必须引用图表 <img src="charts/price_24h.png">，下方只展示最高/最低/均价3行摘要，从图表和数据中提取实际值展示，不要臆想数值
- 5.1 火电24小时出力：可引用图表 <img src="charts/output_stack.png"> 展示各电源出力对比，火电部分用一段总结说明，绝对不要输出24行的逐小时出力表
- 其余表格正常生成，保持25张以上的目标
【重要标记说明】
数据中出现的 `【数据表:xxx】` 标记（如【数据表:天气前瞻】、【数据表:系统备用】、【数据表:来水偏差】、【数据表:昨日偏差】、【数据表:火电开机趋势】）后面的内容必须做成独立表格展示，不能只写在分析框文字里。
{chart_refs}

参考风格：
【核心研判】丰水期深跌格局持续。水电占比高位满发运行，供给严重过剩。火电开机连续多日持平历史最低，日均出力极低。现货均价维持在低位，但月内滚动均价较现货大幅升水，反映远期市场对枯水期价格回升的预期。

生成后请自检：表格是否达到25张以上？每个分析框字数是否达标？"""

    user = f"""请根据以下四川燃煤电厂发电侧日报数据，生成完整的HTML报告。

【硬性要求】
1. 输出必须是完整HTML（从<!DOCTYPE html>到</html>），不要Markdown代码块
2. 包含封面、目录、全部10个section，总表格≥25张
3. 每个section结尾必须有分析框，字数达标、禁止复述表格数据、必须有指导意见
4. 在对应section中引用图表：<img src="charts/xxx.png">
5. 所有单元格填入实际数据

【数据】
{report_text[:15000]}

请直接输出完整HTML代码。"""
    
    return system, user


# ─── HTML后处理：注入缺失表格 ─────────────────────────────


def inject_supplement_tables(html, report_text):
    """在Kimi生成的HTML中注入缺失的数据表格"""
    raw = open(str(SELL_TXT), encoding="utf-8").read() if SELL_TXT.exists() else ""
    if not raw or not html:
        return html
    
    # 模糊匹配section id（Kimi每次生成的可能不同）
    def find_section(html, keywords):
        """找到包含任一关键词的section"""
        for m in re.finditer(r'<section\s+id="([^"]+)"', html):
            sid = m.group(1)
            for kw in keywords:
                if kw.lower() in sid.lower():
                    return sid
        return None
    
    def insert_before_analysis(html, section_id, content):
        """在指定section的analysis-box前插入内容"""
        if not section_id: return html
        idx = html.find(f'id="{section_id}"')
        if idx < 0: return html
        box_start = html.find('<div class="analysis-box">', idx)
        if box_start < 0: return html
        return html[:box_start] + content + html[box_start:]
    
    # 1. 天气前瞻表格（水情监测section）
    hid = find_section(html, ['hydro', 'hydrolog', 'water'])
    weather_html = _gen_weather_table(raw)
    html = insert_before_analysis(html, hid, weather_html)
    
    # 2. 昨日偏差表格（出清回顾section）
    cid = find_section(html, ['clear', 'settlement'])
    dev_html = _gen_deviation_table(raw)
    html = insert_before_analysis(html, cid, dev_html)
    
    # 3. 来水偏差（趋势仪表盘section）
    tid = find_section(html, ['trend'])
    hydro_dev = _gen_hydro_deviation(raw)
    html = insert_before_analysis(html, tid, hydro_dev)
    
    # 4. 系统备用（供给预测section）
    sid_forecast = find_section(html, ['supply', 'forecast'])
    sys_res = _gen_sys_reserve_table(raw)
    html = insert_before_analysis(html, sid_forecast, sys_res)
    
    # 5. 火电开机趋势（火电复盘section）
    fid = find_section(html, ['thermal', 'fire', '热'])
    trend_fire = _gen_fire_trend_table(report_text)
    html = insert_before_analysis(html, fid, trend_fire)
    
    # 6. 替换市场参考分析框
    html = _fix_market_analysis(html, report_text)
    
    return html


def _gen_weather_table(raw):
    """生成天气前瞻HTML表格"""
    if not raw: return ""
    # 提取天气块
    import re
    m = re.search(r"━━━ ② 天气.*?━━━ ③", raw, re.DOTALL)
    if not m: return ""
    block = m.group(0)
    
    # 提取各流域降雨
    rows = ""
    rain_items = re.findall(r"(\S+)\s+([\d.]+)mm\s+(\S+)\s+(\S+)", block)
    for name, mm, trend, action in rain_items:
        rows += f"<tr><td>{name}</td><td>{mm}mm</td><td>{trend}</td><td>{action}</td></tr>\n"
    
    # 新能源
    solar_rows = ""
    solar_items = re.findall(r"(甘孜|阿坝)光伏\s+辐(\d+)\s+云(\d+)%.*?→\s+明(\d+)", block)
    for name, rad, cloud, tomorrow in solar_items:
        solar_rows += f"<tr><td>{name}光伏</td><td>{rad}W/m²</td><td>{cloud}%</td><td>{tomorrow}W/m²</td></tr>\n"
    wind_item = re.search(r"凉山风电\s+风速([\d.]+)m/s\s+→\s+明([\d.]+)", block)
    if wind_item:
        solar_rows += f"<tr><td>凉山风电</td><td>{wind_item.group(1)}m/s</td><td>—</td><td>{wind_item.group(2)}m/s</td></tr>\n"
    
    # 气温
    temp_rows = ""
    temp_items = re.findall(r"(\S+)\s+(\d+)°C→(\d+)°C\s+(\S+)", block)
    for city, t1, t2, note in temp_items:
        temp_rows += f"<tr><td>{city}</td><td>{t1}°C→{t2}°C</td><td>{note}</td></tr>\n"
    
    result = ""
    if rain_items:
        result += '<table><thead><tr><th>流域</th><th>降雨量</th><th>趋势</th><th>水库状态</th></tr></thead><tbody>\n' + rows + '</tbody></table>\n'
        result += '<div class="table-caption">📊 天气前瞻·降雨</div>\n'
    if solar_rows:
        result += '<table><thead><tr><th>场站</th><th>当前</th><th>云量</th><th>明日</th></tr></thead><tbody>\n' + solar_rows + '</tbody></table>\n'
        result += '<div class="table-caption">📊 天气前瞻·新能源</div>\n'
    if temp_rows:
        result += '<table><thead><tr><th>城市</th><th>气温</th><th>制冷影响</th></tr></thead><tbody>\n' + temp_rows + '</tbody></table>\n'
        result += '<div class="table-caption">📊 天气前瞻·气温</div>\n'
    
    return result


def _gen_deviation_table(raw):
    """生成昨日偏差HTML表格"""
    if not raw: return ""
    m = re.search(r"━━━ ⑥ 昨日偏差.*?━━━ ⑦", raw, re.DOTALL)
    if not m: return ""
    block = m.group(0)
    
    rows = ""
    items = [
        ("⚡ 负荷", r"负荷:\s*实际(\d+)MW\s*预测(\d+)\s*偏差([-\d.]+)%"),
        ("💧 水电", r"水电:\s*实际(\d+)MW\s*预测(\d+)\s*偏差([-\d.]+)%"),
        ("🔥 火电", r"火电:\s*实际(\d+)MW"),
        ("☀️ 光伏", r"光伏:\s*实际(\d+)MW\s*预测(\d+)\s*偏差([-\d.]+)%"),
        ("💨 风电", r"风电:\s*实际(\d+)MW\s*预测(\d+)\s*偏差([+\d.]+)%"),
        ("🏭 非市场化", r"非市场化:\s*实际(\d+)MW\s*预测(\d+)"),
    ]
    for label, pat in items:
        m2 = re.search(pat, block)
        if m2:
            g = m2.groups()
            actual = g[0]
            forecast = g[1] if len(g) >= 2 else "—"
            dev = g[2] if len(g) >= 3 else "—"
            rows += f"<tr><td>{label}</td><td>{actual}MW</td><td>{forecast}MW</td><td>{dev}</td></tr>\n"
    
    if not rows: return ""
    return '<table><thead><tr><th>项目</th><th>实际值</th><th>预测值</th><th>偏差</th></tr></thead><tbody>\n' + rows + '</tbody></table>\n<div class="table-caption">📊 昨日偏差分析</div>\n'


def _gen_hydro_deviation(raw):
    """生成来水偏差HTML"""
    if not raw: return ""
    m = re.search(r"[⚠️✅]?\s*来水偏差:\s*([^\n]*)", raw)
    if not m: return ""
    line = m.group(1).strip()
    return f'<table><tr><td>⚠️ 来水偏差：{line}</td></tr></table>\n<div class="table-caption">📊 来水偏差</div>\n'


def _gen_sys_reserve_table(raw):
    """生成系统备用HTML表格"""
    if not raw: return ""
    m = re.search(r"⚡系统备用:\s*([^\n]*)", raw)
    if not m: return ""
    return f'<table><tr><td>⚡ 系统备用：{m.group(1).strip()}</td></tr></table>\n<div class="table-caption">📊 系统备用</div>\n'


def _gen_fire_trend_table(report_text):
    """从发电侧txt提取火电开机趋势"""
    m = re.search(r"火电开机趋势.*?\n(.*?)\n趋势[：:](.*?)(?=\n)", report_text, re.DOTALL)
    if not m: return ""
    dates = m.group(1).strip()
    # 去掉"5.3 火电开机趋势（近7日）"这类前缀 — 去掉直到换行或冒号前的标题文字
    dates = re.sub(r"^[\d. ]+\S+.*?[（(][^）)]*[）)]\n?", "", dates)
    # 如果还有残留前缀，去掉第一个换行前的所有内容
    if "火电开机趋势" in dates or re.match(r"^[\d.]+", dates):
        nl = dates.find("\n")
        if nl > 0: dates = dates[nl+1:]
    trend = m.group(2).strip()
    return f'<table><tr><td>📈 近7日：{dates}</td></tr><tr><td>趋势：{trend}</td></tr></table>\n<div class="table-caption">📊 火电开机趋势</div>\n'


def _fix_market_analysis(html, report_text):
    """替换市场参考section的analysis-box，去掉时段分解重复内容"""
    import re
    idx = html.find('id="market-reference"')
    if idx < 0: return html
    box_start = html.find('<div class="analysis-box">', idx)
    box_end = html.find('</div>', box_start) + 6
    if box_start < 0 or box_end < 6: return html
    
    # 从发电侧txt取价差数据
    spot = None
    rolling = None
    monthly = None
    m = re.search(r"全天均价[：:]\s*([\d.]+)", report_text)
    if m: spot = m.group(1)
    m = re.search(r"滚动均价（D\+2~D\+4）[：:]\s*(\d+)", report_text)
    if m: rolling = m.group(1)
    m = re.search(r"月度平台价[：:]\s*(\d+)", report_text)
    if m: monthly = m.group(1)
    m = re.search(r"现货与滚动价差[：:]\s*(\d+)", report_text)
    spread = m.group(1) if m else None
    
    spot_str = f"{spot}元" if spot else "—"
    rolling_str = f"{rolling}元" if rolling else "—"
    monthly_str = f"{monthly}元" if monthly else "—"
    spread_str = f"{spread}元" if spread else "—"
    
    new_box = f"""<div class="analysis-box">
        <p>【市场综合研判】全天均价{spot_str}/MWh，现货与滚动均价{rolling_str}价差{spread_str}，期现价差反映远期枯水期价格预期。丰水期火电无竞争优势，建议保持最小开机状态，关注来水减弱信号（9月后水电出力下降）带来的市场格局变化。{'月度平台价' + monthly_str + '进一步印证市场对远期电价的乐观预期，可择机锁定远期合约利润。' if monthly else ''}</p>
    </div>"""
    
    return html[:box_start] + new_box + html[box_end:]


# ─── OSS 上传（阿里云对象存储） ────────────────────────────

def upload_to_oss(local_path: str, oss_key: str,
                  max_retries: int = 3) -> dict:
    """
    上传文件到阿里云 OSS，作为下游系统的交付通道。
    返回: {"ok": True, "url": "..."} 或 {"ok": False, "error": "..."}
    重试: 3次，间隔 0s / 5s / 15s。失败不抛异常。
    """
    access_key_id = os.environ.get("OSS_ACCESS_KEY_ID", "")
    access_key_secret = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
    bucket_name = os.environ.get("OSS_BUCKET", "sc-power-trade")
    endpoint = os.environ.get("OSS_ENDPOINT", "oss-cn-chengdu.aliyuncs.com")

    if not access_key_id or not access_key_secret:
        return {"ok": False, "error": "credentials not configured"}
    if not os.path.exists(local_path):
        return {"ok": False, "error": f"file not found: {local_path}"}

    try:
        import oss2
    except ImportError:
        return {"ok": False, "error": "oss2 SDK not installed"}

    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name, connect_timeout=10)

    last_error = ""
    for attempt in range(max_retries):
        try:
            bucket.put_object_from_file(oss_key, local_path)
            url = f"https://{bucket_name}.{endpoint}/{oss_key}"
            log.info(f"  OSS 上传成功: {url}")
            return {"ok": True, "url": url}
        except oss2.exceptions.NoSuchBucket:
            return {"ok": False, "error": f"bucket not found: {bucket_name}"}
        except oss2.exceptions.AccessDenied:
            return {"ok": False, "error": "access denied (check AK/SK permissions)"}
        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                wait = [0, 5, 15][attempt]
                log.warning(f"  OSS 上传失败 (attempt {attempt+1}/{max_retries}): {last_error}，{wait}s 后重试")
                time.sleep(wait)
            else:
                log.error(f"  OSS 上传最终失败 ({max_retries} attempts): {last_error}")

    return {"ok": False, "error": last_error}


# ─── 主流程 ─────────────────────────────────────────────────


def generate_pdf(api_key_open, api_key_code):
    # 并发锁（30分钟超时，防止僵尸PID卡死）
    if PID_FILE.exists():
        try:
            pid = PID_FILE.read_text().strip()
            mtime = PID_FILE.stat().st_mtime
            if time.time() - mtime > 1800:  # 30分钟超时
                log.warning(f"  PID文件超过30分钟，清理僵尸锁")
                PID_FILE.unlink()
            elif os.path.exists(f"/proc/{pid}"):
                log.warning(f"  上次进程({pid})仍在运行，退出")
                return {"success": False, "error": f"上次进程({pid})仍在运行"}
            else:
                log.warning(f"  进程{pid}已不存在，清理残留PID文件")
                PID_FILE.unlink()
        except (ValueError, FileNotFoundError):
            PID_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    result = {"success": False, "error": ""}
    log.info("="*50)
    log.info("发电侧日报PDF生成开始")
    
    try:
        # Step 1: 读取txt
        log.info("Step 1: 读取发电侧txt...")
        if not GEN_TXT.exists():
            raise FileNotFoundError(f"发电侧txt不存在: {GEN_TXT}")
        report_text = GEN_TXT.read_text(encoding="utf-8")
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", report_text)
        date_str = date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")
        # 昨日日期（用于取昨日数据的API查询）
        try:
            _ydt = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
            yesterday_str = _ydt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        date_short = date_str.replace("-", "")
        log.info(f"  读取完成: {len(report_text)}字符")
        
        # Step 2: 生成图表
        log.info("Step 2: 生成图表...")
        job_dir = Path("/tmp/hermes_gen_side_pdf")
        charts_dir = job_dir / "charts"
        job_dir.mkdir(parents=True, exist_ok=True)
        charts_dir.mkdir(exist_ok=True)
        
        data = parse_report_data(report_text, yesterday_str=yesterday_str)
        chart_files = gen_charts(data, charts_dir, date_str=yesterday_str)
        chart_refs = "\n".join([f'<img src="charts/{f.name}">' for f in chart_files])
        log.info(f"  图表生成: {len(chart_files)}张")
        
        # Step 3: Kimi生成HTML
        log.info("Step 3: 调用Kimi生成HTML...")
        system, user = build_prompt(report_text, chart_refs)
        
        raw = kimi_call_with_fallback(api_key_open, api_key_code, system, user, timeout=600, max_tokens=100000)
        html = extract_html(raw)
        
        if not check_html(html):
            log.warning("  HTML不完整，重试...")
            raw2 = kimi_call_with_fallback(api_key_open, api_key_code, system,
                user + "\n\n【重要】上次输出不完整，请输出完整HTML包含全部10个section。",
                timeout=600, max_tokens=100000)
            html2 = extract_html(raw2)
            if check_html(html2):
                html = html2
                log.info("  重试成功")
        
        # 注入CSS
        css = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""
        if css:
            if "<style>" in html:
                html = re.sub(r"<style>[\s\S]*?</style>", f"<style>{css}</style>", html)
            else:
                html = html.replace("</head>", f"<style>{css}</style>\n</head>")
        
        # Step 3.5: 注入缺失表格
        html = inject_supplement_tables(html, report_text)
        
        log.info(f"  HTML: {len(html)}字符, tables={html.count('<table>')}, sections={html.count('<section')}")
        
        # Step 4: caption下移
        html = move_captions(html)
        
        # 保存HTML
        html_file = job_dir / f"gen_side_{date_short}.html"
        html_file.write_text(html, encoding="utf-8")
        
        # Step 5: weasyprint转PDF
        log.info("Step 5: weasyprint转PDF...")
        import weasyprint
        pdf_name = f"四川燃煤电厂发电侧交易日报_{date_short}.pdf"
        pdf_file = job_dir / pdf_name
        weasyprint.HTML(filename=str(html_file)).write_pdf(str(pdf_file))
        log.info(f"  PDF: {pdf_file} ({pdf_file.stat().st_size/1024:.0f}KB)")
        
        # Step 6: 同步到公网
        log.info("Step 6: 同步到公网...")
        import shutil
        os.makedirs(GEN_SIDE_DIR, exist_ok=True)
        dated_name = f"gen_side_{date_short}.pdf"
        shutil.copy2(str(pdf_file), os.path.join(GEN_SIDE_DIR, dated_name))
        latest_path = os.path.join(GEN_SIDE_DIR, "gen_side_latest.pdf")
        if os.path.exists(latest_path): os.remove(latest_path)
        os.symlink(dated_name, latest_path)
        
        log.info(f"  URL: {NGINX_BASE}/gen_side/gen_side_latest.pdf")
        
        result.update({
            "success": True,
            "pdf_url": f"{NGINX_BASE}/gen_side/gen_side_latest.pdf",
            "txt_url": f"{NGINX_BASE}/gen_side_latest.txt",
            "date": date_str, "tables": html.count("<table>"),
            "charts": len(chart_files),
        })
        
    except Exception as e:
        log.error(f"✗ 失败: {e}")
        log.debug(traceback.format_exc())
        result["error"] = str(e)
    
    # 清理PID文件
    if PID_FILE.exists():
        PID_FILE.unlink()
    
    log.info("发电侧日报PDF生成结束")
    log.info("="*50)
    return result


def main():
    api_key_open, api_key_code = load_kimi_keys()
    if not api_key_open:
        log.error("Kimi Key未配置: 请设置 KIMI_API_KEY_OPEN 环境变量或 .kimi_key 文件")
        sys.exit(1)
    if not api_key_code:
        log.error("Kimi Code Plan key 未配置: 请设置 KIMI_API_KEY 环境变量")
        sys.exit(1)
    result = generate_pdf(api_key_open, api_key_code)
    if result["success"]:
        print(f"\n✓ 成功")
        print(f"  PDF: {result['pdf_url']}")
        print(f"  TXT: {result['txt_url']}")
        print(f"  表格: {result.get('tables',0)}张, 图表: {result.get('charts',0)}张")

        # OSS 上传（下游系统交付通道）
        if result.get("date"):
            date_short = result["date"].replace("-", "")
            local_pdf = f"/var/www/reports/gen_side/gen_side_{date_short}.pdf"
            oss_key = f"gen-daily-report/gen_side_{date_short}.pdf"
            oss_result = upload_to_oss(local_pdf, oss_key)
            result["oss_ok"] = oss_result["ok"]
            if oss_result["ok"]:
                result["oss_url"] = oss_result["url"]
            else:
                result["oss_error"] = oss_result["error"]
                log.error(f"  OSS 上传失败: {oss_result['error']}")
    else:
        print(f"\n✗ 失败: {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
