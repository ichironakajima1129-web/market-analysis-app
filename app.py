import os
import json
import csv
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, stream_with_context, Response, g
import anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
DB_PATH = os.path.join(os.path.dirname(__file__), "analyses.db")

# ---- DB ----

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                company_name TEXT,
                industry TEXT,
                company_size TEXT,
                business_description TEXT,
                challenges TEXT,
                meeting_purpose TEXT,
                additional_info TEXT,
                result TEXT
            )
        """)

init_db()

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
「表面課題→本質課題」の形式で3つ。各仮説は3行以内。

## 5. 提案の切り口（3つ）
刺さるメッセージを一言で示した後、根拠を2行で。

## 6. 商談でのヒアリング質問（5つ）
質問と、その質問で何を確認したいかを1行で。

---
全体を通じて、平易な言葉・短い文章で。専門用語は最小限に。"""


# ---- Routes ----

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
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
            with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    full_result.append(text)
                    yield f"data: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"

            # 完了後にDBへ保存
            with sqlite3.connect(DB_PATH) as db:
                db.execute(
                    """INSERT INTO analyses
                       (created_at, company_name, industry, company_size,
                        business_description, challenges, meeting_purpose,
                        additional_info, result)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        datetime.now().strftime("%Y-%m-%d %H:%M"),
                        data.get("company_name", ""),
                        data.get("industry", ""),
                        data.get("company_size", ""),
                        data.get("business_description", ""),
                        data.get("challenges", ""),
                        data.get("meeting_purpose", ""),
                        data.get("additional_info", ""),
                        "".join(full_result),
                    ),
                )

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Content-Encoding": "identity"},
    )


@app.route("/history")
def history():
    db = get_db()
    rows = db.execute(
        "SELECT id, created_at, company_name, industry, company_size FROM analyses ORDER BY id DESC"
    ).fetchall()
    return render_template("history.html", rows=rows)


@app.route("/history/<int:analysis_id>")
def history_detail(analysis_id):
    db = get_db()
    row = db.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
    if not row:
        return "Not found", 404
    return render_template("detail.html", row=row)


@app.route("/export")
def export():
    db = get_db()
    rows = db.execute("SELECT * FROM analyses ORDER BY id DESC").fetchall()

    def generate_csv():
        headers = ["ID", "日時", "企業名", "業種", "企業規模", "事業内容", "課題", "商談目的", "補足", "分析結果"]
        yield "\ufeff"  # BOM for Excel
        yield ",".join(headers) + "\n"
        for row in rows:
            values = [str(row["id"]), row["created_at"], row["company_name"],
                      row["industry"], row["company_size"], row["business_description"],
                      row["challenges"], row["meeting_purpose"], row["additional_info"],
                      row["result"]]
            yield ",".join(f'"{v.replace(chr(34), chr(34)*2)}"' for v in values) + "\n"

    return Response(
        generate_csv(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=analyses.csv"},
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)
