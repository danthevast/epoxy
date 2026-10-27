"""Commercial flooring inquiry matching. No contractor delivery is implemented."""
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit

import click
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import SecurityError
from werkzeug.security import check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

ROOT = Path(__file__).resolve().parent
TYPES = ('Warehouse / industrial', 'Retail / showroom', 'Restaurant / commercial kitchen',
         'Healthcare / education', 'Office / commercial space', 'Other commercial project')
TIMES = ('As soon as practical', 'Within 1–3 months', 'Within 3–6 months', 'Planning / researching')
DEFAULT_ZIPS = '63011,63017,63021,63026,63031,63033,63034,63042,63043,63044,63049,63069,63074,63088,63101,63102,63103,63104,63105,63106,63107,63108,63109,63110,63111,63112,63113,63114,63115,63116,63117,63118,63119,63120,63121,63122,63123,63124,63125,63126,63127,63128,63129,63130,63131,63132,63133,63134,63135,63136,63137,63138,63139,63140,63141,63143,63144,63146,63301,63303,63304,63366,63367,63368,63376,63385'
CONSENT = ('I ask STL Floor Match to review my inquiry and share these details with independently operated '
           'commercial flooring contractors serving my project area, only after operator approval. '
           'I agree to contact about this request; I am not consenting to marketing calls or texts.')
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS leads (
 id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, created REAL NOT NULL, payload BLOB NOT NULL,
 qualified INTEGER NOT NULL, reasons TEXT NOT NULL, variant TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending_approval');
CREATE TABLE IF NOT EXISTS contacts (
 lead_id TEXT REFERENCES leads(id) ON DELETE CASCADE, fingerprint TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS contact_lookup ON contacts(fingerprint);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, lead_id TEXT UNIQUE REFERENCES leads(id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'pending', sent_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
 name TEXT NOT NULL, variant TEXT NOT NULL, visitor TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(name,variant,visitor));
CREATE TABLE IF NOT EXISTS rate_limits (
 scope TEXT, identity TEXT, bucket INTEGER, count INTEGER, expires REAL,
 PRIMARY KEY(scope,identity,bucket));
"""


def create_app(overrides=None):
    load_dotenv(ROOT / '.env', override=False)
    app = Flask(__name__)
    production = os.getenv('APP_ENV') == 'production'
    app.config.update(
        SECRET_KEY=os.getenv('SECRET_KEY'), LEAD_KEY=os.getenv('LEAD_ENCRYPTION_KEY'),
        DATABASE=os.getenv('DATABASE_PATH', str(ROOT / 'instance' / 'leads.sqlite3')),
        ADMIN_PASSWORD_HASH=os.getenv('ADMIN_PASSWORD_HASH'),
        SITE_NAME=os.getenv('SITE_NAME', 'STL Floor Match'),
        PRIVACY_EMAIL=os.getenv('PRIVACY_EMAIL', ''),
        LEGAL_OPERATOR=os.getenv('LEGAL_OPERATOR', ''),
        REGION=os.getenv('SITE_REGION', 'St. Louis, Missouri'),
        MIN_SQFT=int(os.getenv('MIN_SQFT', '500')),
        TRUST_PROXY_HOPS=int(os.getenv('TRUST_PROXY_HOPS', '0')),
        PUBLIC_URL=os.getenv('PUBLIC_URL', 'http://127.0.0.1:8000').rstrip('/'),
        SERVICE_ZIPS=os.getenv('SERVICE_ZIPS', DEFAULT_ZIPS).split(','),
        QUALIFY_TYPES=list(TYPES), QUALIFY_TIMES=list(TIMES),
        RETENTION_DAYS=int(os.getenv('RETENTION_DAYS', '90')),
        MAX_CONTENT_LENGTH=16384, SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=production,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        MIN_FORM_SECONDS=2, PRODUCTION=production,
        SMTP_HOST=os.getenv('SMTP_HOST', ''), SMTP_PORT=int(os.getenv('SMTP_PORT', '587')),
        SMTP_USERNAME=os.getenv('SMTP_USERNAME', ''), SMTP_PASSWORD=os.getenv('SMTP_PASSWORD', ''),
        NOTIFY_TO=os.getenv('NOTIFY_TO', ''), NOTIFY_FROM=os.getenv('NOTIFY_FROM', ''),
    )
    if overrides:
        app.config.update(overrides)
    for key in ('SECRET_KEY', 'LEAD_KEY', 'ADMIN_PASSWORD_HASH'):
        if not app.config[key]:
            raise RuntimeError(f'{key} is required. Run python manage.py init first.')
    app.fernet = Fernet(app.config['LEAD_KEY'])
    if app.config['PRODUCTION']:
        if len(app.config['SECRET_KEY']) < 32 or not app.config['PUBLIC_URL'].startswith('https://'):
            raise RuntimeError('Production requires a long SECRET_KEY and an HTTPS PUBLIC_URL.')
        if not all(app.config[k] for k in ('PRIVACY_EMAIL', 'LEGAL_OPERATOR', 'NOTIFY_TO', 'NOTIFY_FROM', 'SMTP_HOST')):
            raise RuntimeError('Production requires operator identity, privacy contact, and owner SMTP settings.')
        app.config['TRUSTED_HOSTS'] = [urlsplit(app.config['PUBLIC_URL']).hostname]
    if app.config['TRUST_PROXY_HOPS']:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=app.config['TRUST_PROXY_HOPS'])
    dbpath = Path(app.config['DATABASE'])
    dbpath.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(dbpath) as connection:
        connection.executescript(SCHEMA)
        columns = {r[1] for r in connection.execute('PRAGMA table_info(outbox)')}
        for column, definition in [('attempts', 'INTEGER NOT NULL DEFAULT 0'),
                                   ('next_attempt', 'REAL NOT NULL DEFAULT 0'),
                                   ('lease_until', 'REAL NOT NULL DEFAULT 0')]:
            if column not in columns:
                connection.execute(f'ALTER TABLE outbox ADD COLUMN {column} {definition}')
    dbpath.chmod(0o600)
    signer = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='quote-v1')

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        if 'db' in g:
            g.db.close()

    def digest(value):
        return hmac.new(app.config['SECRET_KEY'].encode(), value.encode(), hashlib.sha256).hexdigest()

    def limited(scope, maximum, seconds):
        # Use only the socket peer; never trust arbitrary X-Forwarded-For headers.
        identity = digest(request.remote_addr or 'unknown')
        bucket = int(time.time()) // seconds
        con = db()
        with con:
            con.execute('DELETE FROM rate_limits WHERE expires < ?', (time.time(),))
            con.execute('INSERT INTO rate_limits(scope,identity,bucket,count,expires) VALUES(?,?,?,1,?) '
                        'ON CONFLICT(scope,identity,bucket) DO UPDATE SET count=count+1',
                        (scope, identity, bucket, (bucket + 1) * seconds))
            count = con.execute('SELECT count FROM rate_limits WHERE scope=? AND identity=? AND bucket=?',
                                (scope, identity, bucket)).fetchone()[0]
        if count > maximum:
            abort(429)

    def csrf():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return session['csrf']

    @app.context_processor
    def template_globals():
        return dict(site=app.config['SITE_NAME'], csrf=csrf, types=TYPES, times=TIMES,
                    privacy_email=app.config['PRIVACY_EMAIL'], operator=app.config['LEGAL_OPERATOR'],
                    region=app.config['REGION'], consent=CONSENT.replace('STL Floor Match', app.config['SITE_NAME']))

    @app.before_request
    def protect():
        if request.method == 'POST':
            supplied = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
            if not supplied or not hmac.compare_digest(supplied.encode(), session.get('csrf', '').encode()):
                abort(400)
        if request.path.startswith('/admin') and request.path != '/admin/login':
            if not session.get('admin') or time.time() - session.get('admin_since', 0) > 1800:
                session.pop('admin', None)
                return redirect(url_for('login'))

    @app.after_request
    def headers(response):
        response.headers.update({
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
            'Referrer-Policy': 'strict-origin-when-cross-origin',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
            'Cache-Control': 'no-store',
        })
        if app.config['PRODUCTION']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if request.path.startswith('/admin') or request.path == '/thanks':
            response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return response

    def event(name, variant, visitor):
        if request.headers.get('DNT') == '1' or request.headers.get('Sec-GPC') == '1':
            return
        with db():
            db().execute('INSERT OR IGNORE INTO events(name,variant,visitor,created) VALUES(?,?,?,?)',
                         (name, variant, visitor, time.time()))

    @app.get('/')
    def home():
        limited('page', 180, 900)
        variant = request.args.get('v')
        if variant not in ('a', 'b'):
            variant = session.get('variant', 'a')
        session['variant'] = variant
        session.setdefault('visitor', secrets.token_urlsafe(18))
        event('visit', variant, session['visitor'])
        return render_template('home.html', variant=variant, errors={}, values={},
                               form_token=signer.dumps(secrets.token_urlsafe(24)))

    @app.post('/events')
    def track():
        limited('events', 60, 3600)
        if request.is_json and request.json.get('name') == 'form_start' and session.get('visitor'):
            event('form_start', session.get('variant', 'a'), session['visitor'])
        return '', 204

    @app.post('/quote')
    def quote():
        limited('quote', 8, 3600)
        values = request.form.to_dict()
        if values.get('website'):
            return redirect(url_for('thanks'), code=303)
        try:
            nonce, issued = signer.loads(values.get('form_token', ''), max_age=3600, return_timestamp=True)
        except (BadSignature, SignatureExpired):
            abort(400)
        if time.time() - issued.timestamp() < app.config['MIN_FORM_SECONDS']:
            abort(400)
        errors, cleaned = validate(values)
        cleaned['consent']['text'] = CONSENT.replace('STL Floor Match', app.config['SITE_NAME'])
        if errors:
            return render_template('home.html', variant=session.get('variant', 'a'), errors=errors,
                                   values=values, form_token=values['form_token']), 422
        reasons = []
        if cleaned['zip'] not in app.config['SERVICE_ZIPS']:
            reasons.append('ZIP outside configured coverage')
        if cleaned['sqft'] < app.config['MIN_SQFT']:
            reasons.append(f"Below {app.config['MIN_SQFT']} square feet")
        if cleaned['project_type'] not in app.config['QUALIFY_TYPES']:
            reasons.append('Project type needs review')
        if cleaned['timeframe'] not in app.config['QUALIFY_TIMES']:
            reasons.append('Timeframe needs review')
        contacts = [digest('email:' + cleaned['email'].lower())] if cleaned['email'] else []
        if cleaned['phone']:
            contacts.append(digest('phone:' + re.sub(r'\D', '', cleaned['phone'])[-10:]))
        now = time.time()
        con = db()
        con.execute('BEGIN IMMEDIATE')
        existing = con.execute('SELECT id FROM leads WHERE nonce=?', (nonce,)).fetchone()
        duplicate = False
        if not existing:
            for contact in contacts:
                if con.execute('SELECT 1 FROM contacts c JOIN leads l ON c.lead_id=l.id '
                               'WHERE c.fingerprint=? AND l.created>?', (contact, now - 86400)).fetchone():
                    duplicate = True
        created = not existing and not duplicate
        if created:
            lead_id = secrets.token_hex(12)
            payload = app.fernet.encrypt(json.dumps(cleaned).encode())
            con.execute('INSERT INTO leads(id,nonce,created,payload,qualified,reasons,variant) VALUES(?,?,?,?,?,?,?)',
                        (lead_id, nonce, now, payload, int(not reasons), json.dumps(reasons), session.get('variant', 'a')))
            con.executemany('INSERT INTO contacts(lead_id,fingerprint) VALUES(?,?)', [(lead_id, c) for c in contacts])
            con.execute('INSERT INTO outbox(lead_id) VALUES(?)', (lead_id,))
        con.commit()
        if created:
            visitor = session.setdefault('visitor', secrets.token_urlsafe(18))
            event('submission', session.get('variant', 'a'), visitor)
            if not reasons:
                event('qualified', session.get('variant', 'a'), visitor)
        return redirect(url_for('thanks'), code=303)

    @app.get('/thanks')
    def thanks():
        return render_template('thanks.html')

    @app.get('/privacy')
    def privacy():
        return render_template('privacy.html', retention=app.config['RETENTION_DAYS'])

    @app.route('/admin/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            limited('login', 5, 900)
            if check_password_hash(app.config['ADMIN_PASSWORD_HASH'], request.form.get('password', '')):
                session.clear()
                session.update(admin=True, admin_since=time.time(), csrf=secrets.token_urlsafe(32))
                session.permanent = True
                return redirect(url_for('admin'), code=303)
            flash('The password was not accepted.')
            return render_template('login.html'), 401
        return render_template('login.html')

    @app.post('/admin/logout')
    def logout():
        session.clear()
        return redirect(url_for('home'), code=303)

    @app.get('/admin')
    def admin():
        page = max(1, request.args.get('page', 1, type=int))
        rows = db().execute('SELECT l.*,o.state AS email_state FROM leads l LEFT JOIN outbox o ON o.lead_id=l.id '
                            'ORDER BY created DESC LIMIT 50 OFFSET ?', ((page - 1) * 50,)).fetchall()
        leads = []
        for row in rows:
            item = dict(row)
            item['data'] = json.loads(app.fernet.decrypt(item.pop('payload')))
            item['reasons'] = json.loads(item['reasons'])
            item['date'] = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(item['created']))
            leads.append(item)
        counts = db().execute('SELECT variant,name,count(*) AS n FROM events WHERE created>? GROUP BY variant,name',
                              (time.time() - 30 * 86400,)).fetchall()
        metrics = {v: {key: 0 for key in ('visit', 'form_start', 'submission', 'qualified')} for v in ('a', 'b')}
        for row in counts:
            metrics[row['variant']][row['name']] = row['n']
        totals = dict(db().execute('SELECT count(*) AS total,coalesce(sum(qualified),0) AS qualified FROM leads').fetchone())
        return render_template('admin.html', leads=leads, metrics=metrics, totals=totals, page=page,
                               min_sqft=app.config['MIN_SQFT'], zips=len(app.config['SERVICE_ZIPS']))

    @app.post('/admin/leads/<lead_id>')
    def review(lead_id):
        action = request.form.get('action')
        if action not in ('reviewed', 'archived', 'delete'):
            abort(400)
        with db():
            if action == 'delete':
                result = db().execute('DELETE FROM leads WHERE id=?', (lead_id,))
            else:
                result = db().execute('UPDATE leads SET status=? WHERE id=?', (action, lead_id))
            if not result.rowcount:
                abort(404)
        flash('Inquiry deleted.' if action == 'delete' else 'Review saved. No contractor delivery occurred.')
        return redirect(url_for('admin'), code=303)

    @app.get('/health')
    def health():
        db().execute('SELECT 1 FROM leads LIMIT 1')
        return {'status': 'ok'}

    @app.get('/robots.txt')
    def robots():
        return 'User-agent: *\nDisallow: /admin\nDisallow: /thanks\n', 200, {'Content-Type': 'text/plain'}

    for status in (400, 404, 413, 429, 500):
        def handle(error, code=status):
            if isinstance(error, SecurityError):
                return 'Unrecognized host', 400, {'Content-Type': 'text/plain'}
            return render_template('error.html', code=code), code
        app.register_error_handler(status, handle)

    @app.cli.command('notify')
    @click.option('--dry-run', is_flag=True)
    def notify(dry_run):
        sent, failed = send_notifications(app, dry_run=dry_run)
        click.echo(f'{sent} processed; {failed} failed. No inquiry content is included in notifications.')
        if failed:
            raise click.ClickException('Notification delivery failed. Queue retained for retry.')

    @app.cli.command('purge')
    def purge():
        with db():
            n = db().execute('DELETE FROM leads WHERE created<?',
                             (time.time() - app.config['RETENTION_DAYS'] * 86400,)).rowcount
            db().execute('DELETE FROM events WHERE created<?', (time.time() - 30 * 86400,))
            db().execute('DELETE FROM rate_limits WHERE expires<?', (time.time(),))
        click.echo(f'Removed {n} expired inquiries; removed expired analytics and rate counters.')

    return app


def validate(values):
    data = {key: values.get(key, '').strip() for key in
            ('project_type', 'sqft', 'zip', 'timeframe', 'name', 'email', 'phone', 'details')}
    errors = {}
    if data['project_type'] not in TYPES:
        errors['project_type'] = 'Choose a commercial project type.'
    if not re.fullmatch(r'[0-9]{1,7}', data['sqft']) or not 1 <= int(data['sqft']) <= 1000000:
        errors['sqft'] = 'Enter a whole number from 1 to 1,000,000.'
    else:
        data['sqft'] = int(data['sqft'])
    if not re.fullmatch(r'[0-9]{5}', data['zip']):
        errors['zip'] = 'Enter a five-digit ZIP code.'
    if data['timeframe'] not in TIMES:
        errors['timeframe'] = 'Choose a project timeframe.'
    if not 2 <= len(data['name']) <= 100 or any(ord(c) < 32 for c in data['name']):
        errors['name'] = 'Enter your name (2–100 characters).'
    if not data['email'] and not data['phone']:
        errors['email'] = 'Provide an email address or phone number so we can respond.'
    if data['email'] and (len(data['email']) > 254 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', data['email'])):
        errors['email'] = 'Enter a valid email address.'
    digits = re.sub(r'\D', '', data['phone'])
    if data['phone'] and (not re.fullmatch(r'[+() .\-0-9]{10,25}', data['phone']) or
                          not (len(digits) == 10 or len(digits) == 11 and digits.startswith('1'))):
        errors['phone'] = 'Enter a valid US phone number, or leave it blank.'
    if len(data['details']) > 3000:
        errors['details'] = 'Keep project details to 3,000 characters.'
    if values.get('share_consent') != 'yes':
        errors['share_consent'] = 'Your permission is needed to review and match this inquiry.'
    data['consent'] = {'version': '2026-10-27-v1', 'share': True, 'marketing': False, 'text': CONSENT}
    return errors, data


def send_notifications(app, dry_run=False):
    """Owner-only outbox. No inquiry content in email. Failures stay queued."""
    db = sqlite3.connect(app.config['DATABASE'], timeout=10)
    db.row_factory = sqlite3.Row
    sent = failed = 0
    try:
        # Recover abandoned deliveries after a crash; retries are at least once.
        with db:
            db.execute("UPDATE outbox SET state='pending' WHERE state='sending' AND lease_until<?", (time.time(),))
        rows = db.execute("SELECT * FROM outbox WHERE state='pending' AND next_attempt<=? LIMIT 20",
                          (time.time(),)).fetchall()
        for row in rows:
            msg = EmailMessage()
            msg['Subject'] = 'New flooring inquiry awaiting your review'
            msg['From'] = app.config['NOTIFY_FROM'] or 'preview@example.invalid'
            msg['To'] = app.config['NOTIFY_TO'] or 'preview@example.invalid'
            msg.set_content('A new inquiry is ready in your private dashboard.\n'
                            + app.config['PUBLIC_URL'] + '/admin\n\n'
                            'No details have been forwarded to contractors.\n')
            if dry_run:
                sent += 1
                continue
            if not all(app.config[k] for k in ('SMTP_HOST', 'NOTIFY_FROM', 'NOTIFY_TO')):
                failed += 1
                continue
            with db:
                claimed = db.execute("UPDATE outbox SET state='sending',lease_until=? WHERE id=? AND state='pending'",
                                     (time.time()+120, row['id'])).rowcount
            if not claimed:
                continue
            try:
                with smtplib.SMTP(app.config['SMTP_HOST'], app.config['SMTP_PORT'], timeout=10) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    if app.config['SMTP_USERNAME']:
                        smtp.login(app.config['SMTP_USERNAME'], app.config['SMTP_PASSWORD'])
                    smtp.send_message(msg)
                with db:
                    db.execute("UPDATE outbox SET state='sent',sent_at=? WHERE id=?", (time.time(), row['id']))
                sent += 1
            except (OSError, smtplib.SMTPException):
                with db:
                    db.execute("UPDATE outbox SET state='pending',attempts=attempts+1,next_attempt=? WHERE id=?",
                               (time.time()+min(3600, 60 * 2**min(row['attempts'], 6)), row['id']))
                failed += 1
        db.commit()
    finally:
        db.close()
    return sent, failed


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=8000, debug=False)
