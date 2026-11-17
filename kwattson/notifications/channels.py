"""Notification delivery channels.

Each channel class declares its fields (shown in the UI, secret fields stored
encrypted) and a ``send`` coroutine. Add a channel by subclassing ``Channel``
and listing it in ``CHANNELS``. A channel must raise on any failure, including
APIs that return HTTP 200 with an error body.
"""
from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import ClassVar

import httpx

from ..security import safe_destination


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    secret: bool = False
    required: bool = True
    help: str = ''
    pattern: str = ''


class Channel:
    kind: ClassVar[str]
    title: ClassVar[str]
    fields: ClassVar[tuple[Field, ...]]

    def __init__(self, config: dict):
        self.config = config

    @classmethod
    def validate(cls, config: dict) -> dict:
        clean = {}
        for f in cls.fields:
            value = str(config.get(f.name, '') or '').strip()
            if not value:
                if f.required:
                    raise ValueError(f'{cls.title}: {f.label} is required')
                continue
            if len(value) > 2000 or (f.pattern and not re.fullmatch(f.pattern, value)):
                raise ValueError(f'{cls.title}: {f.label} is not valid')
            clean[f.name] = value
        return clean

    async def send(self, title: str, body: str, severity: str, client: httpx.AsyncClient) -> dict:
        raise NotImplementedError


class Ntfy(Channel):
    kind, title = 'ntfy', 'ntfy'
    fields = (Field('server', 'Server URL', required=False, help='Default https://ntfy.sh'),
              Field('topic', 'Topic', pattern=r'[A-Za-z0-9_-]{1,64}', help='Use a long, unguessable topic on public servers'),
              Field('token', 'Access token', secret=True, required=False))

    async def send(self, title, body, severity, client):
        base = safe_destination(self.config.get('server', 'https://ntfy.sh').rstrip('/'))
        headers = {'Title': title.encode('ascii', 'replace').decode(), 'Priority': {'critical': '5', 'warning': '4'}.get(severity, '3')}
        if self.config.get('token'):
            headers['Authorization'] = 'Bearer ' + self.config['token']
        r = await client.post(f"{base}/{self.config['topic']}", content=body.encode(), headers=headers)
        r.raise_for_status()
        return {'status': r.status_code}


class Telegram(Channel):
    kind, title = 'telegram', 'Telegram'
    fields = (Field('token', 'Bot token', secret=True, pattern=r'\d+:[A-Za-z0-9_-]+'),
              Field('chat_id', 'Chat id', pattern=r'-?\d{1,20}'))

    async def send(self, title, body, severity, client):
        r = await client.post(f"https://api.telegram.org/bot{self.config['token']}/sendMessage",
                              json={'chat_id': self.config['chat_id'], 'text': f'{title}\n{body}'[:4000]})
        data = r.json() if r.content else {}
        if r.status_code != 200 or not data.get('ok'):
            raise RuntimeError(f'Telegram refused the message ({r.status_code})')
        return {'status': r.status_code}


class Pushover(Channel):
    kind, title = 'pushover', 'Pushover'
    fields = (Field('token', 'Application token', secret=True, pattern=r'[A-Za-z0-9]{30}'),
              Field('user', 'User key', secret=True, pattern=r'[A-Za-z0-9]{30}'))

    async def send(self, title, body, severity, client):
        r = await client.post('https://api.pushover.net/1/messages.json', data={
            'token': self.config['token'], 'user': self.config['user'], 'title': title[:250], 'message': body[:1000],
            'priority': {'critical': 1}.get(severity, 0)})
        data = r.json() if r.content else {}
        if r.status_code != 200 or data.get('status') != 1:
            raise RuntimeError(f'Pushover refused the message ({r.status_code})')
        return {'status': r.status_code}


class Discord(Channel):
    kind, title = 'discord', 'Discord webhook'
    fields = (Field('url', 'Webhook URL', secret=True, pattern=r'https://(discord\.com|discordapp\.com)/api/webhooks/\d+/[A-Za-z0-9_-]+'),)

    async def send(self, title, body, severity, client):
        safe_destination(self.config['url'], {'discord.com', 'discordapp.com'})
        r = await client.post(self.config['url'], json={'content': f'**{title}**\n{body}'[:1900], 'allowed_mentions': {'parse': []}})
        r.raise_for_status()
        return {'status': r.status_code}


class HomeAssistantNotify(Channel):
    kind, title = 'homeassistant', 'Home Assistant'
    fields = (Field('url', 'Home Assistant URL', help='e.g. https://ha.example.com:8123'),
              Field('token', 'Long-lived token', secret=True),
              Field('service', 'Notify service', pattern=r'[a-z0-9_]{1,64}', help='e.g. mobile_app_your_phone'))

    async def send(self, title, body, severity, client):
        base = safe_destination(self.config['url'].rstrip('/'))
        r = await client.post(f"{base}/api/services/notify/{self.config['service']}", json={'title': title, 'message': body},
                              headers={'Authorization': 'Bearer ' + self.config['token']})
        r.raise_for_status()
        return {'status': r.status_code}


class Webhook(Channel):
    kind, title = 'webhook', 'Generic webhook (JSON POST)'
    fields = (Field('url', 'URL', secret=True), Field('token', 'Bearer token', secret=True, required=False))

    async def send(self, title, body, severity, client):
        safe_destination(self.config['url'])
        headers = {'Authorization': 'Bearer ' + self.config['token']} if self.config.get('token') else {}
        r = await client.post(self.config['url'], json={'title': title, 'body': body, 'severity': severity, 'source': 'kwattson'}, headers=headers)
        r.raise_for_status()
        return {'status': r.status_code}


class Email(Channel):
    kind, title = 'email', 'Email (SMTP)'
    fields = (Field('host', 'SMTP host', pattern=r'[A-Za-z0-9.-]{1,253}'), Field('port', 'Port (465 or 587)', pattern=r'465|587'),
              Field('username', 'Username', required=False), Field('password', 'Password', secret=True, required=False),
              Field('sender', 'From address', pattern=r'[^@\s]+@[^@\s]+'), Field('to', 'To address', pattern=r'[^@\s]+@[^@\s]+'))

    async def send(self, title, body, severity, client):
        await asyncio.to_thread(self._send, title, body)
        return {'status': 'sent'}

    def _send(self, title, body):
        c = self.config
        safe_destination('https://' + c['host'])
        message = EmailMessage()
        message['Subject'], message['From'], message['To'] = title, c['sender'], c['to']
        message.set_content(body)
        context = ssl.create_default_context()
        port = int(c['port'])
        smtp = smtplib.SMTP_SSL(c['host'], port, timeout=20, context=context) if port == 465 else smtplib.SMTP(c['host'], port, timeout=20)
        with smtp:
            if port == 587:
                smtp.starttls(context=context)
            if c.get('username'):
                smtp.login(c['username'], c.get('password', ''))
            smtp.send_message(message)


CHANNELS: dict[str, type[Channel]] = {c.kind: c for c in (Ntfy, Telegram, Pushover, Discord, HomeAssistantNotify, Webhook, Email)}


def describe():
    return [dict(kind=c.kind, title=c.title, fields=[dict(name=f.name, label=f.label, secret=f.secret, required=f.required, help=f.help)
                                                      for f in c.fields]) for c in CHANNELS.values()]
