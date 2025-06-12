import asyncio
import json
import requests
import re
import time
from datetime import datetime, timedelta
from typing import Dict

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
            comments = f"OUTGOING WELCOME EMAIL - Sent: {timestamp} | To: {clean_email} | Subject: {email_subject} | Recipient Name: {client_name} | Product Interest: {product_interest} | Body: {email_body} | Delivery Status: {'Sent successfully' if email_sent else 'Failed to send'}"
            
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

    # Add a test method to verify API connectivity
    def test_api_connection(self):
        """Test the API connection with a simple call"""
        try:
            logger.info("🧪 Testing API connection...")
            test_email = "test@example.com"
            test_comments = f"API CONNECTION TEST - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            
            result = self.send_to_external_api_sync(
                email=test_email,
                comments=test_comments,
                user_type="test"
            )
            
            logger.info(f"API test result: {'✅ Success' if result else '❌ Failed'}")
            return result
        except Exception as e:
            logger.error(f"API test failed: {str(e)}")
            return False