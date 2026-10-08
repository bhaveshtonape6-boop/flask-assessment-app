import os
import json
import sqlite3
import random
import webbrowser
from threading import Timer
from collections import defaultdict
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from google import genai
from google.genai import types

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "fallback-secret-key-12345")
# Initialize for Vertex AI token format
api_key = os.environ.get("AQ.Ab8RN6L8Kc1fqbR129ynf2eUCl_II6y591i60czbEqYpj0pAQA")

if api_key:
    # Set location/project if using Vertex AI express key
    client = genai.Client(api_key="AQ.Ab8RN6L8Kc1fqbR129ynf2eUCl_II6y591i60czbEqYpj0pAQA")
        vertexai=True,
        project=os.environ.get("GCP_PROJECT_ID", "your-project-id"),
        location="us-central1"
    )
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Hello world",
)

DEFAULT_SUBJECTS = [
    "Mathematics", "English", "Physics", "Chemistry", 
    "Biology", "History", "Geography", "Economics", "Information Technology"
]

def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    
    conn.execute('''
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            question TEXT NOT NULL,
            option_a TEXT NOT NULL,
            option_b TEXT NOT NULL,
            option_c TEXT NOT NULL,
            option_d TEXT NOT NULL,
            correct_option TEXT NOT NULL,
            is_published INTEGER DEFAULT 0
        )
    ''')
    
    # Auto-migrate: Add is_published if missing from existing SQLite DB
    cursor = conn.execute("PRAGMA table_info(questions)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'is_published' not in columns:
        conn.execute("ALTER TABLE questions ADD COLUMN is_published INTEGER DEFAULT 0")

    conn.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_name TEXT NOT NULL,
            roll_no TEXT NOT NULL,
            student_class TEXT NOT NULL,
            subject TEXT NOT NULL,
            score INTEGER NOT NULL,
            total INTEGER NOT NULL,
            percentage REAL NOT NULL,
            submission_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor = conn.execute("SELECT value FROM settings WHERE key='password'")
    if not cursor.fetchone():
        conn.execute("INSERT INTO settings (key, value) VALUES ('password', 'admin')")
    
    cursor = conn.execute("SELECT value FROM settings WHERE key='security_answer'")
    if not cursor.fetchone():
        conn.execute("INSERT INTO settings (key, value) VALUES ('security_answer', 'admin')")
        
    conn.commit()
    conn.close()

init_db()

def group_results_by_date(results):
    grouped = defaultdict(list)
    today = date.today()
    
    for res in results:
        submission_dt = datetime.strptime(res['submission_time'], '%Y-%m-%d %H:%M:%S').date()
        
        if submission_dt == today:
            date_label = "Today"
        elif (today - submission_dt).days == 1:
            date_label = "Yesterday"
        else:
            date_label = submission_dt.strftime('%B %d, %Y')
            
        grouped[date_label].append(res)
    return grouped

@app.route('/')
def index():
    return render_template('index.html')

# --- TEACHER AUTH ---
@app.route('/teacher/login', methods=['GET', 'POST'])
def teacher_login():
    if request.method == 'POST':
        password = request.form.get('password')
        conn = get_db_connection()
        row = conn.execute("SELECT value FROM settings WHERE key='password'").fetchone()
        conn.close()
        
        if row and row['value'] == password:
            session['is_teacher'] = True
            return redirect(url_for('teacher'))
        else:
            return render_template('teacher_login.html', error="Invalid passcode!")
            
    return render_template('teacher_login.html')

@app.route('/logout')
def logout():
    session.pop('is_teacher', None)
    return redirect(url_for('index'))

# --- TEACHER DASHBOARD ---
@app.route('/teacher', methods=['GET', 'POST'])
def teacher():
    if not session.get('is_teacher'):
        return redirect(url_for('teacher_login'))
    
    current_subject = request.args.get('subject', None)
    conn = get_db_connection()
    
    if request.method == 'POST':
        sub_to_add = request.form.get('subject')
        existing_q = conn.execute('SELECT is_published FROM questions WHERE subject = ? LIMIT 1', (sub_to_add,)).fetchone()
        is_published = existing_q['is_published'] if existing_q else 0

        conn.execute('''
            INSERT INTO questions (subject, question, option_a, option_b, option_c, option_d, correct_option, is_published)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            sub_to_add,
            request.form['question'],
            request.form['option_a'],
            request.form['option_b'],
            request.form['option_c'],
            request.form['option_d'],
            request.form['correct_option'],
            is_published
        ))
        conn.commit()
        conn.close()
        return redirect(url_for('teacher', subject=sub_to_add))

    questions = []
    if current_subject:
        questions = conn.execute('SELECT * FROM questions WHERE subject = ?', (current_subject,)).fetchall()
        
    results_by_subject = {}
    for sub in DEFAULT_SUBJECTS:
        raw_results = conn.execute(
            'SELECT * FROM results WHERE subject = ? ORDER BY submission_time DESC', (sub,)
        ).fetchall()
        results_by_subject[sub] = group_results_by_date(raw_results)

    conn.close()
    
    return render_template(
        'teacher.html', 
        questions=questions, 
        subjects=DEFAULT_SUBJECTS, 
        current_subject=current_subject, 
        results_by_subject=results_by_subject
    )

# --- AI OPTION GENERATOR & ANSWER SOLVER ---
import random

@app.route('/teacher/generate-options', methods=['POST'])
def generate_options():
    if not session.get('is_teacher'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json()
    question = data.get('question')
    subject = data.get('subject', 'General')

    if not question:
        return jsonify({'error': 'Please enter a question first.'}), 400

    # Pick a random target option (A, B, C, or D) for every request
    target_letter = random.choice(['A', 'B', 'C', 'D'])

    prompt = f"""
    You are an expert test creator for the subject '{subject}'.
    Given the question: "{question}"

    Generate 4 plausible multiple-choice options (A, B, C, D).
    
    CRITICAL INSTRUCTION: Place the CORRECT answer inside Option {target_letter}.
    Fill the remaining options with realistic incorrect distractors.

    Return ONLY a JSON object with this exact format:
    {{
      "option_a": "Text for Option A",
      "option_b": "Text for Option B",
      "option_c": "Text for Option C",
      "option_d": "Text for Option D",
      "correct_option": "{target_letter}"
    }}
    """

    try:
        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        result = json.loads(response.text)

        return jsonify({
            "option_a": result.get('option_a', ''),
            "option_b": result.get('option_b', ''),
            "option_c": result.get('option_c', ''),
            "option_d": result.get('option_d', ''),
            "correct_option": target_letter
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500
        # --- PUBLISH / HIDE ALL QUESTIONS ---
@app.route('/teacher/toggle-publish', methods=['POST'])
def toggle_publish():
    if not session.get('is_teacher'):
        return redirect(url_for('teacher_login'))
    
    subject = request.form.get('subject')
    action = request.form.get('action')
    new_status = 1 if action == 'publish' else 0
    
    conn = get_db_connection()
    conn.execute('UPDATE questions SET is_published = ? WHERE subject = ?', (new_status, subject))
    conn.commit()
    conn.close()
    
    return redirect(url_for('teacher', subject=subject))

# --- DELETE QUESTION ---
@app.route('/teacher/delete/<int:q_id>', methods=['POST'])
def delete_question(q_id):
    if not session.get('is_teacher'):
        return redirect(url_for('teacher_login'))
    
    subject = request.args.get('subject', '')
    conn = get_db_connection()
    conn.execute('DELETE FROM questions WHERE id = ?', (q_id,))
    conn.commit()
    conn.close()
    
    return redirect(url_for('teacher', subject=subject))
@app.route('/teacher/delete-all-questions', methods=['POST'])
def delete_all_questions():
    if not session.get('is_teacher'):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        # Connect using your app's existing database helper function
        conn = get_db_connection()
        conn.execute("DELETE FROM questions")
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'message': 'All questions deleted successfully!'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
# --- DELETE INDIVIDUAL SUBMISSION RESULT ---
@app.route('/teacher/delete-result/<int:result_id>', methods=['POST'])
def delete_result(result_id):
    if not session.get('is_teacher'):
        return redirect(url_for('teacher_login'))
    
    subject = request.args.get('subject', '')
    conn = get_db_connection()
    conn.execute('DELETE FROM results WHERE id = ?', (result_id,))
    conn.commit()
    conn.close()
    
    return redirect(url_for('teacher', subject=subject))

# --- STUDENT PORTAL ---
@app.route('/student', methods=['GET', 'POST'])
def student():
    current_subject = request.args.get('subject', None)
    questions = []
    
    if current_subject:
        conn = get_db_connection()
        questions = conn.execute(
            'SELECT * FROM questions WHERE subject = ? AND is_published = 1', 
            (current_subject,)
        ).fetchall()
        
        if request.method == 'POST' and questions:
            student_name = request.form.get('student_name')
            roll_no = request.form.get('roll_no')
            student_class = request.form.get('student_class')
            
            score = 0
            total = len(questions)
            
            for q in questions:
                selected = request.form.get(f"question_{q['id']}")
                if selected == q['correct_option']:
                    score += 1
                    
            percentage = (score / total * 100) if total > 0 else 0
            
            conn.execute('''
                INSERT INTO results (student_name, roll_no, student_class, subject, score, total, percentage)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (student_name, roll_no, student_class, current_subject, score, total, round(percentage, 1)))
            conn.commit()
            conn.close()
            
            if percentage >= 80:
                remark = "Great job! Outstanding performance!"
                badge_class = "success"
            elif percentage >= 50:
                remark = "Good effort! Keep practicing to improve further."
                badge_class = "warning"
            else:
                remark = "Needs improvement. Please review the material and try again."
                badge_class = "danger"
                
            return render_template('result.html', score=score, total=total, percentage=round(percentage, 1), remark=remark, badge_class=badge_class, subject=current_subject, student_name=student_name)

        conn.close()

    return render_template('student.html', questions=questions, subjects=DEFAULT_SUBJECTS, current_subject=current_subject)

    import webbrowser
from threading import Timer

def open_browser():
    url = 'http://127.0.0.1:5000/'
    chrome_path = "C:/Program Files/Google/Chrome/Application/chrome.exe %s"
    
    if not os.path.exists("C:/Program Files/Google/Chrome/Application/chrome.exe"):
        chrome_path = "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe %s"
    
    try:
        webbrowser.get(chrome_path).open(url)
    except Exception:
        webbrowser.open_new(url)

if __name__ == '__main__':
    # WERKZEUG_RUN_MAIN ensures the timer only runs in the main worker process, not the reloader
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        Timer(1.2, open_browser).start()
        
    app.run(host='0.0.0.0', port=5000, debug=True)
