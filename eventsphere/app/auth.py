import hashlib
import hmac
import secrets
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from .db import get_db
from .models import User


class NeedLogin(Exception):
    pass


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex(), h)


def get_user(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("uid")
    return db.get(User, uid) if uid else None


def require(*roles):
    def dep(user=Depends(get_user)):
        if not user:
            raise NeedLogin()
        if roles and user.role not in roles:
            raise HTTPException(403, "You do not have permission to view this page")
        return user
    return dep


def csrf_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_hex(16)
    return request.session["csrf"]


async def verify_csrf(request: Request):
    if request.method != "POST" or request.url.path.startswith("/agents/"):
        return
    tok = request.headers.get("x-csrf-token")
    if not tok:
        tok = (await request.form()).get("csrf_token")
    if not tok or not hmac.compare_digest(str(tok), request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF token missing or invalid - reload the page and try again")
