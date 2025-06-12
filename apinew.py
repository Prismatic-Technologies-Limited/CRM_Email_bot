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
        self.bearer_token = "XmdeUbd8appKNtzrd8NrjTJTTdDLtmF6mty7OJvF"
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
    
    def send_to_external_api_sync(self, email: str, comments: str, user_type: str = "customer", phone: str = "") -> bool:
        """Synchronous version of API call - sometimes async can cause issues"""
        global monitoring_state
        
        try:
            clean_email = self.extract_email_from_sender(email)
            payload = {
                "email": clean_email,
                "phone": phone,
                "comments": comments,
                "user_type": user_type
            }
            headers = self.get_api_headers()
            
            logger.info(f"=== EXTERNAL API POST ATTEMPT (SYNC) ===")
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
            logger.error(f"⏰ API TIMEOUT: Request timed out after 30 seconds")
            monitoring_state["api_failures"] += 1
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error(f"🔌 API CONNECTION ERROR: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"📡 API REQUEST ERROR: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False
        except Exception as e:
            logger.error(f"💥 UNEXPECTED ERROR in send_to_external_api_sync: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False

    async def send_to_external_api(self, email: str, comments: str, user_type: str = "customer", phone: str = "") -> bool:
        """Async version - falls back to sync if needed"""
        try:
            # Try sync version first as it's more reliable
            return self.send_to_external_api_sync(email, comments, user_type, phone)
        except Exception as e:
            logger.error(f"💥 Error in async send_to_external_api: {str(e)}")
            return False
    
    async def send_welcome_email(self, client_name: str, client_email: str, product_interest: str) -> Dict:
        """Send welcome email to new client"""
        try:
            logger.info(f"🎉 Processing welcome email for {client_name} ({client_email})")
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
            
            # Generate email content using workflow
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
            logger.info(f"📧 Sending welcome email via Gmail...")
            email_sent = self.gmail_service.send_email(
                to_email=client_email,
                subject=email_subject,
                body=email_body
            )
            
            logger.info(f"Gmail send result: {'✅ Success' if email_sent else '❌ Failed'}")
            
            # POST TO EXTERNAL API - Using direct method for better control
            logger.info(f"📤 Posting welcome email to external API (DIRECT METHOD)...")
            
            # Create the API payload directly
            clean_email = self.extract_email_from_sender(client_email)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Create comprehensive comments
            comments = f"OUTGOING WELCOME EMAIL - Sent: {timestamp} | Body: {email_body}"
            
            # Try multiple approaches to ensure API call works
            api_success = False
            
            # Method 1: Direct sync call
            try:
                logger.info("🔄 Attempting Method 1: Direct sync API call")
                api_success = self.send_to_external_api_sync(
                    email=clean_email,
                    comments=comments,
                    user_type="customer",
                    phone=""
                )
                logger.info(f"Method 1 result: {'✅ Success' if api_success else '❌ Failed'}")
            except Exception as e:
                logger.error(f"Method 1 failed: {str(e)}")
            
            # Method 2: If first method fails, try async
            if not api_success:
                try:
                    logger.info("🔄 Attempting Method 2: Async API call")
                    api_success = await self.send_to_external_api(
                        email=clean_email,
                        comments=comments,
                        user_type="customer",
                        phone=""
                    )
                    logger.info(f"Method 2 result: {'✅ Success' if api_success else '❌ Failed'}")
                except Exception as e:
                    logger.error(f"Method 2 failed: {str(e)}")
            
            # Method 3: If all else fails, try raw requests call
            if not api_success:
                try:
                    logger.info("🔄 Attempting Method 3: Raw requests call")
                    payload = {
                        "email": clean_email,
                        "phone": "",
                        "comments": comments,
                        "user_type": "customer"
                    }
                    headers = self.get_api_headers()
                    
                    response = requests.post(
                        self.api_endpoint,
                        json=payload,
                        headers=headers,
                        timeout=30
                    )
                    
                    if response.status_code in [200, 201]:
                        api_success = True
                        logger.info("✅ Method 3: Raw requests call succeeded!")
                        monitoring_state["api_posts_count"] += 1
                    else:
                        logger.error(f"❌ Method 3 failed: Status {response.status_code} - {response.text}")
                        monitoring_state["api_failures"] += 1
                        
                except Exception as e:
                    logger.error(f"Method 3 failed: {str(e)}")
            
            result = {
                "success": email_sent,
                "message": f"Welcome email {'sent successfully' if email_sent else 'failed to send'} to {client_name}",
                "email_sent": email_sent,
                "api_posted": api_success,
                "api_endpoint": self.api_endpoint,
                "clean_email": clean_email,
                "comments_length": len(comments),
                "email_body_preview": email_body[:200] + "..." if len(email_body) > 200 else email_body
            }
            
            logger.info(f"🎯 Welcome email FINAL result: {result}")
            return result
                
        except Exception as e:
            logger.error(f"💥 Error sending welcome email: {str(e)}")
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "email_sent": False,
                "api_posted": False
            }
    
    async def process_incoming_email(self, email_data: Dict) -> Dict:
        """Process a single incoming email"""
        global monitoring_state
        
        try:
            email_id = email_data.get("id", "")
            sender = email_data.get("sender", "Unknown")
            
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
            
            # FIRST: Post incoming email to external API
            logger.info(f"📤 Posting incoming email to external API...")
            api_posted = await self.post_incoming_email_to_api(email_data)
            
            # Mark as processed regardless of API success
            monitoring_state["processed_email_ids"].add(email_id)
            
            # Prepare email for workflow
            initial_state = self.get_initial_state([email_data])
            initial_state["current_email"] = {
                "id": email_data.get("id", ""),
                "threadId": email_data.get("threadId", ""),
                "messageId": email_data.get("messageId", ""),
                "references": email_data.get("references", ""),
                "Hamza": "",
                "sender": email_data.get("sender", ""),
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
                        logger.info(f"Email processing workflow step: {key}")
                
                # Update sendable status from final state
                sendable = initial_state.get("sendable", sendable)
            except Exception as workflow_error:
                logger.error(f"Workflow error: {workflow_error}")
                generated_response = "Thank you for your email. We have received your message and will respond shortly."
                sendable = True
            
            # Send response email if generated and sendable
            email_sent = False
            reply_api_posted = False
            
            if generated_response and sendable:
                logger.info(f"📧 Sending reply email...")
                reply_subject = f"Re: {email_data.get('subject', '')}"
                email_sent = self.gmail_service.send_email(
                    to_email=self.extract_email_from_sender(sender),
                    subject=reply_subject,
                    body=generated_response
                )
                
                logger.info(f"Gmail reply send result: {'✅ Success' if email_sent else '❌ Failed'}")
                
                # POST OUTGOING REPLY TO API - Using same multi-method approach
                if generated_response:  # Only if we have content to post
                    logger.info(f"📤 Posting reply email to external API...")
                    
                    clean_email = self.extract_email_from_sender(sender)
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    comments = f"OUTGOING REPLY EMAIL - Sent: {timestamp} | To: {clean_email} | Subject: {reply_subject} | In Response To Email ID: {email_id} | Body: {generated_response} | Delivery Status: {'Sent successfully' if email_sent else 'Failed to send'}"
                    
                    # Try multiple methods like in welcome email
                    try:
                        reply_api_posted = self.send_to_external_api_sync(
                            email=clean_email,
                            comments=comments,
                            user_type="customer",
                            phone=""
                        )
                        logger.info(f"Reply API post result: {'✅ Success' if reply_api_posted else '❌ Failed'}")
                    except Exception as e:
                        logger.error(f"Error posting reply to API: {str(e)}")
            else:
                logger.info(f"⏭️ Not sending reply - Generated: {bool(generated_response)}, Sendable: {sendable}")
            
            monitoring_state["emails_processed"] += 1
            
            result = {
                "success": True,
                "message": f"Successfully processed email from {sender}",
                "email_id": email_id,
                "response_sent": email_sent,
                "api_posted": api_posted,
                "reply_api_posted": reply_api_posted,
                "generated_response_preview": generated_response[:500] + "..." if len(generated_response) > 500 else generated_response
            }
            
            logger.info(f"🎯 Email processing result: {result}")
            return result
            
        except Exception as e:
            logger.error(f"💥 Error processing email: {str(e)}")
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
            
            # Format incoming email for API
            comments = f"INCOMING EMAIL - Received: {timestamp} | Subject: {subject} | From: {sender_email} | Body: {body[:800]}..."
            
            return self.send_to_external_api_sync(
                email=sender_email,
                phone="",
                comments=comments,
                user_type="customer"
            )
                
        except Exception as e:
            logger.error(f"Error posting incoming email to API: {str(e)}")
            return False
    
    def monitor_emails_background(self):
        """Background email monitoring function"""
        global monitoring_state
        
        logger.info("🔍 Starting email monitoring...")
        monitoring_state["active"] = True
        monitoring_state["last_check"] = datetime.now() - timedelta(hours=1)  # Start checking from 1 hour ago
        
        while monitoring_state["active"]:
            try:
                logger.info(f"📧 Checking for new emails since {monitoring_state['last_check']}")
                
                # Fetch new emails
                new_emails = self.gmail_service.get_emails_since(monitoring_state["last_check"])
                
                logger.info(f"📊 Found {len(new_emails)} emails to check")
                
                if new_emails:
                    logger.info(f"⚙️ Processing {len(new_emails)} new emails")
                    
                    for email in new_emails:
                        try:
                            # Process email in async context
                            result = asyncio.run(self.process_incoming_email(email))
                            logger.info(f"✅ Email {email.get('id', 'unknown')} processed: {result.get('message', 'No message')}")
                            time.sleep(2)  # Brief pause between processing emails
                        except Exception as email_error:
                            logger.error(f"💥 Error processing individual email: {email_error}")
                            continue
                
                monitoring_state["last_check"] = datetime.now()
                logger.info(f"📈 Email check completed. Next check in 6 hours. Stats: Processed: {monitoring_state['emails_processed']}, API Posts: {monitoring_state['api_posts_count']}, API Failures: {monitoring_state['api_failures']}")
                time.sleep(21600)  # Check every 6 hours (21600 seconds)
                
            except Exception as e:
                logger.error(f"💥 Error in email monitoring loop: {str(e)}")
                time.sleep(3600)  # Wait longer on error

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
async def start_email_monitoring(background_tasks: BackgroundTasks):
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
    monitoring_thread = threading.Thread(target=automation_service.monitor_emails_background)
    monitoring_thread.daemon = True
    monitoring_thread.start()
    monitoring_state["thread"] = monitoring_thread
    
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