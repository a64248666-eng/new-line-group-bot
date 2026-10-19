import os
import hmac
import hashlib
import logging
import re
import base64
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from threading import Lock
from collections import defaultdict

import requests
import psycopg
from psycopg.rows import dict_row
from flask import Flask, request, abort, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("new-line-group-bot")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
KST = ZoneInfo("Asia/Seoul")

ADMIN_KEYWORDS = ["무무", "이리", "타워", "랜치", "절도", "주음", "꼬꼬", "짜필"]
MAX_HISTORY_MESSAGES = 12
conversation_histories = defaultdict(list)
history_lock = Lock()
db_init_lock = Lock()
_db_initialized = False


def get_db():
    if not DATABASE_URL:
        raise RuntimeError("Render 환경변수 DATABASE_URL이 설정되지 않았습니다.")
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=15,
        row_factory=dict_row,
    )


def init_database():
    global _db_initialized
    if _db_initialized:
        return
    with db_init_lock:
        if _db_initialized:
            return
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS message_events (
                    event_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    message_date TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_group_date
                ON message_events (group_id, message_date)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_group_user_date
                ON message_events (group_id, user_id, message_date)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS member_profiles (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (group_id, user_id)
                )
            """)
        _db_initialized = True
        logger.info("새 봇 데이터베이스 초기화 완료")


def line_headers():
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def save_member_profile(group_id, user_id, display_name):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO member_profiles (group_id, user_id, display_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (group_id, user_id)
