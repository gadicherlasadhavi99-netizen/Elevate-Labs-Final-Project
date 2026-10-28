from flask import (
    Flask, render_template, request, session,
    redirect, url_for, flash
)
from transformers import pipeline
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime
import sqlite3
import textstat
import os
import sqlite3
import textstat
import os

# PDF Download Imports
from io import BytesIO
from flask import send_file
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from xml.sax.saxutils import escape

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY", "local-development-key-change-this"
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE_DIR = os.path.join(BASE_DIR, "database")
os.makedirs(DATABASE_DIR, exist_ok=True)

DB_PATH = os.path.join(DATABASE_DIR, "app.db")

summarizer = None


# ---------------- DATABASE ----------------

def get_db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                original_text TEXT NOT NULL,
                summary_text TEXT NOT NULL,
                word_count INTEGER NOT NULL,
                readability REAL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)


# ---------------- LOGIN PROTECTION ----------------

def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "info")
            return redirect(url_for("login"))

        return function(*args, **kwargs)

    return wrapper


# ---------------- AI MODEL ----------------

def get_summarizer():
    global summarizer

    if summarizer is None:
        summarizer = pipeline(
            "summarization",
            model="sshleifer/distilbart-cnn-12-6"
        )

    return summarizer


# ---------------- HOME / SUMMARIZER ----------------

@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    original_text = ""
    summary = ""
    word_count = 0
    readability = None

    if request.method == "POST":
        original_text = request.form.get("text", "").strip()
        word_count = len(original_text.split())

        if not original_text:
            flash("Please enter some text.", "error")

        elif word_count < 30:
            summary = "Please enter at least 30 words to generate a summary."

        else:
            try:
                model = get_summarizer()

                result = model(
                    original_text,
                    max_length=300,
                    min_length=150,
                    do_sample=False,
                    truncation=True
                )

                summary = result[0]["summary_text"]

                readability = round(
                    textstat.flesch_reading_ease(original_text), 2
                )

                with get_db() as db:
                    db.execute("""
                        INSERT INTO summaries
                        (user_id, original_text, summary_text,
                         word_count, readability, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        session["user_id"],
                        original_text,
                        summary,
                        word_count,
                        readability,
                        datetime.now().strftime("%Y-%m-%d %H:%M")
                    ))

            except Exception as error:
                app.logger.exception("Summarization failed")
                summary = (
                    "Could not generate the summary. "
                    "Please check your internet connection and try again."
                )

    # Keep the existing summarizer interface variables.
    return render_template(
        "summarizer.html",
        original_text=original_text,
        original=original_text,
        summary=summary,
        result=summary,
        word_count=word_count,
        readability=readability,
        history=get_user_history()
    )


def get_user_history():
    if "user_id" not in session:
        return []

    with get_db() as db:
        rows = db.execute("""
            SELECT id, summary_text AS summary,
                   word_count AS words, created_at
            FROM summaries
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 5
        """, (session["user_id"],)).fetchall()

    return [dict(row) for row in rows]


# ---------------- SIGNUP ----------------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not name or not email or not password:
            flash("Please complete all fields.", "error")

        elif len(password) < 8:
            flash("Password must contain at least 8 characters.", "error")

        elif password != confirm_password:
            flash("Passwords do not match.", "error")

        else:
            try:
                with get_db() as db:
                    db.execute("""
                        INSERT INTO users
                        (name, email, password_hash, created_at)
                        VALUES (?, ?, ?, ?)
                    """, (
                        name,
                        email,
                        generate_password_hash(password),
                        datetime.now().strftime("%Y-%m-%d %H:%M")
                    ))

                flash("Account created successfully. Please log in.", "success")
                return redirect(url_for("login"))

            except sqlite3.IntegrityError:
                flash("This email is already registered.", "error")

    return render_template("signup.html")


# ---------------- LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        with get_db() as db:
            user = db.execute(
                "SELECT * FROM users WHERE email = ?",
                (email,)
            ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["user_email"] = user["email"]

            return redirect(url_for("dashboard"))

        flash("Incorrect email or password.", "error")

    return render_template("login.html")


# ---------------- DASHBOARD ----------------

@app.route("/dashboard")
@login_required
def dashboard():
    with get_db() as db:
        total = db.execute("""
            SELECT COUNT(*) AS total
            FROM summaries
            WHERE user_id = ?
        """, (session["user_id"],)).fetchone()["total"]

        recent = db.execute("""
            SELECT id, summary_text, word_count, created_at
            FROM summaries
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 5
        """, (session["user_id"],)).fetchall()

    return render_template(
        "dashboard.html",
        total=total,
        recent=recent
    )


# ---------------- DOCUMENTS ----------------

@app.route("/documents")
@login_required
def documents():
    with get_db() as db:
        items = db.execute("""
            SELECT * FROM summaries
            WHERE user_id = ?
            ORDER BY id DESC
        """, (session["user_id"],)).fetchall()

    return render_template("documents.html", items=items)
# ---------------- DOWNLOAD SUMMARY AS PDF ----------------

@app.route("/download-pdf/<int:summary_id>")
@login_required
def download_pdf(summary_id):
    with get_db() as db:
        item = db.execute(
            """
            SELECT *
            FROM summaries
            WHERE id = ? AND user_id = ?
            """,
            (summary_id, session["user_id"])
        ).fetchone()

    if item is None:
        return "Summary not found.", 404

    pdf_buffer = BytesIO()

    document = SimpleDocTemplate(
        pdf_buffer,
        pagesize=A4,
        rightMargin=45,
        leftMargin=45,
        topMargin=45,
        bottomMargin=45
    )

    styles = getSampleStyleSheet()
    story = []

    story.append(
        Paragraph("AI Text Summarizer", styles["Title"])
    )
    story.append(Spacer(1, 20))

    story.append(
        Paragraph("Generated Summary", styles["Heading2"])
    )

    summary_text = escape(
        str(item["summary_text"] or "")
    ).replace("\n", "<br/>")

    story.append(
        Paragraph(summary_text, styles["BodyText"])
    )
    story.append(Spacer(1, 20))

    story.append(
        Paragraph("Original Text", styles["Heading2"])
    )

    original_text = escape(
        str(item["original_text"] or "")
    ).replace("\n", "<br/>")

    story.append(
        Paragraph(original_text, styles["BodyText"])
    )

    document.build(story)
    pdf_buffer.seek(0)

    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"summary_{summary_id}.pdf"
    )


# ---------------- SUMMARY HISTORY ----------------

@app.route("/history")
@login_required
def history():
    with get_db() as db:
        items = db.execute("""
            SELECT * FROM summaries
            WHERE user_id = ?
            ORDER BY id DESC
        """, (session["user_id"],)).fetchall()

    return render_template("history.html", items=items)


@app.route("/history/<int:summary_id>")
@login_required
def view_summary(summary_id):
    with get_db() as db:
        item = db.execute("""
            SELECT * FROM summaries
            WHERE id = ? AND user_id = ?
        """, (summary_id, session["user_id"])).fetchone()

    if item is None:
        flash("Summary not found.", "error")
        return redirect(url_for("history"))

    return render_template(
        "history.html",
        items=[item],
        selected=item
    )


# ---------------- PROFILE ----------------

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        name = request.form.get("name", "").strip()

        if not name:
            flash("Name cannot be empty.", "error")
        else:
            with get_db() as db:
                db.execute(
                    "UPDATE users SET name = ? WHERE id = ?",
                    (name, session["user_id"])
                )

            session["user_name"] = name
            flash("Profile updated successfully.", "success")
            return redirect(url_for("profile"))

    with get_db() as db:
        user = db.execute("""
            SELECT name, email, created_at
            FROM users WHERE id = ?
        """, (session["user_id"],)).fetchone()

    return render_template("profile.html", user=user)


# ---------------- SETTINGS ----------------

@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html")


# ---------------- LOGOUT ----------------

@app.route("/logout")
def logout():
    session.clear()
    flash("You have logged out.", "info")
    return redirect(url_for("login"))


# ---------------- START APPLICATION ----------------

initialize_database()

if __name__ == "__main__":
    app.run(debug=True)