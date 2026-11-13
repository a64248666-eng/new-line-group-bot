import os, re, hmac, hashlib, base64, logging, secrets, time
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
        conn.execute("ALTER TABLE message_events ADD COLUMN IF NOT EXISTS message_hour SMALLINT")
        conn.execute("CREATE TABLE IF NOT EXISTS member_profiles (group_id TEXT NOT NULL,user_id TEXT NOT NULL,display_name TEXT NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(group_id,user_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS mention_members (group_id TEXT NOT NULL,category TEXT NOT NULL,user_id TEXT NOT NULL,PRIMARY KEY(group_id,category,user_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS group_admins (group_id TEXT NOT NULL,user_id TEXT NOT NULL,PRIMARY KEY(group_id,user_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        conn.execute("CREATE TABLE IF NOT EXISTS region_members (group_id TEXT NOT NULL, region TEXT NOT NULL, member_name TEXT NOT NULL, PRIMARY KEY(group_id,region,member_name))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_region_members_group_region ON region_members(group_id,region)")
        conn.execute("""CREATE TABLE IF NOT EXISTS point_inventory (
            group_id TEXT NOT NULL, user_id TEXT NOT NULL, item_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(group_id,user_id,item_name))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS hourly_alert_settings (
            group_id TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT FALSE)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS hourly_alert_deliveries (
            group_id TEXT NOT NULL, hour_key TEXT NOT NULL,
            PRIMARY KEY(group_id,hour_key))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_missions (
            group_id TEXT NOT NULL, user_id TEXT NOT NULL, mission_date TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0, last_counted_at TIMESTAMPTZ,
            claimed BOOLEAN NOT NULL DEFAULT FALSE,
            PRIMARY KEY(group_id,user_id,mission_date))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_treasure (
            group_id TEXT NOT NULL, user_id TEXT NOT NULL, treasure_date TEXT NOT NULL,
            reward INTEGER NOT NULL,
            PRIMARY KEY(group_id,user_id,treasure_date))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS game_points (
            group_id TEXT NOT NULL, user_id TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0, attendance_days INTEGER NOT NULL DEFAULT 0,
            last_attendance TEXT, PRIMARY KEY(group_id,user_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS quiz_daily_limits (
            group_id TEXT NOT NULL, quiz_date TEXT NOT NULL,
            issued_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(group_id,quiz_date))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS quiz_history (group_id TEXT NOT NULL, question TEXT NOT NULL, issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(group_id,question))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS quiz_rounds (
            group_id TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL,
            choices TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")


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


WEATHER_CITIES = {
    "서울": (37.5665, 126.9780), "부산": (35.1796, 129.0756),
    "대구": (35.8714, 128.6014), "인천": (37.4563, 126.7052),
    "광주": (35.1595, 126.8526), "대전": (36.3504, 127.3845),
    "울산": (35.5384, 129.3114), "세종": (36.4800, 127.2890),
    "제주": (33.4996, 126.5312), "서귀포": (33.2541, 126.5601),
    "수원": (37.2636, 127.0286), "성남": (37.4200, 127.1265),
    "고양": (37.6584, 126.8320), "용인": (37.2411, 127.1776),
    "부천": (37.5034, 126.7660), "안양": (37.3943, 126.9568),
    "안산": (37.3219, 126.8309), "화성": (37.1995, 126.8312),
    "평택": (36.9921, 127.1129), "파주": (37.7600, 126.7800),
    "김포": (37.6152, 126.7156), "의정부": (37.7381, 127.0337),
    "남양주": (37.6360, 127.2165), "하남": (37.5393, 127.2149),
    "광명": (37.4786, 126.8644), "시흥": (37.3800, 126.8030),
    "군포": (37.3617, 126.9352), "이천": (37.2720, 127.4350),
    "여주": (37.2982, 127.6372), "양평": (37.4918, 127.4876),
    "춘천": (37.8813, 127.7298), "원주": (37.3422, 127.9202),
    "강릉": (37.7519, 128.8761), "속초": (38.2070, 128.5918),
    "동해": (37.5247, 129.1143), "삼척": (37.4499, 129.1652),
    "태백": (37.1641, 128.9856), "청주": (36.6424, 127.4890),
    "충주": (36.9910, 127.9259), "제천": (37.1326, 128.1910),
    "천안": (36.8151, 127.1139), "아산": (36.7898, 127.0018),
    "공주": (36.4465, 127.1190), "보령": (36.3333, 126.6129),
    "서산": (36.7845, 126.4503), "당진": (36.8936, 126.6283),
    "논산": (36.1872, 127.0987), "전주": (35.8242, 127.1480),
    "군산": (35.9677, 126.7366), "익산": (35.9483, 126.9576),
    "정읍": (35.5699, 126.8560), "남원": (35.4164, 127.3904),
    "목포": (34.8118, 126.3922), "여수": (34.7604, 127.6622),
    "순천": (34.9506, 127.4872), "광양": (34.9407, 127.6959),
    "나주": (35.0158, 126.7108), "포항": (36.0190, 129.3435),
    "경주": (35.8562, 129.2247), "구미": (36.1195, 128.3446),
    "김천": (36.1398, 128.1136), "안동": (36.5684, 128.7294),
    "영주": (36.8057, 128.6240), "경산": (35.8251, 128.7415),
    "창원": (35.2281, 128.6811), "김해": (35.2285, 128.8894),
    "진주": (35.1800, 128.1076), "양산": (35.3350, 129.0373),
    "거제": (34.8806, 128.6211), "통영": (34.8544, 128.4332),
    "사천": (35.0038, 128.0642), "밀양": (35.5038, 128.7466),
}
WEATHER_ALIASES = {"제주도": "제주", "서울특별시": "서울", "부산광역시": "부산",
    "대구광역시": "대구", "인천광역시": "인천", "광주광역시": "광주",
    "대전광역시": "대전", "울산광역시": "울산", "세종시": "세종",
    "제주시": "제주", "서귀포시": "서귀포"}

def weather_forecast(question, now):
    """Live daily forecast for explicitly named locations in South Korea."""
    compact = re.sub(r"\s+", "", question)
    if not any(word in compact for word in ("날씨", "기온", "강수확률", "비올", "비와")):
        return None
    if "모레" in compact:
        offset, day_label = 2, "모레"
    elif "내일" in compact:
        offset, day_label = 1, "내일"
    elif any(word in compact for word in ("오늘", "지금", "현재")):
        offset, day_label = 0, "오늘"
    else:
        return None

    # Prefer the longest recognized city name to avoid partial matches.
    place = next((name for name in sorted(
        set(WEATHER_CITIES) | set(WEATHER_ALIASES), key=len, reverse=True)
        if name in compact), None)
    if place:
        place = WEATHER_ALIASES.get(place, place)
        lat, lon = WEATHER_CITIES[place]
    else:
        # Extract the locality immediately preceding the weather keyword.
        match = re.search(r"([가-힣]{2,12})(?:의|에|에서)?(?:날씨|기온|강수확률|비올|비와)", compact)
        if not match:
            return "지역명을 함께 알려줘. 예: !내일 부산 날씨"
        place = re.sub(r"^(오늘|내일|모레|지금|현재)", "", match.group(1))
        place = re.sub(r"(특별시|광역시|특별자치시|특별자치도|자치시|자치도|시|군|구)$", "", place)
        if len(place) < 2:
            return "지역명을 확인하지 못했어. 예: !내일 강릉 날씨"
        try:
            geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                params={"name": place, "count": 10, "language": "ko", "countryCode": "KR"},
                timeout=7)
            geo.raise_for_status()
            results = [x for x in geo.json().get("results", [])
                       if x.get("country_code") == "KR"]
            exact = [x for x in results if x.get("name", "").replace(" ", "") == place]
            selected = (exact or results)
            if not selected:
                return f"'{place}' 지역을 찾지 못했어. 시·군·구 이름으로 다시 물어봐 줘."
            item = selected[0]
            lat, lon = item["latitude"], item["longitude"]
            place = item.get("name") or place
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return "지역 검색 서비스에 연결하지 못했어. 잠시 후 다시 시도해 줘."

    try:
        response = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
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
        return (f"🌤️ {day_label} {place} 날씨 ({expected})\n"
                f"날씨: {condition}\n"
                f"최저 {low:g}°C / 최고 {high:g}°C\n"
                f"강수확률: {rain}%\n"
                "출처: Open-Meteo 기상예보 (예보는 바뀔 수 있어요)")
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        app.logger.warning("Weather forecast lookup failed: %s", type(exc).__name__)
        return f"{place} 날씨 예보를 지금 조회하지 못했어. 잠시 후 다시 시도해 줘."


_working_gemini_model = None
_gemini_quota_pause_until = 0.0

def ai(question):
    global _working_gemini_model, _gemini_quota_pause_until
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
    if not GEMINI_KEY: return "관리자에게 GEMINI_API_KEY 설정을 요청해 주세요."
    if time.monotonic() < _gemini_quota_pause_until:
        return "⏳ Gemini 사용량 제한으로 잠시 쉬고 있어. 몇 분 뒤 다시 질문해 줘."
    # Ground factual lookup questions in web search; avoid unverified model recall.
    # Search only when the user explicitly asks for current, changing facts.
    live_markers = ("최신", "실시간", "오늘", "지금", "현재", "이번주", "이번달", "올해", "최근", "방금", "속보", "뉴스", "현직", "출처", "검색해", "찾아줘")
    changing_topics = ("대통령", "총리", "장관", "환율", "주가", "가격", "금리", "날씨", "선거", "경기결과", "순위", "출시", "정책", "법률", "사건", "사고", "스포츠", "주식", "코인", "부동산", "통계")
    needs_live = any(t in normalized for t in live_markers) or (
        any(t in normalized for t in changing_topics)
        and any(t in normalized for t in ("누구", "누가", "얼마", "몇위", "언제", "어떻게됐", "알려줘", "확인해")))
    base = "https://generativelanguage.googleapis.com/v1beta"
    payload = {
        "systemInstruction": {"parts": [{"text": f"현재 기준 시간은 대한민국 서울(Asia/Seoul) {today_text} {now.hour:02d}시 {now.minute:02d}분입니다. 오늘 날짜와 현재 시간에 관한 질문에는 이 정보를 정확히 사용하세요. 한국어로 핵심만 정확하고 간결하게 답변하세요. 간단한 질문에는 1~3문장으로 답하세요. 질문의 국가·인물·날짜·단위를 정확히 구분하고, 질문이 모호하면 필요한 조건을 짧게 확인하세요. 확인되지 않은 수치·날짜·인용·출처를 만들어내지 마세요. 검색 결과가 충돌하면 최신성과 공신력을 비교하고, 확인된 사실과 불확실한 내용을 구분하세요. 검색 근거가 있는 최신 사실은 확인된 정보만 답하고, 가능하면 출처의 사이트명과 링크를 제시하세요. 실시간 검색이 필요한 최신 사실을 확인하지 못했다면 추측하지 마세요."}]},
        "contents": [{"parts": [{"text": question[:2000]}]}],
        "generationConfig": {"maxOutputTokens": 900}
    }
    if needs_live:
        payload["tools"] = [{"google_search": {}}]
        payload["generationConfig"].pop("thinkingConfig", None)
    def generate(model):
        # Enable modest reasoning for Gemini 2.5 Flash; leave newer models on their defaults.
        if model.startswith("gemini-2.5-flash") and "lite" not in model:
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 256}
        else:
            payload["generationConfig"].pop("thinkingConfig", None)
        response = None
        for attempt in range(3):
            try:
                response = requests.post(f"{base}/models/{model}:generateContent",
                    headers={"x-goog-api-key": GEMINI_KEY}, json=payload, timeout=20)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
                continue
            if response.status_code not in (500, 502, 503, 504):
                return response
            if attempt < 2:
                time.sleep(attempt + 1)
        return response
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
                alternatives.sort(key=lambda m: ("preview" in m.lower(), "lite" in m.lower(), m))
                for candidate in alternatives[:12]:
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
        if r.status_code == 429:
            _gemini_quota_pause_until = time.monotonic() + 180
            app.logger.warning("Gemini quota exceeded (429)")
            return "⏳ Gemini API 요청 한도에 도달했어. 잠시 후 다시 시도해 줘. 반복되면 API 사용량·할당량을 확인해야 해."
        if r.status_code in (500, 502, 503, 504):
            app.logger.warning("Gemini temporarily unavailable: %s", r.status_code)
            return "⏳ Gemini 서버가 잠시 불안정해. 자동 재시도했지만 실패했어. 잠시 후 다시 질문해 줘."
        if r.status_code == 404:
            app.logger.warning("Gemini model unavailable (404)")
            return "⚙️ Gemini 모델을 찾지 못했어. 관리자가 GEMINI_MODEL 설정과 API 키의 모델 접근 권한을 확인해야 해."
        if not r.ok:
            app.logger.warning("Gemini request failed: %s", r.status_code)
            return "AI 연결 오류가 발생했어. 잠시 후 다시 시도해 줘."
        if needs_live:
            candidates = r.json().get("candidates", [])
            metadata = candidates[0].get("groundingMetadata", {}) if candidates else {}
            if not metadata.get("groundingChunks"):
                return "최신 정보를 확인할 근거가 부족해요. 정확하지 않은 답변 대신 확인이 필요하다고 알려드릴게요."
        _working_gemini_model = model
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        answer = "".join(p.get("text", "") for p in parts).strip() or "AI 답변이 비어 있어요."
        if needs_live:
            metadata = r.json().get("candidates", [{}])[0].get("groundingMetadata", {})
            sources = []
            for chunk in metadata.get("groundingChunks", []):
                web = chunk.get("web") or {}
                uri = web.get("uri", "")
                title = web.get("title", "출처")
                if uri.startswith("https://") and uri not in [u for _, u in sources]:
                    sources.append((title[:55], uri))
                if len(sources) >= 2:
                    break
            if sources:
                answer += "\n\n🔎 확인한 출처:\n" + "\n".join(f"• {title}: {uri}" for title, uri in sources)
        return answer[:4800]
    except (requests.RequestException, ValueError, IndexError, KeyError):
        return "AI 연결에 문제가 있어요."


QUIZ_BANK = (
    ("대한민국의 수도는?", ("서울", "부산", "대전", "광주"), 1),
    ("태양계에서 가장 큰 행성은?", ("화성", "금성", "목성", "수성"), 3),
    ("한글을 창제한 왕은?", ("세종대왕", "태조", "정조", "광해군"), 1),
    ("물의 화학식은?", ("CO2", "O2", "H2O", "NaCl"), 3),
    ("1시간은 몇 초일까?", ("600초", "3600초", "6000초", "360초"), 2),
    ("지구의 자연 위성은?", ("달", "화성", "금성", "태양"), 1),
    ("삼각형의 내각의 합은?", ("90도", "180도", "270도", "360도"), 2),
    ("대한민국 국화는?", ("장미", "벚꽃", "무궁화", "해바라기"), 3),
    ("빛의 삼원색에 포함되지 않는 것은?", ("빨강", "초록", "노랑", "파랑"), 3),
    ("세계에서 가장 넓은 대양은?", ("대서양", "인도양", "북극해", "태평양"), 4),
    ("사람의 심장은 일반적으로 몇 개의 방으로 이루어질까?", ("2개", "3개", "4개", "5개"), 3),
    ("컴퓨터에서 CPU는 무엇을 뜻할까?", ("중앙처리장치", "주기억장치", "입력장치", "출력장치"), 1),
    ("대한민국의 법정 화폐 단위는?", ("엔", "원", "달러", "위안"), 2),
    ("지구가 태양을 한 바퀴 도는 데 걸리는 시간은 대략?", ("하루", "한 달", "일 년", "십 년"), 3),
    ("피타고라스 정리가 적용되는 삼각형은?", ("정삼각형", "직각삼각형", "이등변삼각형만", "둔각삼각형만"), 2),
    ("고려를 건국한 인물은?", ("왕건", "이성계", "궁예", "견훤"), 1),
    ("조선을 건국한 인물은?", ("왕건", "이성계", "세종", "정조"), 2),
    ("훈민정음 반포 연도는?", ("1392년", "1592년", "1446년", "1919년"), 3),
    ("임진왜란이 시작된 해는?", ("1446년", "1636년", "1894년", "1592년"), 4),
    ("프랑스의 수도는?", ("파리", "로마", "베를린", "마드리드"), 1),
    ("호주의 수도는?", ("시드니", "캔버라", "멜버른", "퍼스"), 2),
    ("캐나다의 수도는?", ("토론토", "밴쿠버", "오타와", "몬트리올"), 3),
    ("태양과 가장 가까운 행성은?", ("금성", "지구", "화성", "수성"), 4),
    ("붉은 행성은?", ("화성", "목성", "토성", "금성"), 1),
    ("금의 원소 기호는?", ("Ag", "Au", "Fe", "Cu"), 2),
    ("은의 원소 기호는?", ("Au", "Al", "Ag", "Zn"), 3),
    ("철의 원소 기호는?", ("Cu", "Pb", "Sn", "Fe"), 4),
    ("곤충의 다리는 몇 개?", ("6개", "4개", "8개", "10개"), 1),
    ("거미의 다리는 몇 개?", ("6개", "8개", "10개", "12개"), 2),
    ("박쥐는 어떤 동물일까?", ("조류", "파충류", "포유류", "양서류"), 3),
    ("배구 한 팀의 코트 위 선수 수는?", ("5명", "7명", "9명", "6명"), 4),
    ("마라톤 공식 거리는?", ("42.195km", "40km", "41.5km", "45km"), 1),
    ("'티끌 모아' 뒤에 이어지는 말은?", ("바다", "태산", "강산", "구름"), 2),
    ("'등잔 밑이' 뒤에 이어지는 말은?", ("밝다", "따뜻하다", "어둡다", "차갑다"), 3),
    ("'역지사지'의 뜻은?", ("지난 일을 잊음", "다른 지역으로 이사함", "자기 의견만 주장함", "상대방 입장에서 생각함"), 4),
    ("'유비무환'의 뜻은?", ("미리 준비하면 걱정이 없음", "준비해도 소용없음", "근심이 많으면 실패함", "계획 없이 행동함"), 1),
    ("계산: 5 - 2 = ?", ("1", "3", "4", "6"), 2),
    ("계산: 3 × 3 = ?", ("7", "10", "9", "12"), 3),
    ("계산: 12 ÷ 4 = ?", ("1", "4", "6", "3"), 4),
    ("계산: 3 + 5 = ?", ("8", "6", "9", "11"), 1),
    ("계산: 9 - 6 = ?", ("1", "3", "4", "6"), 2),
    ("계산: 3 × 7 = ?", ("19", "22", "21", "24"), 3),
    ("계산: 24 ÷ 8 = ?", ("1", "4", "6", "3"), 4),
    ("계산: 3 + 9 = ?", ("12", "10", "13", "15"), 1),
    ("계산: 13 - 10 = ?", ("1", "3", "4", "6"), 2),
    ("계산: 3 × 11 = ?", ("31", "34", "33", "36"), 3),
    ("계산: 36 ÷ 12 = ?", ("1", "4", "6", "3"), 4),
    ("계산: 3 + 13 = ?", ("16", "14", "17", "19"), 1),
    ("계산: 17 - 14 = ?", ("1", "3", "4", "6"), 2),
    ("계산: 3 × 15 = ?", ("43", "46", "45", "48"), 3),
    ("계산: 48 ÷ 16 = ?", ("1", "4", "6", "3"), 4),
    ("계산: 3 + 17 = ?", ("20", "18", "21", "23"), 1),
    ("계산: 21 - 18 = ?", ("1", "3", "4", "6"), 2),
    ("계산: 3 × 19 = ?", ("55", "58", "57", "60"), 3),
    ("계산: 60 ÷ 20 = ?", ("1", "4", "6", "3"), 4),
    ("계산: 3 + 21 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 25 - 22 = ?", ("1", "3", "4", "6"), 2),
    ("계산: 3 × 23 = ?", ("67", "70", "69", "72"), 3),
    ("계산: 72 ÷ 24 = ?", ("1", "4", "6", "3"), 4),
    ("계산: 3 + 25 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 4 × 2 = ?", ("6", "8", "9", "11"), 2),
    ("계산: 12 ÷ 3 = ?", ("2", "5", "4", "7"), 3),
    ("계산: 4 + 4 = ?", ("6", "9", "11", "8"), 4),
    ("계산: 9 - 5 = ?", ("4", "2", "5", "7"), 1),
    ("계산: 4 × 6 = ?", ("22", "24", "25", "27"), 2),
    ("계산: 28 ÷ 7 = ?", ("2", "5", "4", "7"), 3),
    ("계산: 4 + 8 = ?", ("10", "13", "15", "12"), 4),
    ("계산: 13 - 9 = ?", ("4", "2", "5", "7"), 1),
    ("계산: 4 × 10 = ?", ("38", "40", "41", "43"), 2),
    ("계산: 44 ÷ 11 = ?", ("2", "5", "4", "7"), 3),
    ("계산: 4 + 12 = ?", ("14", "17", "19", "16"), 4),
    ("계산: 17 - 13 = ?", ("4", "2", "5", "7"), 1),
    ("계산: 4 × 14 = ?", ("54", "56", "57", "59"), 2),
    ("계산: 60 ÷ 15 = ?", ("2", "5", "4", "7"), 3),
    ("계산: 4 + 16 = ?", ("18", "21", "23", "20"), 4),
    ("계산: 21 - 17 = ?", ("4", "2", "5", "7"), 1),
    ("계산: 4 × 18 = ?", ("70", "72", "73", "75"), 2),
    ("계산: 76 ÷ 19 = ?", ("2", "5", "4", "7"), 3),
    ("계산: 4 + 20 = ?", ("22", "25", "27", "24"), 4),
    ("계산: 25 - 21 = ?", ("4", "2", "5", "7"), 1),
    ("계산: 4 × 22 = ?", ("86", "88", "89", "91"), 2),
    ("계산: 92 ÷ 23 = ?", ("2", "5", "4", "7"), 3),
    ("계산: 4 + 24 = ?", ("26", "29", "31", "28"), 4),
    ("계산: 29 - 25 = ?", ("4", "2", "5", "7"), 1),
    ("계산: 10 ÷ 2 = ?", ("3", "5", "6", "8"), 2),
    ("계산: 5 + 3 = ?", ("6", "9", "8", "11"), 3),
    ("계산: 9 - 4 = ?", ("3", "6", "8", "5"), 4),
    ("계산: 5 × 5 = ?", ("25", "23", "26", "28"), 1),
    ("계산: 30 ÷ 6 = ?", ("3", "5", "6", "8"), 2),
    ("계산: 5 + 7 = ?", ("10", "13", "12", "15"), 3),
    ("계산: 13 - 8 = ?", ("3", "6", "8", "5"), 4),
    ("계산: 5 × 9 = ?", ("45", "43", "46", "48"), 1),
    ("계산: 50 ÷ 10 = ?", ("3", "5", "6", "8"), 2),
    ("계산: 5 + 11 = ?", ("14", "17", "16", "19"), 3),
    ("계산: 17 - 12 = ?", ("3", "6", "8", "5"), 4),
    ("계산: 5 × 13 = ?", ("65", "63", "66", "68"), 1),
    ("계산: 70 ÷ 14 = ?", ("3", "5", "6", "8"), 2),
    ("계산: 5 + 15 = ?", ("18", "21", "20", "23"), 3),
    ("계산: 21 - 16 = ?", ("3", "6", "8", "5"), 4),
    ("계산: 5 × 17 = ?", ("85", "83", "86", "88"), 1),
    ("계산: 90 ÷ 18 = ?", ("3", "5", "6", "8"), 2),
    ("계산: 5 + 19 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 25 - 20 = ?", ("3", "6", "8", "5"), 4),
    ("계산: 5 × 21 = ?", ("105", "103", "106", "108"), 1),
    ("계산: 110 ÷ 22 = ?", ("3", "5", "6", "8"), 2),
    ("계산: 5 + 23 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 29 - 24 = ?", ("3", "6", "8", "5"), 4),
    ("계산: 5 × 25 = ?", ("125", "123", "126", "128"), 1),
    ("계산: 6 + 2 = ?", ("6", "8", "9", "11"), 2),
    ("계산: 9 - 3 = ?", ("4", "7", "6", "9"), 3),
    ("계산: 6 × 4 = ?", ("22", "25", "27", "24"), 4),
    ("계산: 30 ÷ 5 = ?", ("6", "4", "7", "9"), 1),
    ("계산: 6 + 6 = ?", ("10", "12", "13", "15"), 2),
    ("계산: 13 - 7 = ?", ("4", "7", "6", "9"), 3),
    ("계산: 6 × 8 = ?", ("46", "49", "51", "48"), 4),
    ("계산: 54 ÷ 9 = ?", ("6", "4", "7", "9"), 1),
    ("계산: 6 + 10 = ?", ("14", "16", "17", "19"), 2),
    ("계산: 17 - 11 = ?", ("4", "7", "6", "9"), 3),
    ("계산: 6 × 12 = ?", ("70", "73", "75", "72"), 4),
    ("계산: 78 ÷ 13 = ?", ("6", "4", "7", "9"), 1),
    ("계산: 6 + 14 = ?", ("18", "20", "21", "23"), 2),
    ("계산: 21 - 15 = ?", ("4", "7", "6", "9"), 3),
    ("계산: 6 × 16 = ?", ("94", "97", "99", "96"), 4),
    ("계산: 102 ÷ 17 = ?", ("6", "4", "7", "9"), 1),
    ("계산: 6 + 18 = ?", ("22", "24", "25", "27"), 2),
    ("계산: 25 - 19 = ?", ("4", "7", "6", "9"), 3),
    ("계산: 6 × 20 = ?", ("118", "121", "123", "120"), 4),
    ("계산: 126 ÷ 21 = ?", ("6", "4", "7", "9"), 1),
    ("계산: 6 + 22 = ?", ("26", "28", "29", "31"), 2),
    ("계산: 29 - 23 = ?", ("4", "7", "6", "9"), 3),
    ("계산: 6 × 24 = ?", ("142", "145", "147", "144"), 4),
    ("계산: 150 ÷ 25 = ?", ("6", "4", "7", "9"), 1),
    ("계산: 9 - 2 = ?", ("5", "7", "8", "10"), 2),
    ("계산: 7 × 3 = ?", ("19", "22", "21", "24"), 3),
    ("계산: 28 ÷ 4 = ?", ("5", "8", "10", "7"), 4),
    ("계산: 7 + 5 = ?", ("12", "10", "13", "15"), 1),
    ("계산: 13 - 6 = ?", ("5", "7", "8", "10"), 2),
    ("계산: 7 × 7 = ?", ("47", "50", "49", "52"), 3),
    ("계산: 56 ÷ 8 = ?", ("5", "8", "10", "7"), 4),
    ("계산: 7 + 9 = ?", ("16", "14", "17", "19"), 1),
    ("계산: 17 - 10 = ?", ("5", "7", "8", "10"), 2),
    ("계산: 7 × 11 = ?", ("75", "78", "77", "80"), 3),
    ("계산: 84 ÷ 12 = ?", ("5", "8", "10", "7"), 4),
    ("계산: 7 + 13 = ?", ("20", "18", "21", "23"), 1),
    ("계산: 21 - 14 = ?", ("5", "7", "8", "10"), 2),
    ("계산: 7 × 15 = ?", ("103", "106", "105", "108"), 3),
    ("계산: 112 ÷ 16 = ?", ("5", "8", "10", "7"), 4),
    ("계산: 7 + 17 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 25 - 18 = ?", ("5", "7", "8", "10"), 2),
    ("계산: 7 × 19 = ?", ("131", "134", "133", "136"), 3),
    ("계산: 140 ÷ 20 = ?", ("5", "8", "10", "7"), 4),
    ("계산: 7 + 21 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 29 - 22 = ?", ("5", "7", "8", "10"), 2),
    ("계산: 7 × 23 = ?", ("159", "162", "161", "164"), 3),
    ("계산: 168 ÷ 24 = ?", ("5", "8", "10", "7"), 4),
    ("계산: 7 + 25 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 8 × 2 = ?", ("14", "16", "17", "19"), 2),
    ("계산: 24 ÷ 3 = ?", ("6", "9", "8", "11"), 3),
    ("계산: 8 + 4 = ?", ("10", "13", "15", "12"), 4),
    ("계산: 13 - 5 = ?", ("8", "6", "9", "11"), 1),
    ("계산: 8 × 6 = ?", ("46", "48", "49", "51"), 2),
    ("계산: 56 ÷ 7 = ?", ("6", "9", "8", "11"), 3),
    ("계산: 8 + 8 = ?", ("14", "17", "19", "16"), 4),
    ("계산: 17 - 9 = ?", ("8", "6", "9", "11"), 1),
    ("계산: 8 × 10 = ?", ("78", "80", "81", "83"), 2),
    ("계산: 88 ÷ 11 = ?", ("6", "9", "8", "11"), 3),
    ("계산: 8 + 12 = ?", ("18", "21", "23", "20"), 4),
    ("계산: 21 - 13 = ?", ("8", "6", "9", "11"), 1),
    ("계산: 8 × 14 = ?", ("110", "112", "113", "115"), 2),
    ("계산: 120 ÷ 15 = ?", ("6", "9", "8", "11"), 3),
    ("계산: 8 + 16 = ?", ("22", "25", "27", "24"), 4),
    ("계산: 25 - 17 = ?", ("8", "6", "9", "11"), 1),
    ("계산: 8 × 18 = ?", ("142", "144", "145", "147"), 2),
    ("계산: 152 ÷ 19 = ?", ("6", "9", "8", "11"), 3),
    ("계산: 8 + 20 = ?", ("26", "29", "31", "28"), 4),
    ("계산: 29 - 21 = ?", ("8", "6", "9", "11"), 1),
    ("계산: 8 × 22 = ?", ("174", "176", "177", "179"), 2),
    ("계산: 184 ÷ 23 = ?", ("6", "9", "8", "11"), 3),
    ("계산: 8 + 24 = ?", ("30", "33", "35", "32"), 4),
    ("계산: 33 - 25 = ?", ("8", "6", "9", "11"), 1),
    ("계산: 18 ÷ 2 = ?", ("7", "9", "10", "12"), 2),
    ("계산: 9 + 3 = ?", ("10", "13", "12", "15"), 3),
    ("계산: 13 - 4 = ?", ("7", "10", "12", "9"), 4),
    ("계산: 9 × 5 = ?", ("45", "43", "46", "48"), 1),
    ("계산: 54 ÷ 6 = ?", ("7", "9", "10", "12"), 2),
    ("계산: 9 + 7 = ?", ("14", "17", "16", "19"), 3),
    ("계산: 17 - 8 = ?", ("7", "10", "12", "9"), 4),
    ("계산: 9 × 9 = ?", ("81", "79", "82", "84"), 1),
    ("계산: 90 ÷ 10 = ?", ("7", "9", "10", "12"), 2),
    ("계산: 9 + 11 = ?", ("18", "21", "20", "23"), 3),
    ("계산: 21 - 12 = ?", ("7", "10", "12", "9"), 4),
    ("계산: 9 × 13 = ?", ("117", "115", "118", "120"), 1),
    ("계산: 126 ÷ 14 = ?", ("7", "9", "10", "12"), 2),
    ("계산: 9 + 15 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 25 - 16 = ?", ("7", "10", "12", "9"), 4),
    ("계산: 9 × 17 = ?", ("153", "151", "154", "156"), 1),
    ("계산: 162 ÷ 18 = ?", ("7", "9", "10", "12"), 2),
    ("계산: 9 + 19 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 29 - 20 = ?", ("7", "10", "12", "9"), 4),
    ("계산: 9 × 21 = ?", ("189", "187", "190", "192"), 1),
    ("계산: 198 ÷ 22 = ?", ("7", "9", "10", "12"), 2),
    ("계산: 9 + 23 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 33 - 24 = ?", ("7", "10", "12", "9"), 4),
    ("계산: 9 × 25 = ?", ("225", "223", "226", "228"), 1),
    ("계산: 10 + 2 = ?", ("10", "12", "13", "15"), 2),
    ("계산: 13 - 3 = ?", ("8", "11", "10", "13"), 3),
    ("계산: 10 × 4 = ?", ("38", "41", "43", "40"), 4),
    ("계산: 50 ÷ 5 = ?", ("10", "8", "11", "13"), 1),
    ("계산: 10 + 6 = ?", ("14", "16", "17", "19"), 2),
    ("계산: 17 - 7 = ?", ("8", "11", "10", "13"), 3),
    ("계산: 10 × 8 = ?", ("78", "81", "83", "80"), 4),
    ("계산: 90 ÷ 9 = ?", ("10", "8", "11", "13"), 1),
    ("계산: 10 + 10 = ?", ("18", "20", "21", "23"), 2),
    ("계산: 21 - 11 = ?", ("8", "11", "10", "13"), 3),
    ("계산: 10 × 12 = ?", ("118", "121", "123", "120"), 4),
    ("계산: 130 ÷ 13 = ?", ("10", "8", "11", "13"), 1),
    ("계산: 10 + 14 = ?", ("22", "24", "25", "27"), 2),
    ("계산: 25 - 15 = ?", ("8", "11", "10", "13"), 3),
    ("계산: 10 × 16 = ?", ("158", "161", "163", "160"), 4),
    ("계산: 170 ÷ 17 = ?", ("10", "8", "11", "13"), 1),
    ("계산: 10 + 18 = ?", ("26", "28", "29", "31"), 2),
    ("계산: 29 - 19 = ?", ("8", "11", "10", "13"), 3),
    ("계산: 10 × 20 = ?", ("198", "201", "203", "200"), 4),
    ("계산: 210 ÷ 21 = ?", ("10", "8", "11", "13"), 1),
    ("계산: 10 + 22 = ?", ("30", "32", "33", "35"), 2),
    ("계산: 33 - 23 = ?", ("8", "11", "10", "13"), 3),
    ("계산: 10 × 24 = ?", ("238", "241", "243", "240"), 4),
    ("계산: 250 ÷ 25 = ?", ("10", "8", "11", "13"), 1),
    ("계산: 13 - 2 = ?", ("9", "11", "12", "14"), 2),
    ("계산: 11 × 3 = ?", ("31", "34", "33", "36"), 3),
    ("계산: 44 ÷ 4 = ?", ("9", "12", "14", "11"), 4),
    ("계산: 11 + 5 = ?", ("16", "14", "17", "19"), 1),
    ("계산: 17 - 6 = ?", ("9", "11", "12", "14"), 2),
    ("계산: 11 × 7 = ?", ("75", "78", "77", "80"), 3),
    ("계산: 88 ÷ 8 = ?", ("9", "12", "14", "11"), 4),
    ("계산: 11 + 9 = ?", ("20", "18", "21", "23"), 1),
    ("계산: 21 - 10 = ?", ("9", "11", "12", "14"), 2),
    ("계산: 11 × 11 = ?", ("119", "122", "121", "124"), 3),
    ("계산: 132 ÷ 12 = ?", ("9", "12", "14", "11"), 4),
    ("계산: 11 + 13 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 25 - 14 = ?", ("9", "11", "12", "14"), 2),
    ("계산: 11 × 15 = ?", ("163", "166", "165", "168"), 3),
    ("계산: 176 ÷ 16 = ?", ("9", "12", "14", "11"), 4),
    ("계산: 11 + 17 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 29 - 18 = ?", ("9", "11", "12", "14"), 2),
    ("계산: 11 × 19 = ?", ("207", "210", "209", "212"), 3),
    ("계산: 220 ÷ 20 = ?", ("9", "12", "14", "11"), 4),
    ("계산: 11 + 21 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 33 - 22 = ?", ("9", "11", "12", "14"), 2),
    ("계산: 11 × 23 = ?", ("251", "254", "253", "256"), 3),
    ("계산: 264 ÷ 24 = ?", ("9", "12", "14", "11"), 4),
    ("계산: 11 + 25 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 12 × 2 = ?", ("22", "24", "25", "27"), 2),
    ("계산: 36 ÷ 3 = ?", ("10", "13", "12", "15"), 3),
    ("계산: 12 + 4 = ?", ("14", "17", "19", "16"), 4),
    ("계산: 17 - 5 = ?", ("12", "10", "13", "15"), 1),
    ("계산: 12 × 6 = ?", ("70", "72", "73", "75"), 2),
    ("계산: 84 ÷ 7 = ?", ("10", "13", "12", "15"), 3),
    ("계산: 12 + 8 = ?", ("18", "21", "23", "20"), 4),
    ("계산: 21 - 9 = ?", ("12", "10", "13", "15"), 1),
    ("계산: 12 × 10 = ?", ("118", "120", "121", "123"), 2),
    ("계산: 132 ÷ 11 = ?", ("10", "13", "12", "15"), 3),
    ("계산: 12 + 12 = ?", ("22", "25", "27", "24"), 4),
    ("계산: 25 - 13 = ?", ("12", "10", "13", "15"), 1),
    ("계산: 12 × 14 = ?", ("166", "168", "169", "171"), 2),
    ("계산: 180 ÷ 15 = ?", ("10", "13", "12", "15"), 3),
    ("계산: 12 + 16 = ?", ("26", "29", "31", "28"), 4),
    ("계산: 29 - 17 = ?", ("12", "10", "13", "15"), 1),
    ("계산: 12 × 18 = ?", ("214", "216", "217", "219"), 2),
    ("계산: 228 ÷ 19 = ?", ("10", "13", "12", "15"), 3),
    ("계산: 12 + 20 = ?", ("30", "33", "35", "32"), 4),
    ("계산: 33 - 21 = ?", ("12", "10", "13", "15"), 1),
    ("계산: 12 × 22 = ?", ("262", "264", "265", "267"), 2),
    ("계산: 276 ÷ 23 = ?", ("10", "13", "12", "15"), 3),
    ("계산: 12 + 24 = ?", ("34", "37", "39", "36"), 4),
    ("계산: 37 - 25 = ?", ("12", "10", "13", "15"), 1),
    ("계산: 26 ÷ 2 = ?", ("11", "13", "14", "16"), 2),
    ("계산: 13 + 3 = ?", ("14", "17", "16", "19"), 3),
    ("계산: 17 - 4 = ?", ("11", "14", "16", "13"), 4),
    ("계산: 13 × 5 = ?", ("65", "63", "66", "68"), 1),
    ("계산: 78 ÷ 6 = ?", ("11", "13", "14", "16"), 2),
    ("계산: 13 + 7 = ?", ("18", "21", "20", "23"), 3),
    ("계산: 21 - 8 = ?", ("11", "14", "16", "13"), 4),
    ("계산: 13 × 9 = ?", ("117", "115", "118", "120"), 1),
    ("계산: 130 ÷ 10 = ?", ("11", "13", "14", "16"), 2),
    ("계산: 13 + 11 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 25 - 12 = ?", ("11", "14", "16", "13"), 4),
    ("계산: 13 × 13 = ?", ("169", "167", "170", "172"), 1),
    ("계산: 182 ÷ 14 = ?", ("11", "13", "14", "16"), 2),
    ("계산: 13 + 15 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 29 - 16 = ?", ("11", "14", "16", "13"), 4),
    ("계산: 13 × 17 = ?", ("221", "219", "222", "224"), 1),
    ("계산: 234 ÷ 18 = ?", ("11", "13", "14", "16"), 2),
    ("계산: 13 + 19 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 33 - 20 = ?", ("11", "14", "16", "13"), 4),
    ("계산: 13 × 21 = ?", ("273", "271", "274", "276"), 1),
    ("계산: 286 ÷ 22 = ?", ("11", "13", "14", "16"), 2),
    ("계산: 13 + 23 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 37 - 24 = ?", ("11", "14", "16", "13"), 4),
    ("계산: 13 × 25 = ?", ("325", "323", "326", "328"), 1),
    ("계산: 14 + 2 = ?", ("14", "16", "17", "19"), 2),
    ("계산: 17 - 3 = ?", ("12", "15", "14", "17"), 3),
    ("계산: 14 × 4 = ?", ("54", "57", "59", "56"), 4),
    ("계산: 70 ÷ 5 = ?", ("14", "12", "15", "17"), 1),
    ("계산: 14 + 6 = ?", ("18", "20", "21", "23"), 2),
    ("계산: 21 - 7 = ?", ("12", "15", "14", "17"), 3),
    ("계산: 14 × 8 = ?", ("110", "113", "115", "112"), 4),
    ("계산: 126 ÷ 9 = ?", ("14", "12", "15", "17"), 1),
    ("계산: 14 + 10 = ?", ("22", "24", "25", "27"), 2),
    ("계산: 25 - 11 = ?", ("12", "15", "14", "17"), 3),
    ("계산: 14 × 12 = ?", ("166", "169", "171", "168"), 4),
    ("계산: 182 ÷ 13 = ?", ("14", "12", "15", "17"), 1),
    ("계산: 14 + 14 = ?", ("26", "28", "29", "31"), 2),
    ("계산: 29 - 15 = ?", ("12", "15", "14", "17"), 3),
    ("계산: 14 × 16 = ?", ("222", "225", "227", "224"), 4),
    ("계산: 238 ÷ 17 = ?", ("14", "12", "15", "17"), 1),
    ("계산: 14 + 18 = ?", ("30", "32", "33", "35"), 2),
    ("계산: 33 - 19 = ?", ("12", "15", "14", "17"), 3),
    ("계산: 14 × 20 = ?", ("278", "281", "283", "280"), 4),
    ("계산: 294 ÷ 21 = ?", ("14", "12", "15", "17"), 1),
    ("계산: 14 + 22 = ?", ("34", "36", "37", "39"), 2),
    ("계산: 37 - 23 = ?", ("12", "15", "14", "17"), 3),
    ("계산: 14 × 24 = ?", ("334", "337", "339", "336"), 4),
    ("계산: 350 ÷ 25 = ?", ("14", "12", "15", "17"), 1),
    ("계산: 17 - 2 = ?", ("13", "15", "16", "18"), 2),
    ("계산: 15 × 3 = ?", ("43", "46", "45", "48"), 3),
    ("계산: 60 ÷ 4 = ?", ("13", "16", "18", "15"), 4),
    ("계산: 15 + 5 = ?", ("20", "18", "21", "23"), 1),
    ("계산: 21 - 6 = ?", ("13", "15", "16", "18"), 2),
    ("계산: 15 × 7 = ?", ("103", "106", "105", "108"), 3),
    ("계산: 120 ÷ 8 = ?", ("13", "16", "18", "15"), 4),
    ("계산: 15 + 9 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 25 - 10 = ?", ("13", "15", "16", "18"), 2),
    ("계산: 15 × 11 = ?", ("163", "166", "165", "168"), 3),
    ("계산: 180 ÷ 12 = ?", ("13", "16", "18", "15"), 4),
    ("계산: 15 + 13 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 29 - 14 = ?", ("13", "15", "16", "18"), 2),
    ("계산: 15 × 15 = ?", ("223", "226", "225", "228"), 3),
    ("계산: 240 ÷ 16 = ?", ("13", "16", "18", "15"), 4),
    ("계산: 15 + 17 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 33 - 18 = ?", ("13", "15", "16", "18"), 2),
    ("계산: 15 × 19 = ?", ("283", "286", "285", "288"), 3),
    ("계산: 300 ÷ 20 = ?", ("13", "16", "18", "15"), 4),
    ("계산: 15 + 21 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 37 - 22 = ?", ("13", "15", "16", "18"), 2),
    ("계산: 15 × 23 = ?", ("343", "346", "345", "348"), 3),
    ("계산: 360 ÷ 24 = ?", ("13", "16", "18", "15"), 4),
    ("계산: 15 + 25 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 16 × 2 = ?", ("30", "32", "33", "35"), 2),
    ("계산: 48 ÷ 3 = ?", ("14", "17", "16", "19"), 3),
    ("계산: 16 + 4 = ?", ("18", "21", "23", "20"), 4),
    ("계산: 21 - 5 = ?", ("16", "14", "17", "19"), 1),
    ("계산: 16 × 6 = ?", ("94", "96", "97", "99"), 2),
    ("계산: 112 ÷ 7 = ?", ("14", "17", "16", "19"), 3),
    ("계산: 16 + 8 = ?", ("22", "25", "27", "24"), 4),
    ("계산: 25 - 9 = ?", ("16", "14", "17", "19"), 1),
    ("계산: 16 × 10 = ?", ("158", "160", "161", "163"), 2),
    ("계산: 176 ÷ 11 = ?", ("14", "17", "16", "19"), 3),
    ("계산: 16 + 12 = ?", ("26", "29", "31", "28"), 4),
    ("계산: 29 - 13 = ?", ("16", "14", "17", "19"), 1),
    ("계산: 16 × 14 = ?", ("222", "224", "225", "227"), 2),
    ("계산: 240 ÷ 15 = ?", ("14", "17", "16", "19"), 3),
    ("계산: 16 + 16 = ?", ("30", "33", "35", "32"), 4),
    ("계산: 33 - 17 = ?", ("16", "14", "17", "19"), 1),
    ("계산: 16 × 18 = ?", ("286", "288", "289", "291"), 2),
    ("계산: 304 ÷ 19 = ?", ("14", "17", "16", "19"), 3),
    ("계산: 16 + 20 = ?", ("34", "37", "39", "36"), 4),
    ("계산: 37 - 21 = ?", ("16", "14", "17", "19"), 1),
    ("계산: 16 × 22 = ?", ("350", "352", "353", "355"), 2),
    ("계산: 368 ÷ 23 = ?", ("14", "17", "16", "19"), 3),
    ("계산: 16 + 24 = ?", ("38", "41", "43", "40"), 4),
    ("계산: 41 - 25 = ?", ("16", "14", "17", "19"), 1),
    ("계산: 34 ÷ 2 = ?", ("15", "17", "18", "20"), 2),
    ("계산: 17 + 3 = ?", ("18", "21", "20", "23"), 3),
    ("계산: 21 - 4 = ?", ("15", "18", "20", "17"), 4),
    ("계산: 17 × 5 = ?", ("85", "83", "86", "88"), 1),
    ("계산: 102 ÷ 6 = ?", ("15", "17", "18", "20"), 2),
    ("계산: 17 + 7 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 25 - 8 = ?", ("15", "18", "20", "17"), 4),
    ("계산: 17 × 9 = ?", ("153", "151", "154", "156"), 1),
    ("계산: 170 ÷ 10 = ?", ("15", "17", "18", "20"), 2),
    ("계산: 17 + 11 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 29 - 12 = ?", ("15", "18", "20", "17"), 4),
    ("계산: 17 × 13 = ?", ("221", "219", "222", "224"), 1),
    ("계산: 238 ÷ 14 = ?", ("15", "17", "18", "20"), 2),
    ("계산: 17 + 15 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 33 - 16 = ?", ("15", "18", "20", "17"), 4),
    ("계산: 17 × 17 = ?", ("289", "287", "290", "292"), 1),
    ("계산: 306 ÷ 18 = ?", ("15", "17", "18", "20"), 2),
    ("계산: 17 + 19 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 37 - 20 = ?", ("15", "18", "20", "17"), 4),
    ("계산: 17 × 21 = ?", ("357", "355", "358", "360"), 1),
    ("계산: 374 ÷ 22 = ?", ("15", "17", "18", "20"), 2),
    ("계산: 17 + 23 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 41 - 24 = ?", ("15", "18", "20", "17"), 4),
    ("계산: 17 × 25 = ?", ("425", "423", "426", "428"), 1),
    ("계산: 18 + 2 = ?", ("18", "20", "21", "23"), 2),
    ("계산: 21 - 3 = ?", ("16", "19", "18", "21"), 3),
    ("계산: 18 × 4 = ?", ("70", "73", "75", "72"), 4),
    ("계산: 90 ÷ 5 = ?", ("18", "16", "19", "21"), 1),
    ("계산: 18 + 6 = ?", ("22", "24", "25", "27"), 2),
    ("계산: 25 - 7 = ?", ("16", "19", "18", "21"), 3),
    ("계산: 18 × 8 = ?", ("142", "145", "147", "144"), 4),
    ("계산: 162 ÷ 9 = ?", ("18", "16", "19", "21"), 1),
    ("계산: 18 + 10 = ?", ("26", "28", "29", "31"), 2),
    ("계산: 29 - 11 = ?", ("16", "19", "18", "21"), 3),
    ("계산: 18 × 12 = ?", ("214", "217", "219", "216"), 4),
    ("계산: 234 ÷ 13 = ?", ("18", "16", "19", "21"), 1),
    ("계산: 18 + 14 = ?", ("30", "32", "33", "35"), 2),
    ("계산: 33 - 15 = ?", ("16", "19", "18", "21"), 3),
    ("계산: 18 × 16 = ?", ("286", "289", "291", "288"), 4),
    ("계산: 306 ÷ 17 = ?", ("18", "16", "19", "21"), 1),
    ("계산: 18 + 18 = ?", ("34", "36", "37", "39"), 2),
    ("계산: 37 - 19 = ?", ("16", "19", "18", "21"), 3),
    ("계산: 18 × 20 = ?", ("358", "361", "363", "360"), 4),
    ("계산: 378 ÷ 21 = ?", ("18", "16", "19", "21"), 1),
    ("계산: 18 + 22 = ?", ("38", "40", "41", "43"), 2),
    ("계산: 41 - 23 = ?", ("16", "19", "18", "21"), 3),
    ("계산: 18 × 24 = ?", ("430", "433", "435", "432"), 4),
    ("계산: 450 ÷ 25 = ?", ("18", "16", "19", "21"), 1),
    ("계산: 21 - 2 = ?", ("17", "19", "20", "22"), 2),
    ("계산: 19 × 3 = ?", ("55", "58", "57", "60"), 3),
    ("계산: 76 ÷ 4 = ?", ("17", "20", "22", "19"), 4),
    ("계산: 19 + 5 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 25 - 6 = ?", ("17", "19", "20", "22"), 2),
    ("계산: 19 × 7 = ?", ("131", "134", "133", "136"), 3),
    ("계산: 152 ÷ 8 = ?", ("17", "20", "22", "19"), 4),
    ("계산: 19 + 9 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 29 - 10 = ?", ("17", "19", "20", "22"), 2),
    ("계산: 19 × 11 = ?", ("207", "210", "209", "212"), 3),
    ("계산: 228 ÷ 12 = ?", ("17", "20", "22", "19"), 4),
    ("계산: 19 + 13 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 33 - 14 = ?", ("17", "19", "20", "22"), 2),
    ("계산: 19 × 15 = ?", ("283", "286", "285", "288"), 3),
    ("계산: 304 ÷ 16 = ?", ("17", "20", "22", "19"), 4),
    ("계산: 19 + 17 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 37 - 18 = ?", ("17", "19", "20", "22"), 2),
    ("계산: 19 × 19 = ?", ("359", "362", "361", "364"), 3),
    ("계산: 380 ÷ 20 = ?", ("17", "20", "22", "19"), 4),
    ("계산: 19 + 21 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 41 - 22 = ?", ("17", "19", "20", "22"), 2),
    ("계산: 19 × 23 = ?", ("435", "438", "437", "440"), 3),
    ("계산: 456 ÷ 24 = ?", ("17", "20", "22", "19"), 4),
    ("계산: 19 + 25 = ?", ("44", "42", "45", "47"), 1),
    ("계산: 20 × 2 = ?", ("38", "40", "41", "43"), 2),
    ("계산: 60 ÷ 3 = ?", ("18", "21", "20", "23"), 3),
    ("계산: 20 + 4 = ?", ("22", "25", "27", "24"), 4),
    ("계산: 25 - 5 = ?", ("20", "18", "21", "23"), 1),
    ("계산: 20 × 6 = ?", ("118", "120", "121", "123"), 2),
    ("계산: 140 ÷ 7 = ?", ("18", "21", "20", "23"), 3),
    ("계산: 20 + 8 = ?", ("26", "29", "31", "28"), 4),
    ("계산: 29 - 9 = ?", ("20", "18", "21", "23"), 1),
    ("계산: 20 × 10 = ?", ("198", "200", "201", "203"), 2),
    ("계산: 220 ÷ 11 = ?", ("18", "21", "20", "23"), 3),
    ("계산: 20 + 12 = ?", ("30", "33", "35", "32"), 4),
    ("계산: 33 - 13 = ?", ("20", "18", "21", "23"), 1),
    ("계산: 20 × 14 = ?", ("278", "280", "281", "283"), 2),
    ("계산: 300 ÷ 15 = ?", ("18", "21", "20", "23"), 3),
    ("계산: 20 + 16 = ?", ("34", "37", "39", "36"), 4),
    ("계산: 37 - 17 = ?", ("20", "18", "21", "23"), 1),
    ("계산: 20 × 18 = ?", ("358", "360", "361", "363"), 2),
    ("계산: 380 ÷ 19 = ?", ("18", "21", "20", "23"), 3),
    ("계산: 20 + 20 = ?", ("38", "41", "43", "40"), 4),
    ("계산: 41 - 21 = ?", ("20", "18", "21", "23"), 1),
    ("계산: 20 × 22 = ?", ("438", "440", "441", "443"), 2),
    ("계산: 460 ÷ 23 = ?", ("18", "21", "20", "23"), 3),
    ("계산: 20 + 24 = ?", ("42", "45", "47", "44"), 4),
    ("계산: 45 - 25 = ?", ("20", "18", "21", "23"), 1),
    ("계산: 42 ÷ 2 = ?", ("19", "21", "22", "24"), 2),
    ("계산: 21 + 3 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 25 - 4 = ?", ("19", "22", "24", "21"), 4),
    ("계산: 21 × 5 = ?", ("105", "103", "106", "108"), 1),
    ("계산: 126 ÷ 6 = ?", ("19", "21", "22", "24"), 2),
    ("계산: 21 + 7 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 29 - 8 = ?", ("19", "22", "24", "21"), 4),
    ("계산: 21 × 9 = ?", ("189", "187", "190", "192"), 1),
    ("계산: 210 ÷ 10 = ?", ("19", "21", "22", "24"), 2),
    ("계산: 21 + 11 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 33 - 12 = ?", ("19", "22", "24", "21"), 4),
    ("계산: 21 × 13 = ?", ("273", "271", "274", "276"), 1),
    ("계산: 294 ÷ 14 = ?", ("19", "21", "22", "24"), 2),
    ("계산: 21 + 15 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 37 - 16 = ?", ("19", "22", "24", "21"), 4),
    ("계산: 21 × 17 = ?", ("357", "355", "358", "360"), 1),
    ("계산: 378 ÷ 18 = ?", ("19", "21", "22", "24"), 2),
    ("계산: 21 + 19 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 41 - 20 = ?", ("19", "22", "24", "21"), 4),
    ("계산: 21 × 21 = ?", ("441", "439", "442", "444"), 1),
    ("계산: 462 ÷ 22 = ?", ("19", "21", "22", "24"), 2),
    ("계산: 21 + 23 = ?", ("42", "45", "44", "47"), 3),
    ("계산: 45 - 24 = ?", ("19", "22", "24", "21"), 4),
    ("계산: 21 × 25 = ?", ("525", "523", "526", "528"), 1),
    ("계산: 22 + 2 = ?", ("22", "24", "25", "27"), 2),
    ("계산: 25 - 3 = ?", ("20", "23", "22", "25"), 3),
    ("계산: 22 × 4 = ?", ("86", "89", "91", "88"), 4),
    ("계산: 110 ÷ 5 = ?", ("22", "20", "23", "25"), 1),
    ("계산: 22 + 6 = ?", ("26", "28", "29", "31"), 2),
    ("계산: 29 - 7 = ?", ("20", "23", "22", "25"), 3),
    ("계산: 22 × 8 = ?", ("174", "177", "179", "176"), 4),
    ("계산: 198 ÷ 9 = ?", ("22", "20", "23", "25"), 1),
    ("계산: 22 + 10 = ?", ("30", "32", "33", "35"), 2),
    ("계산: 33 - 11 = ?", ("20", "23", "22", "25"), 3),
    ("계산: 22 × 12 = ?", ("262", "265", "267", "264"), 4),
    ("계산: 286 ÷ 13 = ?", ("22", "20", "23", "25"), 1),
    ("계산: 22 + 14 = ?", ("34", "36", "37", "39"), 2),
    ("계산: 37 - 15 = ?", ("20", "23", "22", "25"), 3),
    ("계산: 22 × 16 = ?", ("350", "353", "355", "352"), 4),
    ("계산: 374 ÷ 17 = ?", ("22", "20", "23", "25"), 1),
    ("계산: 22 + 18 = ?", ("38", "40", "41", "43"), 2),
    ("계산: 41 - 19 = ?", ("20", "23", "22", "25"), 3),
    ("계산: 22 × 20 = ?", ("438", "441", "443", "440"), 4),
    ("계산: 462 ÷ 21 = ?", ("22", "20", "23", "25"), 1),
    ("계산: 22 + 22 = ?", ("42", "44", "45", "47"), 2),
    ("계산: 45 - 23 = ?", ("20", "23", "22", "25"), 3),
    ("계산: 22 × 24 = ?", ("526", "529", "531", "528"), 4),
    ("계산: 550 ÷ 25 = ?", ("22", "20", "23", "25"), 1),
    ("계산: 25 - 2 = ?", ("21", "23", "24", "26"), 2),
    ("계산: 23 × 3 = ?", ("67", "70", "69", "72"), 3),
    ("계산: 92 ÷ 4 = ?", ("21", "24", "26", "23"), 4),
    ("계산: 23 + 5 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 29 - 6 = ?", ("21", "23", "24", "26"), 2),
    ("계산: 23 × 7 = ?", ("159", "162", "161", "164"), 3),
    ("계산: 184 ÷ 8 = ?", ("21", "24", "26", "23"), 4),
    ("계산: 23 + 9 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 33 - 10 = ?", ("21", "23", "24", "26"), 2),
    ("계산: 23 × 11 = ?", ("251", "254", "253", "256"), 3),
    ("계산: 276 ÷ 12 = ?", ("21", "24", "26", "23"), 4),
    ("계산: 23 + 13 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 37 - 14 = ?", ("21", "23", "24", "26"), 2),
    ("계산: 23 × 15 = ?", ("343", "346", "345", "348"), 3),
    ("계산: 368 ÷ 16 = ?", ("21", "24", "26", "23"), 4),
    ("계산: 23 + 17 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 41 - 18 = ?", ("21", "23", "24", "26"), 2),
    ("계산: 23 × 19 = ?", ("435", "438", "437", "440"), 3),
    ("계산: 460 ÷ 20 = ?", ("21", "24", "26", "23"), 4),
    ("계산: 23 + 21 = ?", ("44", "42", "45", "47"), 1),
    ("계산: 45 - 22 = ?", ("21", "23", "24", "26"), 2),
    ("계산: 23 × 23 = ?", ("527", "530", "529", "532"), 3),
    ("계산: 552 ÷ 24 = ?", ("21", "24", "26", "23"), 4),
    ("계산: 23 + 25 = ?", ("48", "46", "49", "51"), 1),
    ("계산: 24 × 2 = ?", ("46", "48", "49", "51"), 2),
    ("계산: 72 ÷ 3 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 24 + 4 = ?", ("26", "29", "31", "28"), 4),
    ("계산: 29 - 5 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 24 × 6 = ?", ("142", "144", "145", "147"), 2),
    ("계산: 168 ÷ 7 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 24 + 8 = ?", ("30", "33", "35", "32"), 4),
    ("계산: 33 - 9 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 24 × 10 = ?", ("238", "240", "241", "243"), 2),
    ("계산: 264 ÷ 11 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 24 + 12 = ?", ("34", "37", "39", "36"), 4),
    ("계산: 37 - 13 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 24 × 14 = ?", ("334", "336", "337", "339"), 2),
    ("계산: 360 ÷ 15 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 24 + 16 = ?", ("38", "41", "43", "40"), 4),
    ("계산: 41 - 17 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 24 × 18 = ?", ("430", "432", "433", "435"), 2),
    ("계산: 456 ÷ 19 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 24 + 20 = ?", ("42", "45", "47", "44"), 4),
    ("계산: 45 - 21 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 24 × 22 = ?", ("526", "528", "529", "531"), 2),
    ("계산: 552 ÷ 23 = ?", ("22", "25", "24", "27"), 3),
    ("계산: 24 + 24 = ?", ("46", "49", "51", "48"), 4),
    ("계산: 49 - 25 = ?", ("24", "22", "25", "27"), 1),
    ("계산: 50 ÷ 2 = ?", ("23", "25", "26", "28"), 2),
    ("계산: 25 + 3 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 29 - 4 = ?", ("23", "26", "28", "25"), 4),
    ("계산: 25 × 5 = ?", ("125", "123", "126", "128"), 1),
    ("계산: 150 ÷ 6 = ?", ("23", "25", "26", "28"), 2),
    ("계산: 25 + 7 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 33 - 8 = ?", ("23", "26", "28", "25"), 4),
    ("계산: 25 × 9 = ?", ("225", "223", "226", "228"), 1),
    ("계산: 250 ÷ 10 = ?", ("23", "25", "26", "28"), 2),
    ("계산: 25 + 11 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 37 - 12 = ?", ("23", "26", "28", "25"), 4),
    ("계산: 25 × 13 = ?", ("325", "323", "326", "328"), 1),
    ("계산: 350 ÷ 14 = ?", ("23", "25", "26", "28"), 2),
    ("계산: 25 + 15 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 41 - 16 = ?", ("23", "26", "28", "25"), 4),
    ("계산: 25 × 17 = ?", ("425", "423", "426", "428"), 1),
    ("계산: 450 ÷ 18 = ?", ("23", "25", "26", "28"), 2),
    ("계산: 25 + 19 = ?", ("42", "45", "44", "47"), 3),
    ("계산: 45 - 20 = ?", ("23", "26", "28", "25"), 4),
    ("계산: 25 × 21 = ?", ("525", "523", "526", "528"), 1),
    ("계산: 550 ÷ 22 = ?", ("23", "25", "26", "28"), 2),
    ("계산: 25 + 23 = ?", ("46", "49", "48", "51"), 3),
    ("계산: 49 - 24 = ?", ("23", "26", "28", "25"), 4),
    ("계산: 25 × 25 = ?", ("625", "623", "626", "628"), 1),
    ("계산: 26 + 2 = ?", ("26", "28", "29", "31"), 2),
    ("계산: 29 - 3 = ?", ("24", "27", "26", "29"), 3),
    ("계산: 26 × 4 = ?", ("102", "105", "107", "104"), 4),
    ("계산: 130 ÷ 5 = ?", ("26", "24", "27", "29"), 1),
    ("계산: 26 + 6 = ?", ("30", "32", "33", "35"), 2),
    ("계산: 33 - 7 = ?", ("24", "27", "26", "29"), 3),
    ("계산: 26 × 8 = ?", ("206", "209", "211", "208"), 4),
    ("계산: 234 ÷ 9 = ?", ("26", "24", "27", "29"), 1),
    ("계산: 26 + 10 = ?", ("34", "36", "37", "39"), 2),
    ("계산: 37 - 11 = ?", ("24", "27", "26", "29"), 3),
    ("계산: 26 × 12 = ?", ("310", "313", "315", "312"), 4),
    ("계산: 338 ÷ 13 = ?", ("26", "24", "27", "29"), 1),
    ("계산: 26 + 14 = ?", ("38", "40", "41", "43"), 2),
    ("계산: 41 - 15 = ?", ("24", "27", "26", "29"), 3),
    ("계산: 26 × 16 = ?", ("414", "417", "419", "416"), 4),
    ("계산: 442 ÷ 17 = ?", ("26", "24", "27", "29"), 1),
    ("계산: 26 + 18 = ?", ("42", "44", "45", "47"), 2),
    ("계산: 45 - 19 = ?", ("24", "27", "26", "29"), 3),
    ("계산: 26 × 20 = ?", ("518", "521", "523", "520"), 4),
    ("계산: 546 ÷ 21 = ?", ("26", "24", "27", "29"), 1),
    ("계산: 26 + 22 = ?", ("46", "48", "49", "51"), 2),
    ("계산: 49 - 23 = ?", ("24", "27", "26", "29"), 3),
    ("계산: 26 × 24 = ?", ("622", "625", "627", "624"), 4),
    ("계산: 650 ÷ 25 = ?", ("26", "24", "27", "29"), 1),
    ("계산: 29 - 2 = ?", ("25", "27", "28", "30"), 2),
    ("계산: 27 × 3 = ?", ("79", "82", "81", "84"), 3),
    ("계산: 108 ÷ 4 = ?", ("25", "28", "30", "27"), 4),
    ("계산: 27 + 5 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 33 - 6 = ?", ("25", "27", "28", "30"), 2),
    ("계산: 27 × 7 = ?", ("187", "190", "189", "192"), 3),
    ("계산: 216 ÷ 8 = ?", ("25", "28", "30", "27"), 4),
    ("계산: 27 + 9 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 37 - 10 = ?", ("25", "27", "28", "30"), 2),
    ("계산: 27 × 11 = ?", ("295", "298", "297", "300"), 3),
    ("계산: 324 ÷ 12 = ?", ("25", "28", "30", "27"), 4),
    ("계산: 27 + 13 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 41 - 14 = ?", ("25", "27", "28", "30"), 2),
    ("계산: 27 × 15 = ?", ("403", "406", "405", "408"), 3),
    ("계산: 432 ÷ 16 = ?", ("25", "28", "30", "27"), 4),
    ("계산: 27 + 17 = ?", ("44", "42", "45", "47"), 1),
    ("계산: 45 - 18 = ?", ("25", "27", "28", "30"), 2),
    ("계산: 27 × 19 = ?", ("511", "514", "513", "516"), 3),
    ("계산: 540 ÷ 20 = ?", ("25", "28", "30", "27"), 4),
    ("계산: 27 + 21 = ?", ("48", "46", "49", "51"), 1),
    ("계산: 49 - 22 = ?", ("25", "27", "28", "30"), 2),
    ("계산: 27 × 23 = ?", ("619", "622", "621", "624"), 3),
    ("계산: 648 ÷ 24 = ?", ("25", "28", "30", "27"), 4),
    ("계산: 27 + 25 = ?", ("52", "50", "53", "55"), 1),
    ("계산: 28 × 2 = ?", ("54", "56", "57", "59"), 2),
    ("계산: 84 ÷ 3 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 28 + 4 = ?", ("30", "33", "35", "32"), 4),
    ("계산: 33 - 5 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 28 × 6 = ?", ("166", "168", "169", "171"), 2),
    ("계산: 196 ÷ 7 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 28 + 8 = ?", ("34", "37", "39", "36"), 4),
    ("계산: 37 - 9 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 28 × 10 = ?", ("278", "280", "281", "283"), 2),
    ("계산: 308 ÷ 11 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 28 + 12 = ?", ("38", "41", "43", "40"), 4),
    ("계산: 41 - 13 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 28 × 14 = ?", ("390", "392", "393", "395"), 2),
    ("계산: 420 ÷ 15 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 28 + 16 = ?", ("42", "45", "47", "44"), 4),
    ("계산: 45 - 17 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 28 × 18 = ?", ("502", "504", "505", "507"), 2),
    ("계산: 532 ÷ 19 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 28 + 20 = ?", ("46", "49", "51", "48"), 4),
    ("계산: 49 - 21 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 28 × 22 = ?", ("614", "616", "617", "619"), 2),
    ("계산: 644 ÷ 23 = ?", ("26", "29", "28", "31"), 3),
    ("계산: 28 + 24 = ?", ("50", "53", "55", "52"), 4),
    ("계산: 53 - 25 = ?", ("28", "26", "29", "31"), 1),
    ("계산: 58 ÷ 2 = ?", ("27", "29", "30", "32"), 2),
    ("계산: 29 + 3 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 33 - 4 = ?", ("27", "30", "32", "29"), 4),
    ("계산: 29 × 5 = ?", ("145", "143", "146", "148"), 1),
    ("계산: 174 ÷ 6 = ?", ("27", "29", "30", "32"), 2),
    ("계산: 29 + 7 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 37 - 8 = ?", ("27", "30", "32", "29"), 4),
    ("계산: 29 × 9 = ?", ("261", "259", "262", "264"), 1),
    ("계산: 290 ÷ 10 = ?", ("27", "29", "30", "32"), 2),
    ("계산: 29 + 11 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 41 - 12 = ?", ("27", "30", "32", "29"), 4),
    ("계산: 29 × 13 = ?", ("377", "375", "378", "380"), 1),
    ("계산: 406 ÷ 14 = ?", ("27", "29", "30", "32"), 2),
    ("계산: 29 + 15 = ?", ("42", "45", "44", "47"), 3),
    ("계산: 45 - 16 = ?", ("27", "30", "32", "29"), 4),
    ("계산: 29 × 17 = ?", ("493", "491", "494", "496"), 1),
    ("계산: 522 ÷ 18 = ?", ("27", "29", "30", "32"), 2),
    ("계산: 29 + 19 = ?", ("46", "49", "48", "51"), 3),
    ("계산: 49 - 20 = ?", ("27", "30", "32", "29"), 4),
    ("계산: 29 × 21 = ?", ("609", "607", "610", "612"), 1),
    ("계산: 638 ÷ 22 = ?", ("27", "29", "30", "32"), 2),
    ("계산: 29 + 23 = ?", ("50", "53", "52", "55"), 3),
    ("계산: 53 - 24 = ?", ("27", "30", "32", "29"), 4),
    ("계산: 29 × 25 = ?", ("725", "723", "726", "728"), 1),
    ("계산: 30 + 2 = ?", ("30", "32", "33", "35"), 2),
    ("계산: 33 - 3 = ?", ("28", "31", "30", "33"), 3),
    ("계산: 30 × 4 = ?", ("118", "121", "123", "120"), 4),
    ("계산: 150 ÷ 5 = ?", ("30", "28", "31", "33"), 1),
    ("계산: 30 + 6 = ?", ("34", "36", "37", "39"), 2),
    ("계산: 37 - 7 = ?", ("28", "31", "30", "33"), 3),
    ("계산: 30 × 8 = ?", ("238", "241", "243", "240"), 4),
    ("계산: 270 ÷ 9 = ?", ("30", "28", "31", "33"), 1),
    ("계산: 30 + 10 = ?", ("38", "40", "41", "43"), 2),
    ("계산: 41 - 11 = ?", ("28", "31", "30", "33"), 3),
    ("계산: 30 × 12 = ?", ("358", "361", "363", "360"), 4),
    ("계산: 390 ÷ 13 = ?", ("30", "28", "31", "33"), 1),
    ("계산: 30 + 14 = ?", ("42", "44", "45", "47"), 2),
    ("계산: 45 - 15 = ?", ("28", "31", "30", "33"), 3),
    ("계산: 30 × 16 = ?", ("478", "481", "483", "480"), 4),
    ("계산: 510 ÷ 17 = ?", ("30", "28", "31", "33"), 1),
    ("계산: 30 + 18 = ?", ("46", "48", "49", "51"), 2),
    ("계산: 49 - 19 = ?", ("28", "31", "30", "33"), 3),
    ("계산: 30 × 20 = ?", ("598", "601", "603", "600"), 4),
    ("계산: 630 ÷ 21 = ?", ("30", "28", "31", "33"), 1),
    ("계산: 30 + 22 = ?", ("50", "52", "53", "55"), 2),
    ("계산: 53 - 23 = ?", ("28", "31", "30", "33"), 3),
    ("계산: 30 × 24 = ?", ("718", "721", "723", "720"), 4),
    ("계산: 750 ÷ 25 = ?", ("30", "28", "31", "33"), 1),
    ("계산: 33 - 2 = ?", ("29", "31", "32", "34"), 2),
    ("계산: 31 × 3 = ?", ("91", "94", "93", "96"), 3),
    ("계산: 124 ÷ 4 = ?", ("29", "32", "34", "31"), 4),
    ("계산: 31 + 5 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 37 - 6 = ?", ("29", "31", "32", "34"), 2),
    ("계산: 31 × 7 = ?", ("215", "218", "217", "220"), 3),
    ("계산: 248 ÷ 8 = ?", ("29", "32", "34", "31"), 4),
    ("계산: 31 + 9 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 41 - 10 = ?", ("29", "31", "32", "34"), 2),
    ("계산: 31 × 11 = ?", ("339", "342", "341", "344"), 3),
    ("계산: 372 ÷ 12 = ?", ("29", "32", "34", "31"), 4),
    ("계산: 31 + 13 = ?", ("44", "42", "45", "47"), 1),
    ("계산: 45 - 14 = ?", ("29", "31", "32", "34"), 2),
    ("계산: 31 × 15 = ?", ("463", "466", "465", "468"), 3),
    ("계산: 496 ÷ 16 = ?", ("29", "32", "34", "31"), 4),
    ("계산: 31 + 17 = ?", ("48", "46", "49", "51"), 1),
    ("계산: 49 - 18 = ?", ("29", "31", "32", "34"), 2),
    ("계산: 31 × 19 = ?", ("587", "590", "589", "592"), 3),
    ("계산: 620 ÷ 20 = ?", ("29", "32", "34", "31"), 4),
    ("계산: 31 + 21 = ?", ("52", "50", "53", "55"), 1),
    ("계산: 53 - 22 = ?", ("29", "31", "32", "34"), 2),
    ("계산: 31 × 23 = ?", ("711", "714", "713", "716"), 3),
    ("계산: 744 ÷ 24 = ?", ("29", "32", "34", "31"), 4),
    ("계산: 31 + 25 = ?", ("56", "54", "57", "59"), 1),
    ("계산: 32 × 2 = ?", ("62", "64", "65", "67"), 2),
    ("계산: 96 ÷ 3 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 32 + 4 = ?", ("34", "37", "39", "36"), 4),
    ("계산: 37 - 5 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 32 × 6 = ?", ("190", "192", "193", "195"), 2),
    ("계산: 224 ÷ 7 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 32 + 8 = ?", ("38", "41", "43", "40"), 4),
    ("계산: 41 - 9 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 32 × 10 = ?", ("318", "320", "321", "323"), 2),
    ("계산: 352 ÷ 11 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 32 + 12 = ?", ("42", "45", "47", "44"), 4),
    ("계산: 45 - 13 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 32 × 14 = ?", ("446", "448", "449", "451"), 2),
    ("계산: 480 ÷ 15 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 32 + 16 = ?", ("46", "49", "51", "48"), 4),
    ("계산: 49 - 17 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 32 × 18 = ?", ("574", "576", "577", "579"), 2),
    ("계산: 608 ÷ 19 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 32 + 20 = ?", ("50", "53", "55", "52"), 4),
    ("계산: 53 - 21 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 32 × 22 = ?", ("702", "704", "705", "707"), 2),
    ("계산: 736 ÷ 23 = ?", ("30", "33", "32", "35"), 3),
    ("계산: 32 + 24 = ?", ("54", "57", "59", "56"), 4),
    ("계산: 57 - 25 = ?", ("32", "30", "33", "35"), 1),
    ("계산: 66 ÷ 2 = ?", ("31", "33", "34", "36"), 2),
    ("계산: 33 + 3 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 37 - 4 = ?", ("31", "34", "36", "33"), 4),
    ("계산: 33 × 5 = ?", ("165", "163", "166", "168"), 1),
    ("계산: 198 ÷ 6 = ?", ("31", "33", "34", "36"), 2),
    ("계산: 33 + 7 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 41 - 8 = ?", ("31", "34", "36", "33"), 4),
    ("계산: 33 × 9 = ?", ("297", "295", "298", "300"), 1),
    ("계산: 330 ÷ 10 = ?", ("31", "33", "34", "36"), 2),
    ("계산: 33 + 11 = ?", ("42", "45", "44", "47"), 3),
    ("계산: 45 - 12 = ?", ("31", "34", "36", "33"), 4),
    ("계산: 33 × 13 = ?", ("429", "427", "430", "432"), 1),
    ("계산: 462 ÷ 14 = ?", ("31", "33", "34", "36"), 2),
    ("계산: 33 + 15 = ?", ("46", "49", "48", "51"), 3),
    ("계산: 49 - 16 = ?", ("31", "34", "36", "33"), 4),
    ("계산: 33 × 17 = ?", ("561", "559", "562", "564"), 1),
    ("계산: 594 ÷ 18 = ?", ("31", "33", "34", "36"), 2),
    ("계산: 33 + 19 = ?", ("50", "53", "52", "55"), 3),
    ("계산: 53 - 20 = ?", ("31", "34", "36", "33"), 4),
    ("계산: 33 × 21 = ?", ("693", "691", "694", "696"), 1),
    ("계산: 726 ÷ 22 = ?", ("31", "33", "34", "36"), 2),
    ("계산: 33 + 23 = ?", ("54", "57", "56", "59"), 3),
    ("계산: 57 - 24 = ?", ("31", "34", "36", "33"), 4),
    ("계산: 33 × 25 = ?", ("825", "823", "826", "828"), 1),
    ("계산: 34 + 2 = ?", ("34", "36", "37", "39"), 2),
    ("계산: 37 - 3 = ?", ("32", "35", "34", "37"), 3),
    ("계산: 34 × 4 = ?", ("134", "137", "139", "136"), 4),
    ("계산: 170 ÷ 5 = ?", ("34", "32", "35", "37"), 1),
    ("계산: 34 + 6 = ?", ("38", "40", "41", "43"), 2),
    ("계산: 41 - 7 = ?", ("32", "35", "34", "37"), 3),
    ("계산: 34 × 8 = ?", ("270", "273", "275", "272"), 4),
    ("계산: 306 ÷ 9 = ?", ("34", "32", "35", "37"), 1),
    ("계산: 34 + 10 = ?", ("42", "44", "45", "47"), 2),
    ("계산: 45 - 11 = ?", ("32", "35", "34", "37"), 3),
    ("계산: 34 × 12 = ?", ("406", "409", "411", "408"), 4),
    ("계산: 442 ÷ 13 = ?", ("34", "32", "35", "37"), 1),
    ("계산: 34 + 14 = ?", ("46", "48", "49", "51"), 2),
    ("계산: 49 - 15 = ?", ("32", "35", "34", "37"), 3),
    ("계산: 34 × 16 = ?", ("542", "545", "547", "544"), 4),
    ("계산: 578 ÷ 17 = ?", ("34", "32", "35", "37"), 1),
    ("계산: 34 + 18 = ?", ("50", "52", "53", "55"), 2),
    ("계산: 53 - 19 = ?", ("32", "35", "34", "37"), 3),
    ("계산: 34 × 20 = ?", ("678", "681", "683", "680"), 4),
    ("계산: 714 ÷ 21 = ?", ("34", "32", "35", "37"), 1),
    ("계산: 34 + 22 = ?", ("54", "56", "57", "59"), 2),
    ("계산: 57 - 23 = ?", ("32", "35", "34", "37"), 3),
    ("계산: 34 × 24 = ?", ("814", "817", "819", "816"), 4),
    ("계산: 850 ÷ 25 = ?", ("34", "32", "35", "37"), 1),
    ("계산: 37 - 2 = ?", ("33", "35", "36", "38"), 2),
    ("계산: 35 × 3 = ?", ("103", "106", "105", "108"), 3),
    ("계산: 140 ÷ 4 = ?", ("33", "36", "38", "35"), 4),
    ("계산: 35 + 5 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 41 - 6 = ?", ("33", "35", "36", "38"), 2),
    ("계산: 35 × 7 = ?", ("243", "246", "245", "248"), 3),
    ("계산: 280 ÷ 8 = ?", ("33", "36", "38", "35"), 4),
    ("계산: 35 + 9 = ?", ("44", "42", "45", "47"), 1),
    ("계산: 45 - 10 = ?", ("33", "35", "36", "38"), 2),
    ("계산: 35 × 11 = ?", ("383", "386", "385", "388"), 3),
    ("계산: 420 ÷ 12 = ?", ("33", "36", "38", "35"), 4),
    ("계산: 35 + 13 = ?", ("48", "46", "49", "51"), 1),
    ("계산: 49 - 14 = ?", ("33", "35", "36", "38"), 2),
    ("계산: 35 × 15 = ?", ("523", "526", "525", "528"), 3),
    ("계산: 560 ÷ 16 = ?", ("33", "36", "38", "35"), 4),
    ("계산: 35 + 17 = ?", ("52", "50", "53", "55"), 1),
    ("계산: 53 - 18 = ?", ("33", "35", "36", "38"), 2),
    ("계산: 35 × 19 = ?", ("663", "666", "665", "668"), 3),
    ("계산: 700 ÷ 20 = ?", ("33", "36", "38", "35"), 4),
    ("계산: 35 + 21 = ?", ("56", "54", "57", "59"), 1),
    ("계산: 57 - 22 = ?", ("33", "35", "36", "38"), 2),
    ("계산: 35 × 23 = ?", ("803", "806", "805", "808"), 3),
    ("계산: 840 ÷ 24 = ?", ("33", "36", "38", "35"), 4),
    ("계산: 35 + 25 = ?", ("60", "58", "61", "63"), 1),
    ("계산: 36 × 2 = ?", ("70", "72", "73", "75"), 2),
    ("계산: 108 ÷ 3 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 36 + 4 = ?", ("38", "41", "43", "40"), 4),
    ("계산: 41 - 5 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 36 × 6 = ?", ("214", "216", "217", "219"), 2),
    ("계산: 252 ÷ 7 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 36 + 8 = ?", ("42", "45", "47", "44"), 4),
    ("계산: 45 - 9 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 36 × 10 = ?", ("358", "360", "361", "363"), 2),
    ("계산: 396 ÷ 11 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 36 + 12 = ?", ("46", "49", "51", "48"), 4),
    ("계산: 49 - 13 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 36 × 14 = ?", ("502", "504", "505", "507"), 2),
    ("계산: 540 ÷ 15 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 36 + 16 = ?", ("50", "53", "55", "52"), 4),
    ("계산: 53 - 17 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 36 × 18 = ?", ("646", "648", "649", "651"), 2),
    ("계산: 684 ÷ 19 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 36 + 20 = ?", ("54", "57", "59", "56"), 4),
    ("계산: 57 - 21 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 36 × 22 = ?", ("790", "792", "793", "795"), 2),
    ("계산: 828 ÷ 23 = ?", ("34", "37", "36", "39"), 3),
    ("계산: 36 + 24 = ?", ("58", "61", "63", "60"), 4),
    ("계산: 61 - 25 = ?", ("36", "34", "37", "39"), 1),
    ("계산: 74 ÷ 2 = ?", ("35", "37", "38", "40"), 2),
    ("계산: 37 + 3 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 41 - 4 = ?", ("35", "38", "40", "37"), 4),
    ("계산: 37 × 5 = ?", ("185", "183", "186", "188"), 1),
    ("계산: 222 ÷ 6 = ?", ("35", "37", "38", "40"), 2),
    ("계산: 37 + 7 = ?", ("42", "45", "44", "47"), 3),
    ("계산: 45 - 8 = ?", ("35", "38", "40", "37"), 4),
    ("계산: 37 × 9 = ?", ("333", "331", "334", "336"), 1),
    ("계산: 370 ÷ 10 = ?", ("35", "37", "38", "40"), 2),
    ("계산: 37 + 11 = ?", ("46", "49", "48", "51"), 3),
    ("계산: 49 - 12 = ?", ("35", "38", "40", "37"), 4),
    ("계산: 37 × 13 = ?", ("481", "479", "482", "484"), 1),
    ("계산: 518 ÷ 14 = ?", ("35", "37", "38", "40"), 2),
    ("계산: 37 + 15 = ?", ("50", "53", "52", "55"), 3),
    ("계산: 53 - 16 = ?", ("35", "38", "40", "37"), 4),
    ("계산: 37 × 17 = ?", ("629", "627", "630", "632"), 1),
    ("계산: 666 ÷ 18 = ?", ("35", "37", "38", "40"), 2),
    ("계산: 37 + 19 = ?", ("54", "57", "56", "59"), 3),
    ("계산: 57 - 20 = ?", ("35", "38", "40", "37"), 4),
    ("계산: 37 × 21 = ?", ("777", "775", "778", "780"), 1),
    ("계산: 814 ÷ 22 = ?", ("35", "37", "38", "40"), 2),
    ("계산: 37 + 23 = ?", ("58", "61", "60", "63"), 3),
    ("계산: 61 - 24 = ?", ("35", "38", "40", "37"), 4),
    ("계산: 37 × 25 = ?", ("925", "923", "926", "928"), 1),
    ("계산: 38 + 2 = ?", ("38", "40", "41", "43"), 2),
    ("계산: 41 - 3 = ?", ("36", "39", "38", "41"), 3),
    ("계산: 38 × 4 = ?", ("150", "153", "155", "152"), 4),
    ("계산: 190 ÷ 5 = ?", ("38", "36", "39", "41"), 1),
    ("계산: 38 + 6 = ?", ("42", "44", "45", "47"), 2),
    ("계산: 45 - 7 = ?", ("36", "39", "38", "41"), 3),
    ("계산: 38 × 8 = ?", ("302", "305", "307", "304"), 4),
    ("계산: 342 ÷ 9 = ?", ("38", "36", "39", "41"), 1),
    ("계산: 38 + 10 = ?", ("46", "48", "49", "51"), 2),
    ("계산: 49 - 11 = ?", ("36", "39", "38", "41"), 3),
    ("계산: 38 × 12 = ?", ("454", "457", "459", "456"), 4),
    ("계산: 494 ÷ 13 = ?", ("38", "36", "39", "41"), 1),
    ("계산: 38 + 14 = ?", ("50", "52", "53", "55"), 2),
    ("계산: 53 - 15 = ?", ("36", "39", "38", "41"), 3),
    ("계산: 38 × 16 = ?", ("606", "609", "611", "608"), 4),
    ("계산: 646 ÷ 17 = ?", ("38", "36", "39", "41"), 1),
    ("계산: 38 + 18 = ?", ("54", "56", "57", "59"), 2),
    ("계산: 57 - 19 = ?", ("36", "39", "38", "41"), 3),
    ("계산: 38 × 20 = ?", ("758", "761", "763", "760"), 4),
    ("계산: 798 ÷ 21 = ?", ("38", "36", "39", "41"), 1),
    ("계산: 38 + 22 = ?", ("58", "60", "61", "63"), 2),
    ("계산: 61 - 23 = ?", ("36", "39", "38", "41"), 3),
    ("계산: 38 × 24 = ?", ("910", "913", "915", "912"), 4),
    ("계산: 950 ÷ 25 = ?", ("38", "36", "39", "41"), 1),
    ("계산: 41 - 2 = ?", ("37", "39", "40", "42"), 2),
    ("계산: 39 × 3 = ?", ("115", "118", "117", "120"), 3),
    ("계산: 156 ÷ 4 = ?", ("37", "40", "42", "39"), 4),
    ("계산: 39 + 5 = ?", ("44", "42", "45", "47"), 1),
    ("계산: 45 - 6 = ?", ("37", "39", "40", "42"), 2),
    ("계산: 39 × 7 = ?", ("271", "274", "273", "276"), 3),
    ("계산: 312 ÷ 8 = ?", ("37", "40", "42", "39"), 4),
    ("계산: 39 + 9 = ?", ("48", "46", "49", "51"), 1),
    ("계산: 49 - 10 = ?", ("37", "39", "40", "42"), 2),
    ("계산: 39 × 11 = ?", ("427", "430", "429", "432"), 3),
    ("계산: 468 ÷ 12 = ?", ("37", "40", "42", "39"), 4),
    ("계산: 39 + 13 = ?", ("52", "50", "53", "55"), 1),
    ("계산: 53 - 14 = ?", ("37", "39", "40", "42"), 2),
    ("계산: 39 × 15 = ?", ("583", "586", "585", "588"), 3),
    ("계산: 624 ÷ 16 = ?", ("37", "40", "42", "39"), 4),
    ("계산: 39 + 17 = ?", ("56", "54", "57", "59"), 1),
    ("계산: 57 - 18 = ?", ("37", "39", "40", "42"), 2),
    ("계산: 39 × 19 = ?", ("739", "742", "741", "744"), 3),
    ("계산: 780 ÷ 20 = ?", ("37", "40", "42", "39"), 4),
    ("계산: 39 + 21 = ?", ("60", "58", "61", "63"), 1),
    ("계산: 61 - 22 = ?", ("37", "39", "40", "42"), 2),
    ("계산: 39 × 23 = ?", ("895", "898", "897", "900"), 3),
    ("계산: 936 ÷ 24 = ?", ("37", "40", "42", "39"), 4),
    ("계산: 39 + 25 = ?", ("64", "62", "65", "67"), 1),
    ("계산: 40 × 2 = ?", ("78", "80", "81", "83"), 2),
    ("계산: 120 ÷ 3 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 40 + 4 = ?", ("42", "45", "47", "44"), 4),
    ("계산: 45 - 5 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 40 × 6 = ?", ("238", "240", "241", "243"), 2),
    ("계산: 280 ÷ 7 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 40 + 8 = ?", ("46", "49", "51", "48"), 4),
    ("계산: 49 - 9 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 40 × 10 = ?", ("398", "400", "401", "403"), 2),
    ("계산: 440 ÷ 11 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 40 + 12 = ?", ("50", "53", "55", "52"), 4),
    ("계산: 53 - 13 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 40 × 14 = ?", ("558", "560", "561", "563"), 2),
    ("계산: 600 ÷ 15 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 40 + 16 = ?", ("54", "57", "59", "56"), 4),
    ("계산: 57 - 17 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 40 × 18 = ?", ("718", "720", "721", "723"), 2),
    ("계산: 760 ÷ 19 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 40 + 20 = ?", ("58", "61", "63", "60"), 4),
    ("계산: 61 - 21 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 40 × 22 = ?", ("878", "880", "881", "883"), 2),
    ("계산: 920 ÷ 23 = ?", ("38", "41", "40", "43"), 3),
    ("계산: 40 + 24 = ?", ("62", "65", "67", "64"), 4),
    ("계산: 65 - 25 = ?", ("40", "38", "41", "43"), 1),
    ("계산: 82 ÷ 2 = ?", ("39", "41", "42", "44"), 2),
    ("계산: 41 + 3 = ?", ("42", "45", "44", "47"), 3),
    ("계산: 45 - 4 = ?", ("39", "42", "44", "41"), 4),
    ("계산: 41 × 5 = ?", ("205", "203", "206", "208"), 1),
    ("계산: 246 ÷ 6 = ?", ("39", "41", "42", "44"), 2),
    ("계산: 41 + 7 = ?", ("46", "49", "48", "51"), 3),
    ("계산: 49 - 8 = ?", ("39", "42", "44", "41"), 4),
    ("계산: 41 × 9 = ?", ("369", "367", "370", "372"), 1),
    ("계산: 410 ÷ 10 = ?", ("39", "41", "42", "44"), 2),
    ("계산: 41 + 11 = ?", ("50", "53", "52", "55"), 3),
    ("계산: 53 - 12 = ?", ("39", "42", "44", "41"), 4),
    ("계산: 41 × 13 = ?", ("533", "531", "534", "536"), 1),
    ("계산: 574 ÷ 14 = ?", ("39", "41", "42", "44"), 2),
    ("계산: 41 + 15 = ?", ("54", "57", "56", "59"), 3),
    ("계산: 57 - 16 = ?", ("39", "42", "44", "41"), 4),
    ("계산: 41 × 17 = ?", ("697", "695", "698", "700"), 1),
    ("계산: 738 ÷ 18 = ?", ("39", "41", "42", "44"), 2),
    ("계산: 41 + 19 = ?", ("58", "61", "60", "63"), 3),
    ("계산: 61 - 20 = ?", ("39", "42", "44", "41"), 4),
    ("계산: 41 × 21 = ?", ("861", "859", "862", "864"), 1),
    ("계산: 902 ÷ 22 = ?", ("39", "41", "42", "44"), 2),
    ("계산: 41 + 23 = ?", ("62", "65", "64", "67"), 3),
    ("계산: 65 - 24 = ?", ("39", "42", "44", "41"), 4),
    ("계산: 41 × 25 = ?", ("1025", "1023", "1026", "1028"), 1),
    ("계산: 42 + 2 = ?", ("42", "44", "45", "47"), 2),
    ("계산: 45 - 3 = ?", ("40", "43", "42", "45"), 3),
    ("계산: 42 × 4 = ?", ("166", "169", "171", "168"), 4),
    ("계산: 210 ÷ 5 = ?", ("42", "40", "43", "45"), 1),
    ("계산: 42 + 6 = ?", ("46", "48", "49", "51"), 2),
    ("계산: 49 - 7 = ?", ("40", "43", "42", "45"), 3),
    ("계산: 42 × 8 = ?", ("334", "337", "339", "336"), 4),
    ("계산: 378 ÷ 9 = ?", ("42", "40", "43", "45"), 1),
    ("계산: 42 + 10 = ?", ("50", "52", "53", "55"), 2),
    ("계산: 53 - 11 = ?", ("40", "43", "42", "45"), 3),
    ("계산: 42 × 12 = ?", ("502", "505", "507", "504"), 4),
    ("계산: 546 ÷ 13 = ?", ("42", "40", "43", "45"), 1),
    ("계산: 42 + 14 = ?", ("54", "56", "57", "59"), 2),
    ("계산: 57 - 15 = ?", ("40", "43", "42", "45"), 3),
    ("계산: 42 × 16 = ?", ("670", "673", "675", "672"), 4),
    ("계산: 714 ÷ 17 = ?", ("42", "40", "43", "45"), 1),
    ("계산: 42 + 18 = ?", ("58", "60", "61", "63"), 2),
    ("계산: 61 - 19 = ?", ("40", "43", "42", "45"), 3),
    ("계산: 42 × 20 = ?", ("838", "841", "843", "840"), 4),
    ("계산: 882 ÷ 21 = ?", ("42", "40", "43", "45"), 1),
    ("계산: 42 + 22 = ?", ("62", "64", "65", "67"), 2),
    ("계산: 65 - 23 = ?", ("40", "43", "42", "45"), 3),
    ("계산: 42 × 24 = ?", ("1006", "1009", "1011", "1008"), 4),
    ("계산: 1050 ÷ 25 = ?", ("42", "40", "43", "45"), 1),
    ("계산: 45 - 2 = ?", ("41", "43", "44", "46"), 2),
    ("계산: 43 × 3 = ?", ("127", "130", "129", "132"), 3),
    ("계산: 172 ÷ 4 = ?", ("41", "44", "46", "43"), 4),
    ("계산: 43 + 5 = ?", ("48", "46", "49", "51"), 1),
    ("계산: 49 - 6 = ?", ("41", "43", "44", "46"), 2),
    ("계산: 43 × 7 = ?", ("299", "302", "301", "304"), 3),
    ("계산: 344 ÷ 8 = ?", ("41", "44", "46", "43"), 4),
    ("계산: 43 + 9 = ?", ("52", "50", "53", "55"), 1),
    ("계산: 53 - 10 = ?", ("41", "43", "44", "46"), 2),
    ("계산: 43 × 11 = ?", ("471", "474", "473", "476"), 3),
    ("계산: 516 ÷ 12 = ?", ("41", "44", "46", "43"), 4),
    ("계산: 43 + 13 = ?", ("56", "54", "57", "59"), 1),
    ("계산: 57 - 14 = ?", ("41", "43", "44", "46"), 2),
    ("계산: 43 × 15 = ?", ("643", "646", "645", "648"), 3),
    ("계산: 688 ÷ 16 = ?", ("41", "44", "46", "43"), 4),
    ("계산: 43 + 17 = ?", ("60", "58", "61", "63"), 1),
    ("계산: 61 - 18 = ?", ("41", "43", "44", "46"), 2),
    ("계산: 43 × 19 = ?", ("815", "818", "817", "820"), 3),
    ("계산: 860 ÷ 20 = ?", ("41", "44", "46", "43"), 4),
    ("계산: 43 + 21 = ?", ("64", "62", "65", "67"), 1),
    ("계산: 65 - 22 = ?", ("41", "43", "44", "46"), 2),
    ("계산: 43 × 23 = ?", ("987", "990", "989", "992"), 3),
    ("계산: 1032 ÷ 24 = ?", ("41", "44", "46", "43"), 4),
    ("계산: 43 + 25 = ?", ("68", "66", "69", "71"), 1),
)

def game_stats(group, user):
    with db() as conn:
        return conn.execute(
            "SELECT points,attendance_days,last_attendance FROM game_points WHERE group_id=%s AND user_id=%s",
            (group,user)).fetchone()

def daily_fortune(group, user, day):
    import hashlib
    digest = hashlib.sha256(f"{group}:{user}:{day}:redbot-fortune".encode()).digest()
    fortunes = (
        "작은 도전이 즐거운 결과로 이어질 수 있는 날이야! 🌱",
        "주변 사람과의 대화에서 좋은 아이디어를 얻을 수 있어. 💬",
        "서두르지 말고 차근차근 하면 만족스러운 하루가 될 거야. ✨",
        "평소 미뤄둔 일을 하나 끝내 보면 뿌듯할 거야. 🎯",
        "맛있는 음식과 잠깐의 휴식이 기분을 바꿔 줄 거야. 🍀",
        "오늘은 새로운 취미나 재미있는 일을 시도해 봐! 🎨",
        "가벼운 칭찬 한마디가 좋은 분위기를 만들 수 있어. 💖",
        "집중력이 필요한 일부터 시작해 보면 좋겠어. 🚀",
    )
    return fortunes[digest[0] % len(fortunes)], digest[1] % 45 + 1

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
        conn.execute("""INSERT INTO message_events(event_id,group_id,user_id,display_name,message_date,message_hour)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (eid,group,user,name,day,datetime.now(KST).hour))
    # Count ordinary conversation only, once per 60 seconds, at most 30/day.
    if text and not text.startswith(("*", "/", "!")) and len(re.sub(r"\s+", "", text)) >= 4:
        with db() as conn:
            conn.execute("""INSERT INTO daily_missions(group_id,user_id,mission_date,progress,last_counted_at)
                VALUES(%s,%s,%s,1,NOW())
                ON CONFLICT(group_id,user_id,mission_date) DO UPDATE
                SET progress=daily_missions.progress+1,last_counted_at=NOW()
                WHERE daily_missions.progress < 30
                  AND (daily_missions.last_counted_at IS NULL
                       OR daily_missions.last_counted_at <= NOW()-INTERVAL '60 seconds')""",
                (group,user,day))
    with db() as conn:
        is_admin = user in ADMINS or conn.execute(
            "SELECT 1 FROM group_admins WHERE group_id=%s AND user_id=%s", (group,user)
        ).fetchone() is not None
    admin_command = re.fullmatch(r"/관리자(추가|삭제)(?:\s+.*)?", text)
    if admin_command:
        if not is_admin:
            reply(token, "⛔ 관리자 추가·삭제는 기존 관리자만 가능해.")
            return
        action = admin_command.group(1)
        targets = list(dict.fromkeys(
            m["userId"] for m in (message.get("mention") or {}).get("mentionees", [])
            if m.get("userId")
        ))
        if not targets:
            reply(token, f"사용법: /관리자{action} @멤버 (LINE 실제 멘션)")
            return
        changed = 0
        protected = 0
        with db() as conn:
            for target in targets:
                if target in ADMINS:
                    protected += 1
                    continue
                if action == "추가":
                    result = conn.execute(
                        "INSERT INTO group_admins(group_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING user_id",
                        (group,target)).fetchone()
                else:
                    result = conn.execute(
                        "DELETE FROM group_admins WHERE group_id=%s AND user_id=%s RETURNING user_id",
                        (group,target)).fetchone()
                changed += bool(result)
        reply(token, f"🛡️ 관리자 {action} 완료: {changed}명" +
              (f"\n최초 설정 관리자는 변경 불가: {protected}명" if protected else "") +
              "\n이 단체방에만 적용돼.")
        return
    if text == "/포인트추가" or text.startswith("/포인트추가 "):
        if not is_admin:
            reply(token, "⛔ 포인트 지급은 관리자만 가능해.")
            return
        amount_match = re.fullmatch(r"/포인트추가\s+([0-9]{1,7})(?:\s+.*)?", text)
        if not amount_match:
            reply(token, "사용법: /포인트추가 500 (본인) 또는 /포인트추가 500 @멤버\n※ 1~1,000,000P까지 지급 가능")
            return
        amount = int(amount_match.group(1))
        if not 1 <= amount <= 1000000:
            reply(token, "⛔ 지급 금액은 1~1,000,000P 사이로 입력해 줘.")
            return
        targets = list(dict.fromkeys(
            m["userId"] for m in (message.get("mention") or {}).get("mentionees", [])
            if m.get("userId")
        ))
        if not targets:
            if text != f"/포인트추가 {amount_match.group(1)}":
                reply(token, "멘션 대상이 확인되지 않아. LINE에서 실제 멤버를 멘션하거나, 본인 지급은 /포인트추가 500처럼 입력해 줘.")
                return
            targets = [user]
        with db() as conn:
            for target in targets:
                conn.execute("""INSERT INTO game_points(group_id,user_id,points)
                    VALUES(%s,%s,%s)
                    ON CONFLICT(group_id,user_id)
                    DO UPDATE SET points=game_points.points+EXCLUDED.points""", (group,target,amount))
        reply(token, f"🎁 관리자 포인트 지급 완료!\n대상: {len(targets)}명\n1명당 +{amount:,}P")
        return
    if text in ("/정각알림켜기", "/정각알림끄기", "/정각알림상태"):
        if text != "/정각알림상태" and not is_admin:
            reply(token, "⛔ 정각 알림 켜기·끄기는 관리자만 가능해.")
            return
        with db() as conn:
            if text != "/정각알림상태":
                enabled = text == "/정각알림켜기"
                conn.execute("""INSERT INTO hourly_alert_settings(group_id,enabled)
                    VALUES(%s,%s) ON CONFLICT(group_id)
                    DO UPDATE SET enabled=EXCLUDED.enabled""", (group,enabled))
            row = conn.execute("SELECT enabled FROM hourly_alert_settings WHERE group_id=%s",(group,)).fetchone()
        status = "켜짐 🔔" if row and row[0] else "꺼짐 🔕"
        reply(token, f"⏰ 정각 알림: {status}\n한국 시간 기준 오전 8시~자정(00시) 매 정각\n관리자 명령어: /정각알림켜기 · /정각알림끄기")
        return
    if text in ("*도움말", "*명령어"):
        reply(token, """🤖 RED BOT 3.0 전체 명령어

📊 마딧수·통계
*마딧수
*YYMMDD마딧수 (예: *261113마딧수)
*방통계

👥 멤버 호출
*남초 / *여초
/남초추가 @멘션 /남초삭제 @멘션 /남초목록
/여초추가 @멘션 /여초삭제 @멘션 /여초목록
※ 남초·여초 명단 수정/조회는 관리자만
/관리자추가 @멘션 /관리자삭제 @멘션 (관리자만)

🗺️ 지역 명단 (모든 멤버 수정 가능)
/지역추가 서울 홍길동
/지역삭제 서울 홍길동
/서울 /부산 등 지역명 /지역목록

🎲 게임·추첨
*주사위 / *가위바위보
*랜덤추첨 / *랜덤추첨 3
*퀴즈 (관리자 출제·하루 20회) / *정답 1

🏆 출석·포인트
*출석 / *내포인트 / *출석랭킹\n*오늘미션 / *미션보상 / *보물상자

🛍️ 포인트 상점
*상점 / *구매 왕관 / *인벤토리

🍜 메뉴 추천
*점메추 / *저메추 / *메뉴추천

🔮 운세
*오늘운세 / *행운숫자

🤖 AI·날씨
!질문내용 (예: !내일 서울 날씨)

⏰ 정각 알림
/정각알림상태
/정각알림켜기 /정각알림끄기 (관리자)
※ 오전 8시~자정, 예약 실행 연결 전 자동 발송 안 됨

📖 *도움말 / *명령어""")
        return
    shop = {"왕관": (100, "👑"), "별": (70, "⭐"), "하트": (50, "❤️"), "트로피": (150, "🏆"), "다이아": (500, "💎")}
    if text == "*상점":
        reply(token, "🛍️ 레드봇 포인트 상점\n"+
              "\n".join(f"{emoji} {item}: {price:,}P" for item,(price,emoji) in shop.items())+
              "\n\n구매: *구매 왕관\n보유 아이템: *인벤토리\n포인트 확인: *내포인트\n※ 가상 아이템이며 현금 가치가 없어.")
        return
    if text == "*인벤토리":
        with db() as conn:
            items = conn.execute("""SELECT item_name,quantity FROM point_inventory
                WHERE group_id=%s AND user_id=%s AND quantity>0 ORDER BY item_name""",
                (group,user)).fetchall()
        reply(token, f"🎒 {name}님의 인벤토리\n"+
              ("\n".join(f"{shop[item][1] if item in shop else '🎁'} {item} × {quantity}"
                         for item,quantity in items) if items else "아직 구매한 아이템이 없어.\n*상점에서 구경해 봐!"))
        return
    if text.startswith("*구매"):
        purchase = re.fullmatch(r"\*구매\s+([가-힣]{1,10})",text)
        if not purchase or purchase.group(1) not in shop:
            reply(token,"사용법: *구매 왕관\n판매 목록은 *상점에서 확인해 줘.")
            return
        item = purchase.group(1)
        price, emoji = shop[item]
        with db() as conn:
            row = conn.execute("""UPDATE game_points SET points=points-%s
                WHERE group_id=%s AND user_id=%s AND points >= %s
                RETURNING points""",(price,group,user,price)).fetchone()
            if row:
                conn.execute("""INSERT INTO point_inventory(group_id,user_id,item_name,quantity)
                    VALUES(%s,%s,%s,1) ON CONFLICT(group_id,user_id,item_name)
                    DO UPDATE SET quantity=point_inventory.quantity+1""",(group,user,item))
                result = f"🎉 {name}님 구매 성공!\n{emoji} {item} × 1\n💰 남은 포인트: {row[0]:,}P"
            else:
                current = conn.execute("SELECT points FROM game_points WHERE group_id=%s AND user_id=%s",
                                       (group,user)).fetchone()
                points = current[0] if current else 0
                result = f"💸 포인트가 부족해!\n{emoji} {item}: {price:,}P\n현재 보유: {points:,}P"
        reply(token,result)
        return
    if text == "*오늘미션":
        with db() as conn:
            row = conn.execute("""SELECT progress,claimed FROM daily_missions
                WHERE group_id=%s AND user_id=%s AND mission_date=%s""",
                (group,user,day)).fetchone()
        progress, claimed = row if row else (0,False)
        reply(token, f"🎯 {name}님의 오늘 미션\n일반 대화 30회 참여하기 ({progress}/30)\n"
              "※ 공백 제외 4글자 이상 일반 대화만 인정, 60초 간격으로 집계\n"
              + ("🎉 보상 수령 완료!" if claimed else
                 "🎁 완료! *미션보상 입력해서 20P 받기" if progress >= 30 else
                 "🏅 달성 보상: 20P\n완료 후 *미션보상 입력"))
        return
    if text == "*미션보상":
        with db() as conn:
            claimed_row = conn.execute("""UPDATE daily_missions SET claimed=TRUE
                WHERE group_id=%s AND user_id=%s AND mission_date=%s
                AND progress >= 30 AND claimed=FALSE RETURNING progress""",
                (group,user,day)).fetchone()
            if claimed_row:
                conn.execute("""INSERT INTO game_points(group_id,user_id,points)
                    VALUES(%s,%s,20) ON CONFLICT(group_id,user_id)
                    DO UPDATE SET points=game_points.points+20""",(group,user))
            else:
                status = conn.execute("""SELECT progress,claimed FROM daily_missions
                    WHERE group_id=%s AND user_id=%s AND mission_date=%s""",
                    (group,user,day)).fetchone()
        reply(token, "🎉 오늘 미션 완료! +20P 지급했어!" if claimed_row else
              "✅ 오늘 미션 보상은 이미 받았어." if status and status[1] else
              f"🎯 아직 미션 진행 중이야! ({status[0] if status else 0}/30)")
        return
    if text == "*보물상자":
        with db() as conn:
            existing = conn.execute("""SELECT reward FROM daily_treasure
                WHERE group_id=%s AND user_id=%s AND treasure_date=%s""",
                (group,user,day)).fetchone()
            if existing:
                reward, fresh = existing[0], False
            else:
                # 1% jackpot; remaining prizes 0, 5, 10, 20, or 50 points.
                roll = secrets.randbelow(100)
                reward = 100 if roll == 0 else (50 if roll < 6 else
                         20 if roll < 21 else 10 if roll < 51 else
                         5 if roll < 81 else 0)
                inserted = conn.execute("""INSERT INTO daily_treasure(group_id,user_id,treasure_date,reward)
                    VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING reward""",
                    (group,user,day,reward)).fetchone()
                fresh = bool(inserted)
                if fresh:
                    conn.execute("""INSERT INTO game_points(group_id,user_id,points)
                        VALUES(%s,%s,%s) ON CONFLICT(group_id,user_id)
                        DO UPDATE SET points=game_points.points+EXCLUDED.points""",
                        (group,user,reward))
                else:
                    reward = conn.execute("""SELECT reward FROM daily_treasure
                        WHERE group_id=%s AND user_id=%s AND treasure_date=%s""",
                        (group,user,day)).fetchone()[0]
        reply(token, (f"🎁 {name}님의 보물상자!\n"
              + (f"✨ {reward}P 획득!" if fresh else f"오늘은 이미 열었어! ({reward}P 획득 기록)")
              + "\n⏰ 내일 0시(한국 시간)에 다시 열 수 있어."))
        return
    if text == "*출석":
        from datetime import timedelta
        yesterday = (datetime.now(KST).date() - timedelta(days=1)).isoformat()
        with db() as conn:
            conn.execute("""INSERT INTO game_points(group_id,user_id) VALUES(%s,%s)
                ON CONFLICT DO NOTHING""", (group,user))
            row = conn.execute(
                "SELECT points,attendance_days,last_attendance FROM game_points WHERE group_id=%s AND user_id=%s FOR UPDATE",
                (group,user)).fetchone()
            if row[2] == day:
                result = f"✅ {name}님은 오늘 이미 출석했어!\n💰 현재 포인트: {row[0]:,}P"
            else:
                streak = 5 if row[2] == yesterday else 0
                bonus = 10 + streak
                conn.execute("""UPDATE game_points SET points=points+%s,
                    attendance_days=attendance_days+1,last_attendance=%s
                    WHERE group_id=%s AND user_id=%s""",
                    (bonus,day,group,user))
                result = (f"✅ {name}님 출석 완료!\n🎁 +{bonus}P"
                          + (" (어제도 출석 보너스 +5P)" if streak else "")
                          + f"\n📅 누적 출석: {row[1]+1}일\n💰 총 포인트: {row[0]+bonus:,}P")
        reply(token,result)
        return
    if text == "*내포인트":
        row = game_stats(group,user)
        reply(token, f"💰 {name}님의 포인트\n🏅 {row[0] if row else 0:,}P\n📅 누적 출석: {row[1] if row else 0}일")
        return
    if text == "*출석랭킹":
        with db() as conn:
            rows = conn.execute("""SELECT p.user_id,p.points,p.attendance_days,
                COALESCE(m.display_name,LEFT(p.user_id,12)) FROM game_points p
                LEFT JOIN member_profiles m ON m.group_id=p.group_id AND m.user_id=p.user_id
                WHERE p.group_id=%s ORDER BY p.points DESC,p.attendance_days DESC,p.user_id LIMIT 10""",
                (group,)).fetchall()
        result = "🏆 우리 방 포인트 TOP 10\n" + (
            "\n".join(f"{i}. {display}: {points:,}P (출석 {days}일)"
                      for i, (_,points,days,display) in enumerate(rows,1))
            if rows else "아직 출석한 사람이 없어.")
        reply(token,result)
        return
    if text in ("*오늘운세","*행운숫자"):
        fortune, lucky = daily_fortune(group,user,day)
        if text == "*오늘운세":
            reply(token, f"🔮 {name}님의 오늘의 운세 ({day})\n{fortune}\n🍀 행운의 숫자: {lucky}\n※ 재미로 보는 랜덤 운세야.")
        else:
            reply(token, f"🍀 {name}님의 오늘 행운의 숫자: {lucky}\n※ 재미로 보는 숫자야.")
        return
    if text == "*퀴즈":
        if not is_admin:
            reply(token, "⛔ 퀴즈 출제는 관리자만 가능해!\n정답 제출(*정답 1)은 모든 멤버가 할 수 있어.")
            return
        with db() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("quiz:" + group,))
            recent = {row[0] for row in conn.execute("SELECT question FROM quiz_history WHERE group_id=%s ORDER BY issued_at DESC LIMIT 200", (group,)).fetchall()}
            available = [item for item in QUIZ_BANK if item[0] not in recent]
            question, choices, correct = secrets.choice(available if available else QUIZ_BANK)
            quota = conn.execute("""INSERT INTO quiz_daily_limits(group_id,quiz_date,issued_count)
                VALUES(%s,%s,1)
                ON CONFLICT(group_id,quiz_date) DO UPDATE
                SET issued_count=quiz_daily_limits.issued_count+1
                WHERE quiz_daily_limits.issued_count < 20
                RETURNING issued_count""", (group,day)).fetchone()
            if quota:
                conn.execute("INSERT INTO quiz_history(group_id,question,issued_at) VALUES(%s,%s,NOW()) ON CONFLICT(group_id,question) DO UPDATE SET issued_at=NOW()", (group,question))
                conn.execute("DELETE FROM quiz_history WHERE group_id=%s AND question NOT IN (SELECT question FROM quiz_history WHERE group_id=%s ORDER BY issued_at DESC LIMIT 200)", (group,group))
                conn.execute("""INSERT INTO quiz_rounds(group_id,question,answer,choices,created_at)
                    VALUES(%s,%s,%s,%s,NOW())
                    ON CONFLICT(group_id) DO UPDATE SET question=EXCLUDED.question,
                    answer=EXCLUDED.answer,choices=EXCLUDED.choices,created_at=NOW()""",
                    (group,question,str(correct),"|".join(choices)))
        if not quota:
            reply(token, "🛑 오늘 퀴즈 20회 출제를 모두 사용했어!\n내일 오전 0시(한국 시간)에 다시 20회 출제할 수 있어.")
            return
        reply(token, "🧠 레드봇 퀴즈! ("+str(quota[0])+"/20회)\n"+question+"\n"+
              "\n".join(f"{i}. {choice}" for i,choice in enumerate(choices,1))+
              "\n\n정답은 *정답 1 처럼 입력해 줘!\n⏳ 제한시간 10분 · 정답 +10P\n※ 관리자만 출제 가능 · 하루 20회 · 새 퀴즈는 이전 문제를 교체해.")
        return
    answer_match = re.fullmatch(r"\*정답\s+([1-4])",text)
    if answer_match:
        with db() as conn:
            row = conn.execute("""SELECT answer,created_at FROM quiz_rounds
                WHERE group_id=%s FOR UPDATE""",(group,)).fetchone()
            if not row:
                result = "진행 중인 퀴즈가 없어. *퀴즈로 시작해 줘!"
            else:
                from datetime import timedelta
                if datetime.now(KST) - row[1].astimezone(KST) > timedelta(minutes=10):
                    conn.execute("DELETE FROM quiz_rounds WHERE group_id=%s",(group,))
                    result = "⏳ 퀴즈 시간이 끝났어! *퀴즈로 새 문제를 시작해 줘."
                elif answer_match.group(1) != row[0]:
                    result = "❌ 아쉽지만 오답이야! 다시 도전해 봐."
                else:
                    conn.execute("DELETE FROM quiz_rounds WHERE group_id=%s",(group,))
                    conn.execute("""INSERT INTO game_points(group_id,user_id,points)
                        VALUES(%s,%s,10) ON CONFLICT(group_id,user_id)
                        DO UPDATE SET points=game_points.points+10""",(group,user))
                    points = conn.execute("SELECT points FROM game_points WHERE group_id=%s AND user_id=%s",
                                          (group,user)).fetchone()[0]
                    result = f"🎉 정답! {name}님 +10P 획득!\n💰 현재 포인트: {points:,}P"
        reply(token,result)
        return
    # Region roster: administrator-managed plain names, without LINE mentions.
    region_edit = re.fullmatch(r"/지역(추가|삭제)\s+([가-힣A-Za-z0-9]{2,20})\s+(.{1,60})", text)
    if region_edit:
        action, region, member_name = region_edit.groups()
        member_name = member_name.strip()
        if not member_name or "\n" in member_name or "\r" in member_name:
            reply(token, "사용법: /지역추가 서울 홍길동 또는 /지역삭제 서울 홍길동")
            return
        with db() as conn:
            if action == "추가":
                changed = conn.execute(
                    "INSERT INTO region_members(group_id,region,member_name) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING member_name",
                    (group,region,member_name)).fetchone()
            else:
                changed = conn.execute(
                    "DELETE FROM region_members WHERE group_id=%s AND region=%s AND member_name=%s RETURNING member_name",
                    (group,region,member_name)).fetchone()
        if action == "추가":
            reply(token, f"📍 {region} 명단에 {member_name} 등록 완료!" if changed else f"{member_name}님은 이미 {region} 명단에 있어.")
        else:
            reply(token, f"🗑️ {region} 명단에서 {member_name} 삭제 완료!" if changed else f"{region} 명단에서 {member_name}님을 찾지 못했어.")
        return
    if text.startswith("/지역추가") or text.startswith("/지역삭제"):
        reply(token, "사용법: /지역추가 서울 홍길동\n삭제: /지역삭제 서울 홍길동")
        return
    if text == "/지역목록":
        with db() as conn:
            regions = conn.execute(
                "SELECT region,COUNT(*) FROM region_members WHERE group_id=%s GROUP BY region ORDER BY region",
                (group,)).fetchall()
        if not regions:
            reply(token, "📍 등록된 지역이 아직 없어.\n관리자가 /지역추가 서울 홍길동 으로 등록할 수 있어.")
        else:
            lines = [f"📍 지역별 등록 현황 ({len(regions)}개 지역)"]
            for region, count in regions:
                line = f"• {region}: {count}명"
                if sum(len(x) + 1 for x in lines) + len(line) > 4700:
                    lines.append("…이하 생략")
                    break
                lines.append(line)
            reply(token, "\n".join(lines))
        return
    region_query = re.fullmatch(r"/([가-힣A-Za-z0-9]{2,20})", text)
    if region_query:
        region = region_query.group(1)
        with db() as conn:
            members = conn.execute(
                "SELECT member_name FROM region_members WHERE group_id=%s AND region=%s ORDER BY member_name",
                (group,region)).fetchall()
        if members:
            lines = [f"📍 {region} 거주자 명단 ({len(members)}명)"]
            for i, (member_name,) in enumerate(members, 1):
                line = f"{i}. {member_name}"
                if sum(len(x) + 1 for x in lines) + len(line) > 4700:
                    lines.append("…이하 생략")
                    break
                lines.append(line)
            reply(token, "\n".join(lines))
            return
        # Only respond to recognizable geographic names; keep other slash commands intact.
        if region in WEATHER_CITIES or region in WEATHER_ALIASES or region in (
            "경기", "경기도", "강원", "강원도", "충북", "충청북도", "충남", "충청남도",
            "전북", "전라북도", "전남", "전라남도", "경북", "경상북도", "경남", "경상남도",
            "제주도", "세종시"):
            reply(token, f"📍 {region} 명단은 아직 비어 있어.")
            return
    if text in ("*점메추", "*저메추", "*메뉴추천"):
        lunch = (
            "김치찌개", "된장찌개", "제육볶음", "돈가스", "비빔밥", "순두부찌개",
            "국밥", "냉면", "칼국수", "쌀국수", "짜장면", "짬뽕", "볶음밥",
            "초밥", "우동", "라멘", "김밥", "떡볶이", "카레", "샌드위치",
            "파스타", "부대찌개", "콩나물국밥", "오므라이스", "덮밥",
        )
        dinner = (
            "삼겹살", "치킨", "피자", "족발", "보쌈", "곱창", "닭갈비",
            "찜닭", "감자탕", "마라탕", "샤브샤브", "소고기구이",
            "양꼬치", "회", "초밥", "파스타", "햄버거", "부대찌개",
            "닭볶음탕", "갈비찜", "떡볶이", "순대국", "고등어구이",
            "불고기", "쭈꾸미볶음",
        )
        if text == "*점메추":
            title, choices = "🍱 오늘의 점심 추천", lunch
        elif text == "*저메추":
            title, choices = "🍽️ 오늘의 저녁 추천", dinner
        else:
            title, choices = "🍴 오늘의 메뉴 추천", tuple(dict.fromkeys(lunch + dinner))
        reply(token, f"{title}\n🎯 {secrets.choice(choices)}!\n다시 뽑으려면 {text} 입력해 줘.")
        return
    if text == "*방통계":
        from datetime import timedelta
        today = datetime.now(KST).date()
        start = (today - timedelta(days=6)).isoformat()
        with db() as conn:
            today_row = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT user_id) FROM message_events
                   WHERE group_id=%s AND message_date=%s""",
                (group, today.isoformat())).fetchone()
            week_row = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT user_id) FROM message_events
                   WHERE group_id=%s AND message_date >= %s AND message_date <= %s""",
                (group, start, today.isoformat())).fetchone()
            peak = conn.execute(
                """SELECT message_hour, COUNT(*) AS cnt FROM message_events
                   WHERE group_id=%s AND message_date=%s AND message_hour IS NOT NULL
                   GROUP BY message_hour ORDER BY cnt DESC, message_hour ASC LIMIT 1""",
                (group, today.isoformat())).fetchone()
        peak_text = f"{peak[0]:02d}:00~{peak[0]:02d}:59 ({peak[1]}개)" if peak else "기록 없음"
        reply(token,
              f"📊 단체방 활동 통계\n"
              f"📅 오늘 ({today.strftime('%m/%d')})\n"
              f"💬 메시지: {today_row[0]:,}개\n"
              f"👥 활동 인원: {today_row[1]:,}명\n"
              f"⏰ 가장 활발한 시간: {peak_text}\n\n"
              f"📆 최근 7일 ({start} ~ {today.isoformat()})\n"
              f"💬 총 메시지: {week_row[0]:,}개\n"
              f"👥 활동 인원: {week_row[1]:,}명\n"
              f"※ 레드봇이 수집한 텍스트 메시지 기준. 시간대는 업데이트 이후 기록부터 집계돼.")
        return
    if text == "*주사위":
        reply(token, f"🎲 주사위 결과: {secrets.randbelow(6) + 1}!")
        return
    if text == "*가위바위보":
        reply(token, f"✊✌️🖐️ 레드봇의 선택: {secrets.choice(('가위 ✌️', '바위 ✊', '보 🖐️'))}")
        return
    draw_match = re.fullmatch(r"\*랜덤추첨(?:\s+(\d+))?", text)
    if draw_match:
        requested = int(draw_match.group(1) or "1")
        if not 1 <= requested <= 20:
            reply(token, "추첨 인원은 1~20명으로 입력해 줘. 예: *랜덤추첨 3")
            return
        with db() as conn:
            rows = conn.execute(
                """SELECT user_id, display_name FROM member_profiles
                   WHERE group_id=%s ORDER BY user_id""", (group,)).fetchall()
        if not rows:
            reply(token, "아직 추첨할 참여자가 없어.")
            return
        if requested > len(rows):
            reply(token, f"추첨 대상은 {len(rows)}명이야. {len(rows)}명 이하로 입력해 줘.")
            return
        winners = list(rows)
        secrets.SystemRandom().shuffle(winners)
        winners = winners[:requested]
        result = "\n".join(f"{i}. {name}" for i, (_, name) in enumerate(winners, 1))
        reply(token, f"🎉 랜덤추첨 결과 ({requested}명 / 대상 {len(rows)}명)\n{result}\n※ 이 단체방에서 레드봇이 기록한 참여자 기준")
        return
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
        if not is_admin:
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

@app.post("/internal/hourly-alert")
def hourly_alert():
    secret = os.getenv("HOURLY_ALERT_SECRET", "")
    supplied = request.headers.get("X-Hourly-Secret", "")
    if not secret or not hmac.compare_digest(secret,supplied):
        abort(403)
    initialize()
    now = datetime.now(KST)
    if not (now.hour == 0 or 8 <= now.hour <= 23):
        return {"status":"quiet_hours","sent":0}
    hour_key = now.strftime("%Y-%m-%d-%H")
    hour_label = f"{'오전' if now.hour < 12 else '오후'} {now.hour % 12 or 12}시"
    with db() as conn:
        groups = [row[0] for row in conn.execute(
            "SELECT group_id FROM hourly_alert_settings WHERE enabled=TRUE").fetchall()]
    sent = 0
    for group_id in groups:
        try:
            with db() as conn:
                # Serialize hourly attempts across overlapping scheduler executions.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (group_id+":hourly"))
                exists = conn.execute(
                    "SELECT 1 FROM hourly_alert_deliveries WHERE group_id=%s AND hour_key=%s",
                    (group_id,hour_key)).fetchone()
                enabled = conn.execute(
                    "SELECT enabled FROM hourly_alert_settings WHERE group_id=%s",
                    (group_id,)).fetchone()
                if exists or not enabled or not enabled[0]:
                    continue
                payload = {"to":group_id,"messages":[{"type":"text",
                    "text":f"🔔 레드봇 3.0 정각 알림\n🕒 현재 {hour_label} 정각입니다!\n❤️ 즐거운 시간 보내세요!"}]}
                response = requests.post(API+"/message/push",json=payload,headers=headers(),timeout=15)
                response.raise_for_status()
                conn.execute("INSERT INTO hourly_alert_deliveries(group_id,hour_key) VALUES(%s,%s)",
                             (group_id,hour_key))
                sent += 1
        except Exception:
            app.logger.exception("Hourly alert failed for group")
    return {"status":"ok","hour":hour_key,"sent":sent}

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
