"""
勞動法令每日摘要 → LINE 卡片（Flex Message）推播
免費組合：GitHub Actions（排程）＋ Gemini API 免費額度（摘要）＋ LINE 官方帳號輕用量（推播）

必要環境變數（放在 GitHub Secrets）：
  GEMINI_API_KEY             Google AI Studio 申請的 API 金鑰
  LINE_CHANNEL_ACCESS_TOKEN  LINE Developers 的 Channel access token（long-lived）
  LINE_USER_ID               你自己的 LINE User ID（U 開頭）

選用環境變數：
  GEMINI_MODEL   預設 gemini-flash-latest（Google 會自動指向最新的 Flash 免費模型）
  HOURS_BACK     抓取多少小時內的新聞，預設 26
  MAX_ITEMS      卡片最多顯示幾則，預設 6
  DRY_RUN        設為 1 時只印出卡片內容，不實際推播（測試用）
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import requests

TW = timezone(timedelta(hours=8))

# ---------------- 資料來源（可自行增減） ----------------
KEYWORD_QUERIES = [
    "勞基法 OR 勞動基準法 OR 勞動部",
    "基本工資 OR 最低工資 OR 加班費 OR 工時",
    "勞保 OR 勞退 OR 職災 OR 職業安全衛生",
    "職場霸凌 OR 性騷擾防治 OR 育嬰留職停薪 OR 資遣",
    "移工 OR 勞動檢查 OR 違反勞基法 裁罰",
]

FEEDS = [
    ("勞動部新聞稿", "https://www.mol.gov.tw/1607/1632/1633/RssList"),
    ("勞動部即時新聞澄清", "https://www.mol.gov.tw/1607/1632/2660/RssList"),
] + [
    (
        "Google 新聞",
        "https://news.google.com/rss/search?q="
        + quote(f"({q}) when:1d")
        + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    )
    for q in KEYWORD_QUERIES
]

CATEGORY_COLORS = {
    "法規修正": "#0F6E56",
    "函釋解釋": "#185FA5",
    "裁罰判決": "#993C1D",
    "立法動態": "#534AB7",
    "政策動態": "#854F0B",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (labor-law-digest)"}


# ---------------- 1. 抓新聞 ----------------
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


def fetch_news(hours_back):
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    items, seen = [], set()
    for source_name, url in FEEDS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as ex:  # 單一來源失敗不影響其他來源
            print(f"[警告] 抓取失敗 {source_name}: {ex}", file=sys.stderr)
            continue
        for e in feed.entries:
            t = entry_time(e)
            if t and t < since:
                continue
            title = clean(e.get("title"))
            key = norm_title(title)
            if not title or key in seen:
                continue
            seen.add(key)
            src = source_name
            if source_name == "Google 新聞":
                src = clean((e.get("source") or {}).get("title")) or "Google 新聞"
            items.append(
                {
                    "title": title,
                    "snippet": clean(e.get("summary"), 200),
                    "source": src,
                    "url": e.get("link", ""),
                    "date": (t or datetime.now(timezone.utc)).astimezone(TW).strftime("%m/%d"),
                }
            )
    # 官方來源優先排前面
    items.sort(key=lambda x: 0 if x["source"].startswith("勞動部") else 1)
    return items[:60]


# ---------------- 2. Gemini 摘要 ----------------
PROMPT = """你是台灣企業人資法遵顧問。以下是過去一天抓到的勞動相關新聞清單（含編號）。
請完成：
1. 刪除與「台灣勞動法令、勞動政策、勞資爭議、裁罰判決」無關的內容，以及重複報導同一事件的新聞（保留最權威的來源，勞動部優先）。
2. 依對企業人資的重要性，最多挑出 {max_items} 則。
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


def call_gemini(model, api_key, body):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    for attempt in range(3):  # 免費額度偶爾回 429/503，等一下再試
        r = requests.post(url, headers={"x-goog-api-key": api_key}, json=body, timeout=150)
        if r.status_code in (429, 500, 503) and attempt < 2:
            time.sleep(30 * (attempt + 1))
            continue
        if r.status_code != 200:
            # 印出 Google 回傳的完整錯誤說明，方便判斷原因
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
        m = re.search(r"\{.*\}", text, re.S)  # 有時前後多了說明文字
        if not m:
            raise RuntimeError(f"回傳的不是 JSON：{text[:200]}")
        return json.loads(m.group(0))


def summarize(items, max_items):
    api_key = os.environ["GEMINI_API_KEY"].strip()  # 去掉貼上金鑰時多帶的空白或換行
    news_text = "\n".join(
        f"[{i}] {it['title']}｜{it['source']}｜{it['date']}｜{it['snippet']}"
        for i, it in enumerate(items)
    )
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(max_items=max_items, news=news_text)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    data, errors = None, []
    for model in GEMINI_MODELS:  # 第一個模型失敗就自動換下一個
        try:
            data = call_gemini(model, api_key, body)
            print(f"Gemini 摘要成功（模型：{model}）")
            break
        except Exception as ex:
            errors.append(f"{model}: {ex}")
            print(f"[警告] 模型 {model} 失敗：{ex}", file=sys.stderr)
    if data is None:
        raise RuntimeError(" / ".join(errors))

    picked = []
    for x in data.get("items", []):
        idx = x.get("idx")
        if not isinstance(idx, int) or not (0 <= idx < len(items)):
            continue  # 連結一律用原始資料，不採用 AI 產生的網址
        src = items[idx]
        picked.append(
            {
                "category": x.get("category") if x.get("category") in CATEGORY_COLORS else "政策動態",
                "headline": clean(x.get("headline")) or src["title"],
                "impact": clean(x.get("impact")),
                "source": src["source"],
                "date": src["date"],
                "url": src["url"],
            }
        )
    return picked[:max_items], [clean(d) for d in data.get("deadlines", [])][:3]


def fallback(items, max_items):
    """Gemini 失敗時：仍推播標題清單，不中斷。"""
    return [
        {
            "category": "政策動態",
            "headline": it["title"][:40],
            "impact": "（AI 摘要暫時無法使用，請點原文）",
            "source": it["source"],
            "date": it["date"],
            "url": it["url"],
        }
        for it in items[:max_items]
    ], []


# ---------------- 3. 組 LINE Flex 卡片 ----------------
def safe_uri(u):
    return u if u.startswith("http") and len(u) <= 1000 else "https://www.mol.gov.tw/1607/1632/1633/"


def build_flex(picked, deadlines, today):
    body = []
    for i, it in enumerate(picked):
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

    bubble = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#06C755", "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "勞動法令日報", "color": "#FFFFFF", "weight": "bold", "size": "md"},
                {"type": "text", "text": f"{today}｜今日 {len(picked)} 則重點",
                 "color": "#E8FFF0", "size": "xs"},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "contents": body, "paddingAll": "12px"},
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "uri", "label": "勞動部新聞稿",
                            "uri": "https://www.mol.gov.tw/1607/1632/1633/"}},
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "uri", "label": "法規資料庫",
                            "uri": "https://law.moj.gov.tw/News/NewsList.aspx"}},
            ],
        },
    }
    alt = f"勞動法令日報 {today}：" + "；".join(p["headline"] for p in picked)
    return {"type": "flex", "altText": alt[:395], "contents": bubble}


# ---------------- 4. 推播 ----------------
def push(message):
    if os.environ.get("DRY_RUN") == "1":
        print(json.dumps(message, ensure_ascii=False, indent=2))
        return
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"].strip()
    # 預設 broadcast：推給官方帳號的「所有好友」（含你自己）
    # 若只想推給自己，在 workflow 的 env 加 LINE_PUSH_MODE: "self"
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


def main():
    hours = int(os.environ.get("HOURS_BACK", "26"))
    max_items = int(os.environ.get("MAX_ITEMS", "6"))
    wk = "一二三四五六日"[datetime.now(TW).weekday()]
    today = datetime.now(TW).strftime("%m/%d") + f"（{wk}）"

    items = fetch_news(hours)
    print(f"抓到 {len(items)} 則候選新聞")
    if not items:
        push({"type": "text", "text": f"【勞動法令日報】{today}\n今日沒有抓到相關新聞。"})
        return

    try:
        picked, deadlines = summarize(items, max_items)
    except Exception as ex:
        # ::warning:: 會讓錯誤顯示在 GitHub Actions 執行結果頁面最上方
        print(f"::warning::Gemini 摘要失敗，已改推標題清單。原因：{ex}")
        picked, deadlines = fallback(items, max_items)

    if not picked:
        push({"type": "text", "text": f"【勞動法令日報】{today}\n今日沒有與勞動法令相關的重要新聞。"})
        return
    push(build_flex(picked, deadlines, today))


if __name__ == "__main__":
    main()
