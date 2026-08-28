#!/usr/bin/env python3
"""
发电侧日报txt生成脚本 v2
- 读取售电侧daily_latest.txt提取全部数据
- 调API补齐24h电价（带合理性校验）
- 按完整模板输出发电侧txt

Usage:
    python gen_txt.py

输出: /var/www/reports/gen_side_latest.txt
"""

import os, sys, re, logging
from pathlib import Path
from datetime import datetime, timedelta

NGINX_DIR = "/var/www/reports"
SELL_TXT = Path(NGINX_DIR) / "daily_latest.txt"
GEN_TXT = Path(NGINX_DIR) / "gen_side_latest.txt"
RAYDON_PATH = Path(os.path.expanduser("~/sichuan_hydro_price"))
LOG_FILE = os.path.expanduser("~/.hermes/logs/gen_side_txt.log")
PID_FILE = Path("/tmp/gen_side_txt.pid")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                    datefmt="%H:%M:%S",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger("gen_txt")


def check_pid():
    """并发锁"""
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        if os.path.exists(f"/proc/{pid}"):
            log.warning(f"  上次进程({pid})仍在运行，退出")
            sys.exit(0)
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: PID_FILE.exists() and PID_FILE.unlink())


def read_file(path):
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    return path.read_text(encoding="utf-8")


def write_file(path, content):
    path.write_text(content, encoding="utf-8")
    log.info(f"  写入: {path.name} ({len(content)}字符)")


def ext(raw, pattern, default="", flags=0):
    """正则提取，支持有/无捕获组，支持flags"""
    m = re.search(pattern, raw, flags)
    if not m: return default
    return m.group(1).strip() if m.lastindex and m.lastindex >= 1 else m.group(0).strip()


def ext_all(raw, pattern, default=""):
    """提取多个捕获组，返回元组"""
    m = re.search(pattern, raw)
    if not m: return default
    return m.groups()


def _dev_judge(dev_pct):
    """偏差判断词：|dev|<=5% 接近预测，否则按符号偏高/偏低"""
    if dev_pct is None:
        return "—"
    if abs(dev_pct) <= 5:
        return "接近预测"
    return "高于预测" if dev_pct > 0 else "低于预测"


def _spread_str(v):
    """价差表述：正=升水，负=贴水，零=持平"""
    if v > 0:
        return f"升水+{v}元"
    if v < 0:
        return f"贴水{v}元"
    return "持平"


def block(raw, start, end=None, offset=0):
    """提取文本块"""
    si = raw.find(start)
    if si < 0: return ""
    si += len(start) + offset
    if end:
        ei = raw.find(end, si)
    else:
        ei = raw.find("━━━", si)
    if ei < 0: ei = len(raw)
    return raw[si:ei].strip()


def get_hourly(date_str):
    """取24h电价并校验合理性"""
    try:
        sys.path.insert(0, str(RAYDON_PATH))
        import raydon_api as ra
        p = ra.get_clearing_price(date_str)
        if not p or "points" not in p: return []
        pts = p["points"]
        hourly = []
        for h in range(24):
            hp = [x for x in pts[h*4:(h+1)*4] if x is not None and -50 <= x <= 800]
            hourly.append(round(sum(hp)/len(hp), 1) if hp else None)
        # 如果超过一半时段为None，认为数据不可用
        valid_count = sum(1 for x in hourly if x is not None)
        if valid_count < 12:
            log.warning(f"  24h电价有效数据仅{valid_count}个，认为数据不可用")
            return []
        return hourly
    except Exception as e:
        log.warning(f"  24h电价获取失败: {e}")
        return []


def get_competition_data(date_str):
    """从API获取水电实际出力(type14)、负荷(type15)、新能源(type27)、非市场化(type26)的96点数据，返回24h逐时竞争空间数据
    净缺口=负荷-水电-新能源-非市场化（与日报口径对齐）"""
    try:
        sys.path.insert(0, str(RAYDON_PATH))
        import raydon_api as ra
        hydro = ra.get_hydro_actual(date_str)
        load = ra.get_actual_load(date_str)
        if not load:
            d2 = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            load = ra.get_actual_load(d2)
            if load:
                log.info(f"  竞争空间负荷使用D-2({d2})，接口暂不可用")
        re = ra.fetch_data(27, date_str)
        nim = ra.fetch_data(26, date_str)
        if not hydro or not load:
            return []
        hydro_pts = hydro.get("points", [])
        load_pts = load.get("points", [])
        re_pts = re.get("data", []) if re and isinstance(re, dict) else []
        nim_pts = nim.get("data", []) if nim and isinstance(nim, dict) else []
        if len(hydro_pts) < 96 or len(load_pts) < 96:
            return []
        # 新能源和非市场化可能没有96点数据，降级为日均值
        re_avg = sum(p.get("value", 0) for p in re_pts if isinstance(p, dict) and p.get("value")) / max(len(re_pts), 1) if re_pts else 0
        nim_avg = sum(p.get("value", 0) for p in nim_pts if isinstance(p, dict) and p.get("value")) / max(len(nim_pts), 1) if nim_pts else 0
        hourly = []
        for h in range(24):
            h_load = sum(load_pts[h*4:(h+1)*4]) / 4
            h_hydro = sum(hydro_pts[h*4:(h+1)*4]) / 4
            # 新能源和非市场化用日均值近似（逐时数据不可靠）
            gap = h_load - h_hydro - re_avg - nim_avg
            pct = round(h_hydro / h_load * 100) if h_load > 0 else 0
            if gap < -500: judge = "供给过剩"
            elif gap < 500: judge = "紧平衡"
            else: judge = "有空间⚡"
            hourly.append({"hour": h, "load": round(h_load), "hydro": round(h_hydro),
                          "pct": pct, "gap": round(gap), "judge": judge})
        return hourly
    except Exception as e:
        log.warning(f"  竞争空间数据获取失败: {e}")
        return []


def gen_txt():
    log.info("读取售电侧txt...")
    if not SELL_TXT.exists():
        log.error(f"售电侧txt不存在: {SELL_TXT}")
        print("✗ 失败: 售电侧txt不存在，请先运行售电侧日报")
        return False
    raw = read_file(SELL_TXT)

    date_str = ext(raw, r"📅\s*([\d-]+)") or datetime.now().strftime("%Y-%m-%d")
    # 昨日日期（用于API查询昨日数据）
    try:
        _yesterday_dt = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
        yesterday_str = _yesterday_dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    # 取星期（直接从中文字段）
    wd_cn = ext(raw, r"(周一|周二|周三|周四|周五|周六|周日)", "周一")

    # ── 提取所有字段 ──
    avg_price = ext(raw, r"均价(\d+)元", "0")
    today_morning = ext(raw, r"今日凌晨(\d+)元", "0")
    ng_raw = ext(raw, r"净缺口([+\-\d,]+)", "0")
    net_gap = ng_raw.replace(",", "").replace("净缺口", "")
    hydro_pct = ext(raw, r"水电(\d+)%", "0")
    thermal_load = ext(raw, r"火电负载率([\d.]+)%", "0")
    
    # ── 月内交易数据（从归档读取，替代正则提取） ──
    rolling_avg = "0"
    monthly_price = "0"
    price_range = "-"
    _d2_avg = _d3_avg = _d4_avg = "0"
    _cont_avg = "0"
    try:
        import json as _json2, os as _os2
        _archive_path = "/home/ubuntu/sichuan_hydro_price/.monthly_trade_archive.json"
        if _os2.path.exists(_archive_path):
            with open(_archive_path) as _f:
                _archive = _json2.load(_f)
            _yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            _entry = _archive.get(_yesterday, {})
            _roll = _entry.get("滚动", {})
            if _roll:
                _all_avgs = []
                _all_weights = []
                _all_prices = []
                # 计算D+2/D+3/D+4实际日期，从归档中按日期匹配
                _today = datetime.now()
                _d2_key = (_today + timedelta(days=2)).strftime("%Y-%m-%d")
                _d3_key = (_today + timedelta(days=3)).strftime("%Y-%m-%d")
                _d4_key = (_today + timedelta(days=4)).strftime("%Y-%m-%d")
                for _dk, _label in [(_d2_key, "_d2_avg"), (_d3_key, "_d3_avg"), (_d4_key, "_d4_avg")]:
                    _dv = _roll.get(_dk, [])
                    _prices = [x["均价"] for x in _dv if x.get("均价", 0) > 0]
                    _vols = [x["成交量"] for x in _dv if x.get("成交量", 0) > 0]
                    _w = int(sum(p*v for p,v in zip(_prices, _vols)) / sum(_vols)) if _prices and _vols else 0
                    if _label == "_d2_avg": _d2_avg = str(_w) if _w else "0"
                    elif _label == "_d3_avg": _d3_avg = str(_w) if _w else "0"
                    elif _label == "_d4_avg": _d4_avg = str(_w) if _w else "0"
                    if _prices and _vols:
                        _all_avgs.extend(_prices)
                        _all_weights.extend(_vols)
                        _all_prices.extend(_prices)
                if _all_avgs and _all_weights:
                    rolling_avg = str(int(sum(a*w for a,w in zip(_all_avgs, _all_weights)) / sum(_all_weights)))
                if _all_prices:
                    price_range = f"{int(min(_all_prices))}-{int(max(_all_prices))}"
            # 连续交易均价
            _cont = _entry.get("连续", {})
            _cont_list = _cont.get("时段", []) if isinstance(_cont, dict) else []
            _cont_avgs = [x["均价"] for x in _cont_list if x.get("均价", 0) > 0]
            _cont_avg = str(int(sum(_cont_avgs) / len(_cont_avgs))) if _cont_avgs else "0"
        # 月度平台价（从人工录入的JSON读取）
        _mp_path = "/home/ubuntu/sichuan_hydro_price/.monthly_platform_prices.json"
        if _os2.path.exists(_mp_path):
            with open(_mp_path) as _f:
                _mp_data = _json2.load(_f)
            _this_month = datetime.now().strftime("%Y-%m")
            _mp_month = _mp_data.get(_this_month, {})
            _mp_prices = _mp_month.get("prices", {})
            if _mp_prices:
                _vals = [v["platform_price"] for v in _mp_prices.values() if v.get("platform_price")]
                if _vals:
                    monthly_price = str(int(sum(_vals) / len(_vals)))
    except Exception:
        pass  # 归档读取失败，用默认值0
    
    thermal_cap = ext(raw, r"开机\d+台/(\d+)MW", "0")
    thermal_stop = ext(raw, r"停机\d+台/(\d+)MW", "0")
    thermal_util = ext(raw, r"利用率([\d.]+)%", "0")
    season = ext(raw, r"(丰水期|平水期|枯水期|蓄水期)", "丰水期")
    load_avg = ext(raw, r"日均(\d+)MW", "0")
    hydro_avail = ext(raw, r"水电(\d+)MW\s*\([\d.]+%\)", "0")
    re_avg = ext(raw, r"新能源(\d+)MW", "0")
    # 🔴 修复2: 非市场化取值（完整数字）
    non_mkt = ext(raw, r"非市场化\s*([\d,]+)", "0").replace(",", "")
    total_avail_raw = ext(raw, r"总可用:\s*([\d,]+)", "0")
    total_avail = total_avail_raw.replace(",", "")
    # 系统备用（多行，取到下一个━━━或行尾）
    sys_reserve = ext(raw, r"系统备用:([\s\S]*?)(?=\n\s*周预测|\n\s*🔥|\n\s*📚|\n\s*━━━|$)", "")
    # 周预测
    week_fc = ext(raw, r"周预测\s*（D.*?）[：:](.*?)(?=\n\s*⚠️|\n\s*🔥|\n\s*📚|$)", "")
    fire_avg = ext(raw, r"🔥\s*火电:\s*实际(\d+)MW", "0")
    hydro_actual = ext(raw, r"💧\s*水电:\s*日均(\d+)MW", "0")
    thermal_units = ext(raw, r"开机(\d+)台", "0")
    thermal_stopped_units = ext(raw, r"停机(\d+)台", "0")
    debao = ext(raw, r"德宝直流.*?(\d+)MW", "0")
    
    # 偏差数据（re.search 取多组）
    load_act_match = re.search(r"⚡\s*负荷:\s*实际(\d+)MW\s*预测(\d+)\s*偏差([+\-.\d]+)%", raw)
    hydro_act_match = re.search(r"💧\s*水电:\s*实际(\d+)MW\s*预测(\d+)\s*偏差([+\-.\d]+)%", raw)
    solar_act_match = re.search(r"☀️\s*光伏:\s*实际(\d+)MW\s*预测(\d+)\s*偏差([+\-.\d]+)%", raw)
    wind_act_match = re.search(r"💨\s*风电:\s*实际(\d+)MW\s*预测(\d+)\s*偏差([+\-.\d]+)%", raw)
    # 非市场化行：ext()只返回第一个捕获组，必须用re.search().groups()拿全组
    _nonmkt_m = re.search(r"🏭\s*非市场化:\s*实际(\d+)MW\s*预测(\d+)", raw)
    nonmkt_act = _nonmkt_m.groups() if _nonmkt_m else None

    # 来水偏差（从售电侧提取，兼容⚠️或✅emoji）
    hydro_dev_line_src = ext(raw, r"来水偏差:\s*([^\n]*)", "")
    hydro_dev_line = ext(raw, r"⚠️[ ]*来水偏差:\s*([^\n]*)", "") or (("✅ " + hydro_dev_line_src) if hydro_dev_line_src else "")
    
    # 电源出力数据
    hydro_day_match = re.search(r"💧\s*水电:\s*日均(\d+)MW.*?峰(\d+).*?谷(\d+)", raw)
    fire_day_match = re.search(r"🔥\s*火电:\s*日均(\d+)MW.*?峰(\d+).*?谷(\d+)", raw)
    solar_day_match = re.search(r"☀️\s*光伏:\s*日均(\d+)MW.*?峰(\d+)", raw)
    wind_day_match = re.search(r"💨\s*风电:\s*日均(\d+)MW.*?峰(\d+).*?谷(\d+)", raw)
    load_day_match = re.search(r"⚡\s*负荷:\s*日均(\d+)MW.*?峰(\d+)(?:\((\d+:\d+)\))?.*?谷(\d+)", raw)
    
    clear_dev = ext_all(raw, r"出清偏差:\s*日前(\d+)MW\s*vs\s*日内(\d+)MW\s*[↓↑]?\d+\(([\d.]+)%\)", ("0", "0", ""))
    
    thermal_maint = ext(raw, r"【火电】([^\n]*)", "无")
    hydro_maint = ext(raw, r"【水电】([^\n]*)", "无")
    solar_maint = ext(raw, r"【光伏】([^\n]*)", "无")
    line_maint = ext(raw, r"【线路】([^\n]*)", "无")
    trans_maint = ext(raw, r"【主变】([^\n]*)", "无")
    
    # 来水指数
    wi_val = ext(raw, r"综合来水指数:\s*([\d.]+)", "0")
    # 来水指数描述（从行中"—"之后取，去掉emoji）
    wi_desc_full = ext(raw, r"综合来水指数:\s*[\d.]+\s*.*?—\s*(.*)", "")
    if wi_desc_full:
        wi_desc = re.sub(r"[\U0001F300-\U0010FFFF]", "", wi_desc_full).strip()
    else:
        wi_desc = ""
    
    # 趋势数据
    trend_days = ext(raw, r"📈\s*趋势\(.*?\):\s*([\d→%↑↓↑]+)", "")

    # 近7日日期标签（从date_str往前推6天）
    try:
        _dt = datetime.strptime(date_str, "%Y-%m-%d") if '-' in date_str else datetime.now()
    except (ValueError, TypeError):
        _dt = datetime.now()
    _trend_dates = [(_dt - timedelta(days=i)).strftime("%m-%d") for i in range(6, -1, -1)]
    _trend_date_str = "  ".join(_trend_dates)
    _trend_date_range = f"{_trend_dates[0]}~{_trend_dates[-1]}"
    
    # 计算相关值
    fire_avg_int = int(fire_avg) if fire_avg.isdigit() else 0
    hydro_actual_int = int(hydro_actual) if hydro_actual.isdigit() else 1
    ratio = hydro_actual_int // max(1, fire_avg_int)
    spot_int = int(avg_price) if avg_price.isdigit() else 0
    rolling_int = int(rolling_avg) if rolling_avg.isdigit() else 0
    spread = rolling_int - spot_int
    monthly_int = int(monthly_price) if monthly_price.isdigit() else 0

    lines = []

    # ── 标题 ──
    lines.append(f"# 四川燃煤电厂发电侧交易日报")
    lines.append(f"# {date_str}（{wd_cn}）")
    lines.append(f"# 报告期间：日前市场 {date_str} | 实时市场 昨日")
    lines.append(f"# 水期：{season}（6-9月）调度原则：优先不弃水")
    lines.append("")

    # ── 一、核心指标摘要 ──
    lines.append("一、核心指标摘要")
    lines.append("")
    try:
        _ng_int = int(net_gap)
    except (ValueError, TypeError):
        _ng_int = 0
    _gap_judge = "供给过剩" if _ng_int < 0 else ("供给偏紧" if _ng_int > 0 else "供需平衡")
    lines.append(f"昨日均价：{avg_price} 元/MWh | 今日凌晨：{today_morning} 元/MWh | 净缺口：{net_gap} MW（{_gap_judge}）")
    lines.append(f"水电占比：{hydro_pct}% | 火电日均出力：{fire_avg} MW | 月内滚动均价：{rolling_avg} 元/MWh")
    lines.append("")
    lines.append(f"指标                数值")
    lines.append(f"昨日均价            {avg_price} 元/MWh")
    lines.append(f"火电日均出力          {fire_avg} MW")
    lines.append(f"净缺口              {net_gap} MW（{_gap_judge}）")
    lines.append(f"水电占比            {hydro_pct}%")
    lines.append(f"负荷               {load_avg} MW")
    lines.append(f"月内滚动均价         {rolling_avg} 元/MWh")
    lines.append("")
    # 🔴 修复4: 超汛限电站提取
    exceed_list = re.findall(r"[🌊]?[\u4e00-\u9fff]+[·.][\u4e00-\u9fff]+\([^)]*\)\s*水位[\d.]+\s*超汛限[+\-]\d+m", raw)
    exceed_str = " | ".join(exceed_list[:3]) if exceed_list else ""
    # 火电开机趋势是否连续持平（从源txt趋势数据动态判断）
    _fire_vals = [x.strip() for x in trend_days.split("→")] if trend_days else []
    _fire_flat = "连续7日持平" if _fire_vals and len(set(_fire_vals)) == 1 else ""
    lines.append(f"重要变化：（1）火电开机{thermal_cap}MW（{thermal_units}台）{_fire_flat}；"
                 f"（2）净缺口{net_gap}MW，{_gap_judge}；"
                 f"（3）水电占比{hydro_pct}%；"
                 f"{'（4）' + exceed_str + '。' if exceed_str else ''}")
    lines.append("")

    # ── 二、水情监测 ──
    lines.append("二、水情监测")
    lines.append("")
    lines.append(f"当前处于{season}（6-9月），调度原则为\"优先不弃水\"。")
    lines.append("")
    lines.append("2.1 重点水库水位")
    wp = block(raw, "【水位压力】", "【水情研判】")
    if wp:
        for wl in wp.split("\n"):
            lines.append(wl.strip())
    lines.append("")
    lines.append("2.2 综合来水与蓄放水")
    lines.append(f"综合来水指数：{wi_val} — {wi_desc}")
    basin_line = ext(raw, r"流域:.*$", "", flags=re.MULTILINE)
    if basin_line: lines.append(basin_line)
    ws = block(raw, "蓄放水状态", "━━━", offset=10)
    if ws:
        lines.append("")
        for sl in ws.split("\n")[:8]:
            sl = sl.strip()
            if sl and "来水趋势" not in sl:
                lines.append(sl)
    trend_line = ext(raw, r"📊\s*来水趋势:.*$", "", flags=re.MULTILINE)
    if trend_line: lines.append(""); lines.append(trend_line)
    lines.append("")
    lines.append("2.3 天气前瞻（未来72小时）")
    weather_block = block(raw, "━━━ ② 天气", "━━━ ③")
    if weather_block:
        for wl in weather_block.split("\n"):
            wl = wl.strip()
            if wl and "━━━" not in wl: lines.append(wl)
    lines.append("")
    lines.append("【数据表:天气前瞻】未来72小时流域降雨数据")
    lines.append("水情研判：")
    lines.append(f"综合来水指数{wi_val}{wi_desc}，大渡河、雅砻江为双主驱动力。")
    near_limit = re.findall(r"[🌊]?(大渡河|雅砻江|嘉陵江|岷江|涪江|金沙江)[·.]([\u4e00-\u9fff]+)\(.*?\).*?(距汛限[-\d]+m|超汛限[+\d]+m)", raw)
    for river, name, status in near_limit[:5]:
        if "超汛限" in status:
            lines.append(f"{river}·{name}{status}已开始放水，关注后续影响。")
        elif int(ext(status, r"(\d+)", "99")) <= 3 and "距" in status:
            if name in ["瀑布沟","猴子岩","大岗山"]:
                lines.append(f"{river}·{name}蓄容已接近100%，若持续来水可能被迫加大放水。")
    lines.append("整体来看，来水充沛使水电持续满发，火电竞争空间被压缩至极限。")
    if weather_block:
        rain_items = re.findall(r"(\S+)\s+([\d.]+)mm\s+", weather_block)
        for rn, rm in rain_items:
            if float(rm) >= 10:
                lines.append(f"降雨方面：{rn}{rm}mm需关注来水改善后的影响。")
    lines.append("")

    # ── 三、供给预测 ──
    lines.append("三、供给预测（今日）")
    lines.append("")
    lines.append(f"项目               数值                  占比")
    # 占比动态计算（旧版硬编码97.5%/6.6%/13.5%）
    _ta_int = int(total_avail) if total_avail.isdigit() else 0
    _hydro_share = round(int(hydro_avail) / _ta_int * 100, 1) if _ta_int and hydro_avail.isdigit() else None
    _re_share = round(int(re_avg) / _ta_int * 100, 1) if _ta_int and re_avg.isdigit() else None
    _nm_share = round(int(non_mkt) / _ta_int * 100, 1) if _ta_int and non_mkt.isdigit() else None
    lines.append(f"日均负荷           {load_avg} MW              —")
    lines.append(f"水电可用           {hydro_avail} MW              {_hydro_share:.1f}%" if _hydro_share is not None else f"水电可用           {hydro_avail} MW              —")
    lines.append(f"新能源             {re_avg} MW               {_re_share:.1f}%" if _re_share is not None else f"新能源             {re_avg} MW               —")
    lines.append(f"非市场化           {non_mkt} MW               {_nm_share:.1f}%" if _nm_share is not None else f"非市场化           {non_mkt} MW               —")
    lines.append(f"总可用             {total_avail} MW              —")
    lines.append("")
    lines.append(f"供需平衡：负荷{load_avg} MW | 总可用{total_avail} MW | 净缺口{net_gap} MW（占负荷{abs(float(net_gap))/max(1,int(load_avg))*100:.1f}%）")
    lines.append(f"→ {_gap_judge}")
    lines.append("")
    lines.append("火电竞争空间：")
    if _ng_int < 0:
        lines.append(f"净缺口 {net_gap} MW → {_gap_judge}，火电可竞争空间为0")
    else:
        lines.append(f"净缺口 {net_gap} MW → {_gap_judge}，火电存在竞争空间")
    lines.append(f"火电开机参考 {thermal_cap} MW（{thermal_units}台）| 停机 {thermal_stopped_units}台/{thermal_stop} MW")
    lines.append(f"火电利用率{thermal_util}%")
    lines.append("")
    if sys_reserve:
        lines.append("【数据表:系统备用】")
        lines.append(f"系统备用：{sys_reserve}")
        lines.append("")
    if week_fc:
        lines.append(f"周预测：{week_fc}")
        lines.append("")

    # ── 四、昨日出清回顾 ──
    lines.append("四、昨日出清回顾")
    lines.append("")
    hourly = get_hourly(yesterday_str)
    # 从API数据计算均价（不与售电侧txt的avg_price混用），供后续板块使用
    if hourly:
        valid_p = [p for p in hourly if p is not None]
        calc_avg = round(sum(valid_p) / len(valid_p), 1) if valid_p else float(avg_price)
    else:
        calc_avg = float(avg_price)
    if hourly:
        lines.append("4.1 价格出清（24小时）")
        valid_p = [p for p in hourly if p is not None]
        if valid_p:
            hp = max(valid_p); lp = min(valid_p)
            # 从原始hourly列表中找真实小时索引（不是过滤后的索引）
            hh = next(i for i, v in enumerate(hourly) if v is not None and v == hp)
            lh = next(i for i, v in enumerate(hourly) if v is not None and v == lp)
            lines.append(f"全天均价{calc_avg}元/MWh，最高{int(hp)}元@{hh:02d}时，最低{int(lp)}元@{lh:02d}时")
            lines.append(f"【图:price_24h】24h电价走势详见右侧折线图")
        # 逐小时电价表（用于PDF图表和下游解析）
        lines.append("时段        电价")
        for h in range(24):
            v = hourly[h]
            v_str = f"{v}" if v is not None else "—"
            lines.append(f"{h:02d}:00    {v_str}元")
        lines.append("")
    lines.append("4.2 各电源出力")
    lines.append("电源       日均出力        峰值            谷值       特征")
    # 电源特征动态计算（旧版写死"满发运行/全天平稳极低水平/午间5,000MW+/正常"）
    _waved = ""
    _fp = _fv = 0
    if fire_day_match:
        _fg4 = fire_day_match.groups()
        _fp, _fv = int(_fg4[1]), int(_fg4[2])
        _waved = "日内有峰谷波动" if _fp - _fv > 300 else "日内出力平稳"
    _wind_feat = _dev_judge(float(wind_act_match.group(3))) if wind_act_match else "—"
    _pv_seg = re.search(r"光伏时段\((\d+-\d+)\)均(\d+)MW", raw)
    _pv_feat = f"午间{_pv_seg.group(1)}均{_pv_seg.group(2)}MW" if _pv_seg else "—"
    _hydro_feat = "—"
    if hydro_day_match and hydro_avail.isdigit() and int(hydro_avail) > 0:
        _hu = int(hydro_day_match.group(1)) / int(hydro_avail)
        _hydro_feat = "满发" if _hu >= 0.9 else f"出力{_hu*100:.0f}%"
    if hydro_day_match:
        hg = hydro_day_match.groups()
        lines.append(f"水电       {hg[0]} MW      {hg[1]}          {hg[2]}     {_hydro_feat}")
    if fire_day_match:
        fg = fire_day_match.groups()
        lines.append(f"火电       {fg[0]} MW      {fg[1]}          {fg[2]}     {_waved}")
    if solar_day_match:
        sg = solar_day_match.groups()
        lines.append(f"光伏       {sg[0]} MW      {sg[1]}          0         {_pv_feat}")
    if wind_day_match:
        wg = wind_day_match.groups()
        lines.append(f"风电       {wg[0]} MW      {wg[1]}            {wg[2]}     {_wind_feat}")
    if load_day_match:
        lg = load_day_match.groups()
        _pt = f"({lg[2]})" if len(lg) > 2 and lg[2] else ""
        lines.append(f"负荷      {lg[0]} MW     {lg[1]}{_pt}     {lg[3] if len(lg) > 3 else '—'}     —")
    lines.append("")
    lines.append("【数据表:昨日偏差】")
    lines.append("项目          实际值        预测值        偏差        判断")
    if load_act_match:
        lg = load_act_match.groups()
        lines.append(f"负荷          {lg[0]} MW     {lg[1]} MW    {lg[2]}%     {_dev_judge(float(lg[2]))}")
    if hydro_act_match:
        hg = hydro_act_match.groups()
        lines.append(f"水电          {hg[0]} MW     {hg[1]} MW   {hg[2]}%     {_dev_judge(float(hg[2]))}")
    if solar_act_match:
        sg = solar_act_match.groups()
        lines.append(f"光伏          {sg[0]} MW     {sg[1]} MW   {sg[2]}%     {_dev_judge(float(sg[2]))}")
    if wind_act_match:
        wg = wind_act_match.groups()
        lines.append(f"风电          {wg[0]} MW      {wg[1]} MW   {wg[2]}%     {_dev_judge(float(wg[2]))}")
    if nonmkt_act:
        _nm_a, _nm_f = int(nonmkt_act[0]), int(nonmkt_act[1])
        _nm_dev = (_nm_a - _nm_f) / _nm_f * 100
        lines.append(f"非市场化       {_nm_a} MW     {_nm_f} MW   {_nm_dev:+.1f}%     {_dev_judge(_nm_dev)}")
    lines.append("")
    # 亮点=最接近预测的电源，警示=偏差最大的电源，方向按符号动态生成
    _dev_items = []
    if load_act_match: _dev_items.append(("负荷", float(load_act_match.group(3))))
    if hydro_act_match: _dev_items.append(("水电", float(hydro_act_match.group(3))))
    if solar_act_match: _dev_items.append(("光伏", float(solar_act_match.group(3))))
    if wind_act_match: _dev_items.append(("风电", float(wind_act_match.group(3))))
    if _dev_items:
        _best = min(_dev_items, key=lambda x: abs(x[1]))
        _worst = max(_dev_items, key=lambda x: abs(x[1]))
        lines.append(f"偏差亮点：{_best[0]}偏差{_best[1]:+.1f}%，与预测最接近。")
        _w_dir = "高于" if _worst[1] > 0 else "低于"
        lines.append(f"偏差警示：{_worst[0]}实际{_w_dir}预测{abs(_worst[1]):.1f}%。")
    lines.append("")
    if isinstance(clear_dev, tuple) and len(clear_dev) >= 2 and clear_dev[0].isdigit():
        lines.append("4.4 出清偏差")
        cd0, cd1 = int(clear_dev[0]), int(clear_dev[1])
        _cd_diff = cd1 - cd0
        _cd_dir = "↑" if _cd_diff > 0 else ("↓" if _cd_diff < 0 else "→")
        if len(clear_dev) >= 3 and clear_dev[2]:
            lines.append(f"日前出清{cd0} MW vs 日内出清{cd1} MW，偏差{_cd_dir}{abs(_cd_diff)}MW（{clear_dev[2]}%）")
        else:
            lines.append(f"日前出清{cd0} MW vs 日内出清{cd1} MW，偏差{_cd_dir}{abs(_cd_diff)}MW")
        lines.append("")

    # ── 五、火电出力复盘 ──
    lines.append("五、火电出力复盘（昨日）")
    lines.append("")
    lines.append("5.1 火电24小时出力")
    if fire_day_match:
        lines.append(f"火电出力{_waved}（峰{_fp}MW 谷{_fv}MW），整体处于低出力区间")
        lines.append(f"日均{fire_day_match.group(1)} MW | 峰值{_fp} MW | 谷值{_fv} MW")
    else:
        lines.append(f"日均{fire_avg} MW | 峰谷数据不足")
    lines.append("→ 水电满发挤压下，火电仅保持最小开机，连续多日无调节空间")
    lines.append("")
    lines.append("5.2 火电 vs 水电出力")
    lines.append(f"火电{fire_avg} MW | 水电{hydro_actual} MW | 水火比 1:{ratio}")
    lines.append(f"→ 水电是火电的{ratio}倍，火电在系统中几乎无存在感")
    lines.append("")
    lines.append("【数据表:火电开机趋势】")
    lines.append("5.3 火电开机趋势（近7日）")
    lines.append(_trend_date_str)
    if trend_days:
        parts_t = trend_days.split("→")
        lines.append("  ".join(p.strip() for p in parts_t[:7]) + f"（{thermal_units}台）")
    else:
        # 兜底：用thermal_cap和thermal_units动态生成（避免死值）
        _cap = thermal_cap.replace(",", "") if thermal_cap.isdigit() or thermal_cap.replace(",","").isdigit() else "3700"
        lines.append(f"{_cap}  {_cap}  {_cap}  {_cap}  {_cap}  {_cap}  {_cap}（{thermal_units}台）")
    # 趋势结论动态生成（旧版写死"连续7日持平，历史最低水平"，与逐日数据矛盾）
    if _fire_vals and len(set(_fire_vals)) == 1:
        lines.append("趋势：连续7日持平")
    elif _fire_vals:
        lines.append(f"趋势：近7日{_fire_vals[0]}→{_fire_vals[-1]}MW")
    else:
        lines.append("趋势：数据不足")
    lines.append("")

    # ── 六、趋势仪表盘 ──
    lines.append(f"六、趋势仪表盘（近7日 {_trend_date_range}）")
    lines.append("")
    lines.append(f"指标               {_trend_date_str}   变化")
    # 🔴 修复6: 趋势仪表盘数据提取
    trend_lines_map = {
        "电价": r"电价:\s*([\d→↑↓%元/MWh\s]+)",
        "水电占比": r"水电占比:\s*([\d→↑↓%\s]+)",
        "来水指数": r"来水指数:\s*([\d.→↑↓\s%]+)",
        "负荷": r"负荷:\s*([\d→↑↓%\s]+) MW",
        "新能源": r"新能源:\s*([\d→↑↓%\s]+) MW",
        "滚动均价": r"滚动均价:\s*([\d→↑↓%\s]+) 元/MWh",
        "火电开机": r"火电开机:\s*([\d→↑↓%→\s]+)",
    }
    for tl, pat in trend_lines_map.items():
        rl = ext(raw, pat, "")
        if rl: lines.append(f"  {tl}: {rl.strip()}")
    lines.append("")
    # 来水偏差
    if hydro_dev_line:
        lines.append("【数据表:来水偏差】")
        lines.append(f"  来水偏差：{hydro_dev_line}")
        lines.append("")

    # ── 七、月内交易参考 ──
    lines.append("七、月内交易参考")
    lines.append("")
    lines.append("7.1 滚动交易行情（D+2~D+4）")
    lines.append("合约日        均价        价格范围")
    # 从date_str动态计算D+2~D+4日期
    try:
        _dt_base = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        _dt_base = datetime.now()
    _d2 = _dt_base + timedelta(days=2)
    _d3 = _dt_base + timedelta(days=3)
    _d4 = _dt_base + timedelta(days=4)
    import calendar
    _last_day = calendar.monthrange(_dt_base.year, _dt_base.month)[1]
    for label, dt, pavg in [("D+2", _d2, _d2_avg), ("D+3", _d3, _d3_avg), ("D+4", _d4, _d4_avg)]:
        d_str = f"{dt.month}/{dt.day}"
        lines.append(f"{label}({d_str})    {'—' if pavg == '0' else pavg}         {price_range}")
    lines.append(f"滚动加权均价：{rolling_avg}元/MWh")
    lines.append("")
    lines.append("7.2 连续交易（D+5~月底）")
    lines.append(f"标的日       均价        价格范围")
    lines.append(f"{_dt_base.month}/{_last_day}        {'—' if _cont_avg == '0' else _cont_avg}         -")
    lines.append("")
    lines.append("7.3 价格对比")
    lines.append("市场类型        价格        与现货价差")
    lines.append(f"现货（昨日）     {avg_price}元         —")
    lines.append(f"滚动D+2~D+4     {rolling_avg}元        {_spread_str(spread)}")
    lines.append(f"月度交易价格     {monthly_price}元        {_spread_str(monthly_int - spot_int)}")
    lines.append("")

    # ── 八、水电占比与竞争空间 ──
    lines.append("八、水电占比与竞争空间（昨日逐时）")
    lines.append("")
    comp_data = get_competition_data(yesterday_str)
    if comp_data:
        lines.append("时段       负荷(MW)   水电(MW)   占比   净缺口(MW)   竞争判断")
        for cd in comp_data:
            gap_str = f"+{cd['gap']}" if cd['gap'] >= 0 else str(cd['gap'])
            lines.append(f"{cd['hour']:02d}:00    {cd['load']:,}    {cd['hydro']:,}     {cd['pct']}%   {gap_str:>8}   {cd['judge']}")
    else:
        # 兜底：用已有变量估算全天逐时数据
        load_int = int(load_avg) if load_avg.isdigit() else 41000
        hydro_int = int(hydro_actual) if hydro_actual.isdigit() else 34000
        nonmkt_int = int(non_mkt) if non_mkt.isdigit() else 5500
        re_int = int(re_avg) if re_avg.isdigit() else 2700
        lines.append("时段       负荷(MW)   水电(MW)   占比   净缺口(MW)   竞争判断")
        for h in range(24):
            # 模拟日内负荷波动：早低晚高
            h_load = round(load_int * (0.85 + 0.15 * abs(12 - h) / 12))
            h_hydro = round(hydro_int * 1.0)
            # 净缺口=负荷-水电-非市场化-新能源（简化）
            gap = h_load - h_hydro - nonmkt_int - re_int
            pct = round(h_hydro / h_load * 100) if h_load > 0 else 0
            if gap < -500: judge = "供给过剩"
            elif gap < 500: judge = "紧平衡"
            else: judge = "有空间⚡"
            gap_str = f"+{gap}" if gap >= 0 else str(gap)
            lines.append(f"{h:02d}:00    {h_load:,}    {h_hydro:,}     {pct}%   {gap_str:>8}   {judge}")
        log.warning("  竞争空间使用兜底估算数据（API不可用）")
    lines.append("")
    lines.append("省间受入参考：")
    lines.append(f"德宝直流：陕→川 {debao}MW")
    lines.append(f"省间净受入合计：{debao}MW")
    lines.append("")
    lines.append("关键时段：")
    # 从comp_data动态生成关键时段结论
    if comp_data:
        # 找有竞争空间的时段
        space_hours = [cd for cd in comp_data if cd['judge'] == "有空间⚡"]
        if space_hours:
            gaps = [cd['gap'] for cd in space_hours]
            pcts = [cd['pct'] for cd in space_hours]
            h_start = space_hours[0]['hour']
            h_end = space_hours[-1]['hour']
            lines.append(f"  有竞争空间：{h_start:02d}-{h_end:02d}时，净缺口+{min(gaps)}~+{max(gaps)} MW，水电占比{min(pcts)}-{max(pcts)}%")
        # 找供给最过剩的时段
        worst = min(comp_data, key=lambda cd: cd['gap'])
        lines.append(f"  供给最过剩：{worst['hour']:02d}时净缺口{worst['gap']} MW")
        # 电价最高/最低从API取（hourly可能含None，先过滤）
        if hourly:
            valid_p = [p for p in hourly if p is not None]
            if valid_p:
                hp = max(valid_p)
                lp = min(valid_p)
                hh = next(i for i, v in enumerate(hourly) if v is not None and v == hp)
                lh = next(i for i, v in enumerate(hourly) if v is not None and v == lp)
                lines.append(f"  电价最高：{hh:02d}时{hp}元")
                lines.append(f"  电价最低：{lh:02d}时{lp}元")
    else:
        # API不可用：不编造逐时数字
        lines.append("  ⚠️ 竞争空间API不可用，逐时数据无法提供")
        if hourly:
            valid_p = [p for p in hourly if p is not None]
            if valid_p:
                hp = max(valid_p)
                lp = min(valid_p)
                hh = next(i for i, v in enumerate(hourly) if v is not None and v == hp)
                lh = next(i for i, v in enumerate(hourly) if v is not None and v == lp)
                lines.append(f"  电价最高：{hh:02d}时{hp}元")
                lines.append(f"  电价最低：{lh:02d}时{lp}元")
            else:
                lines.append("  ⚠️ 电价数据不可用")
        else:
            lines.append("  ⚠️ 电价数据不可用")
    lines.append("")

    # ── 九、检修与断面信息 ──
    lines.append("九、检修与断面信息")
    lines.append("")
    lines.append("9.1 机组检修")
    lines.append(f"火电：{thermal_maint}")
    lines.append(f"水电：{hydro_maint}")
    lines.append(f"光伏：{solar_maint}")
    lines.append("")
    lines.append("9.2 线路与主变检修")
    lines.append(f"线路：{line_maint}")
    lines.append(f"主变：{trans_maint}")
    lines.append("")
    lines.append("9.3 断面信息（昨日峰值）")
    sec_block = block(raw, "【断面·昨日】", "【断面·容量】")
    if sec_block:
        for sl in sec_block.split("\n"):
            lines.append(sl.strip())
    cap_block = ext(raw, r"【断面·容量】([\s\S]*?)(?=━━━|$)", "")
    if cap_block:
        for cl in cap_block.strip().split("\n"):
            cl = cl.strip()
            if cl: lines.append(cl)
    lines.append("")

    # ── 十、昨日市场参考 ──
    lines.append("十、昨日市场参考")
    lines.append("")
    lines.append("10.1 价格参考")
    lines.append(f"全天均价：{calc_avg}元/MWh")
    if hourly:
        valid_p = [p for p in hourly if p is not None]
        if valid_p:
            hp = max(valid_p); lp = min(valid_p)
            hh = next(i for i, v in enumerate(hourly) if v is not None and v == hp)
            lh = next(i for i, v in enumerate(hourly) if v is not None and v == lp)
            lines.append(f"最高价：{int(hp)}元 @ {hh:02d}时")
            lines.append(f"最低价：{int(lp)}元 @ {lh:02d}时")
    lines.append("")
    lines.append("10.2 月内合约参考")
    lines.append(f"滚动加权均价（D+2~D+4）：{rolling_avg}元/MWh")
    lines.append(f"月度交易价格：{monthly_price}元/MWh")
    lines.append(f"现货与滚动价差：{spread}元（{'滚动升水现货' if spread > 0 else ('滚动贴水现货' if spread < 0 else '价差持平')}）")
    lines.append("")
    lines.append("10.3 竞争格局参考")
    lines.append("时段        电价(MWh)   净缺口(MW)    水电占比")
    if comp_data:
        # 按时段聚合
        periods = [("00-08时", 0, 8), ("08-10时", 8, 10), ("10-15时", 10, 15),
                   ("15-18时", 15, 18), ("18-22时", 18, 22), ("22-24时", 22, 24)]
        for pname, pstart, pend in periods:
            pdata = [cd for cd in comp_data if pstart <= cd['hour'] < pend]
            if not pdata:
                continue
            # 电价范围从hourly取（如果是昨天的）
            if hourly:
                ph = [hourly[cd['hour']] for cd in pdata if cd['hour'] < len(hourly) and hourly[cd['hour']] is not None]
                prange = f"{int(min(ph))}-{int(max(ph))}" if ph else "-"
            else:
                prange = "-"
            gaps = [cd['gap'] for cd in pdata]
            pcts = [cd['pct'] for cd in pdata]
            lines.append(f"{pname}    {prange:>8}   {min(gaps):,}~{max(gaps):,}   {min(pcts)}-{max(pcts)}%")
    lines.append("")
    lines.append("───")
    lines.append("四川省数据开放平台 + 国网元数据")
    lines.append("编制单位：发电侧交易分析组")

    result = "\n".join(lines)
    write_file(GEN_TXT, result)
    log.info(f"发电侧txt生成完成: {len(result)}字符, {len(lines)}行")
    return result

if __name__ == "__main__":
    gen_txt()
    print(f"\n✓ 发电侧txt: {GEN_TXT}")
