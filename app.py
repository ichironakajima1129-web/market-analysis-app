import os
import json
import secrets
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, stream_with_context,
                   Response, session, redirect, url_for)
import anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "")

# ---- Database ----

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras

    def _connect():
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

    def init_db():
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS analyses (
                        id SERIAL PRIMARY KEY,
                        created_at TEXT, company_name TEXT, industry TEXT,
                        company_size TEXT, business_description TEXT,
                        challenges TEXT, meeting_purpose TEXT,
                        additional_info TEXT, result TEXT
                    )
                """)

    def insert_analysis(row):
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO analyses (created_at,company_name,industry,company_size,"
                    "business_description,challenges,meeting_purpose,additional_info,result)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    row,
                )

    def fetch_list():
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id,created_at,company_name,industry,company_size"
                    " FROM analyses ORDER BY id DESC"
                )
                return cur.fetchall()

    def fetch_one(aid):
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM analyses WHERE id=%s", (aid,))
                return cur.fetchone()

    def fetch_all():
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM analyses ORDER BY id DESC")
                return cur.fetchall()

else:
    import sqlite3
    DB_PATH = os.path.join(os.path.dirname(__file__), "analyses.db")

    def _connect():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db():
        with _connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT, company_name TEXT, industry TEXT,
                    company_size TEXT, business_description TEXT,
                    challenges TEXT, meeting_purpose TEXT,
                    additional_info TEXT, result TEXT
                )
            """)

    def insert_analysis(row):
        with _connect() as conn:
            conn.execute(
                "INSERT INTO analyses (created_at,company_name,industry,company_size,"
                "business_description,challenges,meeting_purpose,additional_info,result)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                row,
            )

    def fetch_list():
        with _connect() as conn:
            return conn.execute(
                "SELECT id,created_at,company_name,industry,company_size"
                " FROM analyses ORDER BY id DESC"
            ).fetchall()

    def fetch_one(aid):
        with _connect() as conn:
            return conn.execute(
                "SELECT * FROM analyses WHERE id=?", (aid,)
            ).fetchone()

    def fetch_all():
        with _connect() as conn:
            return conn.execute(
                "SELECT * FROM analyses ORDER BY id DESC"
            ).fetchall()


init_db()

# ---- Auth ----

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if TEAM_PASSWORD and not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == TEAM_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "パスワードが違います"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---- Prompts ----

SYSTEM_PROMPT = """あなたは外資系トップコンサルティングファーム出身のマーケティングコンサルタントです。
クライアントの商談・提案準備を支援するため、入力された顧客情報を基に実践的な市場・競合分析と仮説を提供します。

分析の特徴：
- データと論理に基づいた仮説思考
- 商談で即使える具体的な切り口
- 顧客の「言葉の裏にある本質課題」を見抜く視点
- 競合との差別化ポイントを明確に示す

出力形式：Markdownを使って構造化されたレポートとして出力してください。"""

ANALYSIS_PROMPT = """以下の顧客情報を基に、商談準備レポートを作成してください。

## 顧客情報
- **企業名**: {company_name}
- **業種・業界**: {industry}
- **企業規模**: {company_size}
- **事業内容**: {business_description}
- **課題・悩み**: {challenges}
- **商談の目的**: {meeting_purpose}
- **補足情報**: {additional_info}

---

以下の構成で作成してください。各セクションは簡潔に。冗長な説明は不要です。

## 1. ビジネスモデル整理
顧客企業のビジネスモデルを以下の表で整理してください：

| 項目 | 内容 |
|------|------|
| 誰に売るか（顧客） | |
| 何を提供するか（価値） | |
| どう売るか（チャネル） | |
| どう稼ぐか（収益源） | |
| 強みとなるリソース | |

表の後に、ビジネスモデルの特徴を2〜3行で端的にまとめる。

## 2. 市場環境（3点以内）
箇条書きで端的に。1項目2行以内。

## 3. 競合状況
主要競合を3社以内で表にまとめ、この企業の立ち位置を一言で示す。

| 競合 | 強み | 弱み |
|------|------|------|
| | | |

## 4. 課題仮説（3つ）
「表面課題→本質課題」の形式で3つ。各仮説は2行以内。箇条書き不要。

## 5. 提案の切り口（3つ）
各切り口を1〜2行で。

## 6. 商談でのヒアリング質問（3つ）
質問と確認したいことを1行で。

---
全体を通じて超簡潔に。各セクション合計100文字程度を目安に。"""


# ---- Routes ----

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
@login_required
def analyze():
    data = request.get_json()

    prompt = ANALYSIS_PROMPT.format(
        company_name=data.get("company_name", "未入力"),
        industry=data.get("industry", "未入力"),
        company_size=data.get("company_size", "未入力"),
        business_description=data.get("business_description", "未入力"),
        challenges=data.get("challenges", "未入力"),
        meeting_purpose=data.get("meeting_purpose", "未入力"),
        additional_info=data.get("additional_info", "なし"),
    )

    full_result = []

    def generate():
        try:
            yield ": keepalive\n\n"

            with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=3000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    full_result.append(text)
                    yield f"data: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"

            insert_analysis((
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                data.get("company_name", ""),
                data.get("industry", ""),
                data.get("company_size", ""),
                data.get("business_description", ""),
                data.get("challenges", ""),
                data.get("meeting_purpose", ""),
                data.get("additional_info", ""),
                "".join(full_result),
            ))

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Content-Encoding": "identity"},
    )


@app.route("/history")
@login_required
def history():
    rows = fetch_list()
    return render_template("history.html", rows=rows)


@app.route("/history/<int:analysis_id>")
@login_required
def history_detail(analysis_id):
    row = fetch_one(analysis_id)
    if not row:
        return "Not found", 404
    return render_template("detail.html", row=row)


@app.route("/export")
@login_required
def export():
    rows = fetch_all()

    def generate_csv():
        headers = ["ID", "日時", "企業名", "業種", "企業規模", "事業内容", "課題", "商談目的", "補足", "分析結果"]
        yield "\ufeff"
        yield ",".join(headers) + "\n"
        for row in rows:
            values = [
                str(row["id"]), row["created_at"] or "", row["company_name"] or "",
                row["industry"] or "", row["company_size"] or "",
                row["business_description"] or "", row["challenges"] or "",
                row["meeting_purpose"] or "", row["additional_info"] or "",
                row["result"] or "",
            ]
            yield ",".join(f'"{v.replace(chr(34), chr(34)*2)}"' for v in values) + "\n"

    return Response(
        generate_csv(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=analyses.csv"},
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)
