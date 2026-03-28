from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
import httpx
import os

app = FastAPI()

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend")

@app.get("/", response_class=HTMLResponse)
def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)

    url = f"{BACKEND_URL}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers
            )
    except httpx.RequestError as e:
        return JSONResponse(
            status_code=502,
            content={"detail": f"Backend unreachable: {str(e)}"}
        )

    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except Exception:
            return JSONResponse(
                status_code=502,
                content={"detail": "Backend returned invalid JSON"}
            )

    return JSONResponse(
        status_code=resp.status_code,
        content={"detail": resp.text or "Request failed"}
    )