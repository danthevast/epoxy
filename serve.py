"""Single-service runner: web process plus owner notification and retention jobs."""
import logging
import os
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from app import create_app, send_notifications

ROOT = Path(__file__).resolve().parent

def backup(app):
    """Consistent SQLite copy; encrypted lead content stays encrypted."""
    source = Path(app.config['DATABASE'])
    directory = source.parent / 'backups'
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / (time.strftime('%Y-%m-%d', time.gmtime()) + '.sqlite3')
    # Create private before SQLite writes. Backup files never live in static/.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    target.chmod(0o600)
    for old in directory.glob('*.sqlite3'):
        if old.stat().st_mtime < time.time() - 7 * 86400:
            old.unlink()
    return target

def maintain(app, stop):
    next_cleanup = 0
    while not stop.is_set():
        try:
            if app.config['SMTP_HOST']:
                sent, failed = send_notifications(app)
                if sent or failed:
                    logging.info('Owner notifications: %s sent, %s failed', sent, failed)
            if time.time() >= next_cleanup:
                result = app.test_cli_runner().invoke(args=['purge'])
                if result.exit_code:
                    raise RuntimeError('Retention job failed')
                backup(app)
                next_cleanup = time.time() + 3600
        except Exception:
            # Do not log payloads, email addresses, or credentials.
            logging.error('Maintenance operation failed; inspect configuration and retry.')
        stop.wait(60)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    app = create_app()
    host = os.getenv('BIND_HOST', '0.0.0.0' if app.config['PRODUCTION'] else '127.0.0.1')
    port = int(os.getenv('PORT', '8000'))
    gunicorn = ROOT / '.venv' / 'bin' / 'gunicorn'
    executable = str(gunicorn) if gunicorn.exists() else 'gunicorn'
    child = subprocess.Popen([executable, '--bind', f'{host}:{port}', '--workers', '2',
                              '--timeout', '30', '--no-control-socket', 'app:create_app()'], cwd=ROOT)
    stop = threading.Event()
    def shutdown(signum, frame):
        stop.set()
        child.terminate()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker = threading.Thread(target=maintain, args=(app,stop), daemon=True)
    worker.start()
    try:
        code = child.wait()
    finally:
        stop.set()
        worker.join(timeout=15)
        if child.poll() is None:
            child.terminate()
    raise SystemExit(code)

if __name__ == '__main__':
    main()
