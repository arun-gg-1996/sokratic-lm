from fastapi import APIRouter

router = APIRouter()

USERS = [
    {"id": "arun", "display_name": "Arun"},
    {"id": "nidhi", "display_name": "Nidhi"},
]


@router.get("/users")
async def list_users():
    return USERS
