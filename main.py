"""
勞動法令日報 → LINE 卡片（Flex Message）推播
免費組合：GitHub Actions（排程）＋ Gemini API 免費額度（摘要）＋ LINE 官方帳號輕用量（推播）

每天做兩件事：
  A. 勞動部最新公告：新聞稿、公布欄、即時新聞澄清。只要有「沒推過的新公告」就列出。
  B. 媒體新聞摘要：只抓台灣時間「昨天」主要新聞網站的勞動新聞，交給 AI 摘要。
  兩者都沒有新內容時，當天不推播。

必要環境變數（放在 GitHub Secrets）：
  GEMINI_API_KEY             Google AI Studio 申請的 API 金鑰
  LINE_CHANNEL_ACCESS_TOKEN  LINE Developers 的 Channel access token（long-lived）
  LINE_USER_ID               你自己的 LINE User ID（只在 LINE_PUSH_MODE=self 時使用）

選用環境變數：
  GEMINI_MODEL     以逗號分隔的模型清單，預設依序嘗試 gemini-flash-latest 等三個
  MAX_ITEMS        媒體新聞最多顯示幾則，預設 6
  MAX_MOL          勞動部公告最多顯示幾則，預設 8
  LINE_PUSH_MODE   broadcast（預設，推給所有好友）或 self（只推給自己）
  DRY_RUN          設為 1 時只印出卡片內容，不推播、不寫入推播紀錄（測試用）
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import feedparser
import requests

TW = timezone(timedelta(hours=8))
START_TIME = time.time()
HISTORY_FILE = Path(__file__).with_name("sent_history.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# ---------------- 資料來源（可自行增減） ----------------
# 勞動部官方 RSS（網址來自勞動部 RSS 訂閱頁）
MOL_FEEDS = [
    ("新聞稿", "https://www.mol.gov.tw/1607/1632/1633/RssList"),
    ("公布欄", "https://www.mol.gov.tw/1607/1632/2665/RssList"),
    ("澄清", "https://www.mol.gov.tw/1607/1632/2660/RssList"),
]
MOL_LOOKBACK_DAYS = 3  # 只看最近 3 天的公告（涵蓋週末），再用推播紀錄排除推過的

KEYWORD_QUERIES = [
    "勞基法 OR 勞動基準法 OR 勞動部",
    "基本工資 OR 最低工資 OR 加班費 OR 工時",
    "勞保 OR 勞退 OR 職災 OR 職業安全衛生",
    "職場霸凌 OR 性騷擾防治 OR 育嬰留職停薪 OR 資遣",
    "移工 OR 勞動檢查 OR 違反勞基法 裁罰",
]

# Google 新聞「白名單」：只收這些新聞網站「主站」的報導（看網域判斷）。
# 轉載站與子網站（例如 stock.ltn.com.tw、ai.newtalk.tw、LINE TODAY、Yahoo）
# 常把舊文章重新上架、日期顯示成最新，所以一律不收。
TRUSTED_HOSTS = {
    "www.cna.com.tw", "cna.com.tw", "www.rti.org.tw", "news.pts.org.tw",
    "news.ltn.com.tw", "ec.ltn.com.tw",
    "udn.com", "money.udn.com",
    "www.chinatimes.com", "www.ctee.com.tw",
    "www.setn.com", "www.ettoday.net", "news.tvbs.com.tw", "news.ebc.net.tw",
    "www.storm.mg", "www.cw.com.tw", "www.businesstoday.com.tw", "www.mirrormedia.mg",
    "www.nownews.com", "newtalk.tw", "www.thenewslens.com", "www.twreporter.org",
}

CATEGORY_COLORS = {
    "法規修正": "#0F6E56",
    "函釋解釋": "#185FA5",
    "裁罰判決": "#993C1D",
    "立法動態": "#534AB7",
    "政策動態": "#854F0B",
}
MOL_COLORS = {"新聞稿": "#0F6E56", "公布欄": "#185FA5", "澄清": "#993C1D"}


# ---------------- 共用工具 ----------------
def yesterday_range():
    """回傳台灣時間「昨天 00:00」到「今天 00:00」"""
    today0 = datetime.now(TW).replace(hour=0, minute=0, second=0, microsecond=0)
    return today0 - timedelta(days=1), today0


def entry_time(e):
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def clean(text, limit=None):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def norm_title(t):
    t = re.sub(r"\s[-–|｜]\s[^-–|｜]+$", "", t)  # 去掉 Google 新聞標題尾端的媒體名
    return re.sub(r"[\s\W_]+", "", t)[:40]


def get_feed(name, url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return feedparser.parse(resp.content).entries
    except Exception as ex:  # 單一來源失敗不影響其他來源
        print(f"[警告] 抓取失敗 {name}: {ex}", file=sys.stderr)
        return []


# ---------------- 推播紀錄（避免重複推播） ----------------
def load_history():
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return {"mol": list(data.get("mol", [])), "news": list(data.get("news", []))}
    except Exception:
        return {"mol": [], "news": []}


def save_history(history):
    for k in history:
        history[k] = history[k][-800:]  # 只保留最近 800 筆
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")


def history_key(title, url=""):
    return norm_title(title) or url


# ---------------- A. 勞動部最新公告 ----------------
def fetch_mol(history):
    seen_keys = set(history["mol"])
    since = datetime.now(TW) - timedelta(days=MOL_LOOKBACK_DAYS)
    items, keys = [], set()
    for kind, url in MOL_FEEDS:
        entries = get_feed(f"勞動部{kind}", url)
        print(f"勞動部{kind}：RSS 共 {len(entries)} 則")
        for e in entries:
            title = clean(e.get("title"))
            t = entry_time(e)
            if not title or (t and t.astimezone(TW) < since):
                continue
            key = history_key(title, e.get("link", ""))
            if key in seen_keys or key in keys:
                continue
            keys.add(key)
            items.append({
                "kind": kind,
                "title": title,
                "url": e.get("link", "") or "https://www.mol.gov.tw/1607/1632/",
                "date": t.astimezone(TW).strftime("%m/%d") if t else "",
                "key": key,
                "sort": t or datetime.now(timezone.utc),
            })
    items.sort(key=lambda x: x["sort"], reverse=True)
    print(f"勞動部新公告（未推播過）：{len(items)} 則")
    return items


# ---------------- B. 媒體新聞 ----------------
def build_news_feeds(start, end):
    # after:/before: 讓 Google 新聞只回傳昨天前後的報導，之後程式再精確篩選
    date_filter = f"after:{(start - timedelta(days=1)):%Y-%m-%d} before:{end:%Y-%m-%d}"
    return [
        "https://news.google.com/rss/search?q=" + quote(f"({q}) {date_filter}")
        + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        for q in KEYWORD_QUERIES
    ]


def fetch_news(history, mol_items):
    start, end = yesterday_range()
    print(f"媒體新聞抓取範圍（台灣時間）：{start:%Y-%m-%d %H:%M} ～ {end:%Y-%m-%d %H:%M}")
    sent = set(history["news"])
    seen = {m["key"] for m in mol_items}  # 跟勞動部公告重複的標題不再列入
    items = []
    dropped = {"無日期": 0, "不是昨天": 0, "非白名單網站": 0, "推播過": 0}
    for url in build_news_feeds(start, end):
        for e in get_feed("Google 新聞", url):
            title = clean(e.get("title"))
            t = entry_time(e)
            if t is None:
                dropped["無日期"] += 1
                continue
            if not (start <= t.astimezone(TW) < end):
                dropped["不是昨天"] += 1
                continue
            source = e.get("source") or {}
            host = urlparse(source.get("href", "")).netloc.lower()
            if host not in TRUSTED_HOSTS:
                dropped["非白名單網站"] += 1
                continue
            key = history_key(title)
            if not title or key in seen:
                continue
            if key in sent:
                dropped["推播過"] += 1
                continue
            seen.add(key)
            items.append({
                "title": title,
                "snippet": clean(e.get("summary"), 200),
                "source": clean(source.get("title")) or host,
                "url": e.get("link", ""),
                "date": t.astimezone(TW).strftime("%m/%d"),
                "key": key,
            })
    print(f"排除：{dropped}")
    print(f"媒體新聞候選：{len(items)} 則")
    return items[:60]


# ---------------- 連結還原與日期檢查 ----------------
def decode_google_links(items):
    """把 Google 新聞的轉址連結還原成新聞網站的原始網址。
    Google 的轉址連結在手機上常會出現「We're sorry...」頁面，所以一定要還原。"""
    google = [it for it in items if "news.google.com" in it["url"]]
    if not google:
        return items
    try:
        from googlenewsdecoder import gnewsdecoder
        results = gnewsdecoder([it["url"] for it in google], interval=1)
    except Exception as ex:
        print(f"[警告] 連結還原失敗：{ex}", file=sys.stderr)
        results = [{"success": False}] * len(google)
    ok = 0
    for it, r in zip(google, results):
        if r.get("success") and r.get("decoded_url"):
            it["url"] = r["decoded_url"]
            ok += 1
        else:
            it["url"] = ""  # 還原失敗的不推播，避免點開是錯誤頁
    print(f"Google 新聞連結還原：成功 {ok}／{len(google)} 則")
    return [it for it in items if it["url"]]


DATE_PATTERNS = [
    r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)',
    r'content=["\']([^"\']+)["\'][^>]*property=["\']article:published_time',
    r'itemprop=["\']datePublished["\'][^>]*content=["\']([^"\']+)',
    r'name=["\'](?:pubdate|publishdate|date)["\'][^>]*content=["\']([^"\']+)',
    r'"datePublished"\s*:\s*"([^"]+)"',
]


def published_date(url):
    """讀取新聞網頁標示的發布日期；讀不到就回傳 None。"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        page = r.text[:300000]
    except Exception:
        return None
    for p in DATE_PATTERNS:
        m = re.search(p, page, re.I)
        if not m:
            continue
        s = m.group(1).strip()
        m2 = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
        if m2:
            y, mo, d = map(int, m2.groups())
            try:
                return datetime(y, mo, d, tzinfo=TW)
            except ValueError:
                return None
    return None


def drop_old_articles(items):
    """打開每篇新聞，網頁上的發布日期早於昨天的（舊文換日期重發）就排除。"""
    start, _ = yesterday_range()
    kept = []
    for it in items:
        d = published_date(it["url"])
        if d and d < start:
            print(f"  排除舊文（網頁發布日 {d:%Y-%m-%d}）：{it['headline'][:30]}")
            continue
        kept.append(it)
    return kept


# ---------------- Gemini 摘要 ----------------
PROMPT = """你是台灣企業人資法遵顧問。今天是 {today}，以下是「昨天（{yesterday}）」的勞動相關新聞清單（含編號）。
請完成：
1. 刪除與「台灣勞動法令、勞動政策、勞資爭議、裁罰判決」無關的內容，以及重複報導同一事件的新聞（保留最權威的來源）。
   也要刪除看起來是舊聞的項目：例如內容提到的事件、會議或公布日期明顯早於昨天，或是「懶人包、整理、回顧」類的舊文重發。
2. 依對企業人資的重要性排序，最多挑出 {max_items} 則。
3. 每則分類為以下其一：法規修正、函釋解釋、裁罰判決、立法動態、政策動態。
4. 每則寫：
   - headline：20 字內的重點標題
   - impact：25 字內「對企業人資的影響或建議行動」
5. 只能根據清單內容，不要推測或補充清單沒有的事實。
6. 若有提到生效日或截止期限，整理在 deadlines（每項 25 字內），沒有就給空陣列。

只輸出 JSON，格式：
{{"items":[{{"idx":編號,"category":"分類","headline":"...","impact":"..."}}],"deadlines":["..."]}}

新聞清單：
{news}
"""

GEMINI_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODEL", "gemini-flash-latest,gemini-flash-lite-latest,gemini-2.5-flash"
    ).split(",")
    if m.strip()
]
GEMINI_SECONDS = 300  # AI 最多嘗試 5 分鐘，確保整體在時限內一定推得出去


def call_gemini(model, api_key, body, deadline):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    for attempt in range(3):  # 免費額度偶爾回 429/503，等一下再試
        left = deadline - time.time()
        if left < 20:
            raise RuntimeError("已超過 AI 嘗試時間上限")
        r = requests.post(url, headers={"x-goog-api-key": api_key}, json=body, timeout=min(120, left))
        if r.status_code in (429, 500, 503) and attempt < 2 and deadline - time.time() > 60:
            time.sleep(20 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {r.text[:500]}")
        break
    res = r.json()
    cands = res.get("candidates") or []
    if not cands:
        raise RuntimeError(f"沒有回傳內容：{json.dumps(res.get('promptFeedback', res), ensure_ascii=False)[:300]}")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise RuntimeError(f"回傳內容是空的（finishReason={cands[0].get('finishReason')}）")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise RuntimeError(f"回傳的不是 JSON：{text[:200]}")
        return json.loads(m.group(0))


def summarize(items, max_items):
    api_key = os.environ["GEMINI_API_KEY"].strip()
    news_text = "\n".join(
        f"[{i}] {it['title']}｜{it['source']}｜{it['date']}｜{it['snippet']}"
        for i, it in enumerate(items)
    )
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(
            max_items=max_items, news=news_text,
            today=datetime.now(TW).strftime("%Y-%m-%d"),
            yesterday=yesterday_range()[0].strftime("%Y-%m-%d"),
        )}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    deadline = time.time() + GEMINI_SECONDS
    data, errors = None, []
    for model in GEMINI_MODELS:  # 第一個模型失敗就自動換下一個
        try:
            data = call_gemini(model, api_key, body, deadline)
            print(f"Gemini 摘要成功（模型：{model}）")
            break
        except Exception as ex:
            errors.append(f"{model}: {ex}")
            print(f"[警告] 模型 {model} 失敗：{ex}", file=sys.stderr)
    if data is None:
        raise RuntimeError(" / ".join(errors))

    picked, used = [], set()
    for x in data.get("items", []):
        idx = x.get("idx")
        if not isinstance(idx, int) or not (0 <= idx < len(items)) or idx in used:
            continue  # 連結一律用原始資料，不採用 AI 產生的網址
        used.add(idx)
        src = items[idx]
        picked.append({
            "category": x.get("category") if x.get("category") in CATEGORY_COLORS else "政策動態",
            "headline": clean(x.get("headline")) or src["title"],
            "impact": clean(x.get("impact")),
            "source": src["source"],
            "date": src["date"],
            "url": src["url"],
            "key": src["key"],
        })
    return picked, [clean(d) for d in data.get("deadlines", [])][:3]


def fallback(items):
    """Gemini 失敗時：仍推播標題清單，不中斷。"""
    return [{
        "category": "政策動態",
        "headline": it["title"][:40],
        "impact": "（AI 摘要暫時無法使用，請點原文）",
        "source": it["source"],
        "date": it["date"],
        "url": it["url"],
        "key": it["key"],
    } for it in items], []


# ---------------- 組 LINE Flex 卡片 ----------------
def safe_uri(u):
    return u if u.startswith("http") and len(u) <= 1000 else "https://www.mol.gov.tw/1607/1632/1633/"


def section_title(text):
    return {"type": "text", "text": text, "size": "sm", "weight": "bold", "color": "#06C755", "margin": "lg"}


def build_flex(mol_items, mol_more, news, deadlines, today):
    body = []
    if mol_items:
        body.append(section_title(f"■ 勞動部最新公告（{len(mol_items) + mol_more} 則）"))
        for it in mol_items:
            body.append({
                "type": "box", "layout": "vertical", "spacing": "xs", "margin": "md",
                "action": {"type": "uri", "label": "公告", "uri": safe_uri(it["url"])},
                "contents": [
                    {"type": "text", "text": f"{it['kind']}　{it['date']}", "size": "xxs",
                     "weight": "bold", "color": MOL_COLORS.get(it["kind"], "#185FA5")},
                    {"type": "text", "text": it["title"], "size": "sm", "wrap": True,
                     "color": "#111111", "maxLines": 3},
                ],
            })
        if mol_more:
            body.append({"type": "text", "text": f"另有 {mol_more} 則，請點下方「勞動部公告」查看",
                         "size": "xxs", "color": "#888888", "margin": "sm"})

    if news:
        if body:
            body.append({"type": "separator", "margin": "lg"})
        body.append(section_title(f"■ 昨日勞動新聞（{len(news)} 則）"))
        for i, it in enumerate(news):
            if i:
                body.append({"type": "separator", "margin": "md"})
            block = [
                {"type": "text", "text": it["category"], "size": "xxs",
                 "color": CATEGORY_COLORS[it["category"]], "weight": "bold"},
                {"type": "text", "text": it["headline"], "size": "sm", "wrap": True,
                 "weight": "bold", "color": "#111111"},
            ]
            if it["impact"]:
                block.append({"type": "text", "text": "▸ " + it["impact"], "size": "xs",
                              "wrap": True, "color": "#555555"})
            block.append({"type": "text", "text": f"{it['source']}｜{it['date']}　看原文 ›",
                          "size": "xxs", "color": "#06C755"})
            body.append({
                "type": "box", "layout": "vertical", "spacing": "xs", "margin": "md",
                "contents": block,
                "action": {"type": "uri", "label": "原文", "uri": safe_uri(it["url"])},
            })

    if deadlines:
        body.append({"type": "separator", "margin": "lg"})
        body.append({
            "type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#FAEEDA",
            "cornerRadius": "md", "paddingAll": "8px",
            "contents": [{"type": "text", "text": "⏰ 近期生效／期限", "size": "xxs",
                          "color": "#633806", "weight": "bold"}]
            + [{"type": "text", "text": "• " + d, "size": "xs", "wrap": True, "color": "#633806"}
               for d in deadlines],
        })

    parts = []
    if mol_items:
        parts.append(f"勞動部公告 {len(mol_items) + mol_more} 則")
    if news:
        parts.append(f"新聞 {len(news)} 則")
    bubble = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#06C755", "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "勞動法令日報", "color": "#FFFFFF", "weight": "bold", "size": "md"},
                {"type": "text", "text": f"{today}｜" + "、".join(parts), "color": "#E8FFF0", "size": "xs"},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "contents": body, "paddingAll": "12px"},
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "uri", "label": "勞動部公告", "uri": "https://www.mol.gov.tw/1607/1632/2665/"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "uri", "label": "勞動部新聞稿", "uri": "https://www.mol.gov.tw/1607/1632/1633/"}},
            ],
        },
    }
    titles = [m["title"] for m in mol_items] + [n["headline"] for n in news]
    alt = f"勞動法令日報 {today}：" + "；".join(titles)
    return {"type": "flex", "altText": alt[:395], "contents": bubble}


# ---------------- 推播 ----------------
def push(message):
    if os.environ.get("DRY_RUN") == "1":
        print(json.dumps(message, ensure_ascii=False, indent=2))
        return
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"].strip()
    mode = os.environ.get("LINE_PUSH_MODE", "broadcast").strip().lower()
    if mode == "self":
        url = "https://api.line.me/v2/bot/message/push"
        payload = {"to": os.environ["LINE_USER_ID"].strip(), "messages": [message]}
    else:
        url = "https://api.line.me/v2/bot/message/broadcast"
        payload = {"messages": [message]}
    r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=30)
    if r.status_code != 200:
        print(f"[錯誤] LINE 推播失敗 {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()
    print(f"LINE 推播成功（模式：{'只推給自己' if mode == 'self' else '推給所有好友'}）")


# ---------------- 主流程 ----------------
def main():
    max_items = int(os.environ.get("MAX_ITEMS", "6"))
    max_mol = int(os.environ.get("MAX_MOL", "8"))
    wk = "一二三四五六日"[datetime.now(TW).weekday()]
    today = datetime.now(TW).strftime("%m/%d") + f"（{wk}）"
    history = load_history()

    # A. 勞動部新公告
    mol_all = fetch_mol(history)
    mol_items, mol_more = mol_all[:max_mol], max(0, len(mol_all) - max_mol)

    # B. 媒體新聞
    news, deadlines = [], []
    candidates = fetch_news(history, mol_all)
    if candidates:
        try:
            # 多挑幾則備用：之後連結還原失敗或判定為舊文的會被剔除
            news, deadlines = summarize(candidates, max_items + 4)
        except Exception as ex:
            print(f"::warning::Gemini 摘要失敗，已改推標題清單。原因：{ex}")
            news, deadlines = fallback(candidates[: max_items + 4])
        news = decode_google_links(news)
        news = drop_old_articles(news)[:max_items]

    # 兩者都沒有新內容：今天不推播
    if not mol_items and not news:
        print("今天沒有新的勞動部公告，也沒有昨日相關新聞，不推播。")
        return

    push(build_flex(mol_items, mol_more, news, deadlines, today))

    if os.environ.get("DRY_RUN") != "1":
        history["mol"] += [m["key"] for m in mol_all]
        history["news"] += [n["key"] for n in news]
        save_history(history)
        print("已更新推播紀錄 sent_history.json")
    print(f"總執行時間：{time.time() - START_TIME:.0f} 秒")


if __name__ == "__main__":
    main()
