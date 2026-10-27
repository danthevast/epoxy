"""Local setup and password management. Never commits secrets."""
import argparse
import getpass
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('init', 'set-password'))
    args = parser.parse_args()
    path = ROOT / '.env'
    if args.command == 'init':
        if path.exists():
            print('Existing .env preserved. Use set-password to change dashboard access.')
            return
        # A random unknown admin password closes access until the operator chooses one.
        contents = ('APP_ENV=development\nSECRET_KEY=' + secrets.token_urlsafe(48) +
                    '\nLEAD_ENCRYPTION_KEY=' + Fernet.generate_key().decode() +
                    "\nADMIN_PASSWORD_HASH='" + generate_password_hash(secrets.token_urlsafe(40)) +
                    "'\nPUBLIC_URL=http://127.0.0.1:8000\n")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(contents)
        print('Local secrets generated. Run python manage.py set-password to enable your dashboard login.')
    else:
        if not path.exists():
            raise SystemExit('Run init first.')
        password = getpass.getpass('Choose a dashboard password (at least 16 characters): ')
        if len(password) < 16 or password != getpass.getpass('Confirm password: '):
            raise SystemExit('Passwords must match and be at least 16 characters.')
        lines = path.read_text().splitlines()
        lines = [line for line in lines if not line.startswith('ADMIN_PASSWORD_HASH=')]
        lines.append("ADMIN_PASSWORD_HASH='" + generate_password_hash(password) + "'")
        path.write_text('\n'.join(lines) + '\n')
        path.chmod(0o600)
        print('Password saved locally as a hash. Restart the server to apply it.')

if __name__ == '__main__':
    main()
