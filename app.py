from flask import Flask, render_template, request, session, redirect, url_for, jsonify
import json
import os
import re
import random
import sqlite3
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from collections import Counter

app = Flask(__name__)
app.secret_key = 'your_secret_key_here_12345'

DB_FILE = os.path.join(app.root_path, 'interview_coach.db')


def find_chrome_path():
    candidates = [
        os.path.join(os.environ.get('ProgramFiles', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('LocalAppData', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
    ]

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate

    return None


def open_in_chrome(url):
    chrome_path = find_chrome_path()
    if chrome_path:
        subprocess.Popen([chrome_path, url])
        return

    webbrowser.open_new(url)


def clear_interview_session_state():
    for key in [
        'category',
        'current_question',
        'answers',
        'saved_answers',
        'results',
        'active_questions',
        'mixed_questions'
    ]:
        session.pop(key, None)


@app.after_request
def add_no_cache_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    with get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                password TEXT NOT NULL
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS interview_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                category TEXT NOT NULL,
                total_questions INTEGER NOT NULL,
                total_score REAL NOT NULL,
                avg_score REAL NOT NULL,
                timestamp TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS interview_result_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id INTEGER NOT NULL,
                question_order INTEGER NOT NULL,
                question TEXT NOT NULL,
                user_answer TEXT NOT NULL,
                correct_answer TEXT,
                score REAL NOT NULL,
                feedback TEXT NOT NULL,
                missing_keywords TEXT,
                suggestions TEXT,
                FOREIGN KEY (result_id) REFERENCES interview_results(id) ON DELETE CASCADE
            )
        ''')

        # Backward compatible schema migration for existing DBs.
        item_columns = [row['name'] for row in conn.execute("PRAGMA table_info(interview_result_items)").fetchall()]
        if 'correct_answer' not in item_columns:
            conn.execute('ALTER TABLE interview_result_items ADD COLUMN correct_answer TEXT')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS interview_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                question TEXT NOT NULL,
                keywords TEXT,
                answer_hint TEXT,
                options_json TEXT,
                correct_option TEXT
            )
        ''')

        question_columns = [row['name'] for row in conn.execute("PRAGMA table_info(interview_questions)").fetchall()]
        if 'options_json' not in question_columns:
            conn.execute('ALTER TABLE interview_questions ADD COLUMN options_json TEXT')
        if 'correct_option' not in question_columns:
            conn.execute('ALTER TABLE interview_questions ADD COLUMN correct_option TEXT')


def get_user_by_email(email):
    with get_db_connection() as conn:
        row = conn.execute(
            'SELECT email, name, password FROM users WHERE email = ?',
            (email,)
        ).fetchone()
    return dict(row) if row else None


def create_user(name, email, password):
    try:
        with get_db_connection() as conn:
            conn.execute(
                'INSERT INTO users (email, name, password) VALUES (?, ?, ?)',
                (email, name, password)
            )
        return True
    except sqlite3.IntegrityError:
        return False


def normalize_missing_keywords(value):
    if isinstance(value, list):
        return ', '.join(str(item) for item in value)
    if value is None:
        return ''
    return str(value)


def extract_correct_answer(question_data):
    options = question_data.get('options') or {}
    correct_option = (question_data.get('correct_option') or '').strip().upper()
    if options and correct_option in options:
        return f"{correct_option}: {options[correct_option]}"

    answer_hint = (question_data.get('answer_hint') or '').strip()
    if not answer_hint:
        return 'Not available'
    return answer_hint


def save_result(email, result_data):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    with get_db_connection() as conn:
        attempt_number = conn.execute(
            'SELECT COUNT(*) AS count FROM interview_results WHERE user_email = ?',
            (email,)
        ).fetchone()['count'] + 1

        cursor = conn.execute(
            '''
            INSERT INTO interview_results (
                user_email, category, total_questions, total_score, avg_score, timestamp, attempt_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                email,
                result_data.get('category', 'Unknown'),
                result_data.get('total_questions', 0),
                result_data.get('total_score', 0),
                result_data.get('avg_score', 0),
                timestamp,
                attempt_number
            )
        )
        result_id = cursor.lastrowid

        for idx, item in enumerate(result_data.get('results', []), start=1):
            conn.execute(
                '''
                INSERT INTO interview_result_items (
                    result_id, question_order, question, user_answer, score, feedback,
                    correct_answer, missing_keywords, suggestions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    result_id,
                    idx,
                    item.get('question', ''),
                    item.get('user_answer', ''),
                    item.get('score', 0),
                    item.get('feedback', ''),
                    item.get('correct_answer', 'Not available'),
                    normalize_missing_keywords(item.get('missing_keywords', '')),
                    json.dumps(item.get('suggestions', []))
                )
            )


def get_user_results(email):
    with get_db_connection() as conn:
        result_rows = conn.execute(
            '''
            SELECT id, category, total_questions, total_score, avg_score, timestamp, attempt_number
            FROM interview_results
            WHERE user_email = ?
            ORDER BY id ASC
            ''',
            (email,)
        ).fetchall()

        all_results = []
        for row in result_rows:
            item_rows = conn.execute(
                '''
                SELECT question, user_answer, score, feedback, correct_answer, missing_keywords, suggestions
                FROM interview_result_items
                WHERE result_id = ?
                ORDER BY question_order ASC
                ''',
                (row['id'],)
            ).fetchall()

            items = []
            for item in item_rows:
                suggestions_raw = item['suggestions'] or '[]'
                try:
                    suggestions = json.loads(suggestions_raw)
                except json.JSONDecodeError:
                    suggestions = []

                items.append({
                    'question': item['question'],
                    'user_answer': item['user_answer'],
                    'score': item['score'],
                    'feedback': item['feedback'],
                    'correct_answer': item['correct_answer'] if 'correct_answer' in item.keys() else 'Not available',
                    'missing_keywords': item['missing_keywords'],
                    'suggestions': suggestions
                })

            all_results.append({
                'category': row['category'],
                'total_questions': row['total_questions'],
                'total_score': row['total_score'],
                'avg_score': row['avg_score'],
                'timestamp': row['timestamp'],
                'attempt_number': row['attempt_number'],
                'results': items
            })

    return all_results


def get_previously_asked_questions(email, category):
    if not email:
        return set()

    with get_db_connection() as conn:
        rows = conn.execute(
            '''
            SELECT DISTINCT i.question
            FROM interview_result_items i
            INNER JOIN interview_results r ON r.id = i.result_id
            WHERE r.user_email = ? AND r.category = ?
            ''',
            (email, category)
        ).fetchall()

    return {row['question'] for row in rows}


def select_session_questions(all_questions, seen_questions, limit=20):
    unseen_questions = [q for q in all_questions if q.get('question') not in seen_questions]
    seen_pool = [q for q in all_questions if q.get('question') in seen_questions]

    selected_questions = []

    if unseen_questions:
        random.shuffle(unseen_questions)
        selected_questions.extend(unseen_questions[:limit])

    remaining_slots = limit - len(selected_questions)
    if remaining_slots > 0 and seen_pool:
        random.shuffle(seen_pool)
        selected_questions.extend(seen_pool[:remaining_slots])

    if not selected_questions and all_questions:
        random.shuffle(all_questions)
        selected_questions = all_questions[:limit]

    return selected_questions


def load_questions():
    with get_db_connection() as conn:
        rows = conn.execute(
            'SELECT category, question, keywords, answer_hint, options_json, correct_option FROM interview_questions ORDER BY id ASC'
        ).fetchall()

    questions_by_category = {}
    for row in rows:
        category = row['category']
        keywords_raw = row['keywords'] or '[]'
        try:
            keywords = json.loads(keywords_raw)
        except json.JSONDecodeError:
            keywords = []

        options_raw = row['options_json'] or '{}'
        try:
            options = json.loads(options_raw)
        except json.JSONDecodeError:
            options = {}

        if not isinstance(options, dict):
            options = {}

        questions_by_category.setdefault(category, []).append({
            'question': row['question'],
            'keywords': keywords,
            'answer_hint': row['answer_hint'] or '',
            'options': options,
            'correct_option': (row['correct_option'] or '').strip().upper()
        })

    return questions_by_category


def migrate_users_from_json_if_needed():
    users_file = 'users.json'
    with get_db_connection() as conn:
        existing = conn.execute('SELECT COUNT(*) AS count FROM users').fetchone()['count']
        if existing > 0 or not os.path.exists(users_file):
            return

        with open(users_file, 'r') as f:
            users_data = json.load(f)

        for email, user in users_data.items():
            conn.execute(
                'INSERT OR IGNORE INTO users (email, name, password) VALUES (?, ?, ?)',
                (email, user.get('name', ''), user.get('password', ''))
            )


def migrate_results_from_json_if_needed():
    results_file = 'results.json'
    with get_db_connection() as conn:
        existing = conn.execute('SELECT COUNT(*) AS count FROM interview_results').fetchone()['count']
        if existing > 0 or not os.path.exists(results_file):
            return

        with open(results_file, 'r') as f:
            all_results = json.load(f)

        for email, attempts in all_results.items():
            if not isinstance(attempts, list):
                continue

            conn.execute(
                'INSERT OR IGNORE INTO users (email, name, password) VALUES (?, ?, ?)',
                (email, email.split('@')[0], 'migrated_user')
            )

            for idx, result_data in enumerate(attempts, start=1):
                if not isinstance(result_data, dict):
                    continue

                attempt_number_raw = result_data.get('attempt_number', idx)
                try:
                    attempt_number = int(attempt_number_raw)
                except (TypeError, ValueError):
                    attempt_number = idx

                cursor = conn.execute(
                    '''
                    INSERT INTO interview_results (
                        user_email, category, total_questions, total_score, avg_score, timestamp, attempt_number
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        email,
                        result_data.get('category', 'Unknown'),
                        result_data.get('total_questions', 0),
                        result_data.get('total_score', 0),
                        result_data.get('avg_score', 0),
                        result_data.get('timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
                        attempt_number
                    )
                )
                result_id = cursor.lastrowid

                for q_idx, item in enumerate(result_data.get('results', []), start=1):
                    conn.execute(
                        '''
                        INSERT INTO interview_result_items (
                            result_id, question_order, question, user_answer, score, feedback,
                            correct_answer, missing_keywords, suggestions
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                        (
                            result_id,
                            q_idx,
                            item.get('question', ''),
                            item.get('user_answer', ''),
                            item.get('score', 0),
                            item.get('feedback', ''),
                            item.get('correct_answer', 'Not available'),
                            normalize_missing_keywords(item.get('missing_keywords', '')),
                            json.dumps(item.get('suggestions', []))
                        )
                    )


def migrate_questions_from_json_if_needed():
    questions_file = 'questions.json'
    with get_db_connection() as conn:
        existing = conn.execute('SELECT COUNT(*) AS count FROM interview_questions').fetchone()['count']
        if existing > 0 or not os.path.exists(questions_file):
            return

        with open(questions_file, 'r') as f:
            questions_data = json.load(f)

        for category, question_list in questions_data.items():
            for question_data in question_list:
                conn.execute(
                    '''
                    INSERT INTO interview_questions (category, question, keywords, answer_hint)
                    VALUES (?, ?, ?, ?)
                    ''',
                    (
                        category,
                        question_data.get('question', ''),
                        json.dumps(question_data.get('keywords', [])),
                        question_data.get('answer_hint', '')
                    )
                )


def initialize_storage():
    init_db()
    migrate_users_from_json_if_needed()
    migrate_results_from_json_if_needed()
    migrate_questions_from_json_if_needed()


initialize_storage()

# NLP-based evaluation using keyword matching and text analysis
def evaluate_answer(question_data, user_answer):
    """
    Evaluates user answer using NLP techniques:
    - Keyword matching
    - Answer length analysis
    - Concept coverage scoring
    Returns score, feedback, and missing keywords
    """
    options = question_data.get('options') or {}
    correct_option = (question_data.get('correct_option') or '').strip().upper()
    if options and correct_option:
        normalized = user_answer.strip().lower()
        correct_text = str(options.get(correct_option, '')).strip().lower()
        is_correct = normalized == correct_option.lower() or normalized == correct_text

        if is_correct:
            return {
                'score': 10,
                'feedback': 'Correct answer.',
                'missing_keywords': 'None',
                'suggestions': ['Great work!'],
                'found_keywords': 1,
                'total_keywords': 1
            }

        return {
            'score': 0,
            'feedback': 'Incorrect answer.',
            'missing_keywords': 'None',
            'suggestions': [f"Correct answer: {correct_option}: {options.get(correct_option, '')}"],
            'found_keywords': 0,
            'total_keywords': 1
        }

    # Convert to lowercase for comparison
    user_answer_lower = user_answer.lower()
    answer_words = set(re.findall(r'\b\w+\b', user_answer_lower))
    
    # Keyword matching
    keywords = question_data['keywords']
    found_keywords = []
    missing_keywords = []
    
    for keyword in keywords:
        keyword_lower = keyword.lower()
        # Check if keyword or its variations are in the answer
        if keyword_lower in user_answer_lower:
            found_keywords.append(keyword)
        else:
            missing_keywords.append(keyword)
    
    # Calculate base score from keyword coverage
    keyword_score = ((len(found_keywords) / len(keywords)) * 6) if keywords else 0  # Max 6 points
    
    # Length analysis (good answers are usually detailed)
    word_count = len(answer_words)
    if word_count < 10:
        length_score = 1
    elif word_count < 30:
        length_score = 2
    elif word_count < 50:
        length_score = 3
    else:
        length_score = 4  # Max 4 points
    
    # Total score out of 10
    total_score = min(10, keyword_score + length_score)
    
    # Generate feedback based on score
    if total_score >= 8:
        feedback = "Excellent answer! You've covered all the key concepts well."
    elif total_score >= 6:
        feedback = "Good answer! You've covered most key concepts, but there's room for improvement."
    elif total_score >= 4:
        feedback = "Average answer. You've covered some concepts but missed several important points."
    else:
        feedback = "Needs improvement. Your answer is missing many key concepts."
    
    # Generate specific suggestions
    suggestions = []
    
    if missing_keywords:
        suggestions.append(f"Include these important concepts: {', '.join(missing_keywords[:3])}")
    
    if word_count < 30:
        suggestions.append("Provide more detailed explanation")
    
    if keywords and len(found_keywords) < len(keywords) / 2:
        suggestions.append("Focus on the core concepts related to the question")
    
    suggestions.append(f"Hint: {question_data['answer_hint']}")
    
    if not suggestions:
        suggestions.append("Try to add more real-world examples")
    
    return {
        'score': round(total_score, 1),
        'feedback': feedback,
        'missing_keywords': ', '.join(missing_keywords) if missing_keywords else 'None',
        'suggestions': suggestions,
        'found_keywords': len(found_keywords),
        'total_keywords': len(keywords)
    }

@app.route('/')
def index():
    return render_template('signin.html')

@app.route('/signup', methods=['POST'])
def signup():
    name = request.form.get('name')
    email = request.form.get('email')
    password = request.form.get('password')
    confirm_password = request.form.get('confirm_password')
    
    # Validation
    if not name or not email or not password:
        return render_template('signin.html', error='All fields are required')
    
    if password != confirm_password:
        return render_template('signin.html', error='Passwords do not match')
    
    if len(password) < 6:
        return render_template('signin.html', error='Password must be at least 6 characters')
    
    # Check if user already exists
    if get_user_by_email(email):
        return render_template('signin.html', error='Email already registered. Please login.')

    # Store user in SQLite
    create_user(name, email, password)
    
    return render_template('signin.html', success='Account created successfully! Please login.')

@app.route('/login', methods=['POST'])
def login():
    email = request.form.get('email')
    password = request.form.get('password')
    
    # Validation
    if not email or not password:
        return render_template('signin.html', error='Email and password are required')
    
    user = get_user_by_email(email)

    # Check if user exists
    if not user:
        return render_template('signin.html', error='Email not found. Please sign up first.')

    # Check password
    if user['password'] != password:
        return render_template('signin.html', error='Incorrect password')

    # Login successful - Regenerate session to prevent session fixation attacks
    session.clear()  # Clear any existing session data
    session.permanent = True  # Enable session timeout (PERMANENT_SESSION_LIFETIME)
    session['username'] = user['name']
    session['email'] = email
    session['results'] = []
    
    return redirect(url_for('home'))

@app.route('/home')
def home():
    if 'username' not in session or 'email' not in session:
        return redirect(url_for('index'))
    
    email = session['email']
    username = session['username']
    
    # Get all past results
    past_results = get_user_results(email)
    
    # Calculate statistics
    total_attempts = len(past_results)
    
    if total_attempts > 0:
        total_score = sum(r['total_score'] for r in past_results)
        avg_overall = total_score / total_attempts if total_attempts > 0 else 0
        best_score = max(r['avg_score'] for r in past_results)
        
        # Get category-wise average scores
        category_scores = {}
        for result in past_results:
            category = result['category']
            if category not in category_scores:
                category_scores[category] = []
            category_scores[category].append(result['avg_score'])
        
        recent_categories = []
        for category, scores in category_scores.items():
            avg_score = sum(scores) / len(scores)
            recent_categories.append({
                'name': category,
                'avg_score': round(avg_score, 1)
            })
        
        # Get top 4 categories
        recent_categories = sorted(recent_categories, key=lambda x: x['avg_score'], reverse=True)[:4]
        categories_attempted = list(set(r['category'] for r in past_results))
    else:
        avg_overall = 0
        best_score = 0
        categories_attempted = []
        recent_categories = []
    
    return render_template('home.html',
                         username=username,
                         total_attempts=total_attempts,
                         avg_overall=round(avg_overall, 1),
                         best_score=round(best_score, 1),
                         categories_attempted=categories_attempted,
                         recent_categories=recent_categories)

@app.route('/courses')
def courses():
    if 'username' not in session or 'email' not in session:
        return redirect(url_for('index'))
    
    return render_template('courses.html')

@app.route('/faq')
def faq():
    if 'username' not in session or 'email' not in session:
        return redirect(url_for('index'))
    
    return render_template('faq.html')

@app.route('/company-questions')
def company_questions():
    if 'username' not in session or 'email' not in session:
        return redirect(url_for('index'))
    
    return render_template('company_questions.html')

@app.route('/learn/<category>')
def learn(category):
    if 'username' not in session or 'email' not in session:
        return redirect(url_for('index'))
    
    # Define comprehensive learning content for each course
    course_data = {
        'Python': {
            'name': 'Python Programming',
            'icon': '🐍',
            'description': 'Master Python concepts for technical interviews',
            'full_description': 'Python is one of the most popular programming languages for interviews. This course covers everything from basic syntax to advanced OOP concepts, functional programming paradigms, and real-world applications. You\'ll learn best practices used in industry and gain deep understanding of Python\'s core mechanisms.',
            'topics': [
                'Variables, Data Types & Basic Syntax',
                'Control Flow (if, for, while loops)',
                'Functions, Lambda & Closures',
                'Object-Oriented Programming (OOP)',
                'List Comprehensions & Generators',
                'Decorators & Function Composition',
                'Exception Handling & Error Management',
                'File I/O & Working with Files',
                'Regular Expressions (Regex)',
                'Modules, Packages & Imports'
            ],
            'outcomes': [
                'Write efficient Python code',
                'Master OOP principles',
                'Handle errors properly',
                'Use advanced Python features'
            ],
            'concepts': [
                {
                    'title': 'List vs Tuple',
                    'description': 'Lists are mutable (can be modified) while tuples are immutable (cannot be changed after creation). This affects performance and use cases.',
                    'example': 'lst = [1, 2, 3]  # Mutable\ntpl = (1, 2, 3)  # Immutable\nlst[0] = 10  # OK\n# tpl[0] = 10  # Error!'
                },
                {
                    'title': 'Decorators',
                    'description': 'Decorators are functions that modify or enhance other functions without permanently changing their source code.',
                    'example': '@decorator\ndef function():\n    pass'
                },
                {
                    'title': 'List Comprehensions',
                    'description': 'Concise way to create lists based on existing lists or ranges.',
                    'example': 'squares = [x**2 for x in range(10)]\nfiltered = [x for x in range(10) if x % 2 == 0]'
                }
            ],
            'resources': [
                {'icon': '📖', 'title': 'Python Documentation', 'description': 'Official Python docs with comprehensive examples', 'url': 'https://docs.python.org/3/'},
                {'icon': '💻', 'title': 'LeetCode - Practice Problems', 'description': 'Solve algorithmic problems by difficulty and topic.', 'url': 'https://leetcode.com/'},
                {'icon': '💻', 'title': 'HackerRank - Practice Challenges', 'description': 'Practice coding challenges and company-specific prep.', 'url': 'https://www.hackerrank.com/'},
                {'icon': '📺', 'title': 'Corey Schafer - Python Playlist', 'description': 'Comprehensive and practical Python tutorials.', 'url': 'https://www.youtube.com/playlist?list=PL-osiE80TeTt2d9bfVyTiXJA-UTHn6WwU'},
                {'icon': '📺', 'title': 'freeCodeCamp - Python Full Course', 'description': 'Beginner-friendly full Python course video.', 'url': 'https://www.youtube.com/watch?v=rfscVS0vtbw'},
                {'icon': '📺', 'title': 'Sentdex - Practical Python Tutorials', 'description': 'Hands-on Python projects and tutorials.', 'url': 'https://www.youtube.com/user/sentdex'}
            ],
            'tips': [
                'Practice writing clean and readable code',
                'Understand the difference between mutable and immutable types',
                'Learn to use Python built-in functions efficiently',
                'Master list comprehensions and generators',
                'Practice exception handling and debugging'
            ]
        },
        'Java': {
            'name': 'Java Fundamentals',
            'icon': '☕',
            'description': 'Learn Java programming with OOP focus',
            'full_description': 'Java is a widely-used language in enterprise environments. This course teaches OOP principles, design patterns, and Java-specific features. You\'ll understand how Java works behind the scenes and learn best practices for writing scalable applications.',
            'topics': [
                'Classes & Objects',
                'Inheritance & Polymorphism',
                'Interfaces & Abstract Classes',
                'Collections Framework (List, Set, Map)',
                'Exception Handling & Custom Exceptions',
                'Multithreading & Concurrency',
                'Java Streams & Lambda Expressions',
                'File I/O & Serialization',
                'JDBC & Database Connectivity',
                'Design Patterns (Singleton, Factory, etc.)'
            ],
            'outcomes': [
                'Build robust Java applications',
                'Design with SOLID principles',
                'Handle concurrent programming',
                'Work with enterprise patterns'
            ],
            'concepts': [
                {
                    'title': 'Inheritance',
                    'description': 'Mechanism for a class to acquire properties from another class, promoting code reuse.',
                    'example': 'class Animal {}\nclass Dog extends Animal {}'
                },
                {
                    'title': 'Polymorphism',
                    'description': 'Ability of objects to take multiple forms. Enables method overriding and overloading.',
                    'example': 'Animal animal = new Dog();'
                },
                {
                    'title': 'Collections',
                    'description': 'Framework providing interfaces and classes for storing and manipulating groups of objects.',
                    'example': 'List<String> list = new ArrayList<>();\nSet<Integer> set = new HashSet<>();'
                }
            ],
            'resources': [
                {'icon': '📖', 'title': 'Oracle Java Docs', 'description': 'Complete Java API documentation', 'url': 'https://docs.oracle.com/javase/'},
                {'icon': '💻', 'title': 'LeetCode - Java', 'description': 'Practice Java coding problems with solutions', 'url': 'https://leetcode.com/problems/?topicTags=java'},
                {'icon': '💻', 'title': 'HackerRank - Java', 'description': 'Interactive Java challenges and tutorials', 'url': 'https://www.hackerrank.com/domains/java'},
                {'icon': '🎬', 'title': 'Java Tutorials - YouTube', 'description': 'Comprehensive Java video tutorials for all levels', 'url': 'https://www.youtube.com/results?search_query=java+programming+tutorial'},
                {'icon': '🎓', 'title': 'Online Courses', 'description': 'Structured learning from Coursera and Udacity', 'url': 'https://www.coursera.org/courses?query=java'}
            ],
            'tips': [
                'Understand OOP principles deeply',
                'Learn the Collections framework well',
                'Practice writing thread-safe code',
                'Study design patterns and when to use them',
                'Work on small projects to reinforce concepts'
            ]
        },
        'SQL': {
            'name': 'SQL & Databases',
            'icon': '🗄️',
            'description': 'Master SQL queries and database design',
            'full_description': 'SQL is essential for working with databases. This course covers query optimization, database design principles, normalization, and advanced SQL techniques. You\'ll learn to write efficient queries and understand database architecture.',
            'topics': [
                'SELECT & WHERE Clauses',
                'JOIN Operations (INNER, LEFT, RIGHT, FULL)',
                'Subqueries & Nested Queries',
                'Aggregation Functions (SUM, AVG, COUNT, etc.)',
                'GROUP BY & HAVING Clauses',
                'Database Design & Normalization (1NF to 3NF)',
                'Indexes & Query Performance',
                'Transactions & ACID Properties',
                'Stored Procedures & Triggers',
                'Query Optimization Techniques'
            ],
            'outcomes': [
                'Write efficient SQL queries',
                'Design normalized databases',
                'Optimize query performance',
                'Master advanced SQL concepts'
            ],
            'concepts': [
                {
                    'title': 'JOINs',
                    'description': 'Combine rows from multiple tables based on related columns.',
                    'example': 'SELECT * FROM table1\nINNER JOIN table2 ON table1.id = table2.id'
                },
                {
                    'title': 'Normalization',
                    'description': 'Process of organizing data to minimize redundancy and dependency.',
                    'example': 'First Normal Form (1NF), Second Normal Form (2NF), Third Normal Form (3NF)'
                },
                {
                    'title': 'Indexes',
                    'description': 'Data structures that improve query performance for faster data retrieval.',
                    'example': 'CREATE INDEX idx_name ON table(column);'
                }
            ],
            'resources': [
                {'icon': '📖', 'title': 'SQL Documentation', 'description': 'Language reference and best practices', 'url': 'https://www.w3schools.com/sql/'},
                {'icon': '💻', 'title': 'LeetCode - SQL', 'description': 'Practice SQL queries by difficulty level', 'url': 'https://leetcode.com/problems/?topicTags=database'},
                {'icon': '💻', 'title': 'HackerRank - SQL', 'description': 'Interactive SQL challenges and tutorials', 'url': 'https://www.hackerrank.com/domains/sql'},
                {'icon': '🎬', 'title': 'SQL Tutorials - YouTube', 'description': 'Comprehensive SQL video tutorials', 'url': 'https://www.youtube.com/results?search_query=sql+tutorial'},
                {'icon': '🔧', 'title': 'Database Tools', 'description': 'Learn MySQL, PostgreSQL, or SQL Server', 'url': 'https://www.mysql.com/'}
            ],
            'tips': [
                'Understand JOIN operations thoroughly',
                'Learn database normalization principles',
                'Practice writing complex queries',
                'Study query execution plans',
                'Focus on performance optimization'
            ]
        },
        'C': {
            'name': 'C Programming',
            'icon': '⚙️',
            'description': 'Deep dive into systems programming with C',
            'full_description': 'C is a powerful language that gives you direct access to memory. This course covers pointers, memory management, and system-level concepts. Understanding C deeply helps you become a better programmer across all languages.',
            'topics': [
                'Variables, Data Types & Storage Classes',
                'Pointers & References',
                'Arrays & Strings',
                'Structures & Unions',
                'Dynamic Memory Allocation (malloc, free)',
                'File I/O Operations',
                'Function Pointers & Callbacks',
                'Bitwise Operations & Bit Manipulation',
                'Preprocessor Directives (#define, #include)',
                'Memory Management & Debugging'
            ],
            'outcomes': [
                'Master pointer manipulation',
                'Manage memory efficiently',
                'Understand low-level programming',
                'Write optimized C code'
            ],
            'concepts': [
                {
                    'title': 'Pointers',
                    'description': 'Variables that store memory addresses. Fundamental to C programming.',
                    'example': 'int x = 5;\nint *ptr = &x;  // Pointer to x\nprintf("%d", *ptr);  // Dereference'
                },
                {
                    'title': 'Dynamic Memory',
                    'description': 'Allocate memory at runtime using malloc and free.',
                    'example': 'int *arr = (int*)malloc(10 * sizeof(int));\nfree(arr);'
                },
                {
                    'title': 'Structures',
                    'description': 'Group multiple variables of different types together.',
                    'example': 'struct Person {\n  char name[50];\n  int age;\n};'
                }
            ],
            'resources': [
                {'icon': '📖', 'title': 'C Programming Guide', 'description': 'Comprehensive C reference', 'url': 'https://www.cprogramming.com/'},
                {'icon': '💻', 'title': 'LeetCode - C', 'description': 'Practice C coding problems', 'url': 'https://leetcode.com/problems/?topicTags=c'},
                {'icon': '💻', 'title': 'HackerRank - C', 'description': 'Interactive C challenges', 'url': 'https://www.hackerrank.com/domains/c'},
                {'icon': '🎬', 'title': 'C Tutorials - YouTube', 'description': 'Video tutorials on C programming', 'url': 'https://www.youtube.com/results?search_query=c+programming+tutorial'},
                {'icon': '🔬', 'title': 'Debugging Tools', 'description': 'Learn GDB and Valgrind', 'url': 'https://www.gnu.org/software/gdb/'}
            ],
            'tips': [
                'Master pointers before moving to advanced topics',
                'Always free allocated memory to avoid leaks',
                'Learn to use debugging tools',
                'Understand memory layout and stack vs heap',
                'Practice low-level bit manipulation'
            ]
        },
        'AI': {
            'name': 'Artificial Intelligence',
            'icon': '🤖',
            'description': 'Explore AI concepts and algorithms',
            'full_description': 'AI is transforming technology. This course covers fundamental AI concepts, machine learning algorithms, neural networks, and practical applications. You\'ll understand how AI systems learn and make decisions.',
            'topics': [
                'AI Fundamentals & History',
                'Supervised Learning (Classification & Regression)',
                'Unsupervised Learning (Clustering & Dimensionality)',
                'Neural Networks & Deep Learning',
                'Backpropagation & Training',
                'Natural Language Processing (NLP)',
                'Computer Vision & Image Processing',
                'Reinforcement Learning Basics',
                'Feature Engineering & Selection',
                'Model Evaluation & Validation'
            ],
            'outcomes': [
                'Understand AI concepts deeply',
                'Build and train neural networks',
                'Apply AI to real-world problems',
                'Evaluate AI model performance'
            ],
            'concepts': [
                {
                    'title': 'Neural Networks',
                    'description': 'Computational models inspired by biological neurons. Used for complex pattern recognition.',
                    'example': 'Input Layer -> Hidden Layers -> Output Layer'
                },
                {
                    'title': 'Supervised Learning',
                    'description': 'Learning from labeled data where the target output is known.',
                    'example': 'Classification: predicting categories\nRegression: predicting continuous values'
                },
                {
                    'title': 'Backpropagation',
                    'description': 'Algorithm for training neural networks by adjusting weights based on errors.',
                    'example': 'Forward pass -> Calculate loss -> Backward pass -> Update weights'
                }
            ],
            'resources': [
                {'icon': '📚', 'title': 'AI Research Papers', 'description': 'Read latest AI innovations', 'url': 'https://arxiv.org/list/cs.AI/recent'},
                {'icon': '💻', 'title': 'LeetCode - AI/ML Problems', 'description': 'Practice AI/ML coding challenges', 'url': 'https://leetcode.com/problems/?topicTags=machine-learning'},
                {'icon': '🎬', 'title': 'AI Tutorials - YouTube', 'description': 'Comprehensive AI and deep learning tutorials', 'url': 'https://www.youtube.com/results?search_query=artificial+intelligence+tutorial'},
                {'icon': '🐍', 'title': 'TensorFlow & PyTorch', 'description': 'Popular AI frameworks', 'url': 'https://www.tensorflow.org/'},
                {'icon': '📊', 'title': 'AI Datasets', 'description': 'Practice with real datasets', 'url': 'https://www.kaggle.com/datasets'}
            ],
            'tips': [
                'Start with linear models before neural networks',
                'Understand the math behind algorithms',
                'Practice with real datasets',
                'Learn data preprocessing and cleaning',
                'Study regularization to avoid overfitting'
            ]
        },
        'ML': {
            'name': 'Machine Learning',
            'icon': '📊',
            'description': 'Master machine learning algorithms',
            'full_description': 'Machine Learning enables systems to learn from data. This course covers supervised and unsupervised learning, model evaluation, and hyperparameter tuning. You\'ll learn to build models that make accurate predictions.',
            'topics': [
                'Linear Regression & Polynomial Regression',
                'Logistic Regression & Classification',
                'Decision Trees & Random Forests',
                'Support Vector Machines (SVM)',
                'K-Means Clustering & Hierarchical Clustering',
                'Feature Scaling & Normalization',
                'Feature Selection & Extraction',
                'Cross-Validation & Model Selection',
                'Ensemble Methods (Bagging, Boosting)',
                'Hyperparameter Tuning & Grid Search'
            ],
            'outcomes': [
                'Build accurate predictive models',
                'Choose appropriate algorithms',
                'Tune models for better performance',
                'Avoid overfitting and underfitting'
            ],
            'concepts': [
                {
                    'title': 'Decision Trees',
                    'description': 'Tree-like model for classification and regression. Easy to understand but can overfit.',
                    'example': 'Root Node -> Decision Nodes -> Leaf Nodes'
                },
                {
                    'title': 'Cross-Validation',
                    'description': 'Technique to evaluate model performance using different data splits.',
                    'example': 'K-Fold Cross-Validation (typically k=5 or k=10)'
                },
                {
                    'title': 'Ensemble Methods',
                    'description': 'Combining multiple models to get better performance than individual models.',
                    'example': 'Random Forest, Gradient Boosting, AdaBoost'
                }
            ],
            'resources': [
                {'icon': '🔬', 'title': 'Scikit-Learn', 'description': 'Python ML library with many algorithms', 'url': 'https://scikit-learn.org/'},
                {'icon': '📈', 'title': 'Kaggle Competitions', 'description': 'Compete in ML competitions and learn', 'url': 'https://www.kaggle.com/competitions'},
                {'icon': '💻', 'title': 'LeetCode - ML Problems', 'description': 'Practice ML coding challenges', 'url': 'https://leetcode.com/'},
                {'icon': '🎬', 'title': 'Machine Learning - YouTube', 'description': 'Comprehensive ML video tutorials', 'url': 'https://www.youtube.com/results?search_query=machine+learning+tutorial'},
                {'icon': '📊', 'title': 'Data Visualization', 'description': 'Understand data with matplotlib and seaborn', 'url': 'https://matplotlib.org/'}
            ],
            'tips': [
                'Start with exploratory data analysis',
                'Always split data into train/test sets',
                'Normalize features when needed',
                'Use cross-validation for reliable estimates',
                'Monitor for overfitting and underfitting'
            ]
        },
        'FullStack': {
            'name': 'Full Stack Development',
            'icon': '🌐',
            'description': 'Build complete web applications',
            'full_description': 'Full Stack Development involves building complete web applications from frontend to backend to database. This course covers modern web technologies and best practices for creating scalable applications.',
            'topics': [
                'HTML5 & Semantic Markup',
                'CSS3 & Responsive Design',
                'JavaScript & ES6+ Features',
                'DOM Manipulation & Events',
                'Frontend Frameworks (React/Angular/Vue)',
                'Backend Frameworks (Node/Django/Flask)',
                'RESTful API Design & Development',
                'Database Design & SQL',
                'Authentication & Authorization',
                'Deployment & DevOps Basics'
            ],
            'outcomes': [
                'Build responsive web interfaces',
                'Create robust backend servers',
                'Design efficient APIs',
                'Deploy applications to production'
            ],
            'concepts': [
                {
                    'title': 'REST APIs',
                    'description': 'Architectural style for building web services using standard HTTP methods.',
                    'example': 'GET /users (fetch)\nPOST /users (create)\nPUT /users/1 (update)\nDELETE /users/1 (delete)'
                },
                {
                    'title': 'Frontend Frameworks',
                    'description': 'Libraries like React provide structure and reusable components for UI development.',
                    'example': 'Component-based architecture\nState management\nVirtual DOM'
                },
                {
                    'title': 'Authentication',
                    'description': 'Verifying user identity using tokens or sessions.',
                    'example': 'JWT tokens, Session cookies, OAuth 2.0'
                }
            ],
            'resources': [
                {'icon': '�', 'title': 'MDN Web Docs', 'description': 'Comprehensive web development reference', 'url': 'https://developer.mozilla.org/'},
                {'icon': '💻', 'title': 'LeetCode - Web Dev', 'description': 'Practice web development coding challenges', 'url': 'https://leetcode.com/'},
                {'icon': '🎬', 'title': 'Full Stack - YouTube', 'description': 'Comprehensive Full Stack development tutorials', 'url': 'https://www.youtube.com/results?search_query=full+stack+web+development+tutorial'},
                {'icon': '🛠️', 'title': 'Development Tools', 'description': 'VS Code, Git, Docker, and more', 'url': 'https://code.visualstudio.com/'},
                {'icon': '🚀', 'title': 'Hosting Platforms', 'description': 'Deploy on Heroku, AWS, or Vercel', 'url': 'https://www.heroku.com/'}
            ],
            'tips': [
                'Master vanilla JavaScript before frameworks',
                'Learn responsive design principles',
                'Understand HTTP and how the web works',
                'Practice building projects end-to-end',
                'Learn version control with Git'
            ]
        }
    }
    
    if category not in course_data:
        return redirect(url_for('courses'))
    
    return render_template('learning.html', course_data=course_data, category=category)

@app.route('/dashboard')
def dashboard():
    if 'username' not in session or 'email' not in session:
        return redirect(url_for('index'))
    
    email = session['email']
    username = session['username']
    
    # Get all past results
    past_results = get_user_results(email)
    
    # Calculate statistics
    total_attempts = len(past_results)
    
    if total_attempts > 0:
        total_score = sum(r['total_score'] for r in past_results)
        avg_overall = total_score / total_attempts if total_attempts > 0 else 0
        best_score = max(r['avg_score'] for r in past_results)
        
        # Count categories attempted
        categories_attempted = list(set(r['category'] for r in past_results))
    else:
        avg_overall = 0
        best_score = 0
        categories_attempted = []
    
    return render_template('dashboard.html',
                         username=username,
                         total_attempts=total_attempts,
                         avg_overall=round(avg_overall, 1),
                         best_score=round(best_score, 1),
                         categories_attempted=categories_attempted,
                         past_results=past_results)

@app.route('/categories')
def categories():
    if 'username' not in session:
        return redirect(url_for('index'))
    return render_template('categories.html', username=session['username'])


@app.route('/practice_tests')
def practice_tests():
    if 'username' not in session:
        return redirect(url_for('index'))
    return render_template('practice_tests.html', username=session['username'])


@app.route('/attempt_interview')
def attempt_interview():
    if 'username' not in session:
        return redirect(url_for('index'))
    return render_template('attempt_interview.html', username=session['username'])


@app.route('/start_interview', methods=['POST'])
def start_interview():
    category = request.form.get('category')
    if category:
        clear_interview_session_state()
        questions_data = load_questions()
        session_email = session.get('email')

        # If it's "All_Sections", mix questions from multiple categories
        if category == 'All_Sections':
            all_questions = []
            
            # Combine questions from different sections
            section_categories = ['Aptitude', 'Verbal', 'HR']
            for section in section_categories:
                if section in questions_data:
                    all_questions.extend(questions_data[section])
            
            # Shuffle and limit to a 20-question session.
            random.shuffle(all_questions)
            all_questions = all_questions[:20]
            
            # Store active questions in session
            session['active_questions'] = all_questions
            session['category'] = 'All_Sections'
        else:
            if category not in questions_data:
                return redirect(url_for('categories'))
            category_questions = questions_data[category]

            seen_questions = get_previously_asked_questions(session_email, category)
            selected_questions = select_session_questions(category_questions, seen_questions, limit=20)

            session['active_questions'] = selected_questions
            session['category'] = category
        
        session['current_question'] = 0
        session['answers'] = []
        session['saved_answers'] = {}
        return redirect(url_for('interview'))
    return redirect(url_for('categories'))

@app.route('/save_answer', methods=['POST'])
def save_answer():
    """Save answer to session without evaluating it"""
    user_answer = request.form.get('answer')
    
    if 'username' not in session or 'category' not in session:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
    
    current_q = session.get('current_question', 0)
    
    # Save any answer input (including short option-style answers like "A").
    session['saved_answers'] = session.get('saved_answers', {})
    session['saved_answers'][str(current_q)] = user_answer or ''
    session.modified = True
    return jsonify({'success': True, 'message': 'Answer saved successfully'})

@app.route('/interview')
def interview():
    if 'username' not in session or 'category' not in session:
        return redirect(url_for('index'))
    
    questions_data = load_questions()
    category = session['category']
    current_q = session.get('current_question', 0)
    
    # Prefer the per-session sampled question set when available.
    if 'active_questions' in session:
        questions = session['active_questions']
    else:
        # Backward compatibility for old sessions that may still carry mixed_questions.
        if category == 'All_Sections' and 'mixed_questions' in session:
            questions = session['mixed_questions']
        else:
            if category not in questions_data:
                return redirect(url_for('categories'))
            questions = questions_data[category]

    if not questions:
        return redirect(url_for('categories'))

    if current_q >= len(questions):
        return redirect(url_for('results'))

    question = questions[current_q]

    # Load saved answer if exists
    saved_answers = session.get('saved_answers', {})
    saved_answer = saved_answers.get(str(current_q), '')

    response = render_template('interview.html', 
                         question=question['question'],
                         question_options=question.get('options', {}),
                         question_num=current_q + 1,
                         total_questions=len(questions),
                         category=category,
                         saved_answer=saved_answer)
    return response


@app.route('/submit_answer', methods=['POST'])
def submit_answer():
    try:
        user_answer = request.form.get('answer', '')
        action = request.form.get('action')  # skip, next, previous
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        
        # Validate session
        if 'username' not in session or 'category' not in session:
            if is_ajax:
                return jsonify({'error': 'Session expired'}), 401
            return redirect(url_for('index'))
        
        questions_data = load_questions()
        category = session['category']
        current_q = session.get('current_question', 0)
        
        # Prefer the sampled per-session question set.
        if 'active_questions' in session:
            questions = session['active_questions']
        # Backward compatibility for existing All_Sections sessions.
        elif category == 'All_Sections':
            if 'mixed_questions' not in session:
                # Regenerate if lost
                all_questions = []
                for cat in ['Aptitude', 'Verbal', 'HR', 'Python']:
                    if cat in questions_data:
                        all_questions.extend(questions_data[cat])
                random.shuffle(all_questions)
                session['mixed_questions'] = all_questions[:20]
                session.modified = True
            questions = session['mixed_questions']
        else:
            if category not in questions_data:
                if is_ajax:
                    return jsonify({'error': 'Invalid category'}), 400
                return redirect(url_for('categories'))
            questions = questions_data[category]

        if not questions:
            if is_ajax:
                return jsonify({'error': 'No questions available'}), 400
            return redirect(url_for('categories'))
        
        # Validate question index
        if current_q < 0 or current_q >= len(questions):
            if is_ajax:
                return jsonify({'error': 'Invalid question index'}), 400
            return redirect(url_for('home'))
        
        question_data = questions[current_q]
        
        # Save current answer to session for skip, next, and previous actions
        if user_answer and user_answer.strip():
            session['saved_answers'] = session.get('saved_answers', {})
            session['saved_answers'][str(current_q)] = user_answer
            session.modified = True
        
        # Handle "Previous" - go to previous question
        if action == 'previous':
            if current_q > 0:
                # Go back to previous question
                session['current_question'] = current_q - 1
                session.modified = True
            # If AJAX, return next question payload
            if is_ajax:
                next_q = session['current_question']
                next_question = questions[next_q]
                saved_answers = session.get('saved_answers', {})
                payload = {
                    'finished': False,
                    'question': next_question['question'],
                    'question_num': next_q + 1,
                    'total_questions': len(questions),
                    'saved_answer': saved_answers.get(str(next_q), '')
                }
                return jsonify(payload)
            return redirect(url_for('interview'))
        
        # Initialize results if not present
        if 'results' not in session:
            session['results'] = []
        
        result = None
        
        # Check if user skipped the question
        if action == 'skip':
            # Store skipped result
            result = {
                'question': question_data['question'],
                'user_answer': 'Skipped',
                'correct_answer': extract_correct_answer(question_data),
                'score': 0,
                'feedback': 'Question was skipped. Consider attempting it for better results.',
                'missing_keywords': ', '.join(question_data.get('keywords', [])),
                'suggestions': ['Try to attempt all questions', 'Review the question and key concepts']
            }
        elif action == 'next':
            # User moved to next question - evaluate if answer exists
            if user_answer and user_answer.strip():
                # Evaluate the answer
                evaluation = evaluate_answer(question_data, user_answer)
                result = {
                    'question': question_data['question'],
                    'user_answer': user_answer,
                    'correct_answer': extract_correct_answer(question_data),
                    'score': evaluation['score'],
                    'feedback': evaluation['feedback'],
                    'missing_keywords': evaluation['missing_keywords'],
                    'suggestions': evaluation['suggestions']
                }
            else:
                # No answer provided
                result = {
                    'question': question_data['question'],
                    'user_answer': 'No answer provided',
                    'correct_answer': extract_correct_answer(question_data),
                    'score': 0,
                    'feedback': 'No answer was provided. Please attempt the question next time.',
                    'missing_keywords': ', '.join(question_data.get('keywords', [])),
                    'suggestions': ['Attempt to answer all questions', 'Refer to learning materials']
                }
        else:
            if is_ajax:
                return jsonify({'error': 'Invalid action'}), 400
            return redirect(url_for('interview'))
        
        # Append result and move to next question
        if result is None:
            if is_ajax:
                return jsonify({'error': 'No result generated'}), 400
            return redirect(url_for('interview'))
        
        session['results'].append(result)
        session['current_question'] = current_q + 1
        session.modified = True

        # Check if there are more questions
        if session['current_question'] >= len(questions):
            if is_ajax:
                return jsonify({'finished': True, 'redirect': url_for('results')})
            return redirect(url_for('results'))

        # If AJAX, return the next question payload to allow client-side navigation without reload
        if is_ajax:
            next_q = session['current_question']
            next_question = questions[next_q]
            saved_answers = session.get('saved_answers', {})
            payload = {
                'finished': False,
                'question': next_question['question'],
                'question_num': next_q + 1,
                'total_questions': len(questions),
                'saved_answer': saved_answers.get(str(next_q), '')
            }
            return jsonify(payload)

        return redirect(url_for('interview'))
    
    except Exception as e:
        print(f"[ERROR] Exception in submit_answer: {str(e)}")
        import traceback
        traceback.print_exc()
        if is_ajax:
            return jsonify({'error': 'Server error occurred'}), 500
        return redirect(url_for('home'))

@app.route('/results')
def results():
    if 'username' not in session or 'results' not in session:
        return redirect(url_for('index'))
    
    results_data = session['results']
    total_score = sum(r['score'] for r in results_data)
    avg_score = total_score / len(results_data) if results_data else 0
    
    # Prepare result data to save
    result_summary = {
        'category': session.get('category', 'Unknown'),
        'total_questions': len(results_data),
        'total_score': round(total_score, 1),
        'avg_score': round(avg_score, 1),
        'results': results_data
    }
    
    # Save to file
    if 'email' in session:
        save_result(session['email'], result_summary)
    
    return render_template('results.html',
                         username=session['username'],
                         results=results_data,
                         total_score=round(total_score, 1),
                         avg_score=round(avg_score, 1),
                         total_questions=len(results_data))


@app.route('/quit_interview')
def quit_interview():
    clear_interview_session_state()
    if 'username' in session:
        return redirect(url_for('home'))
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.clear()
    response = redirect(url_for('index'))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/attempt_details', methods=['POST'])
def attempt_details():
    if 'username' not in session or 'email' not in session:
        return redirect(url_for('index'))
    
    attempt_index = int(request.form.get('attempt_index', 0))
    email = session['email']
    
    # Get all past results
    user_results = get_user_results(email)
    
    # Get the specific attempt (reverse order to match dashboard display)
    user_results_reversed = list(reversed(user_results))
    
    if attempt_index < len(user_results_reversed):
        result = user_results_reversed[attempt_index]
        return render_template('attempt_details.html', result=result)
    
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    threading.Thread(target=lambda: (time.sleep(1.5), open_in_chrome('http://127.0.0.1:5000')), daemon=True).start()
    app.run(debug=True, use_reloader=False)