import os
import re
import time
import json
import base64
import pickle
import asyncio
import logging
import requests
import threading
from src.graph import Workflow
from dotenv import load_dotenv
from colorama import Fore, Style
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from pydantic import BaseModel, EmailStr
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 
          'https://www.googleapis.com/auth/gmail.send']
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class WelcomeEmailRequest(BaseModel):
    client_name: str
    client_email: EmailStr
    product_interest: str

class ExternalAPIData(BaseModel):
    email: EmailStr
    phone: str = ""
    comments: str
    user_type: str = "customer"

class EmailProcessingResponse(BaseModel):
    success: bool
    message: str
    email_id: Optional[str] = None

class MonitoringStatus(BaseModel):
    is_active: bool
    last_check: Optional[datetime] = None
    emails_processed: int = 0

class BlockEmailRequest(BaseModel):
    email: EmailStr
    reason: str = "Manual block"

class UnblockEmailRequest(BaseModel):
    email: EmailStr

class BlockedEmailResponse(BaseModel):
    success: bool
    message: str
    email: str
    is_blocked: bool

# Global monitoring state
monitoring_state = {
    "active": False,
    "last_check": None,
    "emails_processed": 0,
    "thread": None,
    "api_posts_count": 0,
    "api_failures": 0,
    "processed_email_ids": set()  # Track processed emails to avoid duplicates
}

# Global blocked emails state
blocked_emails_state = {
    "blocked_emails": set(),  # Set of blocked email addresses
    "blocked_count": 0,       # Count of blocked response attempts
    "last_blocked": None      # Last blocked email timestamp
}

class GmailService:
    def __init__(self):
        self.service = None
        self.credentials = None
        self.setup_gmail_service()
    
    def setup_gmail_service(self):
        try:
            creds = None
            if os.path.exists('token.pickle'):
                with open('token.pickle', 'rb') as token:
                    creds = pickle.load(token)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        'credentials.json', SCOPES)  
                    creds = flow.run_local_server(port=0)
                with open('token.pickle', 'wb') as token:
                    pickle.dump(creds, token)
            
            self.service = build('gmail', 'v1', credentials=creds)
            self.credentials = creds
            logger.info("Gmail service setup successful")
            
        except Exception as e:
            logger.error(f"Error setting up Gmail service: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Gmail setup failed: {str(e)}")
    
    def get_emails_since(self, since_time: datetime) -> List[Dict]:
        """Fetch emails since specified time"""
        try:
            query_time = since_time.strftime('%Y/%m/%d')
            query = f'after:{query_time} in:inbox'
            results = self.service.users().messages().list(
                userId='me', q=query, maxResults=50
            ).execute()
            
            messages = results.get('messages', [])
            emails = []
            
            for message in messages:
                email_data = self.get_email_details(message['id'])
                if email_data:
                    emails.append(email_data)
            
            return emails
            
        except HttpError as e:
            logger.error(f"Gmail API error: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"Error fetching emails: {str(e)}")
            return []
    
    def get_email_details(self, message_id: str) -> Optional[Dict]:
        """Get detailed information about a specific email"""
        try:
            message = self.service.users().messages().get(
                userId='me', id=message_id, format='full'
            ).execute()
            
            headers = message['payload'].get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), '')
            thread_id = message.get('threadId', '')
            references = next((h['value'] for h in headers if h['name'] == 'References'), '')
            
            # Extract body
            body = self.extract_email_body(message['payload'])
            
            return {
                'id': message_id,
                'threadId': thread_id,
                'messageId': message_id,
                'references': references,
                'sender': sender,
                'subject': subject,
                'body': body,
                'timestamp': datetime.fromtimestamp(int(message['internalDate']) / 1000)
            }
            
        except Exception as e:
            logger.error(f"Error getting email details: {str(e)}")
            return None
    
    def extract_email_body(self, payload) -> str:
        """Extract email body from Gmail payload"""
        try:
            if 'parts' in payload:
                for part in payload['parts']:
                    if part['mimeType'] == 'text/plain':
                        if 'data' in part['body']:
                            return base64.urlsafe_b64decode(
                                part['body']['data']
                            ).decode('utf-8')
            elif payload['mimeType'] == 'text/plain':
                if 'data' in payload['body']:
                    return base64.urlsafe_b64decode(
                        payload['body']['data']
                    ).decode('utf-8')
            return ""
        except Exception as e:
            logger.error(f"Error extracting email body: {str(e)}")
            return ""
    
    def send_email(self, to_email: str, subject: str, body: str) -> bool:
        """Send email using Gmail API"""
        try:
            message = self.create_message('me', to_email, subject, body)
            sent_message = self.service.users().messages().send(
                userId='me', body=message
            ).execute()
            logger.info(f"Email sent successfully. Message ID: {sent_message['id']}")
            return True
        except Exception as e:
            logger.error(f"Error sending email: {str(e)}")
            return False
    
    def create_message(self, sender: str, to: str, subject: str, body: str) -> Dict:
        """Create email message for Gmail API"""
        from email.mime.text import MIMEText
        
        message = MIMEText(body)
        message['to'] = to
        message['subject'] = subject
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        return {'raw': raw_message}

class EmailAutomationService:
    def __init__(self):
        self.config = {'recursion_limit': 100}
        self.workflow = Workflow()
        self.app = self.workflow.app
        # Updated API endpoint to your specific endpoint
        self.api_endpoint = "https://inhouse.prismaticcrm.com/api/post-comment"
        self.bearer_token = "31|GIi7u4iBXfSOfxYVnMEcRrBzOkpYUBm6Adzxx2j9"
        self.gmail_service = GmailService()

    def get_api_headers(self):
        """Get headers with Bearer token authentication"""
        return {
            'Authorization': f'Bearer {self.bearer_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
    def get_initial_state(self, email_data=None):
        """Get initial state for workflow"""
        return {
            "emails": email_data or [],
            "current_email": {
                "id": "",
                "threadId": "",
                "messageId": "",
                "references": "",
                "Hamza": "",
                "sender": "",
                "subject": "",
                "body": ""
            },
            "email_category": "",
            "generated_email": "",
            "rag_queries": [],
            "retrieved_documents": "",
            "writer_messages": [],
            "sendable": False,
            "trials": 0
        }
    
    def extract_incoming_email(self, sender_field: str) -> str:
        """
        Extract sender email from the incoming email's 'From' field.
        This is typically formatted like: "Name <email@domain.com>"
        """
        try:
            if "<" in sender_field and ">" in sender_field:
                match = re.search(r'<([^>]+)>', sender_field)
                if match:
                    return match.group(1).strip()
            match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', sender_field)
            if match:
                return match.group(0).strip()
            return sender_field.strip()
        except Exception as e:
            logger.error(f"[Incoming Email Extractor] Error extracting email from '{sender_field}': {str(e)}")
            return sender_field

    def extract_outgoing_email(self, recipient_field: str) -> str:
        """
        Extract clean recipient email from outgoing email's 'To' field.
        Assumes email may be plain or formatted like: "Name <email@domain.com>"
        """
        try:
            match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', recipient_field)
            if match:
                return match.group(0).strip()
            return recipient_field.strip()
        except Exception as e:
            logger.error(f"[Outgoing Email Extractor] Error extracting email from '{recipient_field}': {str(e)}")
            return recipient_field
        


    def is_email_blocked(self, email: str) -> bool:
        """Check if an email address is blocked from receiving responses"""
        clean_email = self.extract_incoming_email(email).lower()
        return clean_email in blocked_emails_state["blocked_emails"]
    
    def block_email(self, email: str, reason: str = "Manual block") -> Dict:
        """Block an email address from receiving automated responses"""
        try:
            clean_email = self.extract_incoming_email(email).lower()
            
            if clean_email in blocked_emails_state["blocked_emails"]:
                return {
                    "success": False,
                    "message": f"Email {clean_email} is already blocked",
                    "email": clean_email,
                    "is_blocked": True
                }
            
            blocked_emails_state["blocked_emails"].add(clean_email)
            logger.info(f"🚫 Blocked email: {clean_email} - Reason: {reason}")
            
            return {
                "success": True,
                "message": f"Successfully blocked {clean_email} from receiving automated responses",
                "email": clean_email,
                "is_blocked": True,
                "reason": reason
            }
            
        except Exception as e:
            logger.error(f"Error blocking email: {str(e)}")
            return {
                "success": False,
                "message": f"Error blocking email: {str(e)}",
                "email": email,
                "is_blocked": False
            }
    
    def unblock_email(self, email: str) -> Dict:
        """Unblock an email address to resume automated responses"""
        try:
            clean_email = self.extract_incoming_email(email).lower()
            
            if clean_email not in blocked_emails_state["blocked_emails"]:
                return {
                    "success": False,
                    "message": f"Email {clean_email} is not currently blocked",
                    "email": clean_email,
                    "is_blocked": False
                }
            
            blocked_emails_state["blocked_emails"].remove(clean_email)
            logger.info(f"✅ Unblocked email: {clean_email}")
            
            return {
                "success": True,
                "message": f"Successfully unblocked {clean_email} - will now receive automated responses",
                "email": clean_email,
                "is_blocked": False
            }
            
        except Exception as e:
            logger.error(f"Error unblocking email: {str(e)}")
            return {
                "success": False,
                "message": f"Error unblocking email: {str(e)}",
                "email": email,
                "is_blocked": True
            }


    async def send_to_external_api(
        self,
        email: str,
        comments: str,
        user_type: str = "",
        phone: str = "",
        lead_id: str = ""
    ) -> bool:
        global monitoring_state

        try:
            payload = {
                "email": email,  # email is already cleaned by caller
                "lead_id": lead_id,
                "phone": phone,
                "comments": comments,
                "user_type": user_type
            }

            headers = self.get_api_headers()
            logger.info(f"=== EXTERNAL API POST ATTEMPT ===")
            logger.info(f"Endpoint: {self.api_endpoint}")
            logger.info(f"Email: {email}")
            logger.info(f"User Type: {user_type}")
            logger.info(f"Comments Length: {len(comments)} chars")
            logger.info(f"Headers: {headers}")
            logger.info(f"Full Payload: {json.dumps(payload, indent=2)}")

            response = requests.post(
                self.api_endpoint,
                json=payload,
                headers=headers,
                timeout=30
            )

            logger.info(f"API Response Status: {response.status_code}")
            logger.info(f"API Response Headers: {dict(response.headers)}")
            logger.info(f"API Response Body: {response.text}")

            if response.status_code in [200, 201]:
                logger.info(f"✅ SUCCESS: Data posted to API for {email}")
                monitoring_state["api_posts_count"] += 1
                return True
            else:
                logger.error(f"❌ API ERROR: Status {response.status_code} - {response.text}")
                monitoring_state["api_failures"] += 1
                return False

        except requests.exceptions.Timeout:
            logger.error(f"⏱️ API TIMEOUT: Request timed out after 30 seconds")
            monitoring_state["api_failures"] += 1
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error(f"🌐 API CONNECTION ERROR: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"📡 API REQUEST ERROR: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False
        except Exception as e:
            logger.error(f"💥 UNEXPECTED ERROR in send_to_external_api: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False

    
    async def send_welcome_email(self, client_name: str, client_email: str, product_interest: str) -> Dict:
        """Send welcome email to new client"""
        try:
            logger.info(f"Processing welcome email for {client_name} ({client_email})")
            welcome_email_data = {
                "recipient": client_email,
                "recipient_name": client_name,
                "product_interest": product_interest,
                "email_type": "welcome"
            }
            
            initial_state = self.get_initial_state([welcome_email_data])
            initial_state["email_category"] = "welcome"
            initial_state["current_email"]["sender"] = client_email
            initial_state["current_email"]["subject"] = f"Welcome {client_name}! Your Interest in {product_interest}"
            generated_content = ""
            try:
                for output in self.app.stream(initial_state, self.config):
                    for key, value in output.items():
                        if key == "generated_email":
                            generated_content = str(value)
                        logger.info(f"Welcome email workflow step: {key}")
            except Exception as workflow_error:
                logger.error(f"Workflow error: {workflow_error}")
                generated_content = f"""
Welcome {client_name}! Thank you for your interest in {product_interest}. If you want to more information kindly reply this email. 
I will provide more information as per you required.
                
Best regards,
Prismatic Technologies"""
            
            email_subject = f"Welcome {client_name}! Your Interest in {product_interest}"
            email_body = generated_content or f"""
Welcome {client_name}! Thank you for your interest in {product_interest}. If you want to more information kindly reply this email. 
I will provide more information as per you required.
                
Best regards,
Prismatic Technologies"""
            
            # Send email via Gmail API
            email_sent = self.gmail_service.send_email(
                to_email=client_email,
                subject=email_subject,
                body=email_body
            )
            
            # ALWAYS post outgoing email to external API (regardless of send success)
            comments = f"OUTGOING WELCOME EMAIL - Subject: {email_subject} | Body: {email_body[:200]}... | Product Interest: {product_interest} | Delivery Status: {'Sent successfully' if email_sent else 'Failed to send'}"
            #comments = f"{email_body[:-1]}"
            
            logger.info(f" Posting welcome email to external API...")
            api_success = await self.send_to_external_api(
                email=client_email,
                lead_id="",
                phone="",
                comments=comments,
                user_type="customer"
            )
            
            result = {
                "success": email_sent,
                "message": f"Welcome email {'sent successfully' if email_sent else 'failed to send'} to {client_name}",
                "email_sent": email_sent,
                "api_posted": api_success,
                "api_endpoint": self.api_endpoint,
                "email_body_preview": email_body[:200] + "..." if len(email_body) > 200 else email_body
            }
            
            logger.info(f" Welcome email result: {result}")
            return result
                
        except Exception as e:
            logger.error(f" Error sending welcome email: {str(e)}")
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "email_sent": False,
                "api_posted": False
            }
    
    # MODIFY your existing process_incoming_email method
    async def process_incoming_email(self, email_data: Dict) -> Dict:
        """Process a single incoming email"""
        global monitoring_state

        try:
            email_id = email_data.get("id", "")
            sender = email_data.get("sender", "Unknown")
            sender_email = self.extract_incoming_email(sender)

            # Skip if already processed
            if email_id in monitoring_state["processed_email_ids"]:
                logger.info(f"⏭️ Skipping already processed email {email_id}")
                return {
                    "success": True,
                    "message": "Email already processed",
                    "email_id": email_id,
                    "skipped": True
                }

            logger.info(f"📥 Processing incoming email from: {sender}")
            logger.info(f"📧 Email ID: {email_id}")
            logger.info(f"📋 Subject: {email_data.get('subject', 'No Subject')}")

            # FIRST: Post incoming email to external API (always do this)
            logger.info(f"📤 Posting incoming email to external API...")
            api_posted = await self.post_incoming_email_to_api(email_data)

            # Mark as processed regardless of API success
            monitoring_state["processed_email_ids"].add(email_id)

            # CHECK IF EMAIL IS BLOCKED BEFORE GENERATING RESPONSE
            if self.is_email_blocked(sender):
                blocked_emails_state["blocked_count"] += 1
                blocked_emails_state["last_blocked"] = datetime.now()
                logger.info(f"🚫 BLOCKED: Not sending response to blocked email: {sender_email}")
                
                return {
                    "success": True,
                    "message": f"Email from {sender_email} processed but response blocked",
                    "email_id": email_id,
                    "response_sent": False,
                    "blocked": True,
                    "api_posted": api_posted,
                    "sender_email": sender_email
                }

            # Prepare email for workflow (only if not blocked)
            initial_state = self.get_initial_state([email_data])
            initial_state["current_email"] = {
                "id": email_id,
                "threadId": email_data.get("threadId", ""),
                "messageId": email_data.get("messageId", ""),
                "references": email_data.get("references", ""),
                "sender": sender,
                "subject": email_data.get("subject", ""),
                "body": email_data.get("body", "")
            }

            # Run the workflow
            generated_response = ""
            sendable = False
            try:
                for output in self.app.stream(initial_state, self.config):
                    for key, value in output.items():
                        if key == "generated_email":
                            generated_response = str(value)
                        elif key == "sendable":
                            sendable = bool(value)
                        logger.info(f"📊 Workflow step: {key}")
                sendable = initial_state.get("sendable", sendable)
            except Exception as workflow_error:
                logger.error(f"❌ Workflow error: {workflow_error}")
                generated_response = "Thank you for your email. We have received your message and will respond shortly."
                sendable = True

            # Send response email if generated and sendable
            email_sent = False
            reply_api_posted = False

            if generated_response and sendable:
                logger.info(f"📧 Sending reply email to {sender}...")
                reply_subject = f"Re: {email_data.get('subject', '')}"
                email_sent = self.gmail_service.send_email(
                    to_email=self.extract_email_from_sender(sender),
                    subject=reply_subject,
                    body=generated_response
                )

                logger.info(f"📨 Gmail reply result: {'✅ Sent' if email_sent else '❌ Failed'}")

                # Post the outgoing reply to the external API
                try:
                    logger.info(f"📤 Posting outgoing reply email to external API...")
                    reply_api_posted = await self.post_outgoing_email_to_api(
                        recipient_email=sender,
                        subject=reply_subject,
                        body=generated_response,
                        email_type="reply",
                        original_email_id=email_id,
                        sent_successfully=email_sent
                    )
                except Exception as post_err:
                    logger.error(f"💥 Failed to post reply to external API: {str(post_err)}")

            else:
                logger.info(f"⏭️ No reply sent - Generated: {bool(generated_response)}, Sendable: {sendable}")

            monitoring_state["emails_processed"] += 1

            result = {
                "success": True,
                "message": f"Successfully processed email from {sender}",
                "email_id": email_id,
                "response_sent": email_sent,
                "blocked": False,
                "api_posted": api_posted,
                "reply_api_posted": reply_api_posted,
                "sender_email": sender_email,
                "generated_response_preview": generated_response[:200] + "..." if len(generated_response) > 200 else generated_response
            }

            logger.info(f"🎯 Final processing result: {result}")
            return result

        except Exception as e:
            logger.error(f"💥 Error processing email: {str(e)}")
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "email_id": email_data.get("id", ""),
                "blocked": False,
                "api_posted": False
            }
#################################################################
    # async def post_incoming_email_to_api(self, email_data: Dict) -> bool:
    #     """Post incoming email details to external API"""
    #     try:
    #         sender_email = self.extract_incoming_email(email_data.get("sender", ""))
    #         subject = email_data.get("subject", "No Subject")
    #         body = email_data.get("body", "")
    #         timestamp = email_data.get("timestamp", datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    #         # === Clean up quoted reply content ===
    #         # Always move outgoing quoted message to new line with proper label
    #         body_cleaned = re.sub(
    #             r"(On\s.+?wrote:)",
    #             r"\nOUTGOING EMAIL:wrote:",
    #             body,
    #             flags=re.IGNORECASE
    #         )

    #         # Also handle common fallback format (email pasted without Gmail quote line)
    #         body_cleaned = body_cleaned.replace("support@prismatic-technologies.com.pk", "OUTGOING EMAIL:")

    #         # Make sure OUTGOING EMAIL always starts on a new line (in case any remained inline)
    #         body_cleaned = re.sub(r"\s*OUTGOING EMAIL:wrote:", r"\nOUTGOING EMAIL:wrote:", body_cleaned)

    #         # Trim overly long content
    #         if len(body_cleaned) > 800:
    #             body_cleaned = body_cleaned[:800] + "..."

    #         comments = (
    #             f"INCOMING EMAIL - Received: {timestamp} | Subject: {subject} | "
    #             f"From: {sender_email} | Body: {body_cleaned}"
    #         )

    #         return await self.send_to_external_api(
    #             email=sender_email,
    #             phone="",
    #             comments=comments,
    #             user_type="customer"
    #         )

    #     except Exception as e:
    #         logger.error(f"💥 Error posting incoming email to API: {str(e)}")
    #         return False


#################################################################    

    async def post_incoming_email_to_api(self, email_data: Dict) -> bool:
        """Post incoming email details to external API"""
        try:
            sender_email = self.extract_incoming_email(email_data.get("sender", ""))
            subject = email_data.get("subject", "No Subject")
            body = email_data.get("body", "")
            timestamp = email_data.get("timestamp", datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
            
            # === Clean up quoted reply content ===
            # Find where outgoing email content starts and move everything from there to new line
            
            # Pattern 1: Look for "OUTGOING EMAIL" text (case insensitive)
            outgoing_match = re.search(r'OUTGOING EMAIL', body, re.IGNORECASE)
            if outgoing_match:
                before_outgoing = body[:outgoing_match.start()].strip()
                outgoing_content = body[outgoing_match.start():].strip()
                body_cleaned = before_outgoing + "\n\n" + outgoing_content
            else:
                # Pattern 2: Look for Gmail quote pattern "On ... wrote:"
                gmail_match = re.search(r'(On\s.+?wrote:)', body, re.IGNORECASE | re.DOTALL)
                if gmail_match:
                    before_quote = body[:gmail_match.start()].strip()
                    quote_content = body[gmail_match.start():].strip()
                    body_cleaned = before_quote + "\n\nOUTGOING EMAIL - " + quote_content
                else:
                    # Pattern 3: Look for email address that indicates quoted content
                    email_match = re.search(r'support@prismatic-technologies\.com\.pk', body, re.IGNORECASE)
                    if email_match:
                        before_email = body[:email_match.start()].strip()
                        email_content = body[email_match.start():].strip()
                        body_cleaned = before_email + "\n\nOUTGOING EMAIL: " + email_content
                    else:
                        body_cleaned = body
            
            # Remove multiple consecutive newlines (keep max 2)
            body_cleaned = re.sub(r'\n{3,}', '\n\n', body_cleaned)
            
            # Trim leading/trailing whitespace
            body_cleaned = body_cleaned.strip()
            
            # Trim overly long content
            if len(body_cleaned) > 800:
                # Try to cut at a reasonable point (end of sentence or paragraph)
                cut_point = body_cleaned.rfind('.', 0, 800)
                if cut_point == -1:
                    cut_point = body_cleaned.rfind(' ', 0, 800)
                if cut_point == -1:
                    cut_point = 800
                body_cleaned = body_cleaned[:cut_point] + "..."
            
            comments = (
                f"INCOMING EMAIL - Received: {timestamp} | Subject: {subject} | "
                f"From: {sender_email} | Body: {body_cleaned}"
            )
            
            return await self.send_to_external_api(
                email=sender_email,
                phone="",
                comments=comments,
                user_type="customer"
            )
            
        except Exception as e:
            logger.error(f"💥 Error posting incoming email to API: {str(e)}")
            return False

    async def post_outgoing_email_to_api(
        self,
        recipient_email: str,
        subject: str,
        body: str,
        email_type: str = "reply",
        sender_name: str = "",
        product_interest: str = "",
        sent_successfully: bool = True,
        original_email_id: str = "",
        lead_id: str = "3446"
    ) -> bool:
        """Post outgoing email details to external API"""
        try:
            clean_email = self.extract_outgoing_email(recipient_email)
            if not clean_email:
                logger.warning("❌ Cannot post outgoing email: cleaned email is empty or invalid.")
                return False

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"📧 Outgoing email: Cleaned recipient email = {clean_email}")
            logger.info(f"🔗 Lead ID being used = {lead_id}")

            comments_parts = [
                f"OUTGOING EMAIL - Sent: {timestamp}",
                f"Subject: {subject}"
            ]

            if email_type == "welcome":
                comments_parts.append(f"Welcome email sent to {sender_name}")
                if product_interest:
                    comments_parts.append(f"Product interest: {product_interest}")
            elif email_type == "reply":
                comments_parts.append("Reply email sent")
                if original_email_id:
                    comments_parts.append(f"In response to email ID: {original_email_id}")

            comments_parts.extend([
                f"To: {clean_email}",
                f"Body: {body[:200]}...",
                f"Delivery status: {'Sent successfully' if sent_successfully else 'Failed to send'}"
            ])

            comments = " | ".join(comments_parts)

            return await self.send_to_external_api(
                email=clean_email,
                comments=comments,
                user_type="agent",
                lead_id=lead_id
            )

        except Exception as e:
            logger.error(f"💥 Error posting outgoing email to API: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False


    async def monitor_emails_background_async(self):
        global monitoring_state

        logger.info("Starting async email monitoring loop...")
        monitoring_state["active"] = True
        monitoring_state["last_check"] = datetime.now() - timedelta(hours=1)

        while monitoring_state["active"]:
            try:
                logger.info(f"Checking for new emails since {monitoring_state['last_check']}")

                # Fetch new emails
                new_emails = self.gmail_service.get_emails_since(monitoring_state["last_check"])
                logger.info(f"Found {len(new_emails)} new emails")

                max_timestamp = monitoring_state["last_check"]

                for email in new_emails:
                    if email["id"] in monitoring_state["processed_email_ids"]:
                        continue

                    try:
                        result = await self.process_incoming_email(email)
                        logger.info(f"Email {email.get('id')} processed: {result.get('message')}")
                        monitoring_state["processed_email_ids"].add(email["id"])
                        email_ts = email.get("timestamp")
                        if email_ts and email_ts > max_timestamp:
                            max_timestamp = email_ts
                        await asyncio.sleep(2)
                    except Exception as email_error:
                        logger.error(f"Error processing individual email: {email_error}")
                        continue

                monitoring_state["last_check"] = max_timestamp
                logger.info(f"Sleeping for 60 seconds...")
                await asyncio.sleep(0)

            except Exception as e:
                logger.error(f"Monitoring loop error: {e}")
                await asyncio.sleep(600000)

# Initialize the automation service
automation_service = EmailAutomationService()

# Lifespan manager for FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info(" Email Automation API starting up...")
    yield
    # Shutdown
    global monitoring_state
    monitoring_state["active"] = False
    logger.info(" Email Automation API shutting down...")

# Initialize FastAPI app
app = FastAPI(
    title="Email Automation API",
    description="FastAPI service for automated email processing with Gmail integration",
    version="1.0.0",
    lifespan=lifespan
)

# API Endpoints

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "Email Automation API is running", 
        "status": "active",
        "api_endpoint": automation_service.api_endpoint,
        "monitoring_active": monitoring_state["active"]
    }

@app.post("/send-welcome-email", response_model=EmailProcessingResponse)
async def send_welcome_email_endpoint(request: WelcomeEmailRequest):
    """Send welcome email to a new client"""
    try:
        result = await automation_service.send_welcome_email(
            client_name=request.client_name,
            client_email=request.client_email,
            product_interest=request.product_interest
        )
        
        return EmailProcessingResponse(
            success=result["success"],
            message=result["message"],
            email_id=result.get("email_id")
        )
        
    except Exception as e:
        logger.error(f" Welcome email endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/start-monitoring")
async def start_email_monitoring():
    """Start continuous email monitoring"""
    global monitoring_state

    if monitoring_state["active"]:
        return {
            "message": "Email monitoring is already active",
            "status": "running",
            "stats": {
                "emails_processed": monitoring_state["emails_processed"],
                "api_posts": monitoring_state["api_posts_count"],
                "api_failures": monitoring_state["api_failures"]
            }
        }

    monitoring_state["active"] = True

    # Start background async task
    asyncio.create_task(automation_service.monitor_emails_background_async())

    return {
        "message": "Email monitoring started",
        "status": "started",
        "api_endpoint": automation_service.api_endpoint
    }



@app.post("/stop-monitoring")
async def stop_email_monitoring():
    """Stop email monitoring"""
    global monitoring_state
    
    if not monitoring_state["active"]:
        return {"message": "Email monitoring is not active", "status": "stopped"}
    
    monitoring_state["active"] = False
    return {
        "message": "Email monitoring stopped", 
        "status": "stopped",
        "final_stats": {
            "emails_processed": monitoring_state["emails_processed"],
            "api_posts": monitoring_state["api_posts_count"],
            "api_failures": monitoring_state["api_failures"]
        }
    }

@app.get("/monitoring-status", response_model=MonitoringStatus)
async def get_monitoring_status():
    """Get current monitoring status including blocked emails info"""
    global monitoring_state
    
    return {
        "is_active": monitoring_state["active"],
        "last_check": monitoring_state["last_check"],
        "emails_processed": monitoring_state["emails_processed"],
        "blocked_emails_count": len(blocked_emails_state["blocked_emails"]),
        "blocked_response_attempts": blocked_emails_state["blocked_count"],
        "api_posts_successful": monitoring_state["api_posts_count"],
        "api_posts_failed": monitoring_state["api_failures"]
    }

@app.post("/process-email")
async def process_single_email(email_id: str):
    """Process a specific email by ID"""
    try:
        email_data = automation_service.gmail_service.get_email_details(email_id)
        
        if not email_data:
            raise HTTPException(status_code=404, detail="Email not found")
        
        result = await automation_service.process_incoming_email(email_data)
        
        return EmailProcessingResponse(
            success=result["success"],
            message=result["message"],
            email_id=result.get("email_id")
        )
        
    except Exception as e:
        logger.error(f" Process email endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/send-to-external-api")
async def send_to_external_api_endpoint(data: ExternalAPIData):
    """Send data to external API manually"""
    try:
        success = await automation_service.send_to_external_api(
            email=data.email,
            phone=data.phone,
            comments=data.comments,
            user_type=data.user_type
        )
        
        if success:
            return {"message": "Data sent successfully to external API", "success": True}
        else:
            raise HTTPException(status_code=500, detail="Failed to send data to external API")
            
    except Exception as e:
        logger.error(f" External API endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/email-api-logs")
async def get_email_api_logs():
    """Get statistics about emails posted to external API"""
    global monitoring_state
    
    return {
        "emails_processed": monitoring_state["emails_processed"],
        "api_posts_successful": monitoring_state["api_posts_count"],
        "api_posts_failed": monitoring_state["api_failures"],
        "monitoring_active": monitoring_state["active"],
        "last_check": monitoring_state["last_check"].isoformat() if monitoring_state["last_check"] else None,
        "api_endpoint": automation_service.api_endpoint,
        "processed_email_ids_count": len(monitoring_state["processed_email_ids"]),
        "note": "All incoming and outgoing emails are automatically posted to the external API"
    }

@app.get("/recent-emails")
async def get_recent_emails(hours: int = 24):
    """Get recent emails from Gmail"""
    try:
        since_time = datetime.now() - timedelta(hours=hours)
        emails = automation_service.gmail_service.get_emails_since(since_time)
        
        return {
            "count": len(emails),
            "emails": [
                {
                    "id": email["id"],
                    "sender": email["sender"],
                    "subject": email["subject"],
                    "timestamp": email["timestamp"].isoformat(),
                    "processed": email["id"] in monitoring_state["processed_email_ids"]
                }
                for email in emails
            ],
            "api_stats": {
                "posts_successful": monitoring_state["api_posts_count"],
                "posts_failed": monitoring_state["api_failures"]
            }
        }
        
    except Exception as e:
        logger.error(f"Recent emails endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/test-api-connection")
async def test_api_connection():
    try:
        test_data = {
            "email": "test@example.com",
            "phone": "",
            "comments": "API CONNECTION TEST - This is a test message to verify API connectivity",
            "user_type": "customer"
        }
        
        logger.info(f"Testing API connection to {automation_service.api_endpoint}")
        
        response = requests.post(
            automation_service.api_endpoint,
            json=test_data,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            },
            timeout=30
        )
        
        return {
            "success": response.status_code in [200, 201],
            "status_code": response.status_code,
            "response_body": response.text,
            "response_headers": dict(response.headers),
            "api_endpoint": automation_service.api_endpoint,
            "test_payload": test_data
        }
        
    except Exception as e:
        logger.error(f" API connection test error: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "api_endpoint": automation_service.api_endpoint
        }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        try:
            automation_service.gmail_service.service.users().getProfile(userId='me').execute()
            gmail_status = "healthy"
        except Exception as gmail_error:
            gmail_status = f"unhealthy - {str(gmail_error)}"
        try:
            test_response = requests.get(automation_service.api_endpoint.replace('/api/post-comment', '/health'), timeout=10)
            api_status = "reachable" if test_response.status_code < 500 else "unreachable"
        except:
            api_status = "unreachable"
        
        return {
            "status": "healthy",
            "gmail_service": gmail_status,
            "external_api": api_status,
            "api_endpoint": automation_service.api_endpoint,
            "monitoring_active": monitoring_state["active"],
            "stats": {
                "emails_processed": monitoring_state["emails_processed"],
                "api_posts_successful": monitoring_state["api_posts_count"],
                "api_posts_failed": monitoring_state["api_failures"]
            },
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f" Health check error: {str(e)}")
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

# Add these endpoints to your FastAPI app (after your existing endpoints)

@app.post("/block-email", response_model=BlockedEmailResponse)
async def block_email_endpoint(request: BlockEmailRequest):
    """Block an email address from receiving automated responses"""
    try:
        result = automation_service.block_email(
            email=request.email,
            reason=request.reason
        )
        
        return BlockedEmailResponse(
            success=result["success"],
            message=result["message"],
            email=result["email"],
            is_blocked=result["is_blocked"]
        )
        
    except Exception as e:
        logger.error(f"Block email endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/unblock-email", response_model=BlockedEmailResponse)
async def unblock_email_endpoint(request: UnblockEmailRequest):
    """Unblock an email address to resume automated responses"""
    try:
        result = automation_service.unblock_email(email=request.email)
        
        return BlockedEmailResponse(
            success=result["success"],
            message=result["message"],
            email=result["email"],
            is_blocked=result["is_blocked"]
        )
        
    except Exception as e:
        logger.error(f"Unblock email endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/blocked-emails")
async def get_blocked_emails():
    """Get list of all blocked email addresses"""
    try:
        return {
            "blocked_emails": list(blocked_emails_state["blocked_emails"]),
            "blocked_count": len(blocked_emails_state["blocked_emails"]),
            "blocked_response_attempts": blocked_emails_state["blocked_count"],
            "last_blocked": blocked_emails_state["last_blocked"].isoformat() if blocked_emails_state["last_blocked"] else None
        }
        
    except Exception as e:
        logger.error(f"Get blocked emails endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/check-email-blocked")
async def check_email_blocked(email: str):
    """Check if a specific email address is blocked"""
    try:
        is_blocked = automation_service.is_email_blocked(email)
        clean_email = automation_service.extract_incoming_email(email)
        
        return {
            "email": clean_email,
            "is_blocked": is_blocked,
            "message": f"Email {clean_email} is {'blocked' if is_blocked else 'not blocked'}"
        }
        
    except Exception as e:
        logger.error(f"Check email blocked endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug-last-emails")
async def debug_last_emails(count: int = 5):
    """Debug endpoint to see the last few emails with full details"""
    try:
        since_time = datetime.now() - timedelta(hours=24)
        emails = automation_service.gmail_service.get_emails_since(since_time)
        recent_emails = emails[-count:] if len(emails) > count else emails
        debug_info = []
        for email in recent_emails:
            sender_email = automation_service.extract_email_from_sender(email.get("sender", ""))
            debug_info.append({
                "id": email.get("id"),
                "sender_raw": email.get("sender"),
                "sender_extracted": sender_email,
                "subject": email.get("subject"),
                "body_preview": email.get("body", "")[:200] + "..." if len(email.get("body", "")) > 200 else email.get("body", ""),
                "timestamp": email.get("timestamp").isoformat() if email.get("timestamp") else None,
                "processed": email.get("id") in monitoring_state["processed_email_ids"]
            })
        
        return {
            "total_emails_found": len(emails),
            "showing_last": count,
            "emails": debug_info,
            "api_endpoint": automation_service.api_endpoint,
            "monitoring_stats": {
                "active": monitoring_state["active"],
                "emails_processed": monitoring_state["emails_processed"],
                "api_posts_successful": monitoring_state["api_posts_count"],
                "api_posts_failed": monitoring_state["api_failures"]
            }
        }
        
    except Exception as e:
        logger.error(f" Debug emails endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    logger.info(" Starting Email Automation API server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)