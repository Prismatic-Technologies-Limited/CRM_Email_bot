import openai  # or Groq API via requests
from vector_store import VectorStore

class RAGResponder:
    def __init__(self, api_key, use_groq=False):
        self.store = VectorStore()
        self.store.load()
        self.api_key = api_key
        self.use_groq = use_groq

        # Define your custom system prompt here
        self.system_prompt = (
            "You are a helpful email assistant for a software company that offers products like "
            "ERP, LMS, POS, and OMS. Answer client questions using the context below. "
            "If the context is not enough, politely ask for clarification."
        )

    def query(self, user_msg):
        # Search relevant chunks from the vector store
        relevant_chunks = self.store.search(user_msg)
        context = "\n".join(relevant_chunks)

        # Compose the full prompt with retrieved context
        prompt = (
            f"{self.system_prompt}\n\n"
            f"Context:\n{context}\n\n"
            f"User: {user_msg}\n\n"
            f"Assistant:"
        )
        return self._generate_response(prompt)

    def _generate_response(self, prompt):
        if self.use_groq:
            import requests
            headers = {"Authorization": f"Bearer {self.api_key}"}
            payload = {
                "model": "mixtral-8x7b",
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt}
                ]
            }
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload,
                headers=headers
            )
            return response.json()["choices"][0]["message"]["content"]

        else:
            openai.api_key = self.api_key
            response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt}
                ]
            )
            return response['choices'][0]['message']['content'].strip()
