# Launch guide in plain English

The website works locally. It is **not publicly deployed**. No purchases or live email sends
have been made. Keep it that way until you approve a launch.

## Accounts you need

| Account | Purpose | Suggested option | Budget |
| --- | --- | --- | --- |
| GitHub | Holds the website code | Your existing danthevast account and epoxy repository | Free |
| Web hosting | Runs the site and saves requests on a persistent disk | Render paid Starter web service plus a 1 GB persistent disk | Allow $7–10/month plus about $0.25–1/month disk |
| Transactional email | Sends notifications to you | Brevo free plan, SMTP credentials | $0 within its free daily allowance |
| Domain registrar | Reserves your website address | Cloudflare Registrar or another registrar | Usually $10–20/year for an ordinary .com |
| An email inbox you monitor | Receives lead alerts and privacy requests | Existing inbox | $0 if already available |

No Google Analytics, Twilio, database, or contractor portal account is needed.
A new business mailbox is optional; allow $0–7/month depending on provider.
The site's name “STL Floor Match” is a working name, not a claim that a business or trademark exists.

**Estimated operating cost:** roughly **$8–18/month**, plus **$10–20/year** for the domain.
The first month including a normal domain should be roughly **$18–38**, below $50.
These are planning estimates, not verified offers; check current prices, taxes, domain renewal rates,
and free-plan limits before buying. Paid ads and legal/business formation work are not included.
Free hosting is useful for demos, but an ephemeral filesystem can lose all customer inquiries.
Use a paid persistent disk for this SQLite design.

## Before you give launch approval

1. Choose your public website name and domain.
2. Supply the real operator's legal name and a monitored privacy/contact email. The preview policy
   deliberately flags these missing details. Review that policy and consent wording for your actual business.
3. Decide your exact ZIP coverage and project-size preference (default 500 sq ft).
4. Set a strong dashboard password with the setup command. Keep it in a password manager.
5. Make a secure backup of the encryption key and session secret. Losing the encryption key means
   losing access to stored customer details.
6. Verify your email sender domain with the email provider. Add their SPF/DKIM records and a DMARC
   policy using their instructions. Do not remove existing email DNS records blindly.
7. Do not enable sharing until you have a contractor agreement covering permitted uses, security,
   contact practices, payment, and customer requests. No contractor delivery feature exists here.
8. Keep marketing disabled. This form does not collect marketing consent.

## Deployment steps — only after you approve public deployment

The following describes the proposed Render deployment. It has **not** been performed.

1. Sign into Render with GitHub. Connect **danthevast/epoxy**, branch **main**.
2. Create a Python Web Service on the Starter plan. Set Python version **3.12.14**
   using `PYTHON_VERSION` in its environment settings.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python serve.py`
5. Attach a 1 GB persistent disk mounted at `/var/data`. This is essential.
   Run a **single service instance**. SQLite is not intended for multiple replicas here.
6. Set the health-check path to `/health`. Let Render supply `PORT`.
7. Add these private hosting environment settings. Use your hosting dashboard, not GitHub files or chat:

| Setting | What you enter |
| --- | --- |
| APP_ENV | production |
| PUBLIC_URL | Your final https website address, no trailing slash |
| DATABASE_PATH | /var/data/epoxy/leads.sqlite3 |
| SECRET_KEY | A securely generated random secret, at least 32 characters |
| LEAD_ENCRYPTION_KEY | The Fernet key created by local setup, or a freshly generated key for a new empty production database |
| ADMIN_PASSWORD_HASH | The password hash created by the setup tool, not your plain password |
| LEGAL_OPERATOR | Your real legal operator name |
| PRIVACY_EMAIL | Your monitored customer/privacy email address |
| NOTIFY_TO | Your inbox for new inquiry alerts |
| NOTIFY_FROM | An address on your verified sender domain |
| SMTP_HOST | The SMTP server provided by your email service |
| SMTP_PORT | 587 |
| SMTP_USERNAME | The SMTP login supplied by your provider |
| SMTP_PASSWORD | The SMTP credential supplied by your provider |
| TRUST_PROXY_HOPS | 0 until the trusted forwarding chain is verified; see operations guide |
| MIN_SQFT | 500, or your preferred threshold |
| RETENTION_DAYS | 90, or your published retention period |
| SERVICE_ZIPS | Optional comma-separated five-digit ZIP list, or keep the supplied St. Louis area list |

Your local `.env` holds generated secrets. A technical helper can transfer them securely into the
host's settings. Do not upload the `.env` to GitHub or reuse production credentials in test fixtures.
If your host stores hash values containing dollar signs, enter them literally in its dashboard.

8. Add your domain in Render, then add the DNS records Render gives you at your registrar.
   Wait for the HTTPS certificate. Set PUBLIC_URL to that same domain and restart.
   Preview hostnames will be rejected once the production trusted hostname is set.
9. Test one synthetic inquiry from your phone using your own contact details and explicit consent.
   Confirm it appears in your private dashboard and your email inbox, and remains pending.
   The notification worker checks once per minute. Delete the test inquiry.
10. Test a wrong password, a duplicate inquiry, and an invalid ZIP; verify no customer data
    appears in a public page. Verify client rate-limit separation as described in operations.
11. Restart/redeploy and confirm a synthetic test lead survives; verify backup restoration.
12. Enable host downtime alerts and monitor notification failures and disk space. Only then
    promote the site or start accepting real leads.

## What is still unverified

Real sender-domain ownership, SMTP authentication, live email delivery, your hosting proxy headers,
DNS, HTTPS, persistent-disk behavior on a real deployment, and public uptime.
These need the accounts and settings above. Local tests cannot establish them.

## How to use the dashboard

Open your domain followed by `/admin`. Enter the password you chose.
You will see the customer's request, qualification reasons, consent wording, and owner email status.
“Mark reviewed” and “Archive” only organize your queue. They never send a customer's information.
“Permanently delete” removes the inquiry, contact fingerprints, and pending email from the live database.
You can review conversion rates separately for versions A and B.

Version A is the default. Version B can be inspected with `?v=b` on the home page.
Don't interpret small samples as a reliable experiment. Set a success measure and experiment plan
before deliberately splitting traffic.
