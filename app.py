import os, re, hmac, hashlib, base64, logging
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Lock
import requests, psycopg
from flask import Flask, request, abort

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
KST = ZoneInfo("Asia/Seoul")
SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
DB = (os.getenv("DATABASE_URL") or os.getenv("REDBOT_DATABASE_URL") or "").strip()
app.logger.warning("Configuration check: DATABASE_URL_present=%s LINE_SECRET_present=%s LINE_TOKEN_present=%s", bool(DB), bool(os.getenv("LINE_CHANNEL_SECRET")), bool(os.getenv("LINE_CHANNEL_ACCESS_TOKEN")))
ADMINS = {s.strip() for s in os.getenv("ADMIN_USER_IDS", "").split(",") if s.strip()}
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
API = "https://api.line.me/v2/bot"
_ready = False
_lock = Lock()

def db():
    if not DB: raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(DB, connect_timeout=8)

def initialize():
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS message_events (event_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL, display_name TEXT NOT NULL, message_date TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_message_group_date ON message_events(group_id,message_date)")
        conn.execute("CREATE TABLE IF NOT EXISTS member_profiles (group_id TEXT NOT NULL,user_id TEXT NOT NULL,display_name TEXT NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(group_id,user_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS mention_members (group_id TEXT NOT NULL,category TEXT NOT NULL,user_id TEXT NOT NULL,PRIMARY KEY(group_id,category,user_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")

def headers():
    return {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}

def reply(token, text, mention=None):
    message = {"type": "text", "text": text[:5000]}
    if mention: message["mention"] = {"mentionees": mention}
    r = requests.post(API + "/message/reply", json={"replyToken": token, "messages": [message]}, headers=headers(), timeout=12)
    r.raise_for_status()

def profile(group, user):
    try:
        r = requests.get(f"{API}/group/{group}/member/{user}", headers=headers(), timeout=5)
        if r.ok: return r.json().get("displayName") or user[:12]
    except requests.RequestException: pass
    return user[:12]

def counts(group, day):
    with db() as conn:
        rows = conn.execute("""SELECT user_id,MAX(display_name),COUNT(*) AS cnt
            FROM message_events WHERE group_id=%s AND message_date=%s
            GROUP BY user_id HAVING COUNT(*) >= 2
            ORDER BY cnt DESC,MAX(display_name) ASC""", (group, day)).fetchall()
    label = f"{day[:4]}년 {int(day[5:7])}월 {int(day[8:])}일"
    lines = [f"📊 {label} 마딧수 (2마디 이상)"]
    if not rows: return lines[0] + "\n2마디 이상 기록된 멤버가 없습니다."
    for i, (_, name, n) in enumerate(rows, 1):
        item = f"{i}. {name}: {n}마디"
        if len("\n".join(lines)) + len(item) > 4500:
            lines.append("…이하 생략")
            break
        lines.append(item)
    return "\n".join(lines)

def utf16len(s):
    return len(s.encode("utf-16-le")) // 2

def mention_text(group, category):
    with db() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT user_id FROM mention_members WHERE group_id=%s AND category=%s ORDER BY user_id",
            (group, category))]
    if not ids:
        return {"type": "text", "text": f"{category} 명단이 비어 있습니다."}
    # Outgoing mentions require textV2 substitution, not webhook-style mention offsets.
    text = f"📢 {category} 호출!\n"
    substitution = {}
    for i, uid in enumerate(ids[:20]):
        key = f"member{i}"
        text += "{" + key + "} "
        substitution[key] = {"type": "mention", "mentionee": {"type": "user", "userId": uid}}
    if len(ids) > 20:
        text += f"\n(20명까지 호출, 나머지 {len(ids)-20}명 제외)"
    return {"type": "textV2", "text": text, "substitution": substitution}

def reply_message(token, message):
    r = requests.post(API + "/message/reply",
        json={"replyToken": token, "messages": [message]},
        headers=headers(), timeout=12)
    if not r.ok:
        app.logger.warning("LINE reply status %s: %s", r.status_code, r.text[:500])
    r.raise_for_status()

_working_gemini_model = None

def weather_forecast(question, now):
    """Get Seoul forecast directly instead of trusting stale model knowledge."""
    normalized = re.sub(r"\\s+", "", question)
    if "날씨" not in normalized and "기온" not in normalized and "비와" not in normalized:
        return None
    if "서울" not in normalized:
        return None
    if "모레" in normalized:
        offset, day_label = 2, "모레"
    elif "내일" in normalized:
        offset, day_label = 1, "내일"
    elif "오늘" in normalized or "지금" in normalized or "현재" in normalized:
        offset, day_label = 0, "오늘"
    else:
        return None
    try:
        response = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": 37.5665, "longitude": 126.9780,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "Asia/Seoul", "forecast_days": 4
        }, timeout=8)
        response.raise_for_status()
        daily = response.json()["daily"]
        expected = (now.date() + __import__("datetime").timedelta(days=offset)).isoformat()
        index = daily["time"].index(expected)
        code = daily["weather_code"][index]
        if code == 0:
            condition = "맑음"
        elif code in (1, 2):
            condition = "대체로 맑음 또는 구름 조금"
        elif code == 3:
            condition = "흐림"
        elif code in (45, 48):
            condition = "안개"
        elif code in (51, 53, 55, 56, 57):
            condition = "이슬비"
        elif code in (61, 63, 65, 66, 67, 80, 81, 82):
            condition = "비"
        elif code in (71, 73, 75, 77, 85, 86):
            condition = "눈"
        elif code in (95, 96, 99):
            condition = "천둥번개"
        else:
            condition = "변동 가능"
        low = daily["temperature_2m_min"][index]
        high = daily["temperature_2m_max"][index]
        rain = daily["precipitation_probability_max"][index]
        return (f"🌤️ {day_label} 서울 날씨 ({expected})\\n"
                f"날씨: {condition}\\n"
                f"최저 {low:g}°C / 최고 {high:g}°C\\n"
                f"강수확률: {rain}%\\n"
                "출처: Open-Meteo 기상예보 (예보는 바뀔 수 있어요)")
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        app.logger.warning("Weather forecast lookup failed: %s", type(exc).__name__)
        return "서울 날씨 예보를 지금 조회하지 못했어요. 잠시 후 다시 시도해 주세요."

def ai(question):
    global _working_gemini_model
    now = datetime.now(KST)
    normalized = re.sub(r"\s+", "", question).rstrip("?!？!。.")
    forecast = weather_forecast(question, now)
    if forecast is not None:
        return forecast
    weekdays = "월화수목금토일"
    today_text = f"{now.year}년 {now.month}월 {now.day}일 {weekdays[now.weekday()]}요일"
    # Answer current-date questions from the server clock, not the model's training data.
    date_words = ("날짜", "몇월", "몇일", "며칠", "무슨요일", "몇요일", "오늘이", "오늘은")
    date_context = ("오늘", "지금", "현재", "금일", "오늘날짜")
    if (any(word in normalized for word in date_context)
            and any(word in normalized for word in date_words)
            and not any(word in normalized for word in ("어제", "내일", "모레", "지난", "다음", "미래", "과거", "기념일", "생일"))):
        return f"오늘은 {today_text}이에요. 🇰🇷"
    if (any(word in normalized for word in ("지금", "현재", "한국시간"))
            and any(word in normalized for word in ("몇시", "시간", "몇분"))):
        return f"현재 한국 시간은 {now.hour:02d}시 {now.minute:02d}분이에요. 🇰🇷"
    # Authoritative current office-holder answer; avoid stale language-model memory.
    president_question = ("대통령" in normalized and
                          any(t in normalized for t in ("현재", "지금", "오늘", "현직", "누구", "누가", "이름", "대한민국", "한국", "우리나라")))
    historical_question = any(t in normalized for t in ("전직", "전대", "역대", "과거", "이전", "전임", "당시", "2024", "2023", "2022", "탄핵", "윤석열"))
    if president_question and not historical_question:
        return "현재 대한민국 대통령은 이재명 대통령입니다. (2026년 11월 기준)"
    if not GEMINI_KEY: return "관리자에게 GEMINI_API_KEY 설정을 요청해 주세요."
    # For rapidly changing facts, require live grounding; do not answer from stale training data.
    current_terms = ("현재", "지금", "오늘", "최신", "요즘", "이번", "누구", "누가", "대통령", "총리", "장관", "환율", "주가", "날씨", "뉴스", "당선", "취임", "순위", "가격", "현직")
    fresh_topics = ("대통령", "총리", "장관", "정부", "정치", "환율", "주가", "날씨", "뉴스", "선거", "당선", "취임", "현직", "대표", "시장", "가격")
    needs_live = (any(t in normalized for t in fresh_topics)
                  and any(t in normalized for t in current_terms))
    base = "https://generativelanguage.googleapis.com/v1beta"
    payload = {
        "systemInstruction": {"parts": [{"text": f"현재 기준 시간은 대한민국 서울(Asia/Seoul) {today_text} {now.hour:02d}시 {now.minute:02d}분입니다. 오늘 날짜와 현재 시간에 관한 질문에는 이 정보를 정확히 사용하세요. 한국어로 핵심만 간결하게 답변하세요. 간단한 질문에는 1~3문장으로 답하세요. 실시간 검색이 필요한 최신 사실은 확인하지 못했다면 추측하지 마세요."}]},
        "contents": [{"parts": [{"text": question[:2000]}]}],
        "generationConfig": {"maxOutputTokens": 400, "thinkingConfig": {"thinkingBudget": 0}}
    }
    if needs_live:
        payload["tools"] = [{"google_search": {}}]
        payload["generationConfig"].pop("thinkingConfig", None)
    def generate(model):
        return requests.post(f"{base}/models/{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY}, json=payload, timeout=18)
    try:
        model = _working_gemini_model or GEMINI_MODEL
        r = generate(model)
        if r.status_code == 400 and "thinking" in r.text.lower():
            payload["generationConfig"].pop("thinkingConfig", None)
            r = generate(model)
        if r.status_code == 404:
            listing = requests.get(f"{base}/models", headers={"x-goog-api-key": GEMINI_KEY}, timeout=10)
            if listing.ok:
                models = [m.get("name", "").removeprefix("models/") for m in listing.json().get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", [])]
                flash = [m for m in models if "flash" in m.lower() and "image" not in m.lower()
                         and "tts" not in m.lower() and "live" not in m.lower()]
                alternatives = flash or models
                alternatives.sort(key=lambda m: (0 if "flash-lite" in m.lower() else 1,
                                                   "preview" in m.lower(), m))
                for candidate in alternatives[:8]:
                    if candidate == model: continue
                    r = generate(candidate)
                    if r.status_code == 400 and "thinking" in r.text.lower():
                        payload["generationConfig"].pop("thinkingConfig", None)
                        r = generate(candidate)
                    if r.status_code != 404:
                        model = candidate
                        break
            else:
                app.logger.warning("Gemini model listing status %s", listing.status_code)
        if needs_live and not r.ok:
            app.logger.warning("Gemini grounded search status %s", r.status_code)
            return "최신 정보를 실시간으로 확인하지 못했어요. 정확하지 않은 정보를 알려드리지 않기 위해 답변을 보류할게요."
        if r.status_code == 429: return "AI 무료 사용량 한도에 도달했어요. 나중에 다시 시도해 주세요."
        if not r.ok:
            app.logger.warning("Gemini status %s", r.status_code)
            return "AI 답변을 불러올 수 없어요."
        if needs_live:
            candidates = r.json().get("candidates", [])
            metadata = candidates[0].get("groundingMetadata", {}) if candidates else {}
            if not metadata.get("groundingChunks"):
                return "최신 정보를 확인할 근거가 부족해요. 정확하지 않은 답변 대신 확인이 필요하다고 알려드릴게요."
        _working_gemini_model = model
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return ("".join(p.get("text", "") for p in parts).strip() or "AI 답변이 비어 있어요.")[:4800]
    except (requests.RequestException, ValueError, IndexError, KeyError):
        return "AI 연결에 문제가 있어요."

def handle(event):
    source = event.get("source") or {}
    group, user = source.get("groupId"), source.get("userId")
    message = event.get("message") or {}
    if event.get("type") != "message" or message.get("type") != "text" or not group or not user: return
    token = event.get("replyToken")
    eid = event.get("webhookEventId") or message.get("id")
    if not token or not eid: return
    text = message.get("text", "").strip()
    day = datetime.now(KST).strftime("%Y-%m-%d")
    name = profile(group, user)
    with db() as conn:
        inserted = conn.execute("INSERT INTO processed_events(event_id) VALUES(%s) ON CONFLICT DO NOTHING RETURNING event_id", (eid,)).fetchone()
        if not inserted: return
        conn.execute("""INSERT INTO member_profiles(group_id,user_id,display_name) VALUES(%s,%s,%s)
            ON CONFLICT(group_id,user_id) DO UPDATE SET display_name=EXCLUDED.display_name,updated_at=NOW()""", (group,user,name))
        conn.execute("""INSERT INTO message_events(event_id,group_id,user_id,display_name,message_date)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (eid,group,user,name,day))
    if text == "*마딧수":
        reply(token, counts(group, day))
        return
    match = re.fullmatch(r"\*(\d{2})(\d{2})(\d{2})마딧수", text)
    if match:
        yy, mm, dd = map(int, match.groups())
        try:
            date = datetime(2000+yy, mm, dd, tzinfo=KST).date()
            if date > datetime.now(KST).date(): raise ValueError()
        except ValueError:
            reply(token, "올바른 과거 날짜를 입력해 주세요. 예: *261111마딧수")
            return
        reply(token, counts(group, date.isoformat()))
        return
    if text in ("*남초", "*여초"):
        reply_message(token, mention_text(group, text[1:]))
        return
    match = re.fullmatch(r"/(남초|여초)(추가|삭제|목록)(?:\s+.*)?", text)
    if match:
        category, action = match.groups()
        if user not in ADMINS:
            reply(token, "등록된 관리자만 사용할 수 있어요.")
            return
        if action == "목록":
            with db() as conn:
                ids = [r[0] for r in conn.execute("SELECT user_id FROM mention_members WHERE group_id=%s AND category=%s", (group,category))]
            reply(token, f"{category} 명단 ({len(ids)}명)\n" + ("\n".join(profile(group, x) for x in ids[:80]) if ids else "등록된 멤버 없음"))
            return
        targets = [m["userId"] for m in (message.get("mention") or {}).get("mentionees", []) if m.get("userId")]
        if not targets:
            reply(token, f"/{category}{action} @상대방 형태로 LINE 멘션을 넣어 주세요.")
            return
        with db() as conn:
            for target in targets:
                if action == "추가":
                    conn.execute("INSERT INTO mention_members(group_id,category,user_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (group,category,target))
                else:
                    conn.execute("DELETE FROM mention_members WHERE group_id=%s AND category=%s AND user_id=%s", (group,category,target))
        reply(token, f"{category} 명단 {len(targets)}명 {'등록' if action == '추가' else '삭제'} 처리했어요.")
        return
    if text.startswith("!"):
        question = text[1:].strip()
        reply(token, ai(question) if question else "사용법: !오늘 저녁 메뉴 추천해줘")

@app.before_request
def ensure_db():
    global _ready
    if request.path != "/callback" or _ready: return
    with _lock:
        if not _ready:
            initialize()
            _ready = True

@app.get("/")
def health():
    return {"status": "ok", "bot": "Red Bot 3.0"}

@app.post("/callback")
def callback():
    raw = request.get_data()
    if not SECRET or not TOKEN: abort(503)
    signature = request.headers.get("X-Line-Signature", "")
    expected = base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(signature, expected): abort(400)
    for event in (request.get_json(silent=True) or {}).get("events", []):
        try: handle(event)
        except Exception: app.logger.exception("Webhook event failed")
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
