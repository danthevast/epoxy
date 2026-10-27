import json
import re
import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash
from app import create_app, send_notifications, TYPES, TIMES

@pytest.fixture
def app(tmp_path):
    return create_app(dict(TESTING=True, SECRET_KEY='testing-key-' * 4,
        LEAD_KEY=Fernet.generate_key(), ADMIN_PASSWORD_HASH=generate_password_hash('test-password-only'),
        DATABASE=str(tmp_path / 'leads.sqlite3'), MIN_FORM_SECONDS=0, PRODUCTION=False,
        SESSION_COOKIE_SECURE=False, PUBLIC_URL='http://localhost', SMTP_HOST='', NOTIFY_TO='',
        NOTIFY_FROM='', PRIVACY_EMAIL=''))

@pytest.fixture
def client(app):
    return app.test_client()

def tokens(client, variant='a'):
    html=client.get('/?v=' + variant).text
    return {key:re.search('name="' + key + '" value="([^"]+)"',html).group(1)
            for key in ('csrf_token','form_token')}

def payload(client, **changes):
    data = dict(tokens(client), project_type=TYPES[0], sqft='2500', zip='63103',
                timeframe=TIMES[1], name='Test Customer', email='customer@example.test',
                phone='', details='Warehouse floor', share_consent='yes')
    data.update(changes)
    return data

def rows(app, table='leads'):
    # Table names are test constants only.
    with sqlite3.connect(app.config['DATABASE']) as db:
        db.row_factory=sqlite3.Row
        return [dict(r) for r in db.execute('SELECT * FROM ' + table)]

def login(client):
    client.get('/admin/login')
    with client.session_transaction() as session:
        token=session['csrf']
    return client.post('/admin/login',data={'csrf_token':token,'password':'test-password-only'})

def admin_token(client):
    with client.session_transaction() as session:
        return session['csrf']

def test_submit_encrypted_pending_and_consent(app, client):
    response=client.post('/quote',data=payload(client))
    assert response.status_code==303 and response.location.endswith('/thanks')
    lead=rows(app)[0]
    assert lead['status']=='pending_approval' and lead['qualified']==1
    assert b'customer@example.test' not in lead['payload']
    decoded=json.loads(app.fernet.decrypt(lead['payload']))
    assert decoded['consent']['marketing'] is False
    assert decoded['consent']['share'] is True
    assert len(rows(app,'outbox'))==1
    assert 'Thanks for' in client.get('/thanks').text

def test_phone_only_submission(app, client):
    assert client.post('/quote',data=payload(client,email='',phone='(314) 555-0101')).status_code==303
    assert len(rows(app))==1

@pytest.mark.parametrize('changes,key',[
    ({'email':'','phone':''},'email'),
    ({'email':'bad address'},'email'),
    ({'email':'x@example.test\r\nBcc: attacker@example.test'},'email'),
    ({'phone':'abc1234567'},'phone'),
    ({'zip':'6310x'},'zip'),({'sqft':'-1'},'sqft'),({'sqft':'1.5'},'sqft'),
    ({'sqft':'1000001'},'sqft'),({'name':'x'},'name'),
    ({'project_type':'residential'},'project_type'),({'timeframe':'now'},'timeframe'),
    ({'details':'x'*3001},'details'),({'share_consent':''},'share_consent')])
def test_invalid_input(app,client,changes,key):
    response=client.post('/quote',data=payload(client,**changes))
    assert response.status_code==422
    assert len(rows(app))==0
    assert 'Please check your information' in response.text

def test_duplicate_nonce_and_new_form(app,client):
    data=payload(client)
    assert client.post('/quote',data=data).status_code==303
    assert client.post('/quote',data=data).status_code==303
    assert client.post('/quote',data=payload(client,email='CUSTOMER@example.test')).status_code==303
    assert len(rows(app))==len(rows(app,'outbox'))==1
    assert len([r for r in rows(app,'events') if r['name']=='submission'])==1

def test_duplicate_phone_normalized(app,client):
    client.post('/quote',data=payload(client,email='',phone='314-555-0101'))
    client.post('/quote',data=payload(client,email='other@example.test',phone='+1 (314) 555-0101'))
    assert len(rows(app))==1

def test_outside_area_and_small_projects_saved_for_review(app,client):
    client.post('/quote',data=payload(client,zip='90210',sqft='100'))
    lead=rows(app)[0]
    assert lead['qualified']==0 and lead['status']=='pending_approval'
    assert len(json.loads(lead['reasons']))==2

def test_qualification_configuration(app,client):
    app.config.update(MIN_SQFT=3000,SERVICE_ZIPS=['63103'],QUALIFY_TYPES=[])
    client.post('/quote',data=payload(client))
    assert not rows(app)[0]['qualified']

def test_missing_csrf_and_tampered_form(app,client):
    data=payload(client)
    assert client.post('/quote',data={**data,'csrf_token':'bad'}).status_code==400
    assert client.post('/quote',data={**data,'form_token':'bad'}).status_code==400
    assert not rows(app)

def test_honeypot_and_minimum_time(app,client):
    assert client.post('/quote',data=payload(client,website='https://spam.test')).status_code==303
    assert not rows(app)
    app.config['MIN_FORM_SECONDS']=10
    assert client.post('/quote',data=payload(client)).status_code==400
    assert not rows(app)

def test_submission_rate_limit_persistent(app,client):
    data=payload(client,website='spam')
    for _ in range(8):
        assert client.post('/quote',data=data).status_code==303
    assert client.post('/quote',data=data).status_code==429
    other=app.test_client()
    data=payload(other,website='spam')
    assert other.post('/quote',data=data).status_code==429

def test_admin_private_and_login_throttle(app,client):
    assert client.get('/admin').status_code==302
    client.get('/admin/login')
    token=admin_token(client)
    for _ in range(5):
        assert client.post('/admin/login',data={'csrf_token':token,'password':'wrong'}).status_code==401
    assert client.post('/admin/login',data={'csrf_token':token,'password':'wrong'}).status_code==429

def test_admin_review_cannot_deliver_and_delete_cascades(app,client):
    client.post('/quote',data=payload(client))
    assert login(client).status_code==303
    lead=rows(app)[0]
    assert 'Test Customer' in client.get('/admin').text
    token=admin_token(client)
    assert client.post('/admin/leads/'+lead['id'],data={'action':'send','csrf_token':token}).status_code==400
    assert client.post('/admin/leads/'+lead['id'],data={'action':'reviewed','csrf_token':token}).status_code==303
    assert rows(app)[0]['status']=='reviewed'
    assert rows(app,'outbox')[0]['state']=='pending'
    client.post('/admin/leads/'+lead['id'],data={'action':'delete','csrf_token':token})
    assert not rows(app) and not rows(app,'outbox') and not rows(app,'contacts')

def test_session_expiration_and_logout(app,client):
    login(client)
    with client.session_transaction() as s:
        s['admin_since']=time.time()-1900
    assert client.get('/admin').status_code==302
    login(client)
    client.post('/admin/logout',data={'csrf_token':admin_token(client)})
    assert client.get('/admin').status_code==302

def test_security_headers_and_xss_escaping(app,client):
    data=payload(client,details='<script>alert(1)</script>',name="O'Reilly; DROP TABLE leads")
    client.post('/quote',data=data)
    login(client)
    response=client.get('/admin')
    assert '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;' in response.text
    assert response.headers['X-Frame-Options']=='DENY'
    assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    assert response.headers['X-Robots-Tag']=='noindex, nofollow'
    assert response.headers['Cache-Control']=='no-store'
    cookie=app.test_client().get('/').headers.get('Set-Cookie','')
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie

def test_payload_size(app,client):
    data=payload(client,details='x'*17000)
    assert client.post('/quote',data=data).status_code==413

def test_variants_and_analytics(app,client):
    assert 'hard-working' in client.get('/?v=a').text
    assert 'Your next floor' in client.get('/?v=b').text
    data=payload(client)
    token=data['csrf_token']
    for _ in range(2):
        assert client.post('/events',json={'name':'form_start'},headers={'X-CSRF-Token':token}).status_code==204
    client.post('/quote',data=data)
    names=[r['name'] for r in rows(app,'events')]
    assert names.count('form_start')==1 and names.count('submission')==1 and names.count('qualified')==1

@pytest.mark.parametrize('header',['DNT','Sec-GPC'])
def test_analytics_optout(app,header):
    c=app.test_client()
    c.get('/',headers={header:'1'})
    assert not rows(app,'events')

def test_email_dry_run_smtp_success_and_no_pii(app,client):
    client.post('/quote',data=payload(client))
    assert send_notifications(app,dry_run=True)==(1,0)
    assert rows(app,'outbox')[0]['state']=='pending'
    app.config.update(SMTP_HOST='smtp.example.test',SMTP_USERNAME='user',SMTP_PASSWORD='not-a-real-secret',
                      NOTIFY_TO='owner@example.test',NOTIFY_FROM='site@example.test')
    smtp=MagicMock()
    with patch('app.smtplib.SMTP') as factory:
        factory.return_value.__enter__.return_value=smtp
        assert send_notifications(app)==(1,0)
        msg=smtp.send_message.call_args.args[0]
        assert msg['To']=='owner@example.test'
        assert 'customer@example.test' not in msg.as_string()
        assert 'Warehouse floor' not in msg.as_string()
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once()
    assert rows(app,'outbox')[0]['state']=='sent'
    assert send_notifications(app,dry_run=True)==(0,0)

def test_email_failure_is_retryable(app,client):
    client.post('/quote',data=payload(client))
    assert send_notifications(app)==(0,1)
    app.config.update(SMTP_HOST='smtp.example.test',NOTIFY_TO='owner@example.test',NOTIFY_FROM='site@example.test')
    with patch('app.smtplib.SMTP',side_effect=OSError('offline')):
        assert send_notifications(app)==(0,1)
    assert rows(app,'outbox')[0]['state']=='pending'
    assert 'customer@example.test' not in app.test_cli_runner().invoke(args=['notify','--dry-run']).output

def test_purge(app,client):
    client.post('/quote',data=payload(client))
    with sqlite3.connect(app.config['DATABASE']) as db:
        db.execute('UPDATE leads SET created=?',(time.time()-91*86400,))
        db.execute('UPDATE events SET created=?',(time.time()-31*86400,))
    result=app.test_cli_runner().invoke(args=['purge'])
    assert result.exit_code==0
    assert not rows(app) and not rows(app,'outbox') and not rows(app,'events')

def test_production_requires_launch_configuration(tmp_path):
    with pytest.raises(RuntimeError,match='HTTPS'):
        create_app(dict(PRODUCTION=True,SECRET_KEY='x'*40,LEAD_KEY=Fernet.generate_key(),
            ADMIN_PASSWORD_HASH='unused',DATABASE=str(tmp_path/'db'),PUBLIC_URL='http://localhost'))

def test_health_and_privacy(client):
    assert client.get('/health').json=={'status':'ok'}
    assert client.get('/privacy').status_code==200
    assert client.get('/missing').status_code==404

def test_backup_restore_encrypted_payload(app,client,tmp_path):
    from serve import backup
    client.post('/quote',data=payload(client))
    path=backup(app)
    assert path.exists() and path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        encrypted=db.execute('SELECT payload FROM leads').fetchone()[0]
        assert json.loads(app.fernet.decrypt(encrypted))['name']=='Test Customer'

def test_non_ascii_csrf_is_rejected(client):
    data=payload(client)
    assert client.post('/quote',data={**data,'csrf_token':'é'}).status_code==400

def test_untrusted_forwarded_address_cannot_bypass_limit(app,client):
    data=payload(client,website='spam')
    for n in range(8):
        assert client.post('/quote',data=data,headers={'X-Forwarded-For':f'192.0.2.{n}'}).status_code==303
    assert client.post('/quote',data=data,headers={'X-Forwarded-For':'192.0.2.100'}).status_code==429

def test_expired_form_rejected(app,client):
    data=payload(client)
    from itsdangerous import URLSafeTimedSerializer
    with patch('itsdangerous.timed.time.time',return_value=time.time()-7200):
        data['form_token']=URLSafeTimedSerializer(app.config['SECRET_KEY'],salt='quote-v1').dumps('expired')
    assert client.post('/quote',data=data).status_code==400

def test_recover_abandoned_email(app,client):
    client.post('/quote',data=payload(client))
    with sqlite3.connect(app.config['DATABASE']) as db:
        db.execute("UPDATE outbox SET state='sending',lease_until=0")
    assert send_notifications(app,dry_run=True)==(1,0)
    assert rows(app,'outbox')[0]['state']=='pending'

def test_production_cookie_and_trusted_host(app,tmp_path):
    production=create_app(dict(PRODUCTION=True,SESSION_COOKIE_SECURE=True,
        SECRET_KEY='secure-session-'*4,LEAD_KEY=Fernet.generate_key(),
        ADMIN_PASSWORD_HASH=generate_password_hash('local-test-only'),
        PUBLIC_URL='https://floors.example.test',DATABASE=str(tmp_path/'production.sqlite'),
        PRIVACY_EMAIL='privacy@example.test',LEGAL_OPERATOR='Test Operator',
        NOTIFY_TO='owner@example.test',NOTIFY_FROM='site@example.test',SMTP_HOST='smtp.example.test'))
    c=production.test_client()
    response=c.get('/',base_url='https://floors.example.test')
    assert response.status_code==200
    assert 'Secure' in response.headers['Set-Cookie']
    assert 'HttpOnly' in response.headers['Set-Cookie']
    assert response.headers['Strict-Transport-Security']=='max-age=31536000'
    assert c.get('/',base_url='https://evil.example.test').status_code==400
