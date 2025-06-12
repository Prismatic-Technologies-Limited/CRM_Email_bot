import requests # Import the requests library for making HTTP calls to external APIs
from colorama import Fore, Style
from .agents import Agents
from .tools.GmailTools import GmailToolsClass
from .state import GraphState, Email


class Nodes:
    def __init__(self):
        self.agents = Agents()
        self.gmail_tools = GmailToolsClass()
        # Define the URL for your external API endpoint.
        self.external_api_url = "https://inhouse.prismaticcrm.com/api/post-comment" 
        # Define your Bearer Token for API authentication
        self.bearer_token = "31|GIi7u4iBXfSOfxYVnMEcRrBzOkpYUBm6Adzxx2j9"

    def _send_to_external_api(self, email_data: dict, direction: str):
        """
        Helper function to send email data to an external API with the specified payload structure.

        Args:
            email_data (dict): A dictionary containing the email details (e.g., sender, subject, body).
                               For incoming emails, this will be the raw email dict from GmailTools.
                               For outgoing emails, it will be constructed from the current_email and generated_email.
            direction (str): Indicates if the email is "incoming" or "outgoing".
        """
        # Construct the payload to match the external API's expected format
        # {
        #   "email": "jugnua995@gmail.com",
        #   "lead_id": "3446",
        #   "comments": "... OUTGOING EMAIL ...",
        #   "user_type": "agent"
        # }
        payload = {
            "email": email_data.get("sender", "unknown@example.com"), 
            # Placeholder for lead_id. The API expects a numeric-like ID (e.g., "3446").
            # The current email's threadId ('1973f7a20dc9363b') is a long alphanumeric string
            # which caused a "Data truncated" error in the database.
            # You will need to implement logic to map emails to actual lead IDs from your CRM.
            # For now, a default numeric string "0" is used to prevent the truncation error.
            "lead_id": "0", 
            "comments": f"{direction.upper()} EMAIL: \nSubject: {email_data.get('subject', 'N/A')}\n\n{email_data.get('body', 'No body content.')}",
            "user_type": "agent" # As specified by the user
        }
        
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json"
        }

        print(Fore.BLUE + f"Attempting to send {direction} email data to external API..." + Style.RESET_ALL)
        try:
            response = requests.post(self.external_api_url, json=payload, headers=headers)
            response.raise_for_status()  # Raise an exception for HTTP errors (4xx or 5xx)
            print(Fore.GREEN + f"Successfully sent {direction} email data to external API." + Style.RESET_ALL)
            print(Fore.GREEN + f"API Request Payload: {payload}" + Style.RESET_ALL) # Show the payload sent
            print(Fore.GREEN + f"API Response: {response.status_code} - {response.text}" + Style.RESET_ALL)
        except requests.exceptions.RequestException as e:
            print(Fore.RED + f"Failed to send {direction} email data to external API: {e}" + Style.RESET_ALL)
            if hasattr(e, 'response') and e.response is not None:
                print(Fore.RED + f"API Error Response: {e.response.status_code} - {e.response.text}" + Style.RESET_ALL)
            print(Fore.RED + f"Payload attempted: {payload}" + Style.RESET_ALL) # Show the payload that failed
        except Exception as e:
            print(Fore.RED + f"An unexpected error occurred while sending {direction} email data: {e}" + Style.RESET_ALL)


    def load_new_emails(self, state: GraphState) -> GraphState:
        """Loads new emails from Gmail and updates the state.
        
        This node now also sends details of each incoming email to an external API.
        """
        print(Fore.YELLOW + "Loading new emails...\n" + Style.RESET_ALL)
        recent_emails = self.gmail_tools.fetch_unanswered_emails()
        emails = [Email(**email) for email in recent_emails]

        # --- NEW ADDITION: Send incoming emails to external API ---
        # Iterate through the fetched emails (as dictionaries) and send to the external API
        for email_dict in recent_emails:
            self._send_to_external_api(email_dict, "incoming")
        # --- END NEW ADDITION ---

        return {"emails": emails}

    def check_new_emails(self, state: GraphState) -> str:
        """Checks if there are new emails to process."""
        if len(state['emails']) == 0:
            print(Fore.RED + "No new emails" + Style.RESET_ALL)
            return "empty"
        else:
            print(Fore.GREEN + "New emails to process" + Style.RESET_ALL)
            return "process"
        
    def is_email_inbox_empty(self, state: GraphState) -> GraphState:
        return state

    def categorize_email(self, state: GraphState) -> GraphState:
        """Categorizes the current email using the categorize_email agent."""
        print(Fore.YELLOW + "Checking email category...\n" + Style.RESET_ALL)
        
        # Get the last email
        current_email = state["emails"][-1]
        result = self.agents.categorize_email.invoke({"email": current_email.body})
        print(Fore.MAGENTA + f"Email category: {result.category.value}" + Style.RESET_ALL)
        
        return {
            "email_category": result.category.value,
            "current_email": current_email
        }

    def route_email_based_on_category(self, state: GraphState) -> str:
        """Routes the email based on its category."""
        print(Fore.YELLOW + "Routing email based on category...\n" + Style.RESET_ALL)
        category = state["email_category"]
        if category == "product_enquiry":
            return "product related"
        elif category == "unrelated":
            return "unrelated"
        else:
            return "not product related"

    def construct_rag_queries(self, state: GraphState) -> GraphState:
        """Constructs RAG queries based on the email content."""
        print(Fore.YELLOW + "Designing RAG query...\n" + Style.RESET_ALL)
        email_content = state["current_email"].body
        query_result = self.agents.design_rag_queries.invoke({"email": email_content})
        
        return {"rag_queries": query_result.queries}

    def retrieve_from_rag(self, state: GraphState) -> GraphState:
        """Retrieves information from internal knowledge based on RAG questions."""
        print(Fore.YELLOW + "Retrieving information from internal knowledge...\n" + Style.RESET_ALL)
        final_answer = ""
        for query in state["rag_queries"]:
            rag_result = self.agents.generate_rag_answer.invoke(query)
            final_answer += query + "\n" + rag_result + "\n\n"
        
        return {"retrieved_documents": final_answer}

    def write_draft_email(self, state: GraphState) -> GraphState:
        """Writes a draft email based on the current email and retrieved information."""
        print(Fore.YELLOW + "Writing draft email...\n" + Style.RESET_ALL)
        
        # Format input to the writer agent
        inputs = (
            f'# **EMAIL CATEGORY:** {state["email_category"]}\n\n'
            f'# **EMAIL CONTENT:**\n{state["current_email"].body}\n\n'
            f'# **INFORMATION:**\n{state["retrieved_documents"]}' # Empty for feedback or complaint
        )
        
        # Get messages history for current email
        writer_messages = state.get('writer_messages', [])
        
        # Write email
        draft_result = self.agents.email_writer.invoke({
            "email_information": inputs,
            "history": writer_messages
        })
        email = draft_result.email
        trials = state.get('trials', 0) + 1

        # Append writer's draft to the message list
        writer_messages.append(f"**Draft {trials}:**\n{email}")

        return {
            "generated_email": email, 
            "trials": trials,
            "writer_messages": writer_messages
        }

    def verify_generated_email(self, state: GraphState) -> GraphState:
        """Verifies the generated email using the proofreader agent."""
        print(Fore.YELLOW + "Verifying generated email...\n" + Style.RESET_ALL)
        review = self.agents.email_proofreader.invoke({
            "initial_email": state["current_email"].body,
            "generated_email": state["generated_email"],
        })

        writer_messages = state.get('writer_messages', [])
        writer_messages.append(f"**Proofreader Feedback:**\n{review.feedback}")

        return {
            "sendable": review.send,
            "writer_messages": writer_messages
        }

    def must_rewrite(self, state: GraphState) -> str:
        """Determines if the email needs to be rewritten based on the review and trial count."""
        email_sendable = state["sendable"]
        if email_sendable:
            print(Fore.GREEN + "Email is good, ready to be sent!!!" + Style.RESET_ALL)
            state["emails"].pop()
            state["writer_messages"] = []
            return "send"  # This should direct to sending the email, not drafting it
        elif state["trials"] >= 3:
            print(Fore.RED + "Email is not good, we reached max trials must stop!!!" + Style.RESET_ALL)
            state["emails"].pop()
            state["writer_messages"] = []
            return "stop"
        else:
            print(Fore.RED + "Email is not good, must rewrite it..." + Style.RESET_ALL)
            return "rewrite"


    def create_draft_response(self, state: GraphState) -> GraphState:
        """Creates a draft response in Gmail."""
        print(Fore.YELLOW + "Creating draft email...\n" + Style.RESET_ALL)
        self.gmail_tools.create_draft_reply(state["current_email"], state["generated_email"])
        
        return {"retrieved_documents": "", "trials": 0}

    def send_email_response(self, state: GraphState) -> GraphState:
        """Sends the email response directly using Gmail.
        
        This node now also sends details of the outgoing email to an external API.
        """
        print(Fore.YELLOW + "Sending email...\n" + Style.RESET_ALL)

        # --- NEW ADDITION: Send outgoing email to external API ---
        # Create a dictionary from the current_email Pydantic model for the API call.
        # Note: state["current_email"] is the *incoming* email that triggered the response.
        # We also use the generated_email as the body of the outgoing message.
        outgoing_email_data = state["current_email"].model_dump()
        outgoing_email_data["body"] = state["generated_email"] # Use the generated body for the outgoing email
        # You might also want to add recipient information if available
        # e.g., outgoing_email_data["recipient"] = state["current_email"].sender
        
        self._send_to_external_api(outgoing_email_data, "outgoing")
        # --- END NEW ADDITION ---

        self.gmail_tools.send_reply(state["current_email"], state["generated_email"])
        
        return {"retrieved_documents": "", "trials": 0}
    
    def skip_unrelated_email(self, state):
        """Skip unrelated email and remove from emails list."""
        print("Skipping unrelated email...\n")
        state["emails"].pop()
        return state

