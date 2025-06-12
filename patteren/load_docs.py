# load_docs.py
import os
import pickle
import faiss
from sentence_transformers import SentenceTransformer

TEXT_FILE = "pris.txt"
SAVE_DIR = "faiss_store"

def read_document(file_path):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"❌ File not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    if not text.strip():
        raise ValueError("❌ Input file is empty or contains only whitespace.")

    print(f"📏 Document length: {len(text)} characters")
    return split_into_chunks(text)

def split_into_chunks(text, chunk_size=300, overlap=50):
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)
    if not chunks:
        raise ValueError("❌ No chunks created from document.")
    print(f"🧠 Generated {len(chunks)} chunks.")
    return chunks

def build_faiss_index(chunks, model):
    print("🧠 Encoding chunks into embeddings...")
    embeddings = model.encode(chunks, convert_to_numpy=True, show_progress_bar=True)

    if embeddings is None or len(embeddings) == 0:
        raise ValueError("❌ Failed to generate embeddings. Check input data and model.")

    print(f"✅ Embedding shape: {embeddings.shape}")
    dim = embeddings.shape[1]

    index = faiss.IndexFlatL2(dim)
    index.add(embeddings)

    print("📐 FAISS index created and populated.")
    return index

def save_data(index, chunks):
    os.makedirs(SAVE_DIR, exist_ok=True)

    with open(os.path.join(SAVE_DIR, "vectors.pkl"), "wb") as f:
        pickle.dump(index, f)
    with open(os.path.join(SAVE_DIR, "docs.pkl"), "wb") as f:
        pickle.dump(chunks, f)

    print(f"💾 Data saved to: {SAVE_DIR}")

if __name__ == "__main__":
    print(f"🚀 Starting FAISS index build for: {TEXT_FILE}")

    # Step 1: Read and chunk
    chunks = read_document(TEXT_FILE)

    # Step 2: Load model
    print("📦 Loading embedding model (all-MiniLM-L6-v2)...")
    model = SentenceTransformer('all-MiniLM-L6-v2')

    # Step 3: Build FAISS index
    index = build_faiss_index(chunks, model)

    # Step 4: Save to disk
    save_data(index, chunks)

    print("✅ All done. You can now use faiss_store/ for retrieval.")
