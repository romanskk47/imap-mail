#!/usr/bin/env python3
"""Portable read-only IMAP reader; stdlib only. No mailbox mutations."""
import argparse
import base64
import datetime as dt
import email
import email.policy
import html
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import ssl
import subprocess
import sys

MAX_BYTES = 2097152
HEADERS = ('From', 'To', 'Subject', 'Date', 'Message-ID', 'In-Reply-To',
           'References', 'List-ID', 'List-Unsubscribe', 'Auto-Submitted',
           'Authentication-Results', 'Reply-To', 'Return-Path')


class MailError(Exception):
    pass


def safe_text(value, maximum=1024):
    if not isinstance(value, str) or len(value) > maximum or any(c in value for c in '\r\n\0'):
        raise MailError('INVALID_ARGUMENT')
    return value


def private_file(path):
    p = Path(path).expanduser()
    if p.stat().st_mode & 0o077:
        raise MailError('SECRET_FILE_PERMISSIONS_REQUIRE_600')
    return p


def profiles(directory=None):
    if directory is None and os.environ.get('MAIL_PROFILES_JSON'):
        raw = json.loads(os.environ['MAIL_PROFILES_JSON'])
        docs = raw if isinstance(raw, list) else [raw]
    else:
        root = Path(directory or os.environ.get('MAIL_PROFILES_DIR', '~/.secrets/services/mail')).expanduser()
        docs = []
        for path in sorted(root.rglob('*.json')):
            d = json.loads(path.read_text())
            if 'password' in d:
                private_file(path)
            docs.append(d)
    out = {}
    for d in docs:
        for field in ('id', 'host', 'user', 'entity'):
            if not d.get(field):
                raise MailError('INCOMPLETE_PROFILE')
            safe_text(d[field])
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', d['id']):
            raise MailError('INVALID_ACCOUNT_ID')
        if d['id'] in out:
            raise MailError('DUPLICATE_ACCOUNT_ID')
        if d.get('tls', True) is not True:
            raise MailError('TLS_REQUIRED')
        if not isinstance(d.get('port', 993), int) or not 1 <= d.get('port', 993) <= 65535:
            raise MailError('INVALID_PORT')
        out[d['id']] = d
    return out


def identity(d):
    return dict(account=d['id'], entity=d['entity'], email=d['user'],
                host=d['host'], enabled=d.get('enabled', True), access='read-only')


def password(d):
    sources = [k for k in ('password', 'password_env', 'password_file') if k in d]
    if len(sources) != 1:
        raise MailError('EXACTLY_ONE_PASSWORD_SOURCE_REQUIRED')
    k = sources[0]
    if k == 'password_env':
        value = os.environ.get(d[k])
    elif k == 'password_file':
        value = private_file(d[k]).read_text().rstrip('\r\n')
    else:
        value = d[k]
    if not isinstance(value, str) or not value:
        raise MailError('PASSWORD_UNAVAILABLE')
    return value


def utf7_encode(s):
    # RFC 3501 modified UTF-7, including literal ampersands.
    result, chunk = [], []
    def flush():
        if chunk:
            result.append('&' + base64.b64encode(''.join(chunk).encode('utf-16be')).decode().rstrip('=').replace('/', ',') + '-')
            chunk.clear()
    for c in s:
        if ' ' <= c <= '~':
            flush(); result.append('&-' if c == '&' else c)
        else:
            chunk.append(c)
    flush()
    return ''.join(result)


def utf7_decode(s):
    def decode(m):
        if not m[1]:
            return '&'
        b = m[1].replace(',', '/')
        return base64.b64decode(b + '=' * (-len(b) % 4)).decode('utf-16be')
    return re.sub(r'&([^-]*)-', decode, s)


def parse_folder(line):
    if isinstance(line, tuple):
        prefix, name = line
    else:
        prefix, name = line, None
    m = re.match(rb'^\(([^)]*)\) (NIL|"(?:\\.|[^"\\])*") (.*)$', prefix)
    if not m:
        raise MailError('UNSUPPORTED_FOLDER_RESPONSE')
    raw = name if name is not None else m[3]
    if raw.startswith(b'"') and raw.endswith(b'"'):
        raw = re.sub(rb'\\(.)', rb'\1', raw[1:-1])
    flags = m[1].decode('ascii').split()
    kind = next((k.lower() for k in ('Junk','Trash','Sent','Drafts','Archive') if '\\'+k in flags), None)
    folder = utf7_decode(raw.decode('ascii'))
    return {'name': folder, 'flags': flags, 'kind': kind, 'selectable': '\\Noselect' not in flags}


def date_filter(value):
    date = dt.date.fromisoformat(value)
    months = ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')
    return f'{date.day:02}-{months[date.month-1]}-{date.year}'


class Reader:
    def __init__(self, profile):
        self.profile = profile
        if profile.get('enabled') is False:
            raise MailError('ACCOUNT_DISABLED')
        self.client = imaplib.IMAP4_SSL(profile['host'], profile.get('port', 993),
                                       ssl_context=ssl.create_default_context(), timeout=20)
        try:
            self.client.login(profile['user'], password(profile))
        except Exception:
            self.close()
            raise

    def close(self):
        try:
            self.client.logout()  # Never CLOSE/EXPUNGE.
        except Exception:
            pass

    def select(self, mailbox):
        wire = utf7_encode(safe_text(mailbox))
        status, count = self.client.select(self.client._quote(wire), readonly=True)
        if status != 'OK':
            raise MailError('MAILBOX_UNAVAILABLE')
        validity = self.client.response('UIDVALIDITY')[1]
        if not validity or not validity[0]:
            raise MailError('UIDVALIDITY_UNAVAILABLE')
        return validity[0].decode(), int(count[0])

    def folders(self):
        status, rows = self.client.list()
        if status != 'OK':
            raise MailError('FOLDER_LIST_FAILED')
        return {'account':self.profile['id'], 'folders':[parse_folder(r) for r in rows if r not in (None, b')')]}

    def search(self, mailbox, query='', field='SUBJECT', since=None, before=None, before_uid=None, limit=20, exclude_uids=None):
        if not 1 <= limit <= 100 or field not in ('SUBJECT','FROM','TO','TEXT'):
            raise MailError('INVALID_SEARCH')
        validity, count = self.select(mailbox)
        criteria = []
        charset = None
        if query:
            safe_text(query)
            charset = 'UTF-8' if not query.isascii() else None
            quoted = self.client._quote(query)
            criteria += [field, quoted.encode('utf-8') if isinstance(quoted,str) else quoted]
        if since:
            criteria += ['SINCE', date_filter(since)]
        if before:
            criteria += ['BEFORE', date_filter(before)]
        if before_uid is not None:
            if before_uid <= 1:
                return {'account':self.profile['id'], 'mailbox':mailbox,'uidvalidity':validity,'messages':[], 'next_before_uid':None,'matched':0}
            criteria += ['UID', f'1:{before_uid-1}']
        status, data = self.client.uid('SEARCH', charset, *(criteria or ['ALL']))
        if status != 'OK':
            raise MailError('SEARCH_FAILED')
        uids = sorted((int(x) for x in (data[0] or b'').split()), reverse=True)
        if exclude_uids is not None:
            excluded={int(x) for x in exclude_uids}
            uids=[uid for uid in uids if uid not in excluded]
        selected = uids[:limit]
        messages = self.headers(selected)
        return dict(account=self.profile['id'], mailbox=mailbox, uidvalidity=validity,
                    mailbox_count=count, matched=len(uids), messages=messages,
                    next_before_uid=selected[-1] if len(uids)>limit else None,
                    contentTrust='untrusted email')

    def headers(self, uids):
        """One bounded PEEK request per page; validate every returned UID."""
        if not uids:return []
        expected=[str(x) for x in uids]
        if len(expected)>100 or any(not re.fullmatch(r'[1-9][0-9]{0,12}',x) for x in expected):
            raise MailError('INVALID_UID')
        item='BODY.PEEK[HEADER.FIELDS ('+' '.join(h.upper() for h in HEADERS)+')]<0.32768>'
        status,items=self.client.uid('FETCH',','.join(expected),f'(UID FLAGS INTERNALDATE {item})')
        if status!='OK':raise MailError('FETCH_FAILED')
        rows={}
        for literal in items:
            if not isinstance(literal,tuple):continue
            prefix,raw=literal
            match=re.search(rb'\bUID (\d+)\b',prefix)
            uid=match[1].decode() if match else None
            if uid not in expected or uid in rows or not raw:raise MailError('FETCH_IDENTITY_MISMATCH')
            msg=email.message_from_bytes(raw,policy=email.policy.default)
            flags=re.search(rb'FLAGS \(([^)]*)\)',prefix)
            internal=re.search(rb'INTERNALDATE "([^"]+)"',prefix)
            rows[uid]={'uid':uid,**{h:str(msg.get(h,''))[:4000] for h in HEADERS},
                'flags':flags[1].decode('ascii',errors='replace').split() if flags else [],
                'internal_date':internal[1].decode('ascii') if internal else None}
        if set(rows)!=set(expected):raise MailError('MESSAGE_UNAVAILABLE')
        return [rows[uid] for uid in expected]

    def fetch(self, uid, headers_only=False):
        if not re.fullmatch(r'[1-9][0-9]{0,12}', str(uid)):
            raise MailError('INVALID_UID')
        item = 'BODY.PEEK[HEADER.FIELDS ('+' '.join(h.upper() for h in HEADERS)+')]<0.32768>' if headers_only else f'BODY.PEEK[]<0.{MAX_BYTES}>'
        status, items = self.client.uid('FETCH', str(uid), f'(UID RFC822.SIZE FLAGS INTERNALDATE {item})')
        if status != 'OK':
            raise MailError('FETCH_FAILED')
        literals = [x for x in items if isinstance(x, tuple)]
        if len(literals) != 1 or not literals[0][1]:
            raise MailError('MESSAGE_UNAVAILABLE')
        prefix, raw = literals[0]
        match = re.search(rb'\bUID (\d+)\b', prefix)
        if not match or int(match[1]) != int(uid):
            raise MailError('FETCH_IDENTITY_MISMATCH')
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        result = {'uid':str(uid), **{h:str(msg.get(h, ''))[:4000] for h in HEADERS}}
        flags = re.search(rb'FLAGS \(([^)]*)\)', prefix)
        result['flags'] = flags[1].decode('ascii', errors='replace').split() if flags else []
        internal = re.search(rb'INTERNALDATE "([^"]+)"', prefix)
        result['internal_date'] = internal[1].decode('ascii') if internal else None
        if not headers_only:
            part = msg.get_body(preferencelist=('plain','html')) if msg.is_multipart() else msg
            body = part.get_content() if part and part.get_content_maintype()=='text' else ''
            if not isinstance(body, str):
                body = ''
            if part and part.get_content_type() == 'text/html':
                body = re.sub(r'(?is)<(script|style)\b[^>]*>.*?</\1>', '', body)
                body = html.unescape(re.sub(r'<[^>]*>', ' ', body))
                body = re.sub(r'\s+', ' ', body).strip()
            size = re.search(rb'RFC822.SIZE (\d+)', prefix)
            truncated = len(raw)>=MAX_BYTES
            # Some servers report a stale stored RFC822.SIZE after MIME conversion.
            # Verify EOF with a one-byte PEEK instead of treating every such mail as cut off.
            if size and int(size[1])>len(raw) and not truncated:
                probe_status, probe = self.client.uid('FETCH',str(uid),f'(UID BODY.PEEK[]<{len(raw)}.1>)')
                confirmed_empty = probe_status=='OK' and any(
                    (isinstance(x,tuple) and x[1]==b'' and re.search(rb'\bUID '+str(uid).encode()+rb'\b',x[0])) or
                    (isinstance(x,bytes) and re.search(rb'\bUID '+str(uid).encode()+rb'\b',x) and b'BODY[]<' in x and b'""' in x)
                    for x in probe)
                truncated = not confirmed_empty
            result.update(body=body[:60000], truncated=truncated or len(body)>60000,
                          content_sha256=hashlib.sha256(raw).hexdigest(),
                          attachments=[p.get_filename() for p in msg.iter_attachments()],
                          attachments_read=False, contentTrust='untrusted email')
        return result

    def read(self, mailbox, uidvalidity, uid):
        actual, _ = self.select(mailbox)
        if str(uidvalidity) != actual:
            raise MailError('UIDVALIDITY_CHANGED_SEARCH_AGAIN')
        return {**identity(self.profile), 'mailbox':mailbox, 'uidvalidity':actual, **self.fetch(uid)}


def broker_rpc(config, method, params=None):
    request = {'jsonrpc':'2.0', 'id':1, 'method':method, 'params':params or {}}
    path = safe_text(config['socket'])
    if not path.startswith('/'):
        raise MailError('BROKER_SOCKET_MUST_BE_ABSOLUTE')
    payload = (json.dumps(request)+'\n').encode()
    if config.get('ssh_host'):
        host = config['ssh_host']
        if not re.fullmatch(r'[a-zA-Z0-9_.@-]+',host) or host.startswith('-'):
            raise MailError('INVALID_SSH_HOST')
        code = "import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(30); s.connect(sys.argv[1]); s.sendall(sys.stdin.buffer.readline()); print(s.makefile().readline(),end='')"
        remote = 'python3 -c '+shlex.quote(code)+' '+shlex.quote(path)
        p = subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',host,remote],input=payload,capture_output=True,timeout=45)
        if p.returncode:
            raise MailError('BROKER_SSH_UNAVAILABLE')
        raw = p.stdout
    else:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(30); s.connect(path); s.sendall(payload)
            raw = s.makefile('rb').readline(1048576)
    response = json.loads(raw)
    if response.get('id') != 1 or 'error' in response:
        raise MailError('BROKER_RPC_FAILED')
    return response['result']


def broker_call(config, name, args):
    result = broker_rpc(config, 'tools/call', {'name':name,'arguments':args})
    if result.get('isError'):
        raise MailError('BROKER_TOOL_FAILED')
    return json.loads(next(c['text'] for c in result['content'] if c.get('type')=='text'))


def run(args):
    broker = args.broker_config or os.environ.get('MAIL_BROKER_CONFIG')
    if broker:
        if args.profiles_dir:
            raise MailError('CHOOSE_ONE_ACCESS_SOURCE')
        cfg = json.loads(Path(broker).expanduser().read_text())
        tools = {x['name']:x for x in broker_rpc(cfg,'tools/list')['tools']}
        accounts = broker_call(cfg,'mail_accounts',{})
        if args.command == 'accounts':
            return accounts
        if not any(a['account']==args.account for a in accounts):
            raise MailError('ACCOUNT_UNAVAILABLE')
        name = 'mail_'+args.command
        if name not in tools:
            raise MailError('BROKER_CAPABILITY_UNAVAILABLE')
        allowed = tools[name]['inputSchema']['properties']
        values = {k:getattr(args,k,None) for k in ('account','mailbox','query','field','since','before','before_uid','limit','uid','uidvalidity')}
        for key in ('since','before','before_uid'):
            if values[key] is not None and key not in allowed:
                raise MailError('BROKER_FILTER_UNSUPPORTED')
        call_args = {k:v for k,v in values.items() if k in allowed and v is not None}
        limit_max = allowed.get('limit',{}).get('maximum',100)
        if call_args.get('limit',0)>limit_max:
            raise MailError('BROKER_LIMIT_EXCEEDED')
        return broker_call(cfg,name,call_args)
    docs = profiles(args.profiles_dir)
    if args.command == 'accounts':
        return [identity(d) for d in docs.values()]
    if args.account not in docs:
        raise MailError('ACCOUNT_UNAVAILABLE')
    d = docs[args.account]
    reader = Reader(d)
    try:
        if args.command == 'status':
            validity, count = reader.select(args.mailbox)
            return {**identity(d),'connected':True,'readonly':True,'mailbox':args.mailbox,'uidvalidity':validity,'messages':count}
        if args.command == 'folders':
            return reader.folders()
        if args.command == 'search':
            return reader.search(args.mailbox,args.query,args.field,args.since,args.before,args.before_uid,args.limit)
        return reader.read(args.mailbox,args.uidvalidity,args.uid)
    finally:
        reader.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles-dir')
    parser.add_argument('--broker-config')
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('accounts')
    for command in ('status','folders','search','read'):
        p = sub.add_parser(command); p.add_argument('--account',required=True)
        p.add_argument('--mailbox',default='INBOX')
        if command=='search':
            p.add_argument('--query',default=''); p.add_argument('--field',default='SUBJECT',choices=['SUBJECT','FROM','TO','TEXT'])
            p.add_argument('--since'); p.add_argument('--before'); p.add_argument('--before-uid',type=int); p.add_argument('--limit',type=int,default=20)
        if command=='read':
            p.add_argument('--uid',required=True); p.add_argument('--uidvalidity',required=True)
    try:
        print(json.dumps(run(parser.parse_args()),ensure_ascii=False,indent=2))
    except MailError as e:
        print(json.dumps({'error':str(e)})); return 1
    except imaplib.IMAP4.error:
        print(json.dumps({'error':'IMAP_AUTH_OR_REQUEST_REJECTED'})); return 1
    except Exception as e:
        # Never expose server/credential-bearing exception text.
        print(json.dumps({'error':'MAIL_ACCESS_FAILED','type':type(e).__name__})); return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
