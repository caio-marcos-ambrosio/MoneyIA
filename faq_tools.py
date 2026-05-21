import os
from dotenv import load_dotenv
from langchain.tools import tool
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
# pip install faiss-cpu
from langchain_google_genai import GoogleGenerativeAIEmbeddings

load_dotenv()

PDF_PATH = "./pdf/FAQ_assessor_v1.1.pdf"
FAISS_INDEX_PATH = "./faiss_index"

embeddings = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-2-preview",
    google_api_key=os.getenv("GEMINI_API_KEY")
)

def load_or_create_faiss_index():
    if os.path.exists(FAISS_INDEX_PATH):
        return FAISS.load_local(
            FAISS_INDEX_PATH,
            embeddings,
            allow_dangerous_deserialization=True
        )
    
    loader = PyPDFLoader(PDF_PATH)
    docs = loader.load()

    splitter = RecursiveCharacterTextSplitter(chunk_size=200, chunk_overlap=150)
    chunks = splitter.split_documents(docs)

    db = FAISS.from_documents(chunks, embeddings)
    db.save_local(FAISS_INDEX_PATH)
    return db

db= load_or_create_faiss_index()

@tool()
def faq_retriever(question: str) -> str:
    """Busca no FAQ oficial os trechos mais relevantes para responder a pergunta"""  
    results = db.similarity_search(question, k=6)
    
    response = "\n".join([doc.page_content for doc in results])
    
    return response