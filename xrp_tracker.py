#!/usr/bin/env python3
"""
XRP-USDT 价格行为跟踪分析脚本 (GitHub Actions 知识库增强版)

架构: ima OpenAPI搜索知识库 -> 下载PDF解析 -> 规则引擎驱动分析逻辑
分析: 1H确认趋势 -> 15M找回调结束点 -> 生成交易计划
推送: ServerChan (urllib直接调用HTTP API)
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import time
import os
import re
import io
import hashlib
import base64
from datetime import datetime, timezone

# ============================================================
# 配置
# ============================================================

SC_SENDKEY = os.environ.get("SC_SENDKEY", "")

IMA_CLIENT_ID = os.environ.get("IMA_CLIENT_ID", "")
IMA_API_KEY = os.environ.get("IMA_API_KEY", "")
IMA_KB_ID = os.environ.get("IMA_KB_ID", "OaY9MZEm7Mp4evh57u9yOZt3AmbdjChC35YQDSgaZtY=")

# 全部知识库ID列表（用于多库搜索）
ALL_KB_IDS = [
    IMA_KB_ID,  # Space的知识库（个人）
    "zHUwiT01C42Mb_5a5BTTXLLoR-s1br-9I5Rf0tguUWQ=",  # 价格行为｜AI智能体
    "CBcW9MD48OUzXQY13sJI1vWUEgi4PHoHIciogVeb4yA=",  # 价格行为学（1822人）
    "u3YfPPZntBPZfyEzRY5gqDBOh1XbrX9qVKpQFe482yY=",  # 价格行为学（1010人）
    "zZFfYk0ZjI9VoD40h2Lrorqay5Gl9qVFFd113ADJO78=",  # Al Brooks 价格行为学
]

CC_API_KEY = os.environ.get("CC_API_KEY", "")

# OKX 行情接口（公共接口，无需 Key）
OKX_BASE_URL = os.environ.get("OKX_BASE_URL", "https://www.okx.com")
OKX_BAR_MAP = {
    "1h": "1H",
    "15m": "15m",
    "5m": "5m",
    "30m": "30m",
    "4h": "4H",
    "1d": "1D",
}

# 讯飞星火 Coding Plan 配置（PDF识图兜底）
XF_SPARK_BASE_URL = os.environ.get("XF_SPARK_BASE_URL", "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2")
XF_SPARK_API_KEY = os.environ.get("XF_SPARK_API_KEY", "")
XF_SPARK_MODEL = os.environ.get("XF_SPARK_MODEL", "astron-code-latest")
XF_SPARK_ENABLED = os.environ.get("XF_SPARK_ENABLED", "1") == "1"
XF_SPARK_MAX_PAGES = int(os.environ.get("XF_SPARK_MAX_PAGES", "5"))  # 每次最多处理5页，省额度

OUTPUT_DIR = os.environ.get("TRACKER_OUTPUT_DIR", os.path.join(os.getcwd(), "output"))

# 知识库搜索关键词（按分析阶段分组）
KB_SEARCH_QUERIES = {
    "trend": ["趋势", "趋势线", "market structure", "swing", "摆动"],
    "pullback": ["回调", "pullback", "retracement", "回撤"],
    "signal": ["pin bar", "吞没", "engulfing", "K线形态", "蜡烛图", "price action pattern"],
    "support_resistance": ["支撑阻力", "support resistance", "供需"],
    "entry_exit": ["入场", "止损", "entry", "stop loss", "风险管理", "仓位管理"],
}

MAX_PDF_PER_CATEGORY = 3
MAX_CHARS_PER_PDF = 5000

# ============================================================
# 日志
# ============================================================

log_lines = []


def log(msg):
    log_lines.append(msg)
    print(msg)


# ============================================================
# OKX 行情数据获取
# ============================================================

def fetch_candles(symbol, interval, limit=200):
    """从OKX获取K线数据（公共接口，无需认证）

    Args:
        symbol: 如 "XRP-USDT"
        interval: "1h" / "15m"
        limit: 最多300根（OKX单次上限）

    Returns:
        list[list]: OKX 原始K线格式 [[ts, o, h, l, c, vol, volCcy], ...]
    """
    bar = OKX_BAR_MAP.get(interval)
    if not bar:
        log(f"  ❌ 不支持的K线周期: {interval}")
        return []

    inst_id = symbol
    url = f"{OKX_BASE_URL}/api/v5/market/candles"
    params = {
        "instId": inst_id,
        "bar": bar,
        "limit": str(min(limit, 300)),
    }
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"

    try:
        req = urllib.request.Request(full_url, headers={"User-Agent": "XRPTracker/2.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"  OKX request failed: {e}")
        return []

    if data.get("code") != "0":
        log(f"  OKX API error: code={data.get('code')} msg={data.get('msg')}")
        return []

    candles = data.get("data", [])
    if not candles:
        log("  OKX returned empty data")
        return []

    # OKX返回的K线是从最新往后排，反转成从旧到新
    candles.reverse()
    return candles


def parse_candles(raw):
    """解析OKX K线数据为标准格式

    OKX原始格式: [ts_ms, o, h, l, c, vol, volCcy, confirm]
    输出格式: {time, dt, open, high, low, close, volume}
    """
    result = []
    for c in raw:
        ts_ms = int(c[0])
        ts_sec = ts_ms // 1000
        result.append({
            "time": ts_sec,
            "dt": datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%m-%d %H:%M"),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })
    return result


# ============================================================
# ima OpenAPI 知识库集成
# ============================================================

def ima_request(endpoint, body):
    """调用ima OpenAPI"""
    url = f"https://ima.qq.com/openapi/wiki/v1/{endpoint}"
    headers = {
        "ima-openapi-clientid": IMA_CLIENT_ID,
        "ima-openapi-apikey": IMA_API_KEY,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if result.get("code") != 0:
            log(f"  ima API error: {result.get('message', 'unknown')}")
            return None
        return result.get("data", result)
    except Exception as e:
        log(f"  ima API request failed: {e}")
        return None


def search_knowledge(query, top_k=5, kb_id=None):
    """搜索知识库（支持指定 kb_id，默认用 IMA_KB_ID）"""
    kid = kb_id or IMA_KB_ID
    if not kid:
        return []
    data = ima_request("search_knowledge", {
        "knowledge_base_id": kid,
        "query": query,
        "top_k": top_k,
    })
    if not data:
        return []
    results = []
    items = data if isinstance(data, list) else data.get("info_list", data.get("list", data.get("results", [])))
    for item in items:
        media_id = item.get("media_id", "")
        title = item.get("title", item.get("name", ""))
        if media_id:
            results.append({"media_id": media_id, "title": title})
    return results


def download_pdf_text(media_id, kb_id=None):
    """下载PDF并解析：逐页双通道（PyPDF2文字 + 星火识图补充视觉内容）

    流程：
    1. 每页先用 PyPDF2 提取文字（免费）
    2. 每页再转成图片调用星火 Coding Plan 识图（补充图表/表格等视觉内容）
    3. 合并两路结果返回

    Args:
        media_id: 媒体ID
        kb_id: 所属知识库ID，默认用 IMA_KB_ID
    """
    kid = kb_id or IMA_KB_ID
    if not IMA_CLIENT_ID or not IMA_API_KEY or not kid:
        return ""

    data = ima_request("get_media_info", {
        "media_id": media_id,
        "knowledge_base_id": kid,
    })
    if not data:
        return ""

    url_info = data.get("url_info", {})
    download_url = url_info.get("url", "")
    dl_headers = url_info.get("headers", {})

    if not download_url:
        return ""

    try:
        req = urllib.request.Request(download_url, headers=dl_headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            pdf_data = resp.read()
    except Exception as e:
        log(f"  PDF download failed: {e}")
        return ""

    # ——— PyPDF2：逐页提取文字 ———
    text_by_page = {}
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_data))
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            text_by_page[i] = page_text.strip()
    except ImportError:
        log("  PyPDF2 not installed, will rely solely on Spark OCR")
    except Exception as e:
        log(f"  PyPDF2 parse failed ({e}), will rely solely on Spark OCR")

    # ——— 星火识图：逐页补充视觉内容 ———
    if XF_SPARK_ENABLED:
        return spark_ocr_pdf(pdf_data, text_by_page)
    else:
        # 星火禁用时，直接返回 PyPDF2 的文字
        text = "\n".join(t for t in text_by_page.values() if t)
        return text if text else ""


def spark_ocr_pdf(pdf_data, text_by_page=None):
    """逐页双通道：PyPDF2文字 + 星火识图补充视觉元素

    每一页都调星火识别图片，把图表、K线图、表格等视觉内容
    补充到 PyPDF2 已提取的文字中。

    Args:
        pdf_data: PDF 二进制数据
        text_by_page: dict[int, str]，PyPDF2 提取的逐页文字
    """
    if not XF_SPARK_ENABLED:
        return ""

    try:
        import fitz
    except ImportError:
        log("  PyMuPDF (fitz) not installed, cannot do Spark OCR")
        return ""

    try:
        from openai import OpenAI
    except ImportError:
        log("  openai package not installed, cannot do Spark OCR")
        return ""

    if text_by_page is None:
        text_by_page = {}

    doc = fitz.open(stream=pdf_data, filetype="pdf")
    total_pages = len(doc)
    pages_to_process = min(total_pages, XF_SPARK_MAX_PAGES)
    log(f"  [XF Spark] PDF has {total_pages} pages, processing first {pages_to_process} pages...")

    client = OpenAI(
        base_url=XF_SPARK_BASE_URL,
        api_key=XF_SPARK_API_KEY,
    )

    merged_pages = []

    for page_num in range(pages_to_process):
        page = doc[page_num]

        # ——— 通道A：PyPDF2 该页文字 ———
        page_text = text_by_page.get(page_num, "")

        # ——— 通道B：PyMuPDF 渲染图片 ———
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
        img_size_kb = len(img_b64) // 1024

        log(f"  [XF Spark] Page {page_num + 1}/{total_pages} ({img_size_kb}KB, PyPDF2={len(page_text)} chars)...")

        # ——— 构造提示词：有文字就补充视觉，没文字就全量OCR ———
        if page_text:
            prompt = (
                f"这页PDF的部分文字已提取如下：\n---\n{page_text[:800]}\n---\n"
                f"请识别图片中是否有K线图、图表、表格、标注、箭头标记等视觉元素，"
                f"补充其内容和含义。如果图片中没有额外的视觉信息，回复'无视觉内容'即可。"
            )
        else:
            prompt = "把这张图片里的所有文字原样提取出来，包括标题、列表、编号。只输出提取到的文字，不要额外解释。"

        try:
            resp = client.chat.completions.create(
                model=XF_SPARK_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    ],
                }],
                max_tokens=1000,
                temperature=0.1,
            )
            spark_result = resp.choices[0].message.content.strip()
            log(f"    Spark补充: {len(spark_result)} chars")

            # ——— 合并该页两个通道的结果 ———
            if page_text and spark_result and spark_result != "无视觉内容":
                merged_pages.append(
                    f"--- Page {page_num + 1} ---\n"
                    f"[文字部分]\n{page_text}\n"
                    f"[图片视觉内容]\n{spark_result}"
                )
            elif page_text:
                merged_pages.append(f"--- Page {page_num + 1} ---\n{page_text}")
            else:
                merged_pages.append(f"--- Page {page_num + 1} ---\n{spark_result}")

        except Exception as e:
            log(f"  [XF Spark] Page {page_num + 1} failed: {e}")
            # 失败时回退：至少有 PyPDF2 的文字
            if page_text:
                merged_pages.append(f"--- Page {page_num + 1} ---\n{page_text}")
            else:
                merged_pages.append(f"--- Page {page_num + 1} ---\n[OCR failed]")

    doc.close()
    result = "\n\n".join(merged_pages)
    log(f"  [XF Spark] Complete: {len(result)} chars from {pages_to_process} pages")
    return result


def fetch_knowledge_rules():
    """
    从全部知识库搜索并提取交易规则文本
    返回: dict[category] -> list[dict(title, text, source_kb)]
    遍历 ALL_KB_IDS，去重 media_id，每个类别独立计数
    """
    knowledge = {}
    seen_media_ids = set()

    log("\n--- Knowledge Base Rule Fetching ---")

    if not IMA_CLIENT_ID or not IMA_API_KEY:
        log("  ima API credentials not configured, skip knowledge base")
        return knowledge

    if not ALL_KB_IDS:
        log("  No knowledge base IDs configured, skip")
        return knowledge

    log(f"  Searching {len(ALL_KB_IDS)} knowledge bases...")

    for kb_id in ALL_KB_IDS:
        if not kb_id:
            continue

        for category, queries in KB_SEARCH_QUERIES.items():
            # 如果该分类已有足够文档，跳过
            existing = len(knowledge.get(category, []))
            if existing >= MAX_PDF_PER_CATEGORY:
                continue

            category_needed = MAX_PDF_PER_CATEGORY - existing

            for query in queries:
                if category_needed <= 0:
                    break

                results = search_knowledge(query, top_k=3, kb_id=kb_id)
                for item in results:
                    mid = item["media_id"]
                    if mid in seen_media_ids:
                        continue
                    seen_media_ids.add(mid)

                    if category_needed <= 0:
                        break

                    log(f"  [{category}] Downloading: {item['title'][:50]}...")
                    text = download_pdf_text(mid, kb_id=kb_id)
                    if text:
                        if category not in knowledge:
                            knowledge[category] = []
                        knowledge[category].append({
                            "title": item["title"],
                            "text": text[:MAX_CHARS_PER_PDF],
                            "total_chars": len(text),
                            "source_kb": kb_id,
                        })
                        category_needed -= 1
                        log(f"    Got {len(text)} chars (using first {MAX_CHARS_PER_PDF})")

    total_docs = sum(len(docs) for docs in knowledge.values())
    total_chars = sum(sum(d["total_chars"] for d in docs) for docs in knowledge.values())
    log(f"  Knowledge base: {len(knowledge)} categories, {total_docs} docs, {total_chars} chars")
    for cat, docs in knowledge.items():
        chars = sum(d["total_chars"] for d in docs)
        log(f"    - {cat}: {len(docs)} docs, {chars} chars")

    return knowledge


# ============================================================
# 知识库规则提取引擎
# ============================================================

def extract_rules_from_text(text, category):
    """从知识库文本中提取结构化规则"""
    rules = []

    # 通用规则提取模式
    patterns = [
        # 编号规则: "1. xxx" "2. xxx"
        r'(?:^|\n)\s*(?:\d+[\.、）)]|[一二三四五六七八九十]+[、）)])\s*(.+)',
        # 条件规则: "如果...则/就/应该..."
        r'(?:^|\n)\s*(?:如果|若|当|一旦).+?(?:则|就|应该|需|必须|要).+',
        # 关键规则: "必须/应该/需要/一定要..."
        r'(?:^|\n)\s*(?:必须|应该|需要|一定要|务必|切记|注意).+',
        # 禁止规则: "不要/不能/不可/避免/禁止/切勿..."
        r'(?:^|\n)\s*(?:不要|不能|不可|避免|禁止|切勿|绝不|绝不能).+',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            rule = match.strip() if isinstance(match, str) else match[0].strip()
            if len(rule) > 5 and len(rule) < 200:
                rules.append(rule)

    # 去重
    seen = set()
    unique_rules = []
    for r in rules:
        r_clean = re.sub(r'\s+', '', r)
        if r_clean not in seen:
            seen.add(r_clean)
            unique_rules.append(r)

    return unique_rules[:10]  # 每个文档最多10条规则


def build_rule_engine(knowledge):
    """
    从知识库内容构建规则引擎
    返回: dict[category] -> list[dict(rule, source)]
    """
    rule_engine = {}

    for category, docs in knowledge.items():
        category_rules = []
        for doc in docs:
            extracted = extract_rules_from_text(doc["text"], category)
            for rule in extracted:
                category_rules.append({
                    "rule": rule,
                    "source": doc["title"],
                })
        rule_engine[category] = category_rules
        log(f"  Rule engine [{category}]: {len(category_rules)} rules extracted")

    return rule_engine


def apply_knowledge_to_trend(h1_result, rule_engine):
    """将知识库趋势规则应用到1H趋势分析"""
    trend_rules = rule_engine.get("trend", [])
    sr_rules = rule_engine.get("support_resistance", [])

    enhancements = []

    # 规则: 市场倾向是交易优势的核心
    for r in trend_rules:
        if "市场倾向" in r["rule"] or "交易优势" in r["rule"]:
            trend_dir = h1_result["trend_dir"]
            if trend_dir == "UP":
                enhancements.append(
                    f"[KB] {r['rule']} -> 当前1H上涨趋势明确，市场倾向做多，具备交易优势"
                )
            elif trend_dir == "DOWN":
                enhancements.append(
                    f"[KB] {r['rule']} -> 当前1H下跌趋势明确，市场倾向做空，具备交易优势"
                )
            else:
                enhancements.append(
                    f"[KB] {r['rule']} -> 当前1H震荡，无明确市场倾向，缺乏交易优势"
                )
            break

    # 规则: 完整策略=市场倾向+交易设置+退出计划
    for r in trend_rules:
        if "完整" in r["rule"] and ("策略" in r["rule"] or "退出" in r["rule"]):
            enhancements.append(
                f"[KB] {r['rule']} -> 需确认: 1)市场倾向(趋势) 2)交易设置(回调入场) 3)退出计划(止损/目标)"
            )
            break

    # 规则: 支撑阻力相关
    for r in sr_rules:
        if "关键" in r["rule"] and ("支撑" in r["rule"] or "阻力" in r["rule"]):
            enhancements.append(
                f"[KB] {r['rule']} -> 已识别1H关键支撑{h1_result.get('key_support', [])} "
                f"阻力{h1_result.get('key_resistance', [])}"
            )
            break

    return enhancements


def apply_knowledge_to_pullback(m15_result, h1_result, rule_engine):
    """将知识库回调/信号规则应用到15M回调分析"""
    pullback_rules = rule_engine.get("pullback", [])
    signal_rules = rule_engine.get("signal", [])
    entry_rules = rule_engine.get("entry_exit", [])

    enhancements = []
    trend_dir = h1_result["trend_dir"]

    # 规则: 回调交易的核心 - 顺趋势方向交易回调
    for r in pullback_rules:
        if "回调" in r["rule"] and ("趋势" in r["rule"] or "顺" in r["rule"]):
            if trend_dir == "UP":
                enhancements.append(
                    f"[KB] {r['rule']} -> 1H上涨趋势中，只寻找做多回调入场机会"
                )
            elif trend_dir == "DOWN":
                enhancements.append(
                    f"[KB] {r['rule']} -> 1H下跌趋势中，只寻找做空回调入场机会"
                )
            break

    # 规则: Pin Bar情境交易方法
    for r in signal_rules:
        if "pin bar" in r["rule"].lower() or "Pin Bar" in r["rule"]:
            bull_signals = m15_result.get("bull_signals", [])
            bear_signals = m15_result.get("bear_signals", [])
            if trend_dir == "UP" and bull_signals:
                enhancements.append(
                    f"[KB] {r['rule']} -> 15M出现看涨Pin Bar，配合上涨趋势回调，信号有效"
                )
            elif trend_dir == "DOWN" and bear_signals:
                enhancements.append(
                    f"[KB] {r['rule']} -> 15M出现看跌Pin Bar，配合下跌趋势回调，信号有效"
                )
            break

    # 规则: 吞没形态
    for r in signal_rules:
        if "吞没" in r["rule"] or "engulfing" in r["rule"].lower():
            bull_signals = m15_result.get("bull_signals", [])
            bear_signals = m15_result.get("bear_signals", [])
            has_bull_engulf = any("吞没" in s["type"] and "看涨" in s["type"] for s in bull_signals)
            has_bear_engulf = any("吞没" in s["type"] and "看跌" in s["type"] for s in bear_signals)
            if trend_dir == "UP" and has_bull_engulf:
                enhancements.append(
                    f"[KB] {r['rule']} -> 15M出现看涨吞没，上涨趋势回调结束信号增强"
                )
            elif trend_dir == "DOWN" and has_bear_engulf:
                enhancements.append(
                    f"[KB] {r['rule']} -> 15M出现看跌吞没，下跌趋势回调结束信号增强"
                )
            break

    # 规则: 入场/止损相关
    for r in entry_rules:
        if "止损" in r["rule"] or "stop" in r["rule"].lower():
            enhancements.append(f"[KB] {r['rule']}")
            break

    for r in entry_rules:
        if "入场" in r["rule"] or "entry" in r["rule"].lower():
            enhancements.append(f"[KB] {r['rule']}")
            break

    return enhancements


# ============================================================
# 价格行为趋势规则引擎（知识库规则 → 可执行检查）
# ============================================================

# 内置价格行为趋势规则
# 每条规则是一个 dict，包含检查函数和映射知识库关键词的模式
PA_TREND_RULES = [
    {
        "id": "swing_structure",
        "name": "摆动结构：HH > LH 且 HL > LL",
        "desc": "上升趋势需要更多更高的高点和更高的低点",
        "kb_keywords": ["HH", "HL", "higher high", "higher low", "摆动高", "摆动低", "市场结构"],
    },
    {
        "id": "sma20_location",
        "name": "价格在SMA20上方运行",
        "desc": "上升趋势中价格应在SMA20之上，下跌趋势中应在下方",
        "kb_keywords": ["SMA", "均线", "moving average", "MA20", "SMA20"],
    },
    {
        "id": "sma_alignment",
        "name": "MA多头排列（SMA20 > SMA50）",
        "desc": "短期均线在长期均线上方确认上升趋势",
        "kb_keywords": ["MA排列", "多头排列", "空头排列", "cross", "金叉", "死叉", "SMA50"],
    },
    {
        "id": "trend_bar_majority",
        "name": "趋势柱数量占优",
        "desc": "近期K线中顺趋势方向柱体多于逆势方向",
        "kb_keywords": ["趋势柱", "trend bar", "看涨", "看跌", "bull", "bear"],
    },
    {
        "id": "sma_slope",
        "name": "SMA20斜率向上",
        "desc": "上升趋势中SMA20应向上倾斜（最新值大于前值）",
        "kb_keywords": ["斜率", "slope", "倾斜", "SMA方向", "均线方向"],
    },
    {
        "id": "pullback_depth",
        "name": "回调不深（未跌破前摆动低点）",
        "desc": "健康的上升趋势中回调不应跌破前一个摆动低点",
        "kb_keywords": ["回调", "pullback", "回撤", "前低", "前高", "支撑"],
    },
    {
        "id": "swing_consistency",
        "name": "摆动一致性（HH/LH比 ≥ 1.5）",
        "desc": "趋势越强，HH相对LH的优势越明显",
        "kb_keywords": ["一致", "consistency", "clean", "清晰", "明确"],
    },
]

# 知识库文本 → 规则ID 映射模式
KB_RULE_PATTERNS = [
    (r"HH.*HL|higher.high.*higher.low|更高.*高[点位]", "swing_structure"),
    (r"价格.*(?:SMA|均线|MA20).*(?:上[方面]|之上|之下|下[方面])", "sma20_location"),
    (r"(?:SMA|均线|MA).*(?:排列|交叉|金叉|死叉|多头|空头|bullish|bearish)", "sma_alignment"),
    (r"(?:趋势|trend).*(?:柱|bar).*(?:占优|多数|多|少|bull|bear)", "trend_bar_majority"),
    (r"(?:SMA|均线).*(?:斜率|方向|向上|向下|倾斜|slope)", "sma_slope"),
    (r"(?:回调|回撤|pullback|retrace).*(?:不|未|浅|浅|deep|健康)", "pullback_depth"),
    (r"(?:一致|consistency|clean|清晰|明确|结构干净)", "swing_consistency"),
]


def match_kb_rules_to_pa(rule_engine):
    """将知识库提取的文本规则匹配到内置PA规则的ID上

    返回: dict[rule_id] -> list[知识库规则文本]
    """
    matched = {}
    for cat, rules in rule_engine.items():
        for r in rules:
            text = r.get("rule", "")
            for pattern, rule_id in KB_RULE_PATTERNS:
                if re.search(pattern, text):
                    if rule_id not in matched:
                        matched[rule_id] = []
                    matched[rule_id].append(text)
                    break
    return matched


def evaluate_pa_trend_rules(candles, h1_result, matched_kb_rules):
    """逐条执行价格行为趋势规则检查

    Args:
        candles: 1H K线数据
        h1_result: analyze_1h 的分析结果
        matched_kb_rules: match_kb_rules_to_pa() 的返回

    Returns:
        dict: {
            "total": int,          # 总规则数
            "passed": int,         # 通过数
            "score": int,          # 通过分数 (0~7)
            "details": [           # 每条的检查结果
                {
                    "id": "swing_structure",
                    "name": "摆动结构...",
                    "passed": True/False,
                    "detail": "HH=8 vs LH=2, 优势比4.0 ✅",
                    "kb_rules": ["知识库原文..."],
                }
            ],
            "kb_matched_count": int,
            "verdict": "strong_bullish" / "bullish" / "mixed" / "bearish" / "strong_bearish"
        }
    """
    current_price = candles[-1]["close"]
    sma20 = h1_result.get("sma20", 0)
    sma50 = h1_result.get("sma50", 0)
    hh = h1_result["swing"]["HH"]
    lh = h1_result["swing"]["LH"]
    hl = h1_result["swing"]["HL"]
    ll = h1_result["swing"]["LL"]
    trend_dir = h1_result.get("trend_dir", "RANGE")
    bull_count = h1_result.get("bull_count_20", 0)
    bear_count = h1_result.get("bear_count_20", 0)
    last_swing_low = h1_result.get("last_swing_low", {})
    prev_swing_low = h1_result.get("prev_swing_low", {})

    results = []

    # 规则1：摆动结构
    is_up = hh > lh and hl > ll
    is_down = lh > hh and ll > hl
    rules_passed = is_up if trend_dir == "UP" else (is_down if trend_dir == "DOWN" else False)
    detail = f"HH={hh} vs LH={lh}, HL={hl} vs LL={ll}"
    detail += " ✅ 符合趋势方向" if rules_passed else " ⚠️ 与趋势方向矛盾"
    results.append({
        "id": "swing_structure",
        "name": PA_TREND_RULES[0]["name"],
        "passed": rules_passed,
        "detail": detail,
        "kb_rules": matched_kb_rules.get("swing_structure", []),
    })

    # 规则2：价格在SMA20上方/下方
    if sma20:
        rules_passed = (current_price > sma20 and trend_dir == "UP") or \
                       (current_price < sma20 and trend_dir == "DOWN")
        detail = f"现价{current_price:.4f} vs SMA20={sma20:.4f}"
        detail += f" {'上方 ✅' if current_price > sma20 else '下方'}"
        detail += " → 符合趋势" if rules_passed else " → 与趋势不一致"
        results.append({
            "id": "sma20_location",
            "name": PA_TREND_RULES[1]["name"],
            "passed": rules_passed,
            "detail": detail,
            "kb_rules": matched_kb_rules.get("sma20_location", []),
        })
    else:
        results.append({
            "id": "sma20_location", "name": PA_TREND_RULES[1]["name"],
            "passed": False, "detail": "SMA20无数据",
            "kb_rules": matched_kb_rules.get("sma20_location", []),
        })

    # 规则3：MA排列
    if sma20 and sma50:
        ma_bullish = sma20 > sma50
        rules_passed = (ma_bullish and trend_dir == "UP") or (not ma_bullish and trend_dir == "DOWN")
        detail = f"SMA20={sma20:.4f} vs SMA50={sma50:.4f}"
        detail += f" 多头排列" if ma_bullish else " 空头排列"
        detail += " ✅" if rules_passed else " ⚠️ 与趋势方向不一致"
        results.append({
            "id": "sma_alignment",
            "name": PA_TREND_RULES[2]["name"],
            "passed": rules_passed,
            "detail": detail,
            "kb_rules": matched_kb_rules.get("sma_alignment", []),
        })
    else:
        results.append({
            "id": "sma_alignment", "name": PA_TREND_RULES[2]["name"],
            "passed": False, "detail": "MA数据不足",
            "kb_rules": matched_kb_rules.get("sma_alignment", []),
        })

    # 规则4：趋势柱占优
    if trend_dir == "UP":
        rules_passed = bull_count > bear_count
    elif trend_dir == "DOWN":
        rules_passed = bear_count > bull_count
    else:
        rules_passed = False
    detail = f"近20根: 看涨{bull_count} 看跌{bear_count}"
    detail += f" → 顺趋势柱占优 ✅" if rules_passed else " → 逆势柱过多 ⚠️"
    results.append({
        "id": "trend_bar_majority",
        "name": PA_TREND_RULES[3]["name"],
        "passed": rules_passed,
        "detail": detail,
        "kb_rules": matched_kb_rules.get("trend_bar_majority", []),
    })

    # 规则5：SMA20斜率
    if sma20 and len(candles) >= 22:
        sma20_prev = sum(c["close"] for c in candles[-21:-1]) / 20
        sma20_now = sum(c["close"] for c in candles[-20:]) / 20
        slope_up = sma20_now > sma20_prev
        if trend_dir == "UP":
            rules_passed = slope_up
        elif trend_dir == "DOWN":
            rules_passed = not slope_up
        else:
            rules_passed = False
        detail = f"SMA20方向: {'向上 ↗' if slope_up else '向下 ↘'}"
        detail += " ✅" if rules_passed else " ⚠️"
        results.append({
            "id": "sma_slope",
            "name": PA_TREND_RULES[4]["name"],
            "passed": rules_passed,
            "detail": detail,
            "kb_rules": matched_kb_rules.get("sma_slope", []),
        })
    else:
        results.append({
            "id": "sma_slope", "name": PA_TREND_RULES[4]["name"],
            "passed": False, "detail": "数据不足",
            "kb_rules": matched_kb_rules.get("sma_slope", []),
        })

    # 规则6：回调深度（不破前低）
    if trend_dir == "UP" and last_swing_low and prev_swing_low:
        rules_passed = last_swing_low["price"] >= prev_swing_low["price"]
        detail = f"最近低点{last_swing_low['price']:.4f} vs 前低{prev_swing_low['price']:.4f}"
        detail += " → 未破前低 ✅" if rules_passed else " → 跌破前低 ⚠️"
    elif trend_dir == "DOWN" and last_swing_low and prev_swing_low:
        rules_passed = last_swing_low["price"] >= prev_swing_low["price"]
        detail = f"最近低点{last_swing_low['price']:.4f} vs 前低{prev_swing_low['price']:.4f}"
        detail += " → 未破前低 ✅" if rules_passed else " → 跌破前低 ⚠️"
    else:
        rules_passed = False
        detail = "摆动数据不足"
    results.append({
        "id": "pullback_depth",
        "name": PA_TREND_RULES[5]["name"],
        "passed": rules_passed,
        "detail": detail,
        "kb_rules": matched_kb_rules.get("pullback_depth", []),
    })

    # 规则7：摆动一致性
    if hh + lh > 0 and hl + ll > 0:
        if trend_dir == "UP":
            ratio_h = hh / max(lh, 1)
            ratio_l = hl / max(ll, 1)
            rules_passed = ratio_h >= 1.5 or ratio_l >= 1.5
            detail = f"HH/LH比={ratio_h:.1f}, HL/LL比={ratio_l:.1f}"
        elif trend_dir == "DOWN":
            ratio_h = lh / max(hh, 1)
            ratio_l = ll / max(hl, 1)
            rules_passed = ratio_h >= 1.5 or ratio_l >= 1.5
            detail = f"LH/HH比={ratio_h:.1f}, LL/HL比={ratio_l:.1f}"
        else:
            rules_passed = False
            detail = "震荡市无方向"
        detail += " ✅" if rules_passed else " ⚠️ 优势不够明显"
    else:
        rules_passed = False
        detail = "摆动数据不足"
    results.append({
        "id": "swing_consistency",
        "name": PA_TREND_RULES[6]["name"],
        "passed": rules_passed,
        "detail": detail,
        "kb_rules": matched_kb_rules.get("swing_consistency", []),
    })

    # 统计
    total = len(results)
    passed_count = sum(1 for r in results if r["passed"])
    score = passed_count
    total_kb_matched = sum(len(r["kb_rules"]) for r in results)

    # 趋势质量判定
    if trend_dir == "UP":
        if passed_count >= 6:
            verdict = "strong_bullish"
        elif passed_count >= 4:
            verdict = "bullish"
        else:
            verdict = "weak_bullish"
    elif trend_dir == "DOWN":
        if passed_count >= 6:
            verdict = "strong_bearish"
        elif passed_count >= 4:
            verdict = "bearish"
        else:
            verdict = "weak_bearish"
    else:
        verdict = "range"

    return {
        "total": total,
        "passed": passed_count,
        "score": score,
        "details": results,
        "kb_matched_count": total_kb_matched,
        "verdict": verdict,
    }


def format_trend_quality_for_push(tq):
    """将趋势质量评估结果格式化为推送文本"""
    score_bar = "●" * tq["score"] + "○" * (tq["total"] - tq["score"])
    verdict_emoji = {
        "strong_bullish": "🟢🟢",
        "bullish": "🟢",
        "weak_bullish": "🟡",
        "range": "⚪",
        "weak_bearish": "🟠",
        "bearish": "🔴",
        "strong_bearish": "🔴🔴",
    }
    emoji = verdict_emoji.get(tq["verdict"], "⚪")
    lines = [f"📐 趋势质量: {tq['score']}/{tq['total']} {score_bar} {emoji}"]

    for r in tq["details"]:
        icon = "✅" if r["passed"] else "❌"
        lines.append(f"  {icon} {r['detail']}")

    if tq["kb_matched_count"] > 0:
        lines.append(f"  📚 知识库匹配: {tq['kb_matched_count']}条规则参与评估")

    return "\n".join(lines)


# ============================================================
# 分析工具
# ============================================================

def find_swings(candles, left=3, right=3):
    """识别摆动高点和摆动低点"""
    swing_highs = []
    swing_lows = []
    for i in range(left, len(candles) - right):
        if all(candles[i]['high'] >= candles[i-j]['high'] for j in range(1, left+1)) and \
           all(candles[i]['high'] >= candles[i+j]['high'] for j in range(1, right+1)):
            swing_highs.append({'idx': i, 'price': candles[i]['high'], 'dt': candles[i]['dt']})
        if all(candles[i]['low'] <= candles[i-j]['low'] for j in range(1, left+1)) and \
           all(candles[i]['low'] <= candles[i+j]['low'] for j in range(1, right+1)):
            swing_lows.append({'idx': i, 'price': candles[i]['low'], 'dt': candles[i]['dt']})
    return swing_highs, swing_lows


def calc_sma(candles, period):
    """简单移动平均"""
    if len(candles) < period:
        return None
    return sum(c['close'] for c in candles[-period:]) / period


def detect_signals(candles, lookback=30):
    """检测价格行为信号"""
    signals = []
    start = max(1, len(candles) - lookback)
    for i in range(start, len(candles)):
        c = candles[i]
        bar_range = c['high'] - c['low']
        if bar_range == 0:
            continue
        body = abs(c['close'] - c['open'])
        upper_shadow = c['high'] - max(c['open'], c['close'])
        lower_shadow = min(c['open'], c['close']) - c['low']

        # Bullish pin bar
        if lower_shadow > body * 2 and lower_shadow > upper_shadow * 2 and body > 0:
            signals.append({'dt': c['dt'], 'type': '看涨Pin Bar', 'price': c['close']})
        # Bearish pin bar
        if upper_shadow > body * 2 and upper_shadow > lower_shadow * 2 and body > 0:
            signals.append({'dt': c['dt'], 'type': '看跌Pin Bar', 'price': c['close']})

        # Engulfing
        prev = candles[i-1]
        if prev['close'] < prev['open'] and c['close'] > c['open']:
            if c['open'] <= prev['close'] and c['close'] >= prev['open']:
                signals.append({'dt': c['dt'], 'type': '看涨吞没', 'price': c['close']})
        if prev['close'] > prev['open'] and c['close'] < c['open']:
            if c['open'] >= prev['close'] and c['close'] <= prev['open']:
                signals.append({'dt': c['dt'], 'type': '看跌吞没', 'price': c['close']})

    return signals


# ============================================================
# 1H 趋势分析
# ============================================================

def analyze_1h(candles):
    """1H趋势分析"""
    result = {}
    current_price = candles[-1]['close']
    result['current_price'] = current_price
    result['latest_time'] = candles[-1]['dt']

    # 摆动结构
    swing_highs, swing_lows = find_swings(candles)

    # 最近50根摆动分析
    recent_sh = [sh for sh in swing_highs if sh['idx'] >= len(candles) - 50]
    recent_sl = [sl for sl in swing_lows if sl['idx'] >= len(candles) - 50]

    hh = sum(1 for i in range(1, len(recent_sh)) if recent_sh[i]['price'] > recent_sh[i-1]['price'])
    lh = sum(1 for i in range(1, len(recent_sh)) if recent_sh[i]['price'] < recent_sh[i-1]['price'])
    hl = sum(1 for i in range(1, len(recent_sl)) if recent_sl[i]['price'] > recent_sl[i-1]['price'])
    ll = sum(1 for i in range(1, len(recent_sl)) if recent_sl[i]['price'] < recent_sl[i-1]['price'])

    result['swing'] = {'HH': hh, 'LH': lh, 'HL': hl, 'LL': ll}

    # 趋势判定
    if hh > lh and hl > ll:
        trend = "上涨趋势(HH+HL)"
        trend_dir = "UP"
    elif lh > hh and ll > hl:
        trend = "下跌趋势(LH+LL)"
        trend_dir = "DOWN"
    else:
        trend = "震荡/整理"
        trend_dir = "RANGE"

    result['trend'] = trend
    result['trend_dir'] = trend_dir

    # MA
    sma20 = calc_sma(candles, 20)
    sma50 = calc_sma(candles, 50)
    result['sma20'] = sma20
    result['sma50'] = sma50
    result['above_sma20'] = current_price > sma20 if sma20 else None
    result['above_sma50'] = current_price > sma50 if sma50 else None
    result['ma_bullish'] = sma20 and sma50 and sma20 > sma50

    # 近20根K线多空
    last20 = candles[-20:]
    result['bull_count_20'] = sum(1 for c in last20 if c['close'] > c['open'])
    result['bear_count_20'] = sum(1 for c in last20 if c['close'] < c['open'])

    # 最近摆动点
    result['last_swing_high'] = swing_highs[-1] if swing_highs else None
    result['last_swing_low'] = swing_lows[-1] if swing_lows else None
    result['prev_swing_low'] = swing_lows[-2] if len(swing_lows) >= 2 else None

    # 关键支撑阻力
    key_resistance = sorted(set(sh['price'] for sh in recent_sh), reverse=True)[:3]
    key_support = sorted(set(sl['price'] for sl in recent_sl))[:3]
    result['key_resistance'] = key_resistance
    result['key_support'] = key_support

    return result


# ============================================================
# 15M 回调分析
# ============================================================

def analyze_15m(candles, h1_result):
    """15M回调分析"""
    result = {}
    current_price = candles[-1]['close']
    result['current_price'] = current_price
    result['latest_time'] = candles[-1]['dt']

    # 摆动结构
    swing_highs, swing_lows = find_swings(candles)
    result['last_swing_high'] = swing_highs[-1] if swing_highs else None
    result['last_swing_low'] = swing_lows[-1] if swing_lows else None

    # 斐波那契回撤 (基于1H主浪)
    h1_prev_low = h1_result.get('prev_swing_low', {})
    h1_last_high = h1_result.get('last_swing_high', {})

    if h1_prev_low and h1_last_high:
        impulse_start = h1_prev_low.get('price', current_price * 0.95)
        impulse_end = h1_last_high.get('price', current_price * 1.05)
        impulse_range = impulse_end - impulse_start

        fibs = {
            '23.6%': impulse_end - impulse_range * 0.236,
            '38.2%': impulse_end - impulse_range * 0.382,
            '50.0%': impulse_end - impulse_range * 0.500,
            '61.8%': impulse_end - impulse_range * 0.618,
            '78.6%': impulse_end - impulse_range * 0.786,
        }
        result['fib'] = fibs
        result['impulse_start'] = impulse_start
        result['impulse_end'] = impulse_end
        result['pullback_pct'] = (impulse_end - current_price) / impulse_range * 100 if impulse_range > 0 else 0

    # 价格行为信号
    signals = detect_signals(candles, lookback=30)
    result['signals'] = signals
    result['bull_signals'] = [s for s in signals if '看涨' in s['type']]
    result['bear_signals'] = [s for s in signals if '看跌' in s['type']]

    # 趋势柱统计
    last30 = candles[-30:]
    bull_trend = 0
    bear_trend = 0
    for c in last30:
        bar_range = c['high'] - c['low']
        if bar_range == 0:
            continue
        body_pct = abs(c['close'] - c['open']) / bar_range
        if c['close'] > c['open'] and body_pct >= 0.5:
            bull_trend += 1
        elif c['close'] < c['open'] and body_pct >= 0.5:
            bear_trend += 1
    result['bull_trend_bars'] = bull_trend
    result['bear_trend_bars'] = bear_trend

    # 成交量
    avg_vol = sum(c['volume'] for c in candles) / len(candles) if candles else 1
    recent_avg = sum(c['volume'] for c in candles[-20:]) / 20 if len(candles) >= 20 else avg_vol
    result['avg_vol'] = avg_vol
    result['recent_avg_vol'] = recent_avg
    result['volume_shrinking'] = recent_avg < avg_vol

    # 最近5根K线多空
    last5 = candles[-5:]
    result['bear_in_last5'] = sum(1 for c in last5 if c['close'] < c['open'])

    # 最近10根K线详情
    recent_bars = []
    for c in candles[-10:]:
        bar_range = c['high'] - c['low']
        body_pct = int(abs(c['close'] - c['open']) / bar_range * 100) if bar_range > 0 else 0
        recent_bars.append({
            'dt': c['dt'],
            'open': c['open'],
            'high': c['high'],
            'low': c['low'],
            'close': c['close'],
            'dir': '看涨' if c['close'] > c['open'] else '看跌',
            'body_pct': body_pct,
        })
    result['recent_bars'] = recent_bars

    # 回调结束评分
    score = 0
    conditions = []

    # 条件1: 斐波那契位
    fib = result.get('fib', {})
    at_fib = False
    for name, level in fib.items():
        if abs(current_price - level) / level * 100 < 1.5:
            at_fib = True
            conditions.append(f"[Fib] 在{name}斐波那契位附近({level:.4f})")
            score += 1
            break
    if not at_fib:
        conditions.append("[Fib] 不在关键斐波那契位附近")

    # 条件2: 信号确认
    bear_sigs = result.get('bear_signals', [])
    bull_sigs = result.get('bull_signals', [])
    if h1_result.get('trend_dir') == 'DOWN' and bear_sigs:
        conditions.append(f"[信号] 有看跌PA信号: {bear_sigs[0]['type']}")
        score += 1
    elif h1_result.get('trend_dir') == 'UP' and bull_sigs:
        conditions.append(f"[信号] 有看涨PA信号: {bull_sigs[0]['type']}")
        score += 1
    else:
        conditions.append("[信号] 无顺势PA信号确认")

    # 条件3: 趋势柱
    if h1_result.get('trend_dir') == 'DOWN' and result['bear_trend_bars'] >= result['bull_trend_bars']:
        conditions.append("[趋势柱] 看跌趋势柱占优 ✓")
        score += 1
    elif h1_result.get('trend_dir') == 'UP' and result['bull_trend_bars'] >= result['bear_trend_bars']:
        conditions.append("[趋势柱] 看涨趋势柱占优 ✓")
        score += 1
    else:
        conditions.append("[趋势柱] 趋势柱方向不统一")

    # 条件4: 缩量
    if result.get('volume_shrinking'):
        conditions.append("[量能] 缩量 ✓ (回调尾声特征)")
        score += 1
    else:
        conditions.append("[量能] 放量 (回调可能未结束)")

    # 条件5: 回调深度
    pullback_pct = result.get('pullback_pct', 0)
    if 23 <= pullback_pct <= 62:
        conditions.append(f"[深度] 回调{pullback_pct:.1f}%处于23-62%健康区间 ✓")
        score += 1
    elif pullback_pct > 62:
        conditions.append(f"[深度] 回调{pullback_pct:.1f}%过深 (可能趋势反转)")
    else:
        conditions.append(f"[深度] 回调{pullback_pct:.1f}%过浅")

    result['pullback_score'] = score
    result['pullback_conditions'] = conditions

    if score >= 4:
        result['pullback_verdict'] = "✅ 回调结束信号明确，可入场"
    elif score >= 2:
        result['pullback_verdict'] = "⏳ 回调有结束迹象，建议等待进一步确认"
    else:
        result['pullback_verdict'] = "❌ 回调未结束，继续等待"

    return result


# ============================================================
# 交易计划生成
# ============================================================

def generate_plan(h1, m15):
    """根据1H趋势和15M回调生成交易计划"""
    plan = {
        'trend': h1.get('trend', ''),
        'direction': '',
        'entry_zone': '',
        'stop_loss': '',
        'tp1': '',
        'tp2': '',
        'entry_condition': '',
        'invalidation': '',
        'pullback_score': f"{m15.get('pullback_score', 0)}/5",
        'pullback_verdict': m15.get('pullback_verdict', ''),
    }

    trend_dir = h1.get('trend_dir', '')

    if trend_dir == "UP":
        key_support = h1.get('key_support', [])
        s1 = key_support[0] if key_support else None
        s2 = key_support[1] if len(key_support) >= 2 else None
        support_zone = s1 if s1 else 0
        resistance = h1.get('key_resistance', [])
        r1 = resistance[0] if resistance else None
        r2 = resistance[1] if len(resistance) >= 2 else None

        plan['direction'] = "做多（顺1H上涨趋势）"
        if support_zone:
            plan['entry_zone'] = f"{support_zone:.4f}~{support_zone + 0.003:.4f}"
            plan['stop_loss'] = f"{support_zone - 0.003:.4f}"
        plan['tp1'] = f"{r1:.4f}" if r1 else "待定"
        plan['tp2'] = f"{r2:.4f}" if r2 else "待定"
        plan['entry_condition'] = "15M出现看涨Pin Bar/吞没/连续看涨趋势柱 + 支撑区企稳"
        plan['invalidation'] = f"1H收盘跌破{support_zone - 0.003:.4f}" if support_zone else "1H结构转为LH+LL"

    elif trend_dir == "DOWN":
        key_resistance = h1.get('key_resistance', [])
        r1 = key_resistance[0] if key_resistance else None
        r2 = key_resistance[1] if len(key_resistance) >= 2 else None
        resistance_zone = r1 if r1 else 0
        support = h1.get('key_support', [])
        s1 = support[0] if support else None
        s2 = support[1] if len(support) >= 2 else None

        plan['direction'] = "做空（顺1H下跌趋势）"
        if resistance_zone:
            plan['entry_zone'] = f"{resistance_zone - 0.003:.4f}~{resistance_zone:.4f}"
            plan['stop_loss'] = f"{resistance_zone + 0.003:.4f}"
        plan['tp1'] = f"{s1:.4f}" if s1 else "待定"
        plan['tp2'] = f"{s2:.4f}" if s2 else "待定"
        plan['entry_condition'] = "15M出现看跌Pin Bar/吞没/连续看跌趋势柱 + 阻力区承压"
        plan['invalidation'] = f"1H收盘突破{resistance_zone + 0.003:.4f}" if resistance_zone else "1H结构转为HH+HL"

    else:
        plan['direction'] = "观望（1H震荡，无明确趋势）"
        plan['entry_zone'] = "等待趋势明确"
        plan['note'] = "震荡市不宜强行交易，等待1H结构突破"

    return plan


# ============================================================
# 报告格式化
# ============================================================

def format_report(h1, m15, plan):
    """格式化文本报告"""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    lines = []
    lines.append("=" * 60)
    lines.append(f"XRP-USDT 价格行为跟踪 | {now}")
    lines.append("=" * 60)

    # 1H 趋势
    lines.append(f"\n【1H 趋势】{plan.get('trend', '')}")
    lines.append(f"  当前价: {h1['current_price']:.4f}")
    lines.append(f"  摆动结构: HH={h1['swing']['HH']} LH={h1['swing']['LH']} HL={h1['swing']['HL']} LL={h1['swing']['LL']}")
    lines.append(f"  SMA20: {h1['sma20']:.4f} ({'上方' if h1['above_sma20'] else '下方'})")
    lines.append(f"  SMA50: {h1['sma50']:.4f} ({'上方' if h1['above_sma50'] else '下方'})")
    lines.append(f"  MA排列: {'多头' if h1['ma_bullish'] else '空头'}")
    lines.append(f"  近20根: 看涨{h1['bull_count_20']} 看跌{h1['bear_count_20']}")

    if h1.get('last_swing_high'):
        lines.append(f"  最近摆动高点: {h1['last_swing_high']['price']:.4f} ({h1['last_swing_high']['dt'][:16]})")
    if h1.get('last_swing_low'):
        lines.append(f"  最近摆动低点: {h1['last_swing_low']['price']:.4f} ({h1['last_swing_low']['dt'][:16]})")

    lines.append(f"\n  阻力: {', '.join(f'{r:.4f}' for r in h1.get('key_resistance', []))}")
    lines.append(f"  支撑: {', '.join(f'{s:.4f}' for s in h1.get('key_support', []))}")

    # 15M 回调
    lines.append(f"\n【15M 回调分析】")
    lines.append(f"  当前价: {m15['current_price']:.4f}")

    if 'pullback_pct' in m15:
        lines.append(f"  回调深度: {m15['pullback_pct']:.1f}%")
    if 'fib' in m15:
        lines.append(f"  斐波那契位:")
        for name, level in m15['fib'].items():
            marker = " ◄" if abs(m15['current_price'] - level) / level * 100 < 1.5 else ""
            lines.append(f"    {name}: {level:.4f}{marker}")

    lines.append(f"  趋势柱(近30根): 看涨{m15['bull_trend_bars']} 看跌{m15['bear_trend_bars']}")
    lines.append(f"  量能: {'缩量' if m15.get('volume_shrinking') else '放量'} (近期均量{m15.get('recent_avg_vol', 0):.0f})")
    lines.append(f"  近期看涨信号: {len(m15.get('bull_signals', []))}个 | 看跌信号: {len(m15.get('bear_signals', []))}个")

    # 回调评分
    lines.append(f"\n  回调结束评分: {m15.get('pullback_score', 0)}/5")
    for cond in m15.get('pullback_conditions', []):
        lines.append(f"    {cond}")
    lines.append(f"  ➡️ {m15.get('pullback_verdict', '')}")

    # 最近K线
    lines.append(f"\n  最近10根15M K线:")
    for bar in m15.get('recent_bars', []):
        lines.append(f"    {bar['dt']} | O={bar['open']:.4f} H={bar['high']:.4f} L={bar['low']:.4f} C={bar['close']:.4f} | {bar['dir']} 实体{bar['body_pct']}%")

    # 交易计划
    lines.append(f"\n【交易计划】")
    lines.append(f"  方向: {plan.get('direction', '')}")
    if plan.get('entry_zone'):
        lines.append(f"  入场区: {plan['entry_zone']}")
    if plan.get('stop_loss'):
        lines.append(f"  止损: {plan['stop_loss']}")
    if plan.get('tp1'):
        lines.append(f"  目标1: {plan['tp1']}")
    if plan.get('tp2'):
        lines.append(f"  目标2: {plan['tp2']}")
    if plan.get('entry_condition'):
        lines.append(f"  入场条件: {plan['entry_condition']}")
    if plan.get('invalidation'):
        lines.append(f"  计划失效: {plan['invalidation']}")
    lines.append(f"  回调评分: {plan.get('pullback_score', 'N/A')}")

    lines.append(f"\n{'=' * 60}")

    return "\n".join(lines)


# ============================================================
# ServerChan推送 (手机优化版)
# ============================================================

def push_notification(h1, m15, plan):
    """通过ServerChan推送分析结果（手机WeChat优化版）"""
    if not SC_SENDKEY:
        log("  ⚠️ SC_SENDKEY未配置，跳过推送")
        return None

    now = datetime.now(timezone.utc).strftime('%H:%M UTC')
    trend_dir = h1.get('trend_dir', '')
    score = m15.get('pullback_score', 0)
    verdict = m15.get('pullback_verdict', '')

    # ——— 动态标题 ———
    dir_icon = "📈" if trend_dir == "UP" else "📉" if trend_dir == "DOWN" else "📊"
    score_icon = " ✅" if score >= 4 else " ⚠️" if score >= 2 else " ❌"
    title = f"{dir_icon} XRP {h1.get('trend', '')} 回调评分{score}/5{score_icon}"

    lines = []
    sep = "━━━━━━━━━━━━━━━━━━"

    # ——— 顶部快速摘要 ———
    lines.append(sep)

    # 趋势行
    trend_line = f"{dir_icon} 趋势：1H {h1.get('trend', '')}"
    if h1.get('ma_bullish') is not None:
        trend_line += f"，MA{'多头' if h1['ma_bullish'] else '空头'}排列"
    trend_line += f"，现价{h1['current_price']:.4f}"
    lines.append(trend_line)

    # 趋势质量（知识库规则驱动的PA规则评估）
    pa_tq = h1.get('pa_trend_quality')
    if pa_tq:
        tq_bar = "●" * pa_tq["score"] + "○" * (pa_tq["total"] - pa_tq["score"])
        tq_emoji = {"strong_bullish": "🟢🟢", "bullish": "🟢", "weak_bullish": "🟡",
                    "range": "⚪", "weak_bearish": "🟠", "bearish": "🔴", "strong_bearish": "🔴🔴"}
        emoji = tq_emoji.get(pa_tq["verdict"], "⚪")
        lines.append(f"📐 趋势质量: {pa_tq['score']}/{pa_tq['total']} {tq_bar} {emoji}")
        for r in pa_tq["details"]:
            icon = "✅" if r["passed"] else "❌"
            if r["kb_rules"]:
                lines.append(f"  {icon} {r['detail']} 📚")
            else:
                lines.append(f"  {icon} {r['detail']}")

    # 回调行
    pullback_info = f"📉 回调：15M"
    if 'pullback_pct' in m15:
        pullback_info += f" 回撤{m15['pullback_pct']:.1f}%"
    bull_signals = m15.get('bull_signals', [])
    bear_signals = m15.get('bear_signals', [])
    if trend_dir == "UP" and bull_signals:
        pullback_info += f"，出现看涨信号({len(bull_signals)}个)"
    elif trend_dir == "DOWN" and bear_signals:
        pullback_info += f"，出现看跌信号({len(bear_signals)}个)"
    else:
        pullback_info += f"，无明确PA信号"
    lines.append(pullback_info)

    # 评分行（5分 bar）
    score_bar = "●" * score + "○" * (5 - score)
    vol_icon = "✅" if m15.get('volume_shrinking') else "⚠️"
    vol_text = "缩量" if m15.get('volume_shrinking') else "放量"
    lines.append(f"🎯 评分：{score}/5 {score_bar} | 量能：{vol_icon}{vol_text}")

    # 趋势柱统计
    bull_bars = m15.get('bull_trend_bars', 0)
    bear_bars = m15.get('bear_trend_bars', 0)
    lines.append(f"📊 近30根：🟢{bull_bars} 🔴{bear_bars}")

    lines.append(f"💡 结论：{verdict}")
    lines.append("")

    # ——— 交易计划 ———
    lines.append("📋 交易计划")
    dir_emoji = "🟢" if plan.get('direction') == "做多" else "🔴" if plan.get('direction') == "做空" else "⚪"
    lines.append(f"方向：{plan.get('direction', 'N/A')} {dir_emoji}")
    if plan.get('entry_zone'):
        lines.append(f"入场：{plan['entry_zone']}")
    if plan.get('stop_loss'):
        lines.append(f"止损：{plan['stop_loss']}")
    tps = []
    if plan.get('tp1'):
        tps.append(f"目标1：{plan['tp1']}")
    if plan.get('tp2'):
        tps.append(f"目标2：{plan['tp2']}")
    if tps:
        lines.append(" | ".join(tps))
    if plan.get('entry_condition'):
        lines.append(f"条件：{plan['entry_condition']}")
    if plan.get('invalidation'):
        lines.append(f"失效：{plan['invalidation']}")
    lines.append("")

    # ——— 知识库增强（如果有） ———
    kb_parts = []
    kb_trend = h1.get('kb_enhance_trend', [])
    kb_pullback = m15.get('kb_enhance_pullback', [])
    for item in kb_trend + kb_pullback:
        if isinstance(item, str) and item.startswith("[KB]"):
            kb_parts.append(item)
    if kb_parts:
        lines.append("📚 知识库规则")
        for item in kb_parts[:4]:  # 最多4条，省空间
            lines.append(f"• {item}")
        lines.append("")

    # ——— 变化检测 ———
    changes = detect_changes(h1, m15, plan)
    if changes:
        lines.append("⚡ 变化")
        for change in changes[:3]:  # 最多3条
            lines.append(f"• {change}")
        lines.append("")

    # ——— 尾部 ———
    lines.append("⚠️ 10x杠杆，严格风控")
    lines.append(sep)
    lines.append(f"🕐 {now} | 知识库识图增强")

    desp = "\n".join(lines)

    try:
        url = f"https://sctapi.ftqq.com/{SC_SENDKEY}.send"
        data = urllib.parse.urlencode({
            "title": title,
            "desp": desp,
            "tags": "分析总结",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if result.get('code') == 0:
            return result
        else:
            log(f"  ❌ ServerChan返回错误: {result}")
            return None
    except Exception as e:
        log(f"  ❌ ServerChan推送异常: {e}")
        return None


# ============================================================
# 变化检测
# ============================================================

def detect_changes(h1, m15, plan):
    """检测与上次报告的关键变化"""
    changes = []
    prev_path = os.path.join(OUTPUT_DIR, "prev_state.json")

    current_state = {
        'trend_dir': h1.get('trend_dir', ''),
        'trend': h1.get('trend', ''),
        'price': h1['current_price'],
        'pullback_score': m15.get('pullback_score', 0),
        'above_sma20': h1.get('above_sma20', False),
        'above_sma50': h1.get('above_sma50', False),
        'ma_bullish': h1.get('ma_bullish', False),
        'volume_shrinking': m15.get('volume_shrinking', False),
    }

    if os.path.exists(prev_path):
        try:
            with open(prev_path, 'r') as f:
                prev = json.load(f)
        except Exception:
            prev = None

        if prev:
            if prev.get('trend_dir') != current_state['trend_dir']:
                changes.append(f"📊 趋势方向变化: {prev.get('trend_dir','')} → {current_state['trend_dir']}")
            if prev.get('trend') != current_state['trend']:
                changes.append(f"📊 趋势状态变化: {prev.get('trend','')} → {current_state['trend']}")
            if prev.get('ma_bullish') != current_state['ma_bullish']:
                changes.append(f"📈 MA排列反转: {'多头→空头' if prev.get('ma_bullish') else '空头→多头'}")
            if prev.get('above_sma20') != current_state['above_sma20']:
                changes.append(f"📉 价格穿越SMA20: {'上方→下方' if prev.get('above_sma20') else '下方→上方'}")
            if prev.get('volume_shrinking') != current_state['volume_shrinking']:
                changes.append(f"📊 量能变化: {'缩量→放量' if prev.get('volume_shrinking') else '放量→缩量'}")
            if prev.get('pullback_score', -1) != current_state['pullback_score']:
                changes.append(f"🎯 回调评分变化: {prev.get('pullback_score', 0)}→{current_state['pullback_score']}/5")

    # 保存当前状态
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(prev_path, 'w') as f:
            json.dump(current_state, f)
    except Exception as e:
        log(f"  ⚠️ 保存当前状态失败: {e}")

    return changes


# ============================================================
# 主流程
# ============================================================

def main():
    log("🚀 开始执行 XRP-USDT 跟踪分析...")

    # 1. 知识库规则提取
    log("📚 搜索知识库规则...")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rule_engine = {}
    knowledge = {}
    try:
        knowledge = fetch_knowledge_rules()
        rule_engine = build_rule_engine(knowledge)
    except Exception as e:
        log(f"  ⚠️ 知识库集成失败: {e}，继续使用内置规则")

    # 2. 拉取数据
    log("📥 拉取1H数据...")
    h1_raw = fetch_candles("XRP-USDT", "1h", 200)
    if not h1_raw:
        log("❌ 1H数据拉取失败，跳过本次分析")
        return
    h1_candles = parse_candles(h1_raw)
    log(f"  ✅ 1H: {len(h1_candles)}根, {h1_candles[0]['dt']} ~ {h1_candles[-1]['dt']}")

    log("📥 拉取15M数据...")
    m15_raw = fetch_candles("XRP-USDT", "15m", 300)
    if not m15_raw:
        log("❌ 15M数据拉取失败，跳过本次分析")
        return
    m15_candles = parse_candles(m15_raw)
    log(f"  ✅ 15M: {len(m15_candles)}根, {m15_candles[0]['dt']} ~ {m15_candles[-1]['dt']}")

    # 3. 分析
    log("📊 执行1H趋势分析...")
    h1_result = analyze_1h(h1_candles)
    if rule_engine:
        h1_result['kb_enhance_trend'] = apply_knowledge_to_trend(h1_result, rule_engine)
    log(f"  趋势: {h1_result.get('trend', '')} | 方向: {h1_result.get('trend_dir', '')}")

    # 价格行为趋势规则评估（知识库规则+PA规则双驱动）
    matched_kb = match_kb_rules_to_pa(rule_engine) if rule_engine else {}
    pa_tq = evaluate_pa_trend_rules(h1_candles, h1_result, matched_kb)
    h1_result['pa_trend_quality'] = pa_tq
    log(f"  PA规则评估: {pa_tq['passed']}/{pa_tq['total']} 通过, 质量评级: {pa_tq['verdict']}")
    if pa_tq['kb_matched_count'] > 0:
        log(f"  知识库匹配: {pa_tq['kb_matched_count']}条规则参与评估")

    log("📊 执行15M回调分析...")
    m15_result = analyze_15m(m15_candles, h1_result)
    if rule_engine:
        m15_result['kb_enhance_pullback'] = apply_knowledge_to_pullback(m15_result, h1_result, rule_engine)
    log(f"  回调评分: {m15_result.get('pullback_score', 0)}/5 → {m15_result.get('pullback_verdict', '')}")

    # 4. 交易计划
    log("📋 生成交易计划...")
    plan = generate_plan(h1_result, m15_result)

    # 5. 输出报告
    report = format_report(h1_result, m15_result, plan)
    latest_path = os.path.join(OUTPUT_DIR, "latest_report.txt")
    with open(latest_path, "w") as f:
        f.write(report)
    log(f"✅ 报告已保存: {latest_path}")

    # 6. ServerChan推送
    log("📤 推送分析结果到ServerChan...")
    push_result = push_notification(h1_result, m15_result, plan)
    if push_result:
        log(f"  ✅ 推送成功 (pushid: {push_result.get('data', {}).get('pushid', 'N/A')})")
    else:
        log("  ❌ 推送失败")

    log("✅ 分析完成")


if __name__ == "__main__":
    main()
