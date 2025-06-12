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
    
    def extract_email_from_sender(self, sender: str) -> str:
        """Extract clean email address from sender field"""
        try:
            if "<" in sender and ">" in sender:
                email_match = re.search(r'<([^>]+)>', sender)
                if email_match:
                    return email_match.group(1).strip()
            email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', sender)
            if email_match:
                return email_match.group(0).strip()
            
            return sender.strip()
        except Exception as e:
            logger.error(f"Error extracting email from sender '{sender}': {str(e)}")
            return sender
    
    async def send_to_external_api(self, email: str, comments: str, user_type: str = "customer", phone: str = "", lead_id: int = "") -> bool:
        global monitoring_state
        
        try:
            clean_email = self.extract_email_from_sender(email)
            payload = {
                "email": clean_email,
                "phone": phone,
                "comments": comments,
                "user_type": user_type,
                "lead_id": lead_id
            }
            headers = self.get_api_headers()
            logger.info(f"=== EXTERNAL API POST ATTEMPT ===")
            logger.info(f"Endpoint: {self.api_endpoint}")
            logger.info(f"Email: {clean_email}")
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
                logger.info(f"✅ SUCCESS: Data posted to API for {clean_email}")
                monitoring_state["api_posts_count"] += 1
                return True
            else:
                logger.error(f"❌ API ERROR: Status {response.status_code} - {response.text}")
                monitoring_state["api_failures"] += 1
                return False
                
        except requests.exceptions.Timeout:
            logger.error(f"❌ API TIMEOUT: Request timed out after 30 seconds")
            monitoring_state["api_failures"] += 1
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ API CONNECTION ERROR: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ API REQUEST ERROR: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False
        except Exception as e:
            logger.error(f"❌ UNEXPECTED ERROR in send_to_external_api: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False
    
    async def send_welcome_email(self, client_name: str, client_email: str, product_interest: str) -> Dict:
        """Send welcome email to new client"""
        try:
            logger.info(f"📧 Processing welcome email for {client_name} ({client_email})")
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
Welcome {client_name}! Thank you for your interest in {product_interest}. If you want more information kindly reply to this email. 
I will provide more information as per your requirements.
                
Best regards,
Prismatic Technologies"""
            
            email_subject = f"Welcome {client_name}! Your Interest in {product_interest}"
            email_body = generated_content or f"""
Welcome {client_name}! Thank you for your interest in {product_interest}. If you want more information kindly reply to this email. 
I will provide more information as per your requirements.
                
Best regards,
Prismatic Technologies"""
            
            # Send email via Gmail API
            logger.info(f"📤 Sending welcome email to {client_email}...")
            email_sent = self.gmail_service.send_email(
                to_email=client_email,
                subject=email_subject,
                body=email_body
            )
            
            logger.info(f"Email send result: {'✅ Success' if email_sent else '❌ Failed'}")
            
            # ALWAYS post outgoing email to external API (regardless of send success)
            logger.info(f"🔄 Posting welcome email to external API...")
            
            api_success = await self.post_outgoing_email_to_api(
                recipient_email=client_email,
                subject=email_subject,
                body=email_body,
                email_type="welcome",
                sender_name=client_name,
                product_interest=product_interest,
                sent_successfully=email_sent
            )
            
            result = {
                "success": email_sent,
                "message": f"Welcome email {'sent successfully' if email_sent else 'failed to send'} to {client_name}",
                "email_sent": email_sent,
                "api_posted": api_success,
                "api_endpoint": self.api_endpoint,
                "email_body_preview": email_body[:200] + "..." if len(email_body) > 200 else email_body
            }
            
            logger.info(f"📋 Welcome email result: {result}")
            return result
                
        except Exception as e:
            logger.error(f"❌ Error sending welcome email: {str(e)}")
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "email_sent": False,
                "api_posted": False
            }
    
    async def process_incoming_email(self, email_data: Dict) -> Dict:
        """Process a single incoming email with enhanced debugging"""
        global monitoring_state
        
        try:
            email_id = email_data.get("id", "")
            sender = email_data.get("sender", "Unknown")
            subject = email_data.get("subject", "No Subject")
            
            # DEBUG: Print all email data
            logger.info(f"🔍 DEBUG - Full email data: {json.dumps(email_data, indent=2, default=str)}")
            
            # Skip if already processed
            if email_id in monitoring_state["processed_email_ids"]:
                logger.info(f"⏭️  Skipping already processed email {email_id}")
                return {
                    "success": True,
                    "message": "Email already processed",
                    "email_id": email_id,
                    "skipped": True
                }
            
            logger.info(f"📨 Processing incoming email from: {sender}")
            logger.info(f"📧 Email ID: {email_id}")
            logger.info(f"📋 Subject: {subject}")
            
            # Check if this is an outgoing email from our own system
            is_outgoing_email = False
            if "support@prismatic-technologies.com.pk" in sender.lower():
                logger.info(f"🤖 DETECTED: This appears to be an outgoing email from our system")
                is_outgoing_email = True
            
            # Mark as processed early to avoid reprocessing
            monitoring_state["processed_email_ids"].add(email_id)
            
            # Handle outgoing emails differently
            if is_outgoing_email:
                logger.info(f"📤 Processing as OUTGOING email...")
                # Extract recipient from the email data or subject
                recipient_email = email_data.get("to", "")  # Try to get recipient
                if not recipient_email:
                    # Try to extract from subject or other fields
                    if "Re:" in subject:
                        # This is a reply, try to determine original recipient
                        logger.warning(f"⚠️  Could not determine recipient for outgoing email {email_id}")
                        recipient_email = "unknown@example.com"
                
                # Post outgoing email to API
                logger.info(f"🔄 Posting OUTGOING email to external API...")
                api_posted = await self.post_outgoing_email_to_api(
                    recipient_email=recipient_email,
                    subject=subject,
                    body=email_data.get("body", ""),
                    email_type="outgoing",
                    sent_successfully=True,
                    original_email_id=email_id
                )
                
                return {
                    "success": True,
                    "message": f"Processed outgoing email {email_id}",
                    "email_id": email_id,
                    "api_posted": api_posted,
                    "email_type": "outgoing"
                }
                
            # Process as regular incoming email
            logger.info(f"📨 Processing as INCOMING email...")
            
            # Post incoming email to external API
            logger.info(f"🔄 Posting incoming email to external API...")
            api_posted = await self.post_incoming_email_to_api(email_data)
            logger.info(f"📤 Incoming API post result: {'✅ Success' if api_posted else '❌ Failed'}")
            
            # Prepare email for workflow
            initial_state = self.get_initial_state([email_data])
            initial_state["current_email"] = {
                "id": email_data.get("id", ""),
                "threadId": email_data.get("threadId", ""),
                "messageId": email_data.get("messageId", ""),
                "references": email_data.get("references", ""),
                "sender": email_data.get("sender", ""),
                "subject": email_data.get("subject", ""),
                "body": email_data.get("body", "")
            }
            
            # Run the workflow
            generated_response = ""
            sendable = False
            final_state = None
            
            try:
                logger.info("🔄 Starting email workflow processing...")
                for output in self.app.stream(initial_state, self.config):
                    for key, value in output.items():
                        if key == "generated_email":
                            generated_response = str(value)
                            logger.info(f"📝 Generated response length: {len(generated_response)}")
                        elif key == "sendable":
                            sendable = bool(value)
                            logger.info(f"📤 Sendable status: {sendable}")
                        # Capture the final state
                        if isinstance(value, dict) and "sendable" in value:
                            final_state = value
                        logger.info(f"Email processing workflow step: {key}")
                
                # Try to get sendable from final state if available
                if final_state and "sendable" in final_state:
                    sendable = bool(final_state["sendable"])
                    logger.info(f"📤 Updated sendable from final state: {sendable}")
                    
            except Exception as workflow_error:
                logger.error(f"❌ Workflow error: {workflow_error}")
                generated_response = "Thank you for your email. We have received your message and will respond shortly."
                sendable = True
            
            logger.info(f"📋 Workflow results - Generated: {bool(generated_response)}, Sendable: {sendable}")
            
            # Send response email if generated and sendable
            email_sent = False
            reply_api_posted = False
            
            if generated_response and sendable:
                logger.info(f"📤 Attempting to send reply email...")
                reply_subject = f"Re: {email_data.get('subject', '')}"
                
                # Extract clean email address
                recipient_email = self.extract_email_from_sender(sender)
                logger.info(f"📧 Sending reply to: {recipient_email}")
                logger.info(f"📋 Reply subject: {reply_subject}")
                
                try:
                    email_sent = self.gmail_service.send_email(
                        to_email=recipient_email,
                        subject=reply_subject,
                        body=generated_response
                    )
                    logger.info(f"📤 Reply send result: {'✅ Success' if email_sent else '❌ Failed'}")
                    
                    # ALWAYS post outgoing reply to external API
                    logger.info(f"🔄 Posting REPLY email to external API...")
                    logger.info(f"🔍 DEBUG - About to call post_outgoing_email_to_api with:")
                    logger.info(f"  - recipient_email: {recipient_email}")
                    logger.info(f"  - subject: {reply_subject}")
                    logger.info(f"  - body length: {len(generated_response)}")
                    logger.info(f"  - email_type: reply")
                    logger.info(f"  - sent_successfully: {email_sent}")
                    
                    try:
                        reply_api_posted = await self.post_outgoing_email_to_api(
                            recipient_email=recipient_email,
                            subject=reply_subject,
                            body=generated_response,
                            email_type="reply",
                            sent_successfully=email_sent,
                            original_email_id=email_id
                        )
                        logger.info(f"📤 Reply API post result: {'✅ Success' if reply_api_posted else '❌ Failed'}")
                    except Exception as api_error:
                        logger.error(f"❌ Error posting reply to API: {api_error}")
                        logger.error(f"❌ API Error traceback: ", exc_info=True)
                        reply_api_posted = False
                        
                except Exception as send_error:
                    logger.error(f"❌ Error sending reply email: {send_error}")
                    email_sent = False
                    reply_api_posted = False
                
            else:
                logger.info(f"⏭️  Not sending reply - Generated: {bool(generated_response)}, Sendable: {sendable}")
                if not generated_response:
                    logger.info("⚠️  No response was generated by the workflow")
                if not sendable:
                    logger.info("⚠️  Email marked as not sendable by workflow")
            
            monitoring_state["emails_processed"] += 1
            
            result = {
                "success": True,
                "message": f"Successfully processed email from {sender}",
                "email_id": email_id,
                "response_sent": email_sent,
                "api_posted": api_posted,
                "reply_api_posted": reply_api_posted,
                "generated_response_preview": generated_response[:500] + "..." if len(generated_response) > 500 else generated_response,
                "workflow_sendable": sendable,
                "email_type": "incoming"
            }
            
            logger.info(f"📋 Email processing result: {json.dumps(result, indent=2, default=str)}")
            return result
            
        except Exception as e:
            logger.error(f"❌ Error processing email: {str(e)}")
            logger.error(f"❌ Exception traceback: ", exc_info=True)
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "email_id": email_data.get("id", ""),
                "api_posted": False
            }
    
    async def post_incoming_email_to_api(self, email_data: Dict) -> bool:
        """Post incoming email details to external API"""
        try:
            sender_email = self.extract_email_from_sender(email_data.get("sender", ""))
            subject = email_data.get("subject", "No Subject")
            body = email_data.get("body", "")
            timestamp = email_data.get("timestamp", datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
            
            comments = f"INCOMING EMAIL - Received: {timestamp} | Subject: {subject} | From: {sender_email} | Body: {body[:800]}..."
            
            logger.info(f"🔄 Posting incoming email to API for: {sender_email}")
            return await self.send_to_external_api(
                email=sender_email,
                phone="",
                comments=comments,
                user_type="customer",
                lead_id=3446
            )
                
        except Exception as e:
            logger.error(f"❌ Error posting incoming email to API: {str(e)}")
            return False
    
    async def post_outgoing_email_to_api(self, recipient_email: str, subject: str, body: str, 
                                   email_type: str = "reply", sender_name: str = "", 
                                   product_interest: str = "", sent_successfully: bool = True,
                                   original_email_id: str = "") -> bool:
        """Post outgoing email details to external API with enhanced debugging"""
        try:
            logger.info(f"🚀 STARTING post_outgoing_email_to_api")
            logger.info(f"🔍 Input parameters:")
            logger.info(f"  - recipient_email: {recipient_email}")
            logger.info(f"  - subject: {subject}")
            logger.info(f"  - body length: {len(body)}")
            logger.info(f"  - email_type: {email_type}")
            logger.info(f"  - sent_successfully: {sent_successfully}")
            
            clean_email = self.extract_email_from_sender(recipient_email)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            logger.info(f"🔄 Preparing to post outgoing {email_type} email to API for: {clean_email}")
            
            # Create comprehensive comments for outgoing email
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
            elif email_type == "outgoing":
                comments_parts.append("Outgoing email detected")
            
            comments_parts.extend([
                f"To: {clean_email}",
                f"Body: {body[:600]}...",
                f"Delivery status: {'Sent successfully' if sent_successfully else 'Failed to send'}"
            ])
            
            comments = " | ".join(comments_parts)
            
            logger.info(f"🔄 About to call send_to_external_api...")
            logger.info(f"📧 Email: {clean_email}")
            logger.info(f"📝 Comments length: {len(comments)} chars")
            logger.info(f"📝 Comments preview: {comments[:200]}...")
            
            result = await self.send_to_external_api(
                email=clean_email,
                comments=comments,
                user_type="customer",
                lead_id = 3446
            )
            
            logger.info(f"📤 send_to_external_api returned: {result}")
            logger.info(f"📤 Outgoing email API post result: {'✅ Success' if result else '❌ Failed'}")
            return result
                
        except Exception as e:
            logger.error(f"❌ Error posting outgoing email to API: {str(e)}")
            logger.error(f"❌ Exception details: {type(e).__name__}: {str(e)}")
            logger.error(f"❌ Full traceback: ", exc_info=True)
            return False

    async def monitor_emails_background_async(self):
        """FIXED: Monitor emails in background with proper async handling"""
        global monitoring_state

        logger.info("🚀 Starting async email monitoring loop...")
        monitoring_state["active"] = True
        monitoring_state["last_check"] = datetime.now() - timedelta(hours=1)

        while monitoring_state["active"]:
            try:
                logger.info(f"🔍 Checking for new emails since {monitoring_state['last_check']}")

                # Fetch new emails
                new_emails = self.gmail_service.get_emails_since(monitoring_state["last_check"])
                logger.info(f"📨 Found {len(new_emails)} new emails")

                max_timestamp = monitoring_state["last_check"]

                for email in new_emails:
                    if not monitoring_state["active"]:  # Check if monitoring was stopped
                        logger.info("⏹️  Monitoring stopped, breaking email processing loop")
                        break
                        
                    if email["id"] in monitoring_state["processed_email_ids"]:
                        logger.info(f"⏭️  Skipping already processed email {email['id']}")
                        continue

                    try:
                        logger.info(f"🔄 Processing email {email.get('id')} from {email.get('sender')}")
                        result = await self.process_incoming_email(email)
                        logger.info(f"✅ Email {email.get('id')} processed: {result.get('message')}")
                        
                        # Update timestamp tracking
                        email_ts = email.get("timestamp")
                        if email_ts and email_ts > max_timestamp:
                            max_timestamp = email_ts
                            
                        # Small delay between email processing
                        await asyncio.sleep(2)
                        
                    except Exception as email_error:
                        logger.error(f"❌ Error processing individual email {email.get('id', 'unknown')}: {email_error}")
                        logger.error(f"❌ Email processing traceback: ", exc_info=True)
                        continue

                # Update last check timestamp
                monitoring_state["last_check"] = max_timestamp
                
                if not monitoring_state["active"]:  # Check again before sleeping
                    logger.info("⏹️  Monitoring stopped, exiting loop")
                    break
                    
                logger.info(f"😴 Sleeping for 60 seconds before next check...")
                # FIXED: Use proper sleep duration (60 seconds instead of 0)
                await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"❌ Monitoring loop error: {e}")
                logger.error(f"❌ Monitoring loop traceback: ", exc_info=True)
                if monitoring_state["active"]:
                    logger.info("⏳ Waiting 60 seconds before retrying...")
                    await asyncio.sleep(60)
                else:
                    break

        logger.info("🛑 Email monitoring loop ended")

# Initialize the automation service
automation_service = EmailAutomationService()

# Lifespan manager for FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("🚀 Email Automation API starting up...")
    yield
    # Shutdown
    global monitoring_state
    monitoring_state["active"] = False
    logger.info("🛑 Email Automation API shutting down...")

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
        logger.error(f"❌ Welcome email endpoint error: {str(e)}")
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
    """Get current monitoring status"""
    global monitoring_state
    
    return MonitoringStatus(
        is_active=monitoring_state["active"],
        last_check=monitoring_state["last_check"],
        emails_processed=monitoring_state["emails_processed"]
    )

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
        logger.error(f"❌ Process email endpoint error: {str(e)}")
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
        logger.error(f"❌ External API endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Starting Email Automation API server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)