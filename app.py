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

def counts(group, day, end_day=None):
    end_day = end_day or day
    with db() as conn:
        rows = conn.execute("""WITH totals AS (
                SELECT user_id,COUNT(*) AS cnt FROM message_events
                WHERE group_id=%s AND message_date BETWEEN %s AND %s
                GROUP BY user_id HAVING COUNT(*) >= 2
            )
            SELECT t.user_id,COALESCE(
                NULLIF(NULLIF(NULLIF(p.display_name,LEFT(t.user_id,12)),t.user_id),''),
                h.display_name),t.cnt
            FROM totals t
            LEFT JOIN member_profiles p ON p.group_id=%s AND p.user_id=t.user_id
            LEFT JOIN LATERAL (
                SELECT e.display_name FROM message_events e
                WHERE e.group_id=%s AND e.user_id=t.user_id
                  AND e.display_name NOT IN (LEFT(t.user_id,12),t.user_id,'')
                ORDER BY e.message_date DESC,e.message_hour DESC NULLS LAST,e.event_id DESC LIMIT 1
            ) h ON NULLIF(NULLIF(NULLIF(p.display_name,LEFT(t.user_id,12)),t.user_id),'') IS NULL
            ORDER BY t.cnt DESC,t.user_id""", (group, day, end_day, group, group)).fetchall()
    label = f"{day[:4]}년 {int(day[5:7])}월 {int(day[8:])}일"
    if end_day != day:
        label += f" ~ {end_day[:4]}년 {int(end_day[5:7])}월 {int(end_day[8:])}일"
    heading = f"📊 {label} 마딧수 (2마디 이상) · 총 {len(rows)}명"
    if not rows: return [heading + "\n2마디 이상 기록된 멤버가 없습니다."]
    pages = []
    lines = [heading]
    for i, (uid, name, n) in enumerate(rows, 1):
        item = f"{i}. {name or '닉네임 확인 불가'}: {n}마디"
        if len(("\n".join(lines) + "\n" + item).encode("utf-16-le")) // 2 > 4500:
            pages.append("\n".join(lines))
            lines = [heading + " (계속)"]
        lines.append(item)
    pages.append("\n".join(lines))
    return pages

def reply_counts(token, group, pages):
    messages = [{"type":"text","text":page} for page in pages]
    r = requests.post(API + "/message/reply", json={"replyToken":token,"messages":messages[:5]}, headers=headers(), timeout=12)
    r.raise_for_status()
    for i in range(5, len(messages), 5):
        r = requests.post(API + "/message/push", json={"to":group,"messages":messages[i:i+5]}, headers=headers(), timeout=12)
        r.raise_for_status()

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


def weekly_member_counts(group, target, day):
    from datetime import timedelta
    today = datetime.fromisoformat(day).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    with db() as conn:
        rows = dict(conn.execute(
            "SELECT message_date, COUNT(*) FROM message_events WHERE group_id=%s AND user_id=%s AND message_date BETWEEN %s AND %s GROUP BY message_date",
            (group, target, days[0], days[-1])).fetchall())
    total = sum(rows.values())
    lines = [f"📊 {profile(group, target)} 주간 마딧수", f"{days[0]} ~ {days[-1]}"]
    for d in days:
        lines.append(f"{d[5:]} ({'월화수목금토일'[datetime.fromisoformat(d).weekday()]}) : {rows.get(d, 0)}마디")
    lines.extend([f"총합: {total}마디", f"일평균: {total / 7:.1f}마디"])
    if days[0][:7] != day[:7]:
        lines.append("※ 월별 초기화로 이전 달 기록은 조회되지 않을 수 있습니다.")
    return "\n".join(lines)

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
            ON CONFLICT(group_id,user_id) DO UPDATE SET display_name=EXCLUDED.display_name,updated_at=NOW()
            WHERE EXCLUDED.display_name NOT IN (LEFT(EXCLUDED.user_id,12),EXCLUDED.user_id,'')""", (group,user,name))
        conn.execute("""INSERT INTO message_events(event_id,group_id,user_id,display_name,message_date,message_hour)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (eid,group,user,name,day,datetime.now(KST).hour))
    with db() as conn:
        is_admin = user in ADMINS or conn.execute(
            "SELECT 1 FROM group_admins WHERE group_id=%s AND user_id=%s", (group,user)
        ).fetchone() is not None
    if text in ("/관리자목록", "*관리자목록"):
        if not is_admin:
            reply(token, "⛔ 관리자 목록은 등록된 관리자만 확인할 수 있어.")
            return
        with db() as conn:
            registered = {row[0] for row in conn.execute(
                "SELECT user_id FROM group_admins WHERE group_id=%s", (group,)).fetchall()}
        all_admins = sorted(registered | ADMINS)
        lines = [f"🛡️ 이 단체방 관리자 목록 ({len(all_admins)}명)"]
        for number, uid in enumerate(all_admins, 1):
            label = " · 최초 설정 관리자" if uid in ADMINS else ""
            lines.append(f"{number}. {profile(group,uid)}{label}")
        reply(token, "\n".join(lines) if all_admins else "🛡️ 등록된 관리자가 없어.")
        return
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
    if text in ("*도움말", "*명령어"):
        reply(token, """🤖 RED BOT 3.0 전체 명령어

📊 마딧수·통계
*마딧수
*@닉네임 마딧수 (최근 7일, 실제 멘션)
*YYMMDD마딧수 (예: *261113마딧수)
*261101~261125마딧수 (기간 합계)
*261101~마딧수 (시작일부터 오늘까지)
*방통계

👥 멤버 호출
*남초 / *여초
/남초추가 @멘션 /남초삭제 @멘션 /남초목록
/여초추가 @멘션 /여초삭제 @멘션 /여초목록
※ 남초·여초 명단 수정/조회는 관리자만
/관리자추가 @멘션 /관리자삭제 @멘션 /관리자목록 (관리자만)

🗺️ 지역 명단 (모든 멤버 수정 가능)
/지역추가 서울 홍길동
/지역삭제 서울 홍길동
/서울 /부산 등 지역명 /지역목록

🍜 메뉴 추천
*점메추 / *저메추 / *메뉴추천

🔮 운세
*오늘운세 / *행운숫자

🤖 AI·날씨
!질문내용 (예: !내일 서울 날씨)

📖 *도움말 / *명령어""")
        return
    if text in ("*오늘운세","*행운숫자"):
        fortune, lucky = daily_fortune(group,user,day)
        if text == "*오늘운세":
            reply(token, f"🔮 {name}님의 오늘의 운세 ({day})\n{fortune}\n🍀 행운의 숫자: {lucky}\n※ 재미로 보는 랜덤 운세야.")
        else:
            reply(token, f"🍀 {name}님의 오늘 행운의 숫자: {lucky}\n※ 재미로 보는 숫자야.")
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
    if re.fullmatch(r"\*\s*@.+\s*마딧수", text):
        targets = (message.get("mention") or {}).get("mentionees", [])
        if len(targets) != 1 or not targets[0].get("userId"):
            reply(token, "사용법: *@닉네임 마딧수 (한 명을 실제 LINE 멘션)")
            return
        reply(token, weekly_member_counts(group, targets[0]["userId"], day))
        return
    if text == "*마딧수":
        reply_counts(token, group, counts(group, day))
        return
    period_match = re.fullmatch(r"\*?(\d{6})\s*~\s*(\d{6})?\s*마딧수", text)
    if period_match:
        start_raw, end_raw = period_match.groups()
        today = datetime.now(KST).date()
        def parse_day(raw):
            return datetime(2000 + int(raw[:2]), int(raw[2:4]), int(raw[4:6]), tzinfo=KST).date()
        try:
            start_date = parse_day(start_raw)
            end_date = parse_day(end_raw) if end_raw else today
            if not start_date <= end_date <= today:
                raise ValueError()
        except ValueError:
            reply(token, "올바른 기간을 입력해 주세요. 시작일 ≤ 종료일 ≤ 오늘\n예: *261101~261125마딧수 또는 *261101~마딧수")
            return
        reply_counts(token, group, counts(group, start_date.isoformat(), end_date.isoformat()))
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
        reply_counts(token, group, counts(group, date.isoformat()))
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

_cleanup_month = None
_cleanup_lock = Lock()

@app.before_request
def reset_monthly_chat_history():
    global _cleanup_month
    if request.path != "/callback":
        return
    now = datetime.now(KST)
    month = now.strftime("%Y-%m")
    if _cleanup_month == month:
        return
    with _cleanup_lock:
        if _cleanup_month == month:
            return
        # Keep November records until the first scheduled reset.
        if month >= "2026-12":
            with db() as conn:
                conn.execute("DELETE FROM message_events WHERE message_date < %s", (month + "-01",))
        _cleanup_month = month

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
