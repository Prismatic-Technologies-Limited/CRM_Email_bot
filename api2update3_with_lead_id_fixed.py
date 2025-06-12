# main.py
import os
import json
import pickle
import base64
import asyncio
import logging
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from src.graph import Workflow  # <- your LLM pipeline
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/gmail.send']

# Monitoring state
monitoring_state = {
    "active": False,
    "last_check": None,
    "emails_processed": 0,
    "processed_email_ids": set(),
    "api_posts": 0,
    "api_failures": 0
}

class WelcomeEmailRequest(BaseModel):
    client_name: str
    client_email: EmailStr
    product_interest: str

class ExternalAPIData(BaseModel):
    email: EmailStr
    phone: str = ""
    comments: str
    user_type: str = "customer"

class MonitoringStatus(BaseModel):
    is_active: bool
    last_check: Optional[datetime]
    emails_processed: int

class EmailProcessingResponse(BaseModel):
    success: bool
    message: str
    email_id: Optional[str] = None

class GmailService:
    def __init__(self):
        self.service = None
        self.setup()

    def setup(self):
        creds = None
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
            with open('token.pickle', 'wb') as token:
                pickle.dump(creds, token)
        self.service = build('gmail', 'v1', credentials=creds)

    def get_emails_since(self, since: datetime) -> List[Dict]:
        query = f"after:{since.strftime('%Y/%m/%d')} in:inbox"
        results = self.service.users().messages().list(userId='me', q=query, maxResults=50).execute()
        emails = []
        for msg in results.get('messages', []):
            detail = self.get_email_detail(msg['id'])
            if detail:
                emails.append(detail)
        return emails

    def get_email_detail(self, message_id: str) -> Optional[Dict]:
        try:
            msg = self.service.users().messages().get(userId='me', id=message_id, format='full').execute()
            headers = msg['payload'].get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), '')
            body = self.extract_body(msg['payload'])
            return {
                "id": message_id,
                "subject": subject,
                "sender": sender,
                "body": body,
                "timestamp": datetime.fromtimestamp(int(msg['internalDate']) / 1000)
            }
        except:
            return None

    def extract_body(self, payload) -> str:
        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                    return base64.urlsafe_b64decode(part['body']['data']).decode()
        elif 'data' in payload['body']:
            return base64.urlsafe_b64decode(payload['body']['data']).decode()
        return ""

    def send_email(self, to_email: str, subject: str, body: str):
        from email.mime.text import MIMEText
        message = MIMEText(body)
        message['to'] = to_email
        message['subject'] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        return self.service.users().messages().send(userId='me', body={'raw': raw}).execute()

class EmailAutomation:
    def __init__(self):
        self.gmail = GmailService()
        self.workflow = Workflow()
        self.api = "https://inhouse.prismaticcrm.com/api/post-comment"
        self.token = "Bearer 31|GIi7u4iBXfSOfxYVnMEcRrBzOkpYUBm6Adzxx2j9"

    def extract_email(self, sender: str) -> str:
        import re
        match = re.search(r'<([^>]+)>', sender)
        return match.group(1) if match else sender

    async def post_to_api(self, email: str, comment: str) -> bool:
        payload = {
            "email": self.extract_email(email),
            "phone": "",
            "comments": comment,
            "user_type": "customer"
        }
        headers = {"Authorization": self.token, "Content-Type": "application/json"}
        try:
            r = requests.post(self.api, headers=headers, json=payload)
            monitoring_state["api_posts"] += 1 if r.status_code in [200, 201] else 0
            monitoring_state["api_failures"] += 0 if r.status_code in [200, 201] else 1
            return r.status_code in [200, 201]
        except:
            monitoring_state["api_failures"] += 1
            return False

    async def send_welcome_email(self, name, email, interest):
        subject = f"Welcome {name} - {interest}"
        body = f"Hello {name},\nThanks for showing interest in {interest}.\n\nBest,\nPrismatic"
        self.gmail.send_email(email, subject, body)
        await self.post_to_api(email, f"WELCOME EMAIL to {name} about {interest}")
        return True

    async def process_email(self, email_data: Dict):
        eid = email_data['id']
        if eid in monitoring_state["processed_email_ids"]:
            return
        monitoring_state["processed_email_ids"].add(eid)

        await self.post_to_api(email_data['sender'], f"INCOMING: {email_data['subject']}")
        state = {
            "emails": [email_data],
            "current_email": email_data,
            "email_category": "inquiry",
            "generated_email": "",
            "sendable": False
        }

        generated = ""
        sendable = False
        for step in self.workflow.app.stream(state, {"recursion_limit": 100}):
            generated = step.get("generated_email", generated)
            sendable = step.get("sendable", sendable)

        if sendable and generated:
            recipient = self.extract_email(email_data['sender'])
            subject = f"Re: {email_data['subject']}"
            self.gmail.send_email(recipient, subject, generated)
            await self.post_to_api(recipient, f"REPLY: {generated[:500]}")

        monitoring_state["emails_processed"] += 1

    async def monitor_loop(self):
        monitoring_state["active"] = True
        monitoring_state["last_check"] = datetime.now() - timedelta(hours=1)
        while monitoring_state["active"]:
            try:
                emails = self.gmail.get_emails_since(monitoring_state["last_check"])
                for e in emails:
                    await self.process_email(e)
                    await asyncio.sleep(1)
                if emails:
                    latest = max(e["timestamp"] for e in emails)
                    monitoring_state["last_check"] = latest
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Monitoring error: {str(e)}")
                await asyncio.sleep(60)

automation = EmailAutomation()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting email automation service")
    yield
    monitoring_state["active"] = False
    logger.info("Stopping email automation service")

app = FastAPI(lifespan=lifespan)

@app.post("/send-welcome-email")
async def send_welcome_email(req: WelcomeEmailRequest):
    await automation.send_welcome_email(req.client_name, req.client_email, req.product_interest)
    return {"message": "Welcome email sent"}

@app.post("/start-monitoring")
async def start_monitoring():
    if not monitoring_state["active"]:
        asyncio.create_task(automation.monitor_loop())
        return {"message": "Monitoring started"}
    return {"message": "Already running"}

@app.post("/stop-monitoring")
async def stop_monitoring():
    monitoring_state["active"] = False
    return {"message": "Monitoring stopped"}

@app.get("/monitoring-status", response_model=MonitoringStatus)
async def get_status():
    return MonitoringStatus(
        is_active=monitoring_state["active"],
        last_check=monitoring_state["last_check"],
        emails_processed=monitoring_state["emails_processed"]
    )

