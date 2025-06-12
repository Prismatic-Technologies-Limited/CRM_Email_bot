import base64
import os
import time
import json
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from email.mime.text import MIMEText

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
TOKEN_PATH = 'token.json'
CREDENTIALS_PATH = 'credentials.json'

class GmailWatcher:
    def __init__(self):
        self.service = self.authenticate_gmail()

    def authenticate_gmail(self):
        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_PATH, 'w') as token:
                token.write(creds.to_json())
        return build('gmail', 'v1', credentials=creds)

    def fetch_unread(self):
        results = self.service.users().messages().list(userId='me', q='is:unread').execute()
        messages = results.get('messages', [])
        unread_emails = []

        for msg in messages:
            msg_data = self.service.users().messages().get(userId='me', id=msg['id']).execute()
            headers = msg_data['payload']['headers']
            subject = self._get_header(headers, 'Subject')
            sender = self._get_header(headers, 'From')
            thread_id = msg_data['threadId']
            snippet = msg_data.get('snippet', '')

            self.mark_as_read(msg['id'])

            unread_emails.append({
                'id': msg['id'],
                'threadId': thread_id,
                'from': sender,
                'subject': subject,
                'snippet': snippet
            })
        return unread_emails

    def extract_text(self, email):
        return email['snippet']

    def reply(self, email, reply_text):
        message = self.create_message(reply_text, email['threadId'], email['from'])
        self.service.users().messages().send(userId='me', body=message).execute()

        # Log message to external API
        self.post_to_prismatic(
            gmail=email['from'],
            client_msg=email['snippet'],
            agent_msg=reply_text
        )

    def mark_as_read(self, msg_id):
        self.service.users().messages().modify(
            userId='me',
            id=msg_id,
            body={'removeLabelIds': ['UNREAD']}
        ).execute()

    def create_message(self, text, thread_id, to_email):
        message = MIMEText(text)
        message['to'] = to_email
        message['subject'] = "Re: Your Inquiry"
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        return {'raw': raw, 'threadId': thread_id}

    def _get_header(self, headers, name):
        for header in headers:
            if header['name'] == name:
                return header['value']
        return ""

    def post_to_prismatic(self, gmail, client_msg, agent_msg):
        try:
            headers = {
                "Authorization": "Bearer YOUR_BEARER_TOKEN",
                "Content-Type": "application/json"
            }
            payload = {
                "gmail": gmail,
                "client_msg": client_msg,
                "agent_msg": agent_msg,
                "status": "replied"
            }
            res = requests.post("https://inhouse.prismaticcrm.com/api/post-comment", headers=headers, json=payload)
            res.raise_for_status()
        except Exception as e:
            print(f"[POST ERROR] Failed to post to Prismatic: {e}")
