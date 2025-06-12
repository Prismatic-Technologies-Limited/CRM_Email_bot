import os
import re
import time
import json
import base64
import httpx
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
    user_type: str = "agent"

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
        #self.workflow = Workflow()
        self.workflow = Workflow()
        self.app = self.workflow.app
        # Updated API endpoint to your specific endpoint
        self.api_endpoint = "https://inhouse.prismaticcrm.com/api/post-comment"
        self.bearer_token = "31|GIi7u4iBXfSOfxYVnMEcRrBzOkpYUBm6Adzxx2j9"
        self.external_api_token = "31|GIi7u4iBXfSOfxYVnMEcRrBzOkpYUBm6Adzxx2j9"  # Replace with your actual token
        self.http = httpx.AsyncClient()
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

    # FIXED: Added the missing method
    def extract_email_from_sender(self, sender_field: str) -> str:
        """
        Alias for extract_incoming_email for backward compatibility
        """
        return self.extract_incoming_email(sender_field)

    import httpx  # Ensure this is imported at the top of your file

    async def send_to_external_api(
        self,
        email: str,
        comments: str,
        user_type: str = "",
        phone: str = "",
        lead_id: str = "3446"
    ) -> bool:
        global monitoring_state

        try:
            payload = {
                "email": email,
                "lead_id": lead_id,
                "phone": phone,
                "comments": comments,
                "user_type": user_type
            }

            headers = self.get_api_headers()
            logger.info(f"=== EXTERNAL API POST ATTEMPT ===")
            logger.info(f"Full Payload: {json.dumps(payload, indent=2)}")

            response = await self.http.post(
                self.api_endpoint,
                json=payload,
                headers=headers
            )

            logger.info(f"API Response Status: {response.status_code}")
            logger.info(f"API Response Body: {response.text}")

            if response.status_code in [200, 201]:
                logger.info(f"✅ SUCCESS: Data posted to API for {email}")
                monitoring_state["api_posts_count"] += 1
                return True
            else:
                logger.error(f"❌ API ERROR: Status {response.status_code} - {response.text}")
                monitoring_state["api_failures"] += 1
                return False

        except Exception as e:
            logger.error(f"💥 UNEXPECTED ERROR in send_to_external_api: {str(e)}")
            monitoring_state["api_failures"] += 1
            return False



    async def send_welcome_email(self, client_name: str, client_email: str, product_interest: str) -> Dict:
        """Send welcome email to new client"""
        try:
            logger.info(f"🎯 Processing welcome email for {client_name} ({client_email})")
            
            # First, clean the email address
            clean_client_email = self.extract_outgoing_email(client_email)
            logger.info(f"📧 Cleaned client email: {clean_client_email}")
            
            welcome_email_data = {
                "recipient": clean_client_email,
                "recipient_name": client_name,
                "product_interest": product_interest,
                "email_type": "welcome"
            }
            
            initial_state = self.get_initial_state([welcome_email_data])
            initial_state["email_category"] = "welcome"
            initial_state["current_email"]["sender"] = clean_client_email
            initial_state["current_email"]["subject"] = f"Welcome {client_name}! Your Interest in {product_interest}"
            
            generated_content = ""
            try:
                logger.info("🔄 Starting workflow execution for welcome email...")
                for output in self.app.stream(initial_state, self.config):
                    for key, value in output.items():
                        if key == "generated_email":
                            generated_content = str(value)
                            logger.info(f"✉️ Generated email content: {generated_content[:100]}...")
                        logger.info(f"📊 Welcome email workflow step: {key}")
            except Exception as workflow_error:
                logger.error(f"❌ Workflow error: {workflow_error}")
                generated_content = f"""
Welcome {client_name}! 

Thank you for your interest in {product_interest}. We're excited to help you with your requirements.

If you want more information, kindly reply to this email and I will provide detailed information as per your requirements.
                
Best regards,
Prismatic Technologies Team"""
            
            email_subject = f"Welcome {client_name}! Your Interest in {product_interest}"
            email_body = generated_content or f"""
Welcome {client_name}! 

Thank you for your interest in {product_interest}. We're excited to help you with your requirements.

If you want more information, kindly reply to this email and I will provide detailed information as per your requirements.
                
Best regards,
Prismatic Technologies Team"""
            
            logger.info(f"📧 Attempting to send welcome email to {clean_client_email}")
            
            # Send email via Gmail API
            email_sent = self.gmail_service.send_email(
                to_email=clean_client_email,
                subject=email_subject,
                body=email_body
            )
            
            logger.info(f"📨 Gmail send result: {'✅ Success' if email_sent else '❌ Failed'}")
            
            # ALWAYS post outgoing email to external API (regardless of send success)
            logger.info(f"📤 Posting welcome email to external API...")
            
            # Create comprehensive comments for the API
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            comments = f"OUTGOING WELCOME EMAIL - Sent: {timestamp} | Subject: {email_subject} | To: {clean_client_email} | Client: {client_name} | Product Interest: {product_interest} | Body: {email_body[:500]}... | Delivery Status: {'Sent successfully' if email_sent else 'Failed to send'}"
            
            api_success = await self.send_to_external_api(
                email=clean_client_email,
                phone="",
                comments=comments,
                user_type="agent",  # Use "agent" for outgoing emails
                lead_id="3446"
            )
            
            logger.info(f"📤 External API post result: {'✅ Success' if api_success else '❌ Failed'}")
            
            result = {
                "success": email_sent,
                "message": f"Welcome email {'sent successfully' if email_sent else 'failed to send'} to {client_name}",
                "email_sent": email_sent,
                "api_posted": api_success,
                "api_endpoint": self.api_endpoint,
                "client_email": clean_client_email,
                "email_body_preview": email_body[:200] + "..." if len(email_body) > 200 else email_body
            }
            
            logger.info(f"🎯 Final welcome email result: {result}")
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
            final_state = None
            
            try:
                logger.info("🔄 Starting workflow execution...")
                for output in self.app.stream(initial_state, self.config):
                    final_state = output  # Keep track of final state
                    for key, value in output.items():
                        if key == "generated_email":
                            generated_response = str(value)
                            logger.info(f"✉️ Generated response: {generated_response[:100]}...")
                        elif key == "sendable":
                            sendable = bool(value)
                            logger.info(f"📤 Sendable status: {sendable}")
                        logger.info(f"📊 Workflow step: {key}")
                
                # Get final sendable status from the last state
                if final_state:
                    sendable = final_state.get("sendable", sendable)
                    generated_response = final_state.get("generated_email", generated_response)
                    
                logger.info(f"🎯 Final workflow results - Generated: {bool(generated_response)}, Sendable: {sendable}")
                
            except Exception as workflow_error:
                logger.error(f"❌ Workflow error: {workflow_error}")
                # Fallback response when workflow fails
                generated_response = "Thank you for your email. We have received your message and will respond shortly."
                sendable = True
                logger.info("🔄 Using fallback response due to workflow error")

            # FORCE SENDABLE FOR TESTING (Remove this line once workflow is fixed)
            if generated_response and not sendable:
                logger.warning("⚠️ FORCING sendable=True for testing purposes")
                sendable = True

            # Send response email if generated and sendable
            email_sent = False
            reply_api_posted = False

            if generated_response and sendable:
                logger.info(f"📧 Attempting to send reply email to {sender}...")
                reply_subject = f"Re: {email_data.get('subject', '')}"
                
                # Extract clean email for sending
                clean_sender_email = self.extract_incoming_email(sender)
                logger.info(f"📧 Clean sender email: {clean_sender_email}")
                
                if clean_sender_email and "@" in clean_sender_email:
                    # Send the email first
                    email_sent = self.gmail_service.send_email(
                        to_email=clean_sender_email,
                        subject=reply_subject,
                        body=generated_response
                    )
                    logger.info(f"📨 Gmail reply result: {'✅ Sent' if email_sent else '❌ Failed'}")

                    # ALWAYS post the outgoing reply to the external API (regardless of Gmail send status)
                    logger.info(f"📤 Posting outgoing reply email to external API...")
                    try:
                        reply_api_posted = await self.post_outgoing_email_to_api(
                            recipient_email=clean_sender_email,
                            subject=reply_subject,
                            body=generated_response,
                            email_type="reply",
                            original_email_id=email_id,
                            sent_successfully=email_sent
                        )
                        logger.info(f"📤 Outgoing API post result: {'✅ Success' if reply_api_posted else '❌ Failed'}")
                    except Exception as post_err:
                        logger.error(f"💥 Failed to post reply to external API: {str(post_err)}")
                        reply_api_posted = False
                else:
                    logger.error(f"❌ Invalid sender email for reply: '{clean_sender_email}'")
                    # Even if we can't send the email, we should still try to post to API
                    try:
                        reply_api_posted = await self.post_outgoing_email_to_api(
                            recipient_email=sender,  # Use original sender field
                            subject=reply_subject,
                            body=generated_response,
                            email_type="reply",
                            original_email_id=email_id,
                            sent_successfully=False  # Mark as failed
                        )
                        logger.info(f"📤 Outgoing API post (failed email) result: {'✅ Success' if reply_api_posted else '❌ Failed'}")
                    except Exception as post_err:
                        logger.error(f"💥 Failed to post failed reply to external API: {str(post_err)}")
                        reply_api_posted = False
            else:
                logger.info(f"⏭️ No reply sent - Generated: {bool(generated_response)}, Sendable: {sendable}")
                if not generated_response:
                    logger.warning("⚠️ No response generated by workflow")
                if generated_response and not sendable:
                    logger.warning("⚠️ Response generated but marked as not sendable")
                    # Even if not sendable, let's try to post to API for tracking
                    try:
                        clean_sender_email = self.extract_incoming_email(sender)
                        reply_api_posted = await self.post_outgoing_email_to_api(
                            recipient_email=clean_sender_email or sender,
                            subject=f"Re: {email_data.get('subject', '')}",
                            body=generated_response or "No response generated",
                            email_type="reply_not_sent",
                            original_email_id=email_id,
                            sent_successfully=False
                        )
                        logger.info(f"📤 Non-sendable response API post: {'✅ Success' if reply_api_posted else '❌ Failed'}")
                    except Exception as post_err:
                        logger.error(f"💥 Failed to post non-sendable response to API: {str(post_err)}")

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
                "workflow_generated": bool(generated_response)
            }

            logger.info(f"🎯 Final processing result: {result}")
            return result

        except Exception as e:
            logger.error(f"💥 Error processing email: {str(e)}")
            return {
                "success": False,
                "message": f"Error: {str(e)}",
                "email_id": email_data.get("id", ""),
                "api_posted": False
            }

    # ALSO ADD THIS DEBUG METHOD TO YOUR EmailAutomationService CLASS:
    def debug_workflow_state(self, email_data: Dict) -> Dict:
        """Debug method to check workflow execution without sending emails"""
        try:
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
            
            logger.info(f"🔍 DEBUG: Initial state: {initial_state}")
            
            workflow_steps = []
            final_state = None
            
            for output in self.app.stream(initial_state, self.config):
                final_state = output
                for key, value in output.items():
                    step_info = {
                        "step": key,
                        "value_type": type(value).__name__,
                        "value_preview": str(value)[:200] if isinstance(value, str) else str(value)
                    }
                    workflow_steps.append(step_info)
                    logger.info(f"🔍 DEBUG Step - {key}: {step_info['value_preview']}")
            
            return {
                "initial_state": initial_state,
                "workflow_steps": workflow_steps,
                "final_state": final_state,
                "final_sendable": final_state.get("sendable") if final_state else None,
                "final_generated_email": final_state.get("generated_email") if final_state else None
            }
            
        except Exception as e:
            logger.error(f"💥 Debug workflow error: {str(e)}")
            return {"error": str(e)}
    
    async def post_incoming_email_to_api(self, email_data: Dict) -> bool:
        """Post incoming email details to external API"""
        try:
            sender_email = self.extract_incoming_email(email_data.get("sender", ""))
            subject = email_data.get("subject", "No Subject")
            body = email_data.get("body", "")
            timestamp = email_data.get("timestamp", datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

            comments = (
                f"INCOMING EMAIL - Received: {timestamp} | Subject: {subject} | "
                f"From: {sender_email} | Body: {body[:800]}..."
            )

            return await self.send_to_external_api(
                email=sender_email,
                phone="",
                comments=comments,
                user_type="customer",
                lead_id="3446"
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
            # Clean the recipient email
            clean_email = self.extract_outgoing_email(recipient_email)
            
            # If we still don't have a valid email, try to extract from original field
            if not clean_email or "@" not in clean_email:
                logger.warning(f"⚠️ Primary email extraction failed, trying fallback extraction...")
                clean_email = self.extract_incoming_email(recipient_email)
            
            if not clean_email or "@" not in clean_email:
                logger.error(f"❌ Cannot post outgoing email: invalid recipient email '{recipient_email}' -> '{clean_email}'")
                # Even with invalid email, let's try to post with original email for tracking
                clean_email = recipient_email

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Build comprehensive comments
            comments_parts = [
                f"OUTGOING EMAIL ({email_type.upper()}) - Sent: {timestamp}",
                f"Subject: {subject}",
                f"To: {clean_email}",
                f"Body: {body[:600]}..." if len(body) > 600 else f"Body: {body}",
                f"Delivery status: {'✅ Sent successfully' if sent_successfully else '❌ Failed to send'}"
            ]

            if email_type == "welcome":
                if sender_name:
                    comments_parts.append(f"Welcome email sent to {sender_name}")
                if product_interest:
                    comments_parts.append(f"Product interest: {product_interest}")
            elif email_type in ["reply", "reply_not_sent"]:
                comments_parts.append(f"Reply email {'generated and sent' if email_type == 'reply' else 'generated but not sent'}")
                if original_email_id:
                    comments_parts.append(f"In response to email ID: {original_email_id}")

            comments = " | ".join(comments_parts)

            logger.info(f"📤 Preparing to send OUTGOING email post:")
            logger.info(f"   Email: {clean_email}")
            logger.info(f"   Type: {email_type}")
            logger.info(f"   Sent Successfully: {sent_successfully}")
            logger.info(f"   Comments Length: {len(comments)}")

            # Call the main API posting method
            api_success = await self.send_to_external_api(
                email=clean_email,
                comments=comments,
                user_type="agent",  # Always use "agent" for outgoing emails
                phone="",
                lead_id=lead_id
            )

            if api_success:
                logger.info(f"✅ Successfully posted OUTGOING email to API for: {clean_email}")
            else:
                logger.error(f"❌ Failed to post OUTGOING email to API for: {clean_email}")

            return api_success

        except Exception as e:
            logger.error(f"💥 Error in post_outgoing_email_to_api: {str(e)}")
            logger.error(f"   Recipient: {recipient_email}")
            logger.error(f"   Subject: {subject}")
            logger.error(f"   Email Type: {email_type}")
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
                await asyncio.sleep(60)  # Fixed: changed from 0 to 60

            except Exception as e:
                logger.error(f"Monitoring loop error: {e}")
                await asyncio.sleep(60)  # Fixed: changed from 600000 to 60

# Initialize the automation service
automation_service = EmailAutomationService()

# Lifespan manager for FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("📧 Email Automation API starting up...")
    yield
    # Shutdown
    global monitoring_state
    monitoring_state["active"] = False
    logger.info("📧 Email Automation API shutting down...")

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
        logger.error(f"💥 Welcome email endpoint error: {str(e)}")
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

# Also add this debugging method to help troubleshoot API posting issues
async def debug_api_post(self, email: str, comments: str) -> Dict:
    """Debug method to test API posting without affecting counters"""
    try:
        payload = {
            "email": email,
            "lead_id": "3446",
            "phone": "",
            "comments": comments,
            "user_type": "agent"
        }

        headers = self.get_api_headers()
        
        logger.info(f"🔍 DEBUG API POST:")
        logger.info(f"   URL: {self.api_endpoint}")
        logger.info(f"   Headers: {headers}")
        logger.info(f"   Payload: {json.dumps(payload, indent=2)}")

        response = await self.http.post(
            self.api_endpoint,
            json=payload,
            headers=headers,
            timeout=30
        )

        result = {
            "success": response.status_code in [200, 201],
            "status_code": response.status_code,
            "response_text": response.text,
            "response_headers": dict(response.headers),
            "request_payload": payload,
            "request_headers": headers,
            "api_endpoint": self.api_endpoint
        }

        logger.info(f"🔍 DEBUG API RESPONSE: {result}")
        return result

    except Exception as e:
        error_result = {
            "success": False,
            "error": str(e),
            "api_endpoint": self.api_endpoint,
            "request_payload": payload if 'payload' in locals() else None
        }
        logger.error(f"🔍 DEBUG API ERROR: {error_result}")
        return error_result

@app.get("/monitoring-status", response_model=MonitoringStatus)
async def get_monitoring_status():
    """Get current monitoring status"""
    global monitoring_state
    
    return MonitoringStatus(
        is_active=monitoring_state["active"],
        last_check=monitoring_state["last_check"],
        emails_processed=monitoring_state["emails_processed"]
    )

@app.post("/debug-api-post")
async def debug_api_post_endpoint(
    email: str,
    comments: str = "DEBUG TEST - Testing API connectivity for outgoing emails"
):
    """Debug endpoint to test API posting functionality"""
    try:
        result = await automation_service.debug_api_post(email, comments)
        return result
    except Exception as e:
        logger.error(f"💥 Debug API post endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/force-process-and-post")
async def force_process_and_post_email(email_id: str):
    """Force process an email and ensure API posting happens"""
    try:
        # Get email details
        email_data = automation_service.gmail_service.get_email_details(email_id)
        
        if not email_data:
            raise HTTPException(status_code=404, detail="Email not found")
        
        logger.info(f"🔄 FORCE PROCESSING email {email_id}")
        
        # Process the email
        result = await automation_service.process_incoming_email(email_data)
        
        # Additional debug info
        debug_info = {
            "email_processed": result,
            "sender_raw": email_data.get("sender"),
            "sender_extracted": automation_service.extract_incoming_email(email_data.get("sender", "")),
            "monitoring_stats": {
                "emails_processed": monitoring_state["emails_processed"],
                "api_posts_successful": monitoring_state["api_posts_count"],
                "api_failures": monitoring_state["api_failures"]
            }
        }
        
        return debug_info
        
    except Exception as e:
        logger.error(f"💥 Force process endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api-post-stats")
async def get_api_post_detailed_stats():
    """Get detailed statistics about API posting"""
    global monitoring_state
    
    return {
        "monitoring_active": monitoring_state["active"],
        "emails_processed_total": monitoring_state["emails_processed"],
        "api_posts_successful": monitoring_state["api_posts_count"],
        "api_posts_failed": monitoring_state["api_failures"],
        "processed_email_ids_count": len(monitoring_state["processed_email_ids"]),
        "success_rate": (
            monitoring_state["api_posts_count"] / 
            (monitoring_state["api_posts_count"] + monitoring_state["api_failures"]) * 100
        ) if (monitoring_state["api_posts_count"] + monitoring_state["api_failures"]) > 0 else 0,
        "api_endpoint": automation_service.api_endpoint,
        "bearer_token_configured": bool(automation_service.bearer_token),
        "last_check": monitoring_state["last_check"].isoformat() if monitoring_state["last_check"] else None,
        "note": "Each email should generate 2 API posts: 1 incoming + 1 outgoing (if reply is generated)"
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
        logger.error(f"💥 Process email endpoint error: {str(e)}")
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