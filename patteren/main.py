# main.py
import time
from gmail_tools import GmailWatcher
from rag_engine import RAGResponder

rag = RAGResponder(api_key="YOUR_OPENAI_OR_GROQ_API_KEY", use_groq=True)
gmail = GmailWatcher()

def run_bot():
    print("📬 Listening for emails...")
    while True:
        emails = gmail.fetch_unread()
        for email in emails:
            query = email['snippet']
            response = rag.query(query)
            print(f"\nClient: {query}\nBot: {response}\n")
            gmail.reply(email, response)
        time.sleep(15)

if __name__ == "__main__":
    run_bot()
