import io
import json
import os
import asyncio
from typing import Optional

from openai import AzureOpenAI
from redis.asyncio import Redis
import redis.asyncio as redis
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.servicebus.aio import ServiceBusClient

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, func, select

from PyPDF2 import PdfReader
from docx import Document


SECRETS_DIR = os.getenv("SECRETS_DIR", "/mnt/secrets")


def read_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    path = os.path.join(SECRETS_DIR, name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return os.getenv(name, default)


def require_config(name: str, value: Optional[str]) -> str:
    if not value:
        raise RuntimeError(f"Missing required configuration: {name}")
    return value


DB_HOST = require_config("DB_HOST", read_secret("DB_HOST"))
DB_PORT = read_secret("DB_PORT", "5432")
DB_NAME = require_config("DB_NAME", read_secret("DB_NAME"))
DB_USER = require_config("DB_USER", read_secret("DB_USER"))
DB_PASSWORD = require_config("DB_PASSWORD", read_secret("DB_PASSWORD"))

DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

AZURE_OPENAI_KEY = require_config("AZURE_OPENAI_KEY", read_secret("AZURE_OPENAI_KEY"))
AZURE_OPENAI_API_VERSION = require_config("AZURE_OPENAI_API_VERSION", read_secret("AZURE_OPENAI_API_VERSION"))
AZURE_OPENAI_ENDPOINT = require_config("AZURE_OPENAI_ENDPOINT", read_secret("AZURE_OPENAI_ENDPOINT"))
AZURE_DEPLOYMENT_NAME = require_config("AZURE_DEPLOYMENT_NAME", read_secret("AZURE_DEPLOYMENT_NAME"))

REDIS_HOST = require_config("REDIS_HOST", read_secret("REDIS_HOST"))
REDIS_PORT = int(read_secret("REDIS_PORT", "6379"))
REDIS_DB = int(read_secret("REDIS_DB", "0"))
REDIS_PASSWORD = read_secret("REDIS_PASSWORD")
REDIS_USERNAME = read_secret("REDIS_USERNAME", "default")

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    username=REDIS_USERNAME,
    password=REDIS_PASSWORD,
    db=REDIS_DB,
    ssl=True,
    socket_timeout=5,
    socket_connect_timeout=5,
    decode_responses=True
)

if REDIS_PASSWORD:
    REDIS_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
else:
    REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

AZURE_STORAGE_AUTH_MODE = read_secret(
    "AZURE_STORAGE_AUTH_MODE",
    os.getenv("AZURE_STORAGE_AUTH_MODE", "connection_string")
).lower()

AZURE_STORAGE_ACCOUNT_URL = read_secret(
    "AZURE_STORAGE_ACCOUNT_URL",
    os.getenv("AZURE_STORAGE_ACCOUNT_URL")
)

AZURE_STORAGE_CONNECTION_STRING = read_secret(
    "AZURE_STORAGE_CONNECTION_STRING",
    os.getenv("AZURE_STORAGE_CONNECTION_STRING")
)

BLOB_CONTAINER_NAME = read_secret(
    "BLOB_CONTAINER_NAME",
    os.getenv("BLOB_CONTAINER_NAME", "uploads")
)

SERVICE_BUS_AUTH_MODE = read_secret(
    "SERVICE_BUS_AUTH_MODE",
    os.getenv("SERVICE_BUS_AUTH_MODE", "connection_string")
).lower()

SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE = read_secret(
    "SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE",
    os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
)

SERVICE_BUS_CONNECTION_STRING = read_secret(
    "SERVICE_BUS_CONNECTION_STRING",
    os.getenv("SERVICE_BUS_CONNECTION_STRING")
)

SERVICE_BUS_QUEUE_NAME = read_secret(
    "SERVICE_BUS_QUEUE_NAME",
    os.getenv("SERVICE_BUS_QUEUE_NAME", "summary-jobs")
)

MAX_TEXT_LENGTH = int(read_secret("MAX_TEXT_LENGTH", "25000"))

azure_credential = DefaultAzureCredential()


def get_blob_service_client() -> BlobServiceClient:
    if AZURE_STORAGE_AUTH_MODE == "managed_identity":
        if not AZURE_STORAGE_ACCOUNT_URL:
            raise RuntimeError("Missing AZURE_STORAGE_ACCOUNT_URL for managed identity mode")
        return BlobServiceClient(
            account_url=AZURE_STORAGE_ACCOUNT_URL,
            credential=azure_credential
        )

    if AZURE_STORAGE_AUTH_MODE == "connection_string":
        if not AZURE_STORAGE_CONNECTION_STRING:
            raise RuntimeError("Missing AZURE_STORAGE_CONNECTION_STRING for connection string mode")
        return BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)

    raise RuntimeError(f"Unsupported AZURE_STORAGE_AUTH_MODE: {AZURE_STORAGE_AUTH_MODE}")


def get_servicebus_client() -> ServiceBusClient:
    if SERVICE_BUS_AUTH_MODE == "managed_identity":
        if not SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE:
            raise RuntimeError("Missing SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE for managed identity mode")
        return ServiceBusClient(
            fully_qualified_namespace=SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE,
            credential=azure_credential
        )

    if SERVICE_BUS_AUTH_MODE == "connection_string":
        if not SERVICE_BUS_CONNECTION_STRING:
            raise RuntimeError("Missing SERVICE_BUS_CONNECTION_STRING for connection string mode")
        return ServiceBusClient.from_connection_string(SERVICE_BUS_CONNECTION_STRING)

    raise RuntimeError(f"Unsupported SERVICE_BUS_AUTH_MODE: {SERVICE_BUS_AUTH_MODE}")


client = AzureOpenAI(
    api_key=AZURE_OPENAI_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
)

redis_client = Redis.from_url(REDIS_URL, decode_responses=True)

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class SummaryRequestRecord(Base):
    __tablename__ = "summary_requests"
    id = Column(Integer, primary_key=True)
    job_id = Column(String(100), unique=True, index=True, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    input_type = Column(String(20), nullable=False)
    original_text = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    summary_type = Column(String(50), nullable=False, default="short")
    status = Column(String(50), nullable=False, default="queued")
    file_name = Column(String(255), nullable=True)
    blob_name = Column(String(500), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


def build_prompt(text: str, summary_type: str):
    prompt_map = {
        "short": "Summarize the following text in one concise paragraph.",
        "detailed": "Provide a detailed summary of the following text.",
        "bullet": "Summarize the following text into clear bullet points."
    }
    instruction = prompt_map.get(summary_type, prompt_map["short"])
    return [
        {"role": "system", "content": "You are a helpful document summarizer."},
        {"role": "user", "content": f"{instruction}\n\nText:\n{text}"}
    ]


def summarize_with_openai(text: str, summary_type: str) -> str:
    response = client.chat.completions.create(
        model=AZURE_DEPLOYMENT_NAME,
        messages=build_prompt(text, summary_type),
        temperature=0.2,
    )
    return response.choices[0].message.content


def extract_text_from_file(filename: str, content: bytes) -> str:
    lower = filename.lower()

    if lower.endswith(".txt"):
        return content.decode("utf-8", errors="ignore")

    if lower.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages)

    if lower.endswith(".docx"):
        doc = Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs)

    raise ValueError("Unsupported file type")


async def process_message(payload: dict):
    job_id = payload["job_id"]
    blob_name = payload["blob_name"]
    file_name = payload["file_name"]
    summary_type = payload["summary_type"]

    await redis_client.set(f"job_status:{job_id}", "processing", ex=7200)

    async with SessionLocal() as db:
        result = await db.execute(
            select(SummaryRequestRecord).where(SummaryRequestRecord.job_id == job_id)
        )
        record = result.scalar_one_or_none()

        if not record:
            await redis_client.set(f"job_status:{job_id}", "failed", ex=7200)
            await redis_client.set(f"job_error:{job_id}", "Job record not found", ex=7200)
            return

        try:
            record.status = "processing"
            await db.commit()

            blob_service_client = get_blob_service_client()
            blob_client = blob_service_client.get_blob_client(
                container=BLOB_CONTAINER_NAME,
                blob=blob_name
            )
            blob_bytes = blob_client.download_blob().readall()

            text = extract_text_from_file(file_name, blob_bytes).strip()
            if not text:
                raise ValueError("No text could be extracted from file")

            if len(text) > MAX_TEXT_LENGTH:
                text = text[:MAX_TEXT_LENGTH]

            summary_text = summarize_with_openai(text, summary_type)

            record.original_text = text
            record.summary = summary_text
            record.status = "completed"
            record.error = None
            await db.commit()

            await redis_client.set(f"job_status:{job_id}", "completed", ex=7200)
            await redis_client.set(f"job_summary:{job_id}", summary_text, ex=7200)

        except Exception as e:
            record.status = "failed"
            record.error = str(e)
            await db.commit()

            await redis_client.set(f"job_status:{job_id}", "failed", ex=7200)
            await redis_client.set(f"job_error:{job_id}", str(e), ex=7200)


async def worker_loop():
    servicebus_client = get_servicebus_client()

    async with servicebus_client:
        receiver = servicebus_client.get_queue_receiver(queue_name=SERVICE_BUS_QUEUE_NAME)
        async with receiver:
            while True:
                messages = await receiver.receive_messages(max_message_count=1, max_wait_time=5)
                if not messages:
                    await asyncio.sleep(2)
                    continue

                for message in messages:
                    try:
                        payload = json.loads(str(message))
                        await process_message(payload)
                        await receiver.complete_message(message)
                    except Exception:
                        await receiver.abandon_message(message)


if __name__ == "__main__":
    asyncio.run(worker_loop())