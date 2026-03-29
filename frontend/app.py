from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os

app = FastAPI(title="AI Summarizer Frontend")

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")

# Mount static files if needed
# app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main HTML file"""
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Frontend not deployed correctly</h1><p>index.html not found</p>",
            status_code=500
        )

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(path: str, request: Request):
    """Proxy requests to the backend API"""
    
    # Skip proxying for the root path (handled by index())
    if not path:
        return await index()
    
    # Handle OPTIONS for CORS preflight
    if request.method == "OPTIONS":
        return JSONResponse(content={}, status_code=200)
    
    try:
        # Build target URL
        url = f"{BACKEND_URL}/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        
        # Prepare headers (exclude hop-by-hop headers)
        headers = {
            key: value for key, value in request.headers.items()
            if key.lower() not in ["host", "connection", "content-length"]
        }
        
        # Get request body
        body = await request.body() if request.method not in ["GET", "HEAD"] else None
        
        # Make request to backend
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers
            )
        
        # Handle JSON responses
        content_type = resp.headers.get("content-type", "")
        
        if "application/json" in content_type:
            try:
                return JSONResponse(
                    content=resp.json(),
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items() if k.lower() in ['cache-control', 'etag']}
                )
            except Exception as e:
                return JSONResponse(
                    status_code=502,
                    content={"detail": f"Invalid JSON from backend: {str(e)}"}
                )
        
        # Handle HTML/text responses (for debugging)
        if "text/html" in content_type or "text/plain" in content_type:
            return HTMLResponse(content=resp.text, status_code=resp.status_code)
        
        # Default: return as JSON with text content
        return JSONResponse(
            status_code=resp.status_code,
            content={"detail": resp.text or "Request completed"}
        )
        
    except httpx.ConnectError:
        return JSONResponse(
            status_code=503,
            content={"detail": f"Backend service unavailable at {BACKEND_URL}"}
        )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504,
            content={"detail": "Backend request timed out"}
        )
    except httpx.RequestError as e:
        return JSONResponse(
            status_code=502,
            content={"detail": f"Backend request failed: {str(e)}"}
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Proxy error: {str(e)}"}
        )

# Health check endpoint
@app.get("/health")
async def health():
    """Health check for the frontend service"""
    return {
        "status": "ok",
        "service": "frontend",
        "backend_url": BACKEND_URL
    }