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
        conn.execute("""CREATE TABLE IF NOT EXISTS game_points (
            group_id TEXT NOT NULL, user_id TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0, attendance_days INTEGER NOT NULL DEFAULT 0,
            last_attendance TEXT, PRIMARY KEY(group_id,user_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS quiz_daily_limits (
            group_id TEXT NOT NULL, quiz_date TEXT NOT NULL,
            issued_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(group_id,quiz_date))""")
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
    if not GEMINI_KEY: return "관리자에게 GEMINI_API_KEY 설정을 요청해 주세요."
    # Ground factual lookup questions in web search; avoid unverified model recall.
    current_terms = ("현재", "지금", "오늘", "최신", "요즘", "이번", "누구", "누가", "대통령", "총리", "장관", "환율", "주가", "날씨", "뉴스", "당선", "취임", "순위", "가격", "현직", "언제", "어디", "얼마", "몇", "발표", "결과", "통계", "사실", "출처", "근거")
    fresh_topics = ("대통령", "총리", "장관", "정부", "정치", "환율", "주가", "날씨", "뉴스", "선거", "당선", "취임", "현직", "대표", "시장", "가격", "경기", "순위", "출시", "업데이트", "법률", "정책", "인구", "통계", "사건", "사고", "연예", "스포츠", "주식", "코인", "금리", "부동산")
    needs_live = (any(t in normalized for t in fresh_topics)
                  or (any(t in normalized for t in current_terms)
                      and not any(t in normalized for t in ("소설", "시를", "농담", "이야기만들", "번역", "맞춤법"))))
    base = "https://generativelanguage.googleapis.com/v1beta"
    payload = {
        "systemInstruction": {"parts": [{"text": f"현재 기준 시간은 대한민국 서울(Asia/Seoul) {today_text} {now.hour:02d}시 {now.minute:02d}분입니다. 오늘 날짜와 현재 시간에 관한 질문에는 이 정보를 정확히 사용하세요. 한국어로 핵심만 정확하고 간결하게 답변하세요. 간단한 질문에는 1~3문장으로 답하세요. 질문의 국가·인물·날짜·단위를 정확히 구분하고, 질문이 모호하면 필요한 조건을 짧게 확인하세요. 확인되지 않은 수치·날짜·인용·출처를 만들어내지 마세요. 검색 결과가 충돌하면 최신성과 공신력을 비교하고, 확인된 사실과 불확실한 내용을 구분하세요. 검색 근거가 있는 최신 사실은 확인된 정보만 답하고, 가능하면 출처의 사이트명과 링크를 제시하세요. 실시간 검색이 필요한 최신 사실을 확인하지 못했다면 추측하지 마세요."}]},
        "contents": [{"parts": [{"text": question[:2000]}]}],
        "generationConfig": {"maxOutputTokens": 400, "thinkingConfig": {"thinkingBudget": 0}}
    }
    if needs_live:
        payload["tools"] = [{"google_search": {}}]
        payload["generationConfig"].pop("thinkingConfig", None)
    def generate(model):
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
                alternatives.sort(key=lambda m: ("preview" in m.lower(), "lite" not in m.lower(), m))
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
*출석 / *내포인트 / *출석랭킹

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
        question, choices, correct = secrets.choice(QUIZ_BANK)
        with db() as conn:
            quota = conn.execute("""INSERT INTO quiz_daily_limits(group_id,quiz_date,issued_count)
                VALUES(%s,%s,1)
                ON CONFLICT(group_id,quiz_date) DO UPDATE
                SET issued_count=quiz_daily_limits.issued_count+1
                WHERE quiz_daily_limits.issued_count < 20
                RETURNING issued_count""", (group,day)).fetchone()
            if quota:
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
