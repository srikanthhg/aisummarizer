import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form
from pydantic import BaseModel
from jose import jwt, JWTError
from passlib.context import CryptContext
from openai import AzureOpenAI

from redis.asyncio import Redis
import redis.asyncio as redis

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, func, select


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

DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?ssl=require"

AZURE_OPENAI_KEY = require_config("AZURE_OPENAI_KEY", read_secret("AZURE_OPENAI_KEY"))
AZURE_OPENAI_API_VERSION = require_config("AZURE_OPENAI_API_VERSION", read_secret("AZURE_OPENAI_API_VERSION"))
AZURE_OPENAI_ENDPOINT = require_config("AZURE_OPENAI_ENDPOINT", read_secret("AZURE_OPENAI_ENDPOINT"))
AZURE_DEPLOYMENT_NAME = require_config("AZURE_DEPLOYMENT_NAME", read_secret("AZURE_DEPLOYMENT_NAME"))

SECRET_KEY = require_config("SECRET_KEY", read_secret("SECRET_KEY"))
ALGORITHM = "HS256"

REDIS_HOST = require_config("REDIS_HOST", read_secret("REDIS_HOST"))
REDIS_PORT = int(read_secret("REDIS_PORT", "6380"))
REDIS_USERNAME = read_secret("REDIS_USERNAME", "default")
REDIS_DB = int(read_secret("REDIS_DB", "0"))
REDIS_PASSWORD = read_secret("REDIS_PASSWORD")

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

app = FastAPI(title="AI Text Summarizer Backend")

client = AzureOpenAI(
    api_key=AZURE_OPENAI_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
)

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()
pwd_context = CryptContext(schemes=["bcrypt_sha256"], deprecated="auto")
redis_client = Redis.from_url(REDIS_URL, decode_responses=True)

azure_credential = DefaultAzureCredential()

# Blob client helper
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

# Service Bus client helper
def get_service_bus_client() -> ServiceBusClient:
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

async def send_to_service_bus(payload: dict):
    sb_client = get_service_bus_client()
    async with sb_client:
        sender = sb_client.get_queue_sender(queue_name=SERVICE_BUS_QUEUE_NAME)
        async with sender:
            await sender.send_messages(ServiceBusMessage(json.dumps(payload)))

ACCESS_TOKEN_EXPIRE_MINUTES = int(read_secret("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
REFRESH_TOKEN_EXPIRE_DAYS = int(read_secret("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(120), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=False)

class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token_hash = Column(String(255), nullable=False, index=True)
    is_revoked = Column(String(10), nullable=False, default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class SummaryRequestRecord(Base):
    __tablename__ = "summary_requests"
    id = Column(Integer, primary_key=True)
    job_id = Column(String(100), unique=True, index=True, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    input_type = Column(String(20), nullable=False)  # text/file
    original_text = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    summary_type = Column(String(50), nullable=False, default="short")
    status = Column(String(50), nullable=False, default="queued")
    file_name = Column(String(255), nullable=True)
    blob_name = Column(String(500), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AuthRequest(BaseModel):
    username: str
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str

class LogoutRequest(BaseModel):
    refresh_token: str

class SummarizeTextRequest(BaseModel):
    text: str
    summary_type: str = "short"


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with SessionLocal() as session:
        yield session


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["type"] = "access"
    payload["exp"] = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    payload = data.copy()
    payload["type"] = "refresh"
    payload["exp"] = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None

def verify_access_token(token: str):
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    return payload

def verify_refresh_token(token: str):
    payload = decode_token(token)
    if not payload or payload.get("type") != "refresh":
        return None
    return payload

async def get_current_user(authorization: str, db: AsyncSession) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")

    token = authorization.split(" ")[1]
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired access token")

    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def build_prompt(text: str, summary_type: str) -> list:
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


def stable_text_hash(text: str, summary_type: str) -> str:
    raw = f"{summary_type}:{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/register")
async def register(req: AuthRequest, db: AsyncSession = Depends(get_db)):
    if not req.username or not req.username.strip():
        raise HTTPException(status_code=400, detail="Username is required")

    if not req.password:
        raise HTTPException(status_code=400, detail="Password is required")

    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        
    result = await db.execute(select(User).where(User.username == req.username))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="User already exists")

    user = User(username=req.username, password=hash_password(req.password))
    db.add(user)
    await db.commit()
    return {"message": "User registered successfully"}


@app.post("/login")
async def login(req: AuthRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    
    if not verify_password(req.password, user.password):
        raise HTTPException(status_code=401, detail="Wrong password")

    access_token = create_access_token({"sub": user.username})
    refresh_token = create_refresh_token({"sub": user.username})

    session = RefreshSession(
        user_id=user.id,
        token_hash=hash_token(refresh_token),
        is_revoked="false"
    )
    db.add(session)
    await db.commit()

    return {
        "access_token": access_token, 
        "refresh_token": refresh_token, 
        "token_type": "bearer"
    }

@app.post("/refresh")
async def refresh_access_token(req: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = verify_refresh_token(req.refresh_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    old_token_hash = hash_token(req.refresh_token)

    result = await db.execute(
        select(RefreshSession).where(
            RefreshSession.token_hash == old_token_hash,
            RefreshSession.is_revoked == "false"
        )
    )
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=401, detail="Refresh token revoked or not found")

    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid refresh token payload")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    session.is_revoked = "true"

    new_access_token = create_access_token({"sub": user.username})
    new_refresh_token = create_refresh_token({"sub": user.username})

    new_session = RefreshSession(
        user_id=user.id,
        token_hash=hash_token(new_refresh_token),
        is_revoked="false"
    )
    db.add(new_session)
    await db.commit()

    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer"
    }

@app.post("/logout")
async def logout(req: LogoutRequest, db: AsyncSession = Depends(get_db)):
    token_hash = hash_token(req.refresh_token)

    result = await db.execute(
        select(RefreshSession).where(RefreshSession.token_hash == token_hash)
    )
    session = result.scalar_one_or_none()

    if session:
        session.is_revoked = "true"
        await db.commit()

    return {"message": "Logged out successfully"}

@app.post("/summarize/text")
async def summarize_text(
    req: SummarizeTextRequest,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(None)
):
    user = await get_current_user(authorization, db)

    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")
    if len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Text too long. Max {MAX_TEXT_LENGTH} characters")

    cache_key = f"summary:{stable_text_hash(text, req.summary_type)}"
    cached_summary = await redis_client.get(cache_key)

    if cached_summary:
        record = SummaryRequestRecord(
            user_id=user.id,
            input_type="text",
            original_text=text,
            summary=cached_summary,
            summary_type=req.summary_type,
            status="completed"
        )
        db.add(record)
        await db.commit()
        return {"summary": cached_summary, "cached": True}

    try:
        summary_text = summarize_with_openai(text, req.summary_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Azure OpenAI error: {str(e)}")

    await redis_client.set(cache_key, summary_text, ex=3600)

    record = SummaryRequestRecord(
        user_id=user.id,
        input_type="text",
        original_text=text,
        summary=summary_text,
        summary_type=req.summary_type,
        status="completed"
    )
    db.add(record)
    await db.commit()

    return {"summary": summary_text, "cached": False}


@app.post("/summarize/file")
async def summarize_file(
    file: UploadFile = File(...),
    summary_type: str = Form("short"),
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(None),
):
    user = await get_current_user(authorization, db)

    allowed_exts = (".txt", ".pdf", ".docx")
    if not file.filename.lower().endswith(allowed_exts):
        raise HTTPException(status_code=400, detail="Only .txt, .pdf, and .docx files are supported")

    job_id = str(uuid.uuid4())
    blob_name = f"{user.username}/{job_id}/{file.filename}"
    file_bytes = await file.read()

    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        blob_service_client = get_blob_service_client()
        container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)
        try:
            container_client.create_container()
        except Exception:
            pass

        blob_client = container_client.get_blob_client(blob_name)
        blob_client.upload_blob(file_bytes, overwrite=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Blob upload failed: {str(e)}")

    record = SummaryRequestRecord(
        job_id=job_id,
        user_id=user.id,
        input_type="file",
        summary_type=summary_type,
        status="queued",
        file_name=file.filename,
        blob_name=blob_name,
    )
    db.add(record)
    await db.commit()

    await redis_client.set(f"job_status:{job_id}", "queued", ex=7200)

    payload = {
        "job_id": job_id,
        "blob_name": blob_name,
        "file_name": file.filename,
        "summary_type": summary_type,
        "user_id": user.id,
        "queued_at": datetime.now(timezone.utc).isoformat()
    }

    try:
        await send_to_service_bus(payload)
    except Exception as e:
        record.status = "failed"
        record.error = f"Service Bus enqueue failed: {str(e)}"
        await db.commit()
        await redis_client.set(f"job_status:{job_id}", "failed", ex=7200)
        raise HTTPException(status_code=500, detail=f"Service Bus enqueue failed: {str(e)}")

    return {"message": "File uploaded and job queued", "job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(None)
):
    await get_current_user(authorization, db)

    redis_status = await redis_client.get(f"job_status:{job_id}")
    redis_summary = await redis_client.get(f"job_summary:{job_id}")
    redis_error = await redis_client.get(f"job_error:{job_id}")

    result = await db.execute(select(SummaryRequestRecord).where(SummaryRequestRecord.job_id == job_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id": job_id,
        "status": redis_status or record.status,
        "summary": redis_summary or record.summary,
        "error": redis_error or record.error,
        "file_name": record.file_name,
        "summary_type": record.summary_type,
    }


@app.get("/history/{username}")
async def history(
    username: str,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(None)
):
    user = await get_current_user(authorization, db)
    if user.username != username:
        raise HTTPException(status_code=403, detail="Unauthorized")

    result = await db.execute(
        select(SummaryRequestRecord)
        .join(User, SummaryRequestRecord.user_id == User.id)
        .where(User.username == username)
        .order_by(SummaryRequestRecord.id.desc())
    )
    rows = result.scalars().all()

    return {
        "history": [
            {
                "job_id": r.job_id,
                "input_type": r.input_type,
                "summary_type": r.summary_type,
                "status": r.status,
                "file_name": r.file_name,
                "summary": r.summary,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }