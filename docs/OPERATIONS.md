# Security and operations

## Data and access

- Customer name, contact details, project specifics, and consent text are Fernet-encrypted before
  insertion. ZIP/size remain within the encrypted payload. Qualification status/reasons, variant,
  random inquiry ID, and timestamps are plaintext metadata.
- SQLite and its containing directory are private. Database, backups, .env, browser artifacts,
  and virtual environments are ignored by Git and outside Flask's public static directory.
- Contact fingerprints use keyed HMAC for 24-hour duplicate detection. Connections are also HMACed
  for persistent rate counters; no raw IP addresses are stored by this application.
- Host infrastructure can still retain its own access logs. Restrict and minimize those separately.
  Gunicorn access logging is off by default; never log request bodies or credentials.
- Admin uses a scrypt password hash, signed HttpOnly SameSite session cookie, CSRF on every POST,
  a 30-minute absolute login window, and five failed/total attempts per connection per 15-minute bucket.
  There is one operator login and no public account creation. Add a host access gate or identity
  provider with MFA before granting access to multiple operators.
- Production enforces HTTPS site settings, secure cookies, HSTS, trusted hostname, operator identity,
  and owner notification settings. HTTPS termination is supplied by the host. Debug mode is off.
- A same-site CSP excludes third-party scripts. Templates escape submitted content. SQL is parameterized.
- This is a low-volume single-instance design, not a guarantee against all abuse or denial of service.

## Spam and duplicates

A hidden honeypot, signed one-hour form token, minimum two-second completion time, CSRF, 16 KB body
limit, field limits, and eight POSTs per connection per hour provide baseline protection.
The hour and 15-minute login windows are fixed buckets (a burst around the boundary is possible).
Accepted requests with a repeated signed nonce or email/US phone in 24 hours do not create another lead
or email. The response does not reveal whether a contact is already in the database.
If genuine traffic gets flagged, inspect the configuration before raising limits. A WAF challenge
can be added later; no paid CAPTCHA or third-party visitor tracking is required initially.

## Reverse proxies and real client addresses

Default TRUST_PROXY_HOPS=0 ignores all X-Forwarded-For values; forged headers cannot evade limits.
Some hosts put every visitor behind the same socket peer, which would share the default limits.
Before public launch, confirm the host's documented forwarding behavior and count trusted proxies.
Set TRUST_PROXY_HOPS=1 **only if exactly one trusted edge appends the true client address and the app
cannot be reached around that edge**. For a different verified chain, use its actual count.
ProxyFix consumes only that many rightmost forwarded addresses; it does not trust arbitrary leftmost
entries. Don't set a count by guesswork. Test from two independent connections and verify that a
forged leftmost address cannot change the effective rate-limit identity. Host chain verification
is a launch prerequisite. Rate limits intentionally remain strict if this is unset.

## Notifications and recovery

`python serve.py` starts Gunicorn and a maintenance thread in the same service.
The thread checks the durable email outbox every 60 seconds, purges expired records hourly,
and makes/refreshes one SQLite backup per UTC day. Failures are retried; logs report counts without PII.
Local preview has no SMTP credentials and sends no mail.

Notifications go only to configured NOTIFY_TO. They contain a dashboard link and no customer details.
SMTP STARTTLS uses certificate verification. SMTP errors leave messages pending with exponential
retry delays capped at an hour. A two-minute delivery lease recovers abandoned jobs after a crash.
Delivery is at least once: a crash after the provider accepts but before the database commit can
produce a second owner alert, but not a duplicate lead. No contractor address field is supported.

Run manually if needed:

```sh
.venv/bin/flask --app app:create_app notify --dry-run
.venv/bin/flask --app app:create_app notify
.venv/bin/flask --app app:create_app purge
```

The dry run does not deliver or mark anything sent. A real notify command sends only to the owner.
Watch the dashboard notification status and host logs. The maintenance thread is in the web service;
no separate cron service, which could not share Render's disk, is needed.

## Retention and backups

Live inquiries expire at RETENTION_DAYS (default 90); anonymous events after 30 days.
Deletion cascades to contact fingerprints and outbox entries. Cleanup runs hourly while the service
is alive. If it has been offline, cleanup resumes when restarted.
Backups are written to a private `backups/` folder beside the database, rotated after seven days.
They contain encrypted payloads and sensitive metadata; access controls still matter.
A deleted record can persist in a backup for up to seven additional days. Keep this policy accurate.

Local backups protect against mistakes, **not total disk/account loss**. Before accepting real leads,
arrange a secure off-host copy or verify the host's independent disk snapshots and retention.
Store the encryption key separately in a password manager. Do not email keys or put them in Git.
Budget for a small private backup service if the host does not include snapshots.
Restoring an old backup may reintroduce deleted inquiries; maintain deletion instructions and reapply
them before resuming service. Never restore the sample/test database into production.

Restore procedure (perform on a private copy first):

1. Stop the web service and notification worker.
2. Preserve the current database and any WAL/SHM files together.
3. Copy the chosen backup to the configured database path; remove only obsolete WAL/SHM files
   belonging to the database being replaced while the service is stopped.
4. Restore the matching LEAD_ENCRYPTION_KEY. Keep file permissions private.
5. Run SQLite `PRAGMA integrity_check`, confirm dashboard decryption with a synthetic inquiry,
   run purge, and check outbox state before restarting notifications.
6. Check the latest user deletion requests and delete any reintroduced data.

The automated tests exercise a consistent SQLite backup and decryption of a restored payload.

## Future contractors, markets, and analytics

All routing currently targets owner approval, regardless of qualification. There is deliberately
no delivery toggle you can accidentally enable. After a signed agreement and your permission,
a separate implementation can add per-recipient delivery with a consent/audit trail.
Don't copy customer details into a shared spreadsheet without applying the same controls.

Each future market should have separate secrets, database, coverage, copy, operator contact,
consent review, and contractor agreements. The shared intake/security/queue code can be reused.
Built-in metrics are approximate anonymous browser sessions, not people: refreshes don't inflate
an event; clearing cookies and bots can. Do Not Track and Global Privacy Control disable counting.
Submissions are counted only on successful inserts, never on confirmation-page views.
Raw lead totals in the dashboard can differ from opted-in analytics counts. No external analytics account is needed.
