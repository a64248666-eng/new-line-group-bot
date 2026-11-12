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
DB = os.getenv("DATABASE_URL", "")
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
        ids = [r[0] for r in conn.execute("SELECT user_id FROM mention_members WHERE group_id=%s AND category=%s ORDER BY user_id", (group, category))]
    if not ids: return f"{category} 명단이 비어 있습니다.", None
    text = f"📢 {category} 호출!\n"
    mentions = []
    for uid in ids[:20]:
        tag = "@" + profile(group, uid).replace("\n", " ")[:40]
        mentions.append({"index": utf16len(text), "length": utf16len(tag), "userId": uid})
        text += tag + " "
    if len(ids) > 20: text += f"\n(20명까지 호출, 나머지 {len(ids)-20}명 제외)"
    return text, mentions

def ai(question):
    if not GEMINI_KEY: return "관리자에게 GEMINI_API_KEY 설정을 요청해 주세요."
    try:
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            json={"contents": [{"parts": [{"text": question[:3000]}]}], "generationConfig": {"maxOutputTokens": 900}},
            timeout=22)
        if r.status_code == 429: return "AI 무료 사용량 한도에 도달했어요. 나중에 다시 시도해 주세요."
        if not r.ok:
            app.logger.warning("Gemini status %s", r.status_code)
            return "AI 답변을 불러올 수 없어요."
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
        output, mentions = mention_text(group, text[1:])
        reply(token, output, mentions)
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
