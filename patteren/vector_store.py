import faiss
import numpy as np
import pickle
import os
from sentence_transformers import SentenceTransformer

class VectorStore:
    def __init__(self, index_file='faiss.index', db_file='texts.pkl'):
        self.index_file = index_file
        self.db_file = db_file
        self.texts = []
        self.index = None
        self.model = SentenceTransformer("all-MiniLM-L6-v2")  # Load SBERT model

    def embed_texts(self, texts):
        embeddings = self.model.encode(texts, convert_to_numpy=True, show_progress_bar=True)
        return embeddings.astype('float32')

    def add_texts(self, new_texts):
        if not new_texts:
            print("⚠️ No new texts provided.")
            return

        self.texts.extend(new_texts)
        embeddings = self.embed_texts(new_texts)

        if self.index is None:
            dim = embeddings.shape[1]
            self.index = faiss.IndexFlatL2(dim)

        self.index.add(embeddings)
        self._save()
        print(f"✅ Added {len(new_texts)} new texts. Total: {len(self.texts)}")

    def search(self, query, k=3):
        if self.index is None or not self.texts:
            print("❌ Index is empty. Load or add texts first.")
            return []

        query_embedding = self.embed_texts([query])
        D, I = self.index.search(query_embedding, k)
        return [self.texts[i] for i in I[0] if i < len(self.texts)]

    def _save(self):
        if self.index is not None:
            faiss.write_index(self.index, self.index_file)
            with open(self.db_file, 'wb') as f:
                pickle.dump(self.texts, f)
            print("💾 Saved index and texts.")
        else:
            print("⚠️ Index not initialized, nothing to save.")

    def load(self):
        if os.path.exists(self.index_file) and os.path.exists(self.db_file):
            try:
                self.index = faiss.read_index(self.index_file)
                with open(self.db_file, 'rb') as f:
                    self.texts = pickle.load(f)
                print(f"📂 Loaded FAISS index and {len(self.texts)} texts.")
            except Exception as e:
                print(f"❌ Error loading index or texts: {e}")
                self.index = None
                self.texts = []
        else:
            print("ℹ️ No saved index found. Starting fresh.")
