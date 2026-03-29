import io
import json
import os
import asyncio
import signal
import sys
import logging
from typing import Optional

from openai import AzureOpenAI

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, func, select

from PyPDF2 import PdfReader
from docx import Document

# ============== Logging Setup ==============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ============== Configuration ==============
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

# Database
DB_HOST = require_config("DB_HOST", read_secret("DB_HOST"))
DB_PORT = read_secret("DB_PORT", "5432")
DB_NAME = require_config("DB_NAME", read_secret("DB_NAME"))
DB_USER = require_config("DB_USER", read_secret("DB_USER"))
DB_PASSWORD = require_config("DB_PASSWORD", read_secret("DB_PASSWORD"))
DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?ssl=require"

# Azure OpenAI
AZURE_OPENAI_KEY = require_config("AZURE_OPENAI_KEY", read_secret("AZURE_OPENAI_KEY"))
AZURE_OPENAI_API_VERSION = require_config("AZURE_OPENAI_API_VERSION", read_secret("AZURE_OPENAI_API_VERSION"))
AZURE_OPENAI_ENDPOINT = require_config("AZURE_OPENAI_ENDPOINT", read_secret("AZURE_OPENAI_ENDPOINT"))
AZURE_DEPLOYMENT_NAME = require_config("AZURE_DEPLOYMENT_NAME", read_secret("AZURE_DEPLOYMENT_NAME"))

# Redis - Azure Cache for Redis requires SSL
REDIS_HOST = require_config("REDIS_HOST", read_secret("REDIS_HOST"))
REDIS_PORT = int(read_secret("REDIS_PORT", "6380"))
REDIS_DB = int(read_secret("REDIS_DB", "0"))
REDIS_PASSWORD = require_config("REDIS_PASSWORD", read_secret("REDIS_PASSWORD"))
REDIS_USERNAME = read_secret("REDIS_USERNAME", "default")

if REDIS_PASSWORD:
    if REDIS_USERNAME and REDIS_USERNAME != "default":
        REDIS_URL = f"rediss://{REDIS_USERNAME}:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
    else:
        REDIS_URL = f"rediss://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
else:
    REDIS_URL = f"rediss://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

redis_client = redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
    ssl_cert_reqs="required",
)

# Azure Storage
AZURE_STORAGE_AUTH_MODE = read_secret("AZURE_STORAGE_AUTH_MODE", "connection_string").lower()
AZURE_STORAGE_ACCOUNT_URL = read_secret("AZURE_STORAGE_ACCOUNT_URL")
AZURE_STORAGE_CONNECTION_STRING = read_secret("AZURE_STORAGE_CONNECTION_STRING")
BLOB_CONTAINER_NAME = read_secret("BLOB_CONTAINER_NAME", "uploads")

# Service Bus
SERVICE_BUS_AUTH_MODE = read_secret("SERVICE_BUS_AUTH_MODE", "connection_string").lower()
SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE = read_secret("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
SERVICE_BUS_CONNECTION_STRING = read_secret("SERVICE_BUS_CONNECTION_STRING")
SERVICE_BUS_QUEUE_NAME = read_secret("SERVICE_BUS_QUEUE_NAME", "summary-jobs")

MAX_TEXT_LENGTH = int(read_secret("MAX_TEXT_LENGTH", "25000"))

azure_credential = DefaultAzureCredential()
shutdown_event = asyncio.Event()
ENABLE_SIGNAL_HANDLERS = os.getenv("ENABLE_SIGNAL_HANDLERS", "true").lower() == "true"


# ============== Helper Functions ==============

def get_blob_service_client() -> BlobServiceClient:
    if AZURE_STORAGE_AUTH_MODE == "managed_identity":
        if not AZURE_STORAGE_ACCOUNT_URL:
            raise RuntimeError("Missing AZURE_STORAGE_ACCOUNT_URL for managed identity mode")
        return BlobServiceClient(account_url=AZURE_STORAGE_ACCOUNT_URL, credential=azure_credential)
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


async def safe_redis_set(key: str, value: str, ex: int = None):
    try:
        return await redis_client.set(key, value, ex=ex)
    except (RedisConnectionError, RedisTimeoutError) as e:
        logger.warning(f"Redis SET error for key '{key}': {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected Redis error for key '{key}': {e}")
        return None


async def safe_redis_ping():
    try:
        result = await redis_client.ping()
        logger.info("✓ Redis ping successful")
        return result
    except Exception as e:
        logger.error(f"✗ Redis ping failed: {e}")
        return False


# Azure OpenAI Client
client = AzureOpenAI(
    api_key=AZURE_OPENAI_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
)

# Database
engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


# ============== Database Models ==============
# ✅ CRITICAL: Define User model so SQLAlchemy can resolve FK in SummaryRequestRecord

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(120), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=False)


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


# ============== AI Helpers ==============

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


# ============== Message Processing ==============

async def process_message(payload: dict):
    job_id = payload["job_id"]
    blob_name = payload["blob_name"]
    file_name = payload["file_name"]
    summary_type = payload["summary_type"]

    logger.info(f"🔄 Processing job: {job_id}")
    await safe_redis_set(f"job_status:{job_id}", "processing", ex=7200)

    async with SessionLocal() as db:
        result = await db.execute(
            select(SummaryRequestRecord).where(SummaryRequestRecord.job_id == job_id)
        )
        record = result.scalar_one_or_none()

        if not record:
            logger.error(f"✗ Job {job_id}: Record not found in DB")
            await safe_redis_set(f"job_status:{job_id}", "failed", ex=7200)
            await safe_redis_set(f"job_error:{job_id}", "Job record not found", ex=7200)
            return

        try:
            record.status = "processing"
            await db.commit()

            # Download blob
            blob_service_client = get_blob_service_client()
            blob_client = blob_service_client.get_blob_client(
                container=BLOB_CONTAINER_NAME,
                blob=blob_name
            )
            
            loop = asyncio.get_event_loop()
            blob_bytes = await loop.run_in_executor(
                None,
                lambda: blob_client.download_blob().readall()
            )
            logger.info(f"📄 Downloaded blob: {blob_name} ({len(blob_bytes)} bytes)")

            # Extract text
            text = extract_text_from_file(file_name, blob_bytes).strip()
            if not text:
                raise ValueError("No text could be extracted from file")

            if len(text) > MAX_TEXT_LENGTH:
                logger.warning(f"✂️ Truncating text to {MAX_TEXT_LENGTH} chars")
                text = text[:MAX_TEXT_LENGTH]

            # Summarize
            logger.info(f"🤖 Calling Azure OpenAI for job {job_id}")
            summary_text = summarize_with_openai(text, summary_type)
            logger.info(f"✅ OpenAI response received for job {job_id}")

            # Update record
            record.original_text = text
            record.summary = summary_text
            record.status = "completed"
            record.error = None
            await db.commit()

            await safe_redis_set(f"job_status:{job_id}", "completed", ex=7200)
            await safe_redis_set(f"job_summary:{job_id}", summary_text, ex=7200)
            logger.info(f"✅ Job {job_id} completed successfully")

        except Exception as e:
            logger.error(f"✗ Job {job_id} failed: {type(e).__name__}: {e}", exc_info=True)
            record.status = "failed"
            record.error = str(e)
            await db.commit()
            await safe_redis_set(f"job_status:{job_id}", "failed", ex=7200)
            await safe_redis_set(f"job_error:{job_id}", str(e), ex=7200)
            raise


# ============== Worker Loop ==============

async def worker_loop():
    logger.info("🔄 Starting Service Bus worker...")
    
    redis_ok = await safe_redis_ping()
    if not redis_ok:
        logger.warning("⚠ Redis unavailable - worker will continue but caching disabled")
    
    max_reconnect_attempts = 10
    reconnect_delay = 5
    
    while not shutdown_event.is_set():
        servicebus_client = None
        try:
            logger.info(f"🔌 Connecting to Service Bus queue: {SERVICE_BUS_QUEUE_NAME}")
            servicebus_client = get_servicebus_client()
            
            async with servicebus_client:
                # ✅ No auto_lock_renewer=True (was causing bool/register error)
                receiver = servicebus_client.get_queue_receiver(
                    queue_name=SERVICE_BUS_QUEUE_NAME
                )
                
                async with receiver:
                    logger.info(f"✅ Connected to queue '{SERVICE_BUS_QUEUE_NAME}' - waiting for messages...")
                    
                    while not shutdown_event.is_set():
                        try:
                            messages = await receiver.receive_messages(
                                max_message_count=1,
                                max_wait_time=30
                            )
                            
                            if not messages:
                                logger.debug("⏳ No messages received, continuing to wait...")
                                continue

                            for message in messages:
                                if shutdown_event.is_set():
                                    logger.info("🛑 Shutdown requested, abandoning message")
                                    await receiver.abandon_message(message)
                                    break
                                    
                                try:
                                    payload = json.loads(str(message))
                                    await process_message(payload)
                                    await receiver.complete_message(message)
                                    logger.info(f"✅ Message completed: {payload.get('job_id')}")
                                except json.JSONDecodeError as e:
                                    logger.error(f"✗ Invalid JSON in message: {e}")
                                    await receiver.dead_letter_message(message, error_description="Invalid JSON")
                                except Exception as e:
                                    logger.error(f"⚠ Failed to process message: {e}", exc_info=True)
                                    await receiver.abandon_message(message)
                                    
                        except asyncio.CancelledError:
                            logger.info("🛑 Receive loop cancelled")
                            break
                        except Exception as e:
                            logger.error(f"⚠ Error in receive loop: {e}", exc_info=True)
                            await asyncio.sleep(5)
                            
            logger.warning("🔌 Service Bus connection closed, reconnecting...")
            await asyncio.sleep(reconnect_delay)
            
        except Exception as e:
            logger.error(f"✗ Service Bus connection failed: {e}", exc_info=True)
            max_reconnect_attempts -= 1
            if max_reconnect_attempts <= 0:
                logger.error("✗ Max reconnect attempts reached, exiting worker")
                raise
            logger.info(f"🔄 Reconnecting in {reconnect_delay}s (attempts left: {max_reconnect_attempts})")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)
            
    logger.info("🛑 Worker loop exited gracefully")


# ============== Signal Handlers ==============

def setup_shutdown_handlers():
    if not ENABLE_SIGNAL_HANDLERS:
        logger.info("⚠ Signal handlers disabled (ENABLE_SIGNAL_HANDLERS=false)")
        return
        
    loop = asyncio.get_event_loop()
    
    def shutdown_signal_handler():
        logger.info("🛑 Shutdown signal received, stopping worker...")
        shutdown_event.set()
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_signal_handler)
            logger.info(f"✓ Registered handler for {sig.name}")
        except NotImplementedError:
            logger.warning(f"⚠ Could not register handler for {sig.name} on this platform")


# ============== Main Entry Point ==============

async def main():
    logger.info("🚀 Worker starting up...")
    logger.info(f"  - Redis: {REDIS_HOST}:{REDIS_PORT}")
    logger.info(f"  - Service Bus Queue: {SERVICE_BUS_QUEUE_NAME}")
    logger.info(f"  - Signal Handlers: {'enabled' if ENABLE_SIGNAL_HANDLERS else 'disabled'}")
    
    if ENABLE_SIGNAL_HANDLERS:
        setup_shutdown_handlers()
    
    # Create DB tables if not exists
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✓ Database tables ready")
    except Exception as e:
        logger.error(f"✗ Database initialization failed: {e}", exc_info=True)
    
    try:
        await worker_loop()
    except asyncio.CancelledError:
        logger.info("🛑 Worker cancelled")
    except Exception as e:
        logger.error(f"✗ Worker crashed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("🧹 Cleaning up resources...")
        try:
            await redis_client.close()
        except:
            pass
        try:
            await engine.dispose()
        except:
            pass
        logger.info("👋 Worker shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Interrupted")
        sys.exit(0)