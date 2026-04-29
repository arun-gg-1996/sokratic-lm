from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import chat, mastery, memory, session, users

app = FastAPI(title="Sokratic Backend", version="0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router, prefix="/api")
app.include_router(session.router, prefix="/api")
app.include_router(memory.router, prefix="/api")
app.include_router(mastery.router, prefix="/api")
app.include_router(chat.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
