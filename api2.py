import asyncio
import json
import base64
import time
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
import threading
import logging
from contextlib import asynccontextmanager

# Gmail API imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pickle
import os

# Your existing imports
from colorama import Fore, Style
from src.graph import Workflow
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Gmail API scopes
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 
          'https://www.googleapis.com/auth/gmail.send']

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Pydantic models for API requests/responses
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
    "thread": None
}

class GmailService:
    def __init__(self):
        self.service = None
        self.credentials = None
        self.setup_gmail_service()
    
    def setup_gmail_service(self):
        """Setup Gmail API service"""
        try:
            creds = None
            # The file token.pickle stores the user's access and refresh tokens.
            if os.path.exists('token.pickle'):
                with open('token.pickle', 'rb') as token:
                    creds = pickle.load(token)
            
            # If there are no (valid) credentials available, let the user log in.
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        'credentials.json', SCOPES)  # Download this from Google Cloud Console
                    creds = flow.run_local_server(port=0)
                
                # Save the credentials for the next run
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
            # Convert datetime to Gmail query format
            query_time = since_time.strftime('%Y/%m/%d')
            query = f'after:{query_time} in:inbox'
            
            # Get message list
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
        self.gmail_service = GmailService()
        
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
    
    async def send_to_external_api(self, email: str, comments: str, user_type: str = "customer", phone: str = ""):
        """Send data to external API with correct format"""
        try:
            # Ensure payload matches your required format exactly
            payload = {
                "email": email,
                "phone": phone,
                "comments": comments,
                "user_type": user_type
            }
            
            logger.info(f"Sending to external API: {self.api_endpoint}")
            logger.info(f"Payload: {json.dumps(payload, indent=2)}")
            
            response = requests.post(
                self.api_endpoint,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )
            
            if response.status_code == 200:
                logger.info(f"Successfully sent data to API for {email}")
                return True
            else:
                logger.error(f"API request failed with status {response.status_code}: {response.text}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Error sending to external API: {str(e)}")
            return False
    
    async def send_welcome_email(self, client_name: str, client_email: str, product_interest: str) -> Dict:
        """Send welcome email to new client"""
        try:
            logger.info(f"Processing welcome email for {client_name} ({client_email})")
            
            # Prepare welcome email data
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
            
            # Run workflow for welcome email
            generated_content = ""
            for output in self.app.stream(initial_state, self.config):
                for key, value in output.items():
                    if key == "generated_email":
                        generated_content = value
                    logger.info(f"Welcome email step: {key}")
            
            email_subject = f"Welcome {client_name}! Your Interest in {product_interest}"
            email_body = generated_content or f"Welcome {client_name}! Thank you for your interest in {product_interest}."
            
            # Send email via Gmail API
            email_sent = self.gmail_service.send_email(
                to_email=client_email,
                subject=email_subject,
                body=email_body
            )
            
            # ALWAYS post outgoing email to external API (regardless of send success)
            comments = f"OUTGOING WELCOME EMAIL - Subject: {email_subject} | Body: {email_body[:300]}... | Product Interest: {product_interest} | Delivery Status: {'Sent successfully' if email_sent else 'Failed to send'}"
            
            api_success = await self.send_to_external_api(
                email=client_email,
                phone="",
                comments=comments,
                user_type="customer"
            )
            
            if email_sent:
                return {
                    "success": True,
                    "message": f"Welcome email sent successfully to {client_name}",
                    "email_sent": True,
                    "api_posted": api_success
                }
            else:
                return {
                    "success": False,
                    "message": "Failed to send welcome email",
                    "email_sent": False,
                    "api_posted": api_success
                }
                
        except Exception as e:
            logger.error(f"Error sending welcome email: {str(e)}")
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "email_sent": False,
                "api_posted": False
            }
    
    async def post_incoming_email_to_api(self, email_data: Dict) -> bool:
        """Post incoming email details to external API"""
        try:
            # Extract sender email from the sender field (remove name if present)
            sender_email = email_data.get("sender", "")
            if "<" in sender_email and ">" in sender_email:
                sender_email = sender_email.split("<")[1].split(">")[0]
            
            comments = f"INCOMING EMAIL - Subject: {email_data.get('subject', '')} | Body: {email_data.get('body', '')[:500]}..."
            
            return await self.send_to_external_api(
                email=sender_email,
                phone="",
                comments=comments,
                user_type="customer"
            )
                
        except Exception as e:
            logger.error(f"Error posting incoming email to API: {str(e)}")
            return False
    
    async def post_outgoing_email_to_api(self, recipient_email: str, subject: str, body: str, 
                                       email_type: str = "reply", sender_name: str = "", 
                                       product_interest: str = "", sent_successfully: bool = True,
                                       original_email_id: str = "") -> bool:
        """Post outgoing email details to external API"""
        try:
            # Create comprehensive comments for outgoing email
            comments_parts = [f"OUTGOING EMAIL - Subject: {subject}"]
            
            if email_type == "welcome":
                comments_parts.append(f"Welcome email sent to {sender_name}")
                if product_interest:
                    comments_parts.append(f"Product interest: {product_interest}")
            elif email_type == "reply":
                comments_parts.append("Reply email sent")
                if original_email_id:
                    comments_parts.append(f"In response to email ID: {original_email_id}")
            
            comments_parts.append(f"Email body: {body[:300]}...")
            comments_parts.append(f"Delivery status: {'Sent successfully' if sent_successfully else 'Failed to send'}")
            
            comments = " | ".join(comments_parts)
            
            return await self.send_to_external_api(
                email=recipient_email,
                phone="",
                comments=comments,
                user_type="customer"
            )
                
        except Exception as e:
            logger.error(f"Error posting outgoing email to API: {str(e)}")
            return False
    
    async def process_incoming_email(self, email_data: Dict) -> Dict:
        """Process a single incoming email"""
        try:
            logger.info(f"Processing email from: {email_data.get('sender', 'Unknown')}")
            
            # FIRST: Post incoming email to external API
            api_posted = await self.post_incoming_email_to_api(email_data)
            
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
            for output in self.app.stream(initial_state, self.config):
                for key, value in output.items():
                    if key == "generated_email":
                        generated_response = value
                    logger.info(f"Processing step: {key}")
            
            # Send response email if generated and sendable
            email_sent = False
            reply_api_posted = False
            if generated_response and initial_state.get("sendable", False):
                reply_subject = f"Re: {email_data.get('subject', '')}"
                email_sent = self.gmail_service.send_email(
                    to_email=email_data.get("sender", ""),
                    subject=reply_subject,
                    body=generated_response
                )
                
                # ALWAYS post outgoing reply to external API (regardless of send success)
                reply_api_posted = await self.post_outgoing_email_to_api(
                    recipient_email=email_data.get("sender", ""),
                    subject=reply_subject,
                    body=generated_response,
                    email_type="reply",
                    sent_successfully=email_sent,
                    original_email_id=email_data.get("id", "")
                )
            
            return {
                "success": True,
                "message": f"Successfully processed email from {email_data.get('sender', 'Unknown')}",
                "email_id": email_data.get("id", ""),
                "response_sent": email_sent,
                "api_posted": api_posted,
                "reply_api_posted": reply_api_posted
            }
            
        except Exception as e:
            logger.error(f"Error processing email: {str(e)}")
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "email_id": email_data.get("id", ""),
                "api_posted": False
            }
    
    def monitor_emails_background(self):
        """Background email monitoring function"""
        global monitoring_state
        
        logger.info("Starting email monitoring...")
        monitoring_state["active"] = True
        monitoring_state["last_check"] = datetime.now()
        
        while monitoring_state["active"]:
            try:
                # Fetch new emails
                new_emails = self.gmail_service.get_emails_since(monitoring_state["last_check"])
                
                if new_emails:
                    logger.info(f"Found {len(new_emails)} new emails")
                    
                    for email in new_emails:
                        # Process email in async context
                        result = asyncio.run(self.process_incoming_email(email))
                        if result["success"]:
                            monitoring_state["emails_processed"] += 1
                        time.sleep(1)  # Brief pause between processing emails
                
                monitoring_state["last_check"] = datetime.now()
                time.sleep(30)  # Check every 30 seconds
                
            except Exception as e:
                logger.error(f"Error in email monitoring: {str(e)}")
                time.sleep(60)  # Wait longer on error

# Initialize the automation service
automation_service = EmailAutomationService()

# Lifespan manager for FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Email Automation API starting up...")
    yield
    # Shutdown
    global monitoring_state
    monitoring_state["active"] = False
    logger.info("Email Automation API shutting down...")

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
    return {"message": "Email Automation API is running", "status": "active"}

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
        logger.error(f"Welcome email endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/start-monitoring")
async def start_email_monitoring(background_tasks: BackgroundTasks):
    """Start continuous email monitoring"""
    global monitoring_state
    
    if monitoring_state["active"]:
        return {"message": "Email monitoring is already active", "status": "running"}
    
    # Start monitoring in background
    monitoring_thread = threading.Thread(target=automation_service.monitor_emails_background)
    monitoring_thread.daemon = True
    monitoring_thread.start()
    monitoring_state["thread"] = monitoring_thread
    
    return {"message": "Email monitoring started", "status": "started"}

@app.post("/stop-monitoring")
async def stop_email_monitoring():
    """Stop email monitoring"""
    global monitoring_state
    
    if not monitoring_state["active"]:
        return {"message": "Email monitoring is not active", "status": "stopped"}
    
    monitoring_state["active"] = False
    return {"message": "Email monitoring stopped", "status": "stopped"}

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
        # Get email details from Gmail
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
        logger.error(f"Process email endpoint error: {str(e)}")
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
        logger.error(f"External API endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/email-api-logs")
async def get_email_api_logs():
    """Get statistics about emails posted to external API"""
    global monitoring_state
    
    return {
        "emails_processed": monitoring_state["emails_processed"],
        "monitoring_active": monitoring_state["active"],
        "last_check": monitoring_state["last_check"].isoformat() if monitoring_state["last_check"] else None,
        "api_endpoint": automation_service.api_endpoint,
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
                    "timestamp": email["timestamp"].isoformat()
                }
                for email in emails
            ]
        }
        
    except Exception as e:
        logger.error(f"Recent emails endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        # Test Gmail service
        automation_service.gmail_service.service.users().getProfile(userId='me').execute()
        gmail_status = "healthy"
    except:
        gmail_status = "unhealthy"
    
    return {
        "status": "healthy",
        "gmail_service": gmail_status,
        "monitoring_active": monitoring_state["active"],
        "timestamp": datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)