import asyncio
import json
import time
import requests
import smtplib
import imaplib
import email
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from colorama import Fore, Style
from src.graph import Workflow
from dotenv import load_dotenv
import threading
import logging
import os

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

GMAIL_SMTP_SERVER = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
GMAIL_IMAP_SERVER = "imap.gmail.com"
CHECK_INTERVAL = 30  # seconds

# Read from env
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")  # Use app password for SMTP & IMAP auth

class EmailAutomationSystem:
    def __init__(self):
        self.config = {'recursion_limit': 100}
        self.workflow = Workflow()
        self.app = self.workflow.app
        self.api_endpoint = "https://inhouse.prismaticcrm.com/api/post-comment"
        self.monitoring_active = False
        self.last_check_time = datetime.utcnow()

    def get_initial_state(self, email_data=None):
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

    def send_email(self, recipient_email, subject, body):
        """Send email using SMTP"""
        try:
            msg = MIMEText(body, 'plain', 'utf-8')
            msg['Subject'] = subject
            msg['From'] = GMAIL_EMAIL
            msg['To'] = recipient_email

            with smtplib.SMTP(GMAIL_SMTP_SERVER, GMAIL_SMTP_PORT) as smtp:
                smtp.starttls()
                smtp.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
                smtp.sendmail(GMAIL_EMAIL, [recipient_email], msg.as_string())

            logger.info(f"Email sent to {recipient_email} with subject '{subject}'")
            print(Fore.GREEN + f"Email sent to {recipient_email}" + Style.RESET_ALL)
            return True

        except Exception as e:
            logger.error(f"Failed to send email to {recipient_email}: {str(e)}")
            print(Fore.RED + f"Failed to send email: {str(e)}" + Style.RESET_ALL)
            return False

    def send_welcome_email(self, client_name: str, client_email: str, product_interest: str):
        print(Fore.GREEN + f"Preparing to send welcome email to {client_name} ({client_email})" + Style.RESET_ALL)

        subject = f"Welcome {client_name}! Your Interest in {product_interest}"
        body = (
            f"Dear {client_name},\n\n"
            f"Thank you for showing interest in {product_interest}.\n"
            "We will get back to you shortly with more information.\n\n"
            "Best regards,\nYour Company"
        )

        # Send email via SMTP
        if self.send_email(client_email, subject, body):
            # Run your workflow if needed (optional)
            initial_state = self.get_initial_state([{
                "recipient": client_email,
                "recipient_name": client_name,
                "product_interest": product_interest,
                "email_type": "welcome"
            }])
            initial_state["email_category"] = "welcome"
            initial_state["current_email"]["sender"] = client_email
            initial_state["current_email"]["subject"] = subject

            print(Fore.CYAN + "Processing welcome email workflow..." + Style.RESET_ALL)
            for output in self.app.stream(initial_state, self.config):
                for key in output.keys():
                    print(Fore.YELLOW + f"Workflow step: {key}" + Style.RESET_ALL)

            # Send info to external API
            self.send_to_external_api(
                email=client_email,
                comments=f"New client interested in {product_interest}. Welcome email sent.",
                user_type="customer"
            )
            print(Fore.GREEN + f"Welcome email sent successfully to {client_name}!" + Style.RESET_ALL)

    def send_to_external_api(self, email: str, comments: str, user_type: str = "customer", phone: str = ""):
        try:
            payload = {
                "email": email,
                "phone": phone,
                "comments": comments,
                "user_type": user_type
            }

            print(Fore.CYAN + f"Sending data to external API for {email}" + Style.RESET_ALL)
            response = requests.post(
                self.api_endpoint,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )

            if response.status_code == 200:
                print(Fore.GREEN + f"Successfully sent data to API for {email}" + Style.RESET_ALL)
            else:
                logger.error(f"API request failed: {response.status_code} - {response.text}")
                print(Fore.RED + f"API request failed with status {response.status_code}" + Style.RESET_ALL)

        except requests.exceptions.RequestException as e:
            logger.error(f"Error sending to external API: {str(e)}")
            print(Fore.RED + f"Failed to send to external API: {str(e)}" + Style.RESET_ALL)

    def fetch_new_emails(self) -> list:
        """Fetch new unread emails from Gmail IMAP"""
        try:
            mail = imaplib.IMAP4_SSL(GMAIL_IMAP_SERVER)
            mail.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
            mail.select("inbox")

            # Search for unseen emails
            status, data = mail.search(None, '(UNSEEN)')
            email_ids = data[0].split()
            emails = []

            for eid in email_ids:
                status, msg_data = mail.fetch(eid, '(RFC822)')
                if status != 'OK':
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                sender = email.utils.parseaddr(msg.get('From'))[1]
                subject = msg.get('Subject', '')
                msg_id = msg.get('Message-ID', '')
                thread_id = msg.get('References', '') or msg.get('In-Reply-To', '')
                body = ""

                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        cdispo = str(part.get('Content-Disposition'))
                        # skip attachments
                        if ctype == 'text/plain' and 'attachment' not in cdispo:
                            body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            break
                else:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')

                emails.append({
                    "id": eid.decode(),
                    "threadId": thread_id,
                    "messageId": msg_id,
                    "references": msg.get('References', ''),
                    "sender": sender,
                    "subject": subject,
                    "body": body
                })

            mail.logout()
            return emails

        except Exception as e:
            logger.error(f"Error fetching emails: {str(e)}")
            return []

    def process_incoming_email(self, email_data: dict):
        print(Fore.CYAN + f"Processing incoming email from {email_data.get('sender')}" + Style.RESET_ALL)

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

        print(Fore.YELLOW + "Running email processing workflow..." + Style.RESET_ALL)
        for output in self.app.stream(initial_state, self.config):
            for key in output.keys():
                print(Fore.YELLOW + f"Workflow step: {key}" + Style.RESET_ALL)

        self.send_to_external_api(
            email=email_data.get("sender", ""),
            comments=f"Email received: {email_data.get('subject', '')} - {email_data.get('body', '')[:100]}...",
            user_type="customer"
        )
        print(Fore.GREEN + f"Processed email from {email_data.get('sender')}" + Style.RESET_ALL)

    def monitor_emails(self):
        print(Fore.GREEN + "Starting email monitoring..." + Style.RESET_ALL)
        self.monitoring_active = True

        while self.monitoring_active:
            try:
                new_emails = self.fetch_new_emails()
                if new_emails:
                    print(Fore.CYAN + f"Found {len(new_emails)} new emails" + Style.RESET_ALL)
                    for email_data in new_emails:
                        self.process_incoming_email(email_data)
                        time.sleep(1)
                time.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Error in email monitoring: {str(e)}")
                time.sleep(60)

    def stop_monitoring(self):
        self.monitoring_active = False
        print(Fore.YELLOW + "Email monitoring stopped" + Style.RESET_ALL)

    def run_single_email_workflow(self, email_data=None):
        try:
            print(Fore.GREEN + "Starting single email workflow..." + Style.RESET_ALL)
            initial_state = self.get_initial_state(email_data)
            for output in self.app.stream(initial_state, self.config):
                for key in output.keys():
                    print(Fore.CYAN + f"Finished running: {key}" + Style.RESET_ALL)
        except Exception as e:
            logger.error(f"Error in single email workflow: {str(e)}")
            print(Fore.RED + f"Workflow error: {str(e)}" + Style.RESET_ALL)

def main():
    system = EmailAutomationSystem()
    monitor_thread = None

    while True:
        print(Fore.CYAN + """
1. Send welcome email
2. Start monitoring emails (run workflow on new emails)
3. Stop monitoring emails
4. Exit
        """ + Style.RESET_ALL)

        choice = input("Enter your choice (1-4): ").strip()

        if choice == "1":
            client_name = input("Enter client name: ").strip()
            client_email = input("Enter client email: ").strip()
            product_interest = input("Enter product interest: ").strip()
            system.send_welcome_email(client_name, client_email, product_interest)

        elif choice == "2":
            if monitor_thread and monitor_thread.is_alive():
                print(Fore.YELLOW + "Monitoring already running." + Style.RESET_ALL)
            else:
                monitor_thread = threading.Thread(target=system.monitor_emails, daemon=True)
                monitor_thread.start()
                print(Fore.GREEN + "Email monitoring started in background thread." + Style.RESET_ALL)

        elif choice == "3":
            system.stop_monitoring()

        elif choice == "4":
            system.stop_monitoring()
            print(Fore.GREEN + "Exiting program." + Style.RESET_ALL)
            break

        else:
            print(Fore.RED + "Invalid choice. Please enter a number between 1-4." + Style.RESET_ALL)

if __name__ == "__main__":
    main()
