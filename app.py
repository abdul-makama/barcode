from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file, Response
import sqlite3
import qrcode
import io
import csv
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'barcodeattendance-poc-2024'
DATABASE = 'attendance.db'


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS students (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            student_id  TEXT UNIQUE NOT NULL,
            course      TEXT,
            email       TEXT,
            created_at  TEXT DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            course       TEXT NOT NULL,
            lecturer     TEXT,
            session_date TEXT NOT NULL,
            created_at   TEXT DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS attendance (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            scan_time  TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(student_id, session_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS scan_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode_value TEXT NOT NULL,
            session_id    INTEGER,
            scan_time     TEXT DEFAULT (datetime('now', 'localtime')),
            status        TEXT NOT NULL,
            message       TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
        );
    ''')
    db.commit()
    db.close()


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    db = get_db()
    stats = {
        'students':   db.execute('SELECT COUNT(*) FROM students').fetchone()[0],
        'sessions':   db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],
        'attendance': db.execute('SELECT COUNT(*) FROM attendance').fetchone()[0],
        'duplicates': db.execute("SELECT COUNT(*) FROM scan_log WHERE status='duplicate'").fetchone()[0],
    }
    recent = db.execute(
        'SELECT * FROM scan_log ORDER BY scan_time DESC LIMIT 15'
    ).fetchall()
    db.close()
    return render_template('index.html', stats=stats, recent=recent)


# ── Students ───────────────────────────────────────────────────────────────────

@app.route('/students')
def students():
    db = get_db()
    rows = db.execute('SELECT * FROM students ORDER BY name').fetchall()
    db.close()
    return render_template('students.html', students=rows)


@app.route('/students/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name       = request.form['name'].strip()
        student_id = request.form['student_id'].strip().upper()
        course     = request.form.get('course', '').strip()
        email      = request.form.get('email', '').strip()
        if not name or not student_id:
            flash('Name and Student ID are required.', 'danger')
            return render_template('register.html')
        db = get_db()
        try:
            db.execute(
                'INSERT INTO students (name, student_id, course, email) VALUES (?, ?, ?, ?)',
                (name, student_id, course, email)
            )
            db.commit()
            flash(f'Student {name} registered successfully.', 'success')
            return redirect(url_for('students'))
        except sqlite3.IntegrityError:
            flash(f'Student ID "{student_id}" is already registered.', 'danger')
        finally:
            db.close()
    return render_template('register.html')


@app.route('/students/<int:sid>/qr.png')
def student_qr(sid):
    db = get_db()
    student = db.execute('SELECT * FROM students WHERE id = ?', (sid,)).fetchone()
    db.close()
    if not student:
        return 'Student not found', 404
    img = qrcode.make(student['student_id'])
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png',
                     download_name=f'{student["student_id"]}_qr.png')


@app.route('/students/<int:sid>/delete', methods=['POST'])
def delete_student(sid):
    db = get_db()
    db.execute('DELETE FROM students WHERE id = ?', (sid,))
    db.commit()
    db.close()
    flash('Student removed.', 'success')
    return redirect(url_for('students'))


# ── Sessions ───────────────────────────────────────────────────────────────────

@app.route('/sessions')
def sessions():
    db = get_db()
    rows = db.execute('''
        SELECT s.*, COUNT(a.id) AS present
        FROM sessions s
        LEFT JOIN attendance a ON a.session_id = s.id
        GROUP BY s.id
        ORDER BY s.session_date DESC, s.created_at DESC
    ''').fetchall()
    db.close()
    return render_template('sessions.html', sessions=rows)


@app.route('/sessions/new', methods=['GET', 'POST'])
def new_session():
    today = datetime.now().strftime('%Y-%m-%d')
    if request.method == 'POST':
        title        = request.form['title'].strip()
        course       = request.form['course'].strip()
        lecturer     = request.form.get('lecturer', '').strip()
        session_date = request.form['session_date']
        if not title or not course or not session_date:
            flash('Title, course, and date are required.', 'danger')
            return render_template('new_session.html', today=today)
        db = get_db()
        cur = db.execute(
            'INSERT INTO sessions (title, course, lecturer, session_date) VALUES (?, ?, ?, ?)',
            (title, course, lecturer, session_date)
        )
        sid = cur.lastrowid
        db.commit()
        db.close()
        flash(f'Session "{title}" created.', 'success')
        return redirect(url_for('attendance', session_id=sid))
    return render_template('new_session.html', today=today)


@app.route('/sessions/<int:session_id>/delete', methods=['POST'])
def delete_session(session_id):
    db = get_db()
    db.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
    db.commit()
    db.close()
    flash('Session deleted.', 'success')
    return redirect(url_for('sessions'))


# ── Attendance Scanning ────────────────────────────────────────────────────────

@app.route('/attendance/<int:session_id>')
def attendance(session_id):
    db = get_db()
    session = db.execute('SELECT * FROM sessions WHERE id = ?', (session_id,)).fetchone()
    if not session:
        flash('Session not found.', 'danger')
        return redirect(url_for('sessions'))
    records = db.execute('''
        SELECT a.scan_time, s.name, s.student_id, s.course
        FROM attendance a
        JOIN students s ON s.student_id = a.student_id
        WHERE a.session_id = ?
        ORDER BY a.scan_time DESC
    ''', (session_id,)).fetchall()
    logs = db.execute(
        'SELECT * FROM scan_log WHERE session_id = ? ORDER BY scan_time DESC LIMIT 30',
        (session_id,)
    ).fetchall()
    db.close()
    return render_template('attendance.html', session=session, records=records, logs=logs)


@app.route('/scan', methods=['POST'])
def scan():
    data       = request.get_json()
    barcode    = (data.get('barcode') or '').strip().upper()
    session_id = data.get('session_id')

    if not barcode or not session_id:
        return jsonify({'status': 'error', 'message': 'Invalid request.'})

    db = get_db()

    student = db.execute(
        'SELECT * FROM students WHERE student_id = ?', (barcode,)
    ).fetchone()

    if not student:
        db.execute(
            'INSERT INTO scan_log (barcode_value, session_id, status, message) VALUES (?, ?, ?, ?)',
            (barcode, session_id, 'unknown', f'Unregistered ID: {barcode}')
        )
        db.commit()
        db.close()
        return jsonify({'status': 'unknown',
                        'message': f'No student registered with ID "{barcode}".'})

    already = db.execute(
        'SELECT id FROM attendance WHERE student_id = ? AND session_id = ?',
        (barcode, session_id)
    ).fetchone()

    if already:
        db.execute(
            'INSERT INTO scan_log (barcode_value, session_id, status, message) VALUES (?, ?, ?, ?)',
            (barcode, session_id, 'duplicate',
             f'Duplicate scan — {student["name"]} ({barcode}) already present')
        )
        db.commit()
        db.close()
        return jsonify({
            'status': 'duplicate',
            'message': f'{student["name"]} is already marked present in this session.',
            'student': dict(student),
        })

    db.execute(
        'INSERT INTO attendance (student_id, session_id) VALUES (?, ?)',
        (barcode, session_id)
    )
    db.execute(
        'INSERT INTO scan_log (barcode_value, session_id, status, message) VALUES (?, ?, ?, ?)',
        (barcode, session_id, 'success', f'{student["name"]} ({barcode}) marked present')
    )
    db.commit()

    count = db.execute(
        'SELECT COUNT(*) FROM attendance WHERE session_id = ?', (session_id,)
    ).fetchone()[0]
    db.close()

    return jsonify({
        'status': 'success',
        'message': f'{student["name"]} marked present.',
        'student': dict(student),
        'count': count,
    })


# ── Export ─────────────────────────────────────────────────────────────────────

@app.route('/sessions/<int:session_id>/export.csv')
def export_csv(session_id):
    db = get_db()
    session = db.execute('SELECT * FROM sessions WHERE id = ?', (session_id,)).fetchone()
    if not session:
        return 'Session not found', 404
    records = db.execute('''
        SELECT s.name, s.student_id, s.course, s.email, a.scan_time
        FROM attendance a
        JOIN students s ON s.student_id = a.student_id
        WHERE a.session_id = ?
        ORDER BY s.name
    ''', (session_id,)).fetchall()
    db.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Name', 'Student ID', 'Course', 'Email', 'Scan Time'])
    for r in records:
        w.writerow([r['name'], r['student_id'], r['course'] or '',
                    r['email'] or '', r['scan_time']])

    fname = f'attendance_{session["course"]}_{session["session_date"]}.csv'.replace(' ', '_')
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'}
    )


# ── Reports ────────────────────────────────────────────────────────────────────

@app.route('/reports')
def reports():
    db = get_db()
    total_students = db.execute('SELECT COUNT(*) FROM students').fetchone()[0]
    sessions = db.execute('''
        SELECT s.*, COUNT(a.id) AS present
        FROM sessions s
        LEFT JOIN attendance a ON a.session_id = s.id
        GROUP BY s.id
        ORDER BY s.session_date DESC
    ''').fetchall()
    duplicates = db.execute('''
        SELECT sl.*, se.title, se.course
        FROM scan_log sl
        LEFT JOIN sessions se ON se.id = sl.session_id
        WHERE sl.status = 'duplicate'
        ORDER BY sl.scan_time DESC
    ''').fetchall()
    db.close()
    return render_template('reports.html', sessions=sessions,
                           duplicates=duplicates, total_students=total_students)


init_db()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
