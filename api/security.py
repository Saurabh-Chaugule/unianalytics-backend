import os
from datetime import datetime, timedelta, timezone
import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

# Retrieve the secret key from the .env file
SECRET_KEY = os.getenv("SECRET_KEY", "fallback_secret_do_not_use_in_prod")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # Token valid for 24 hours

# Tells FastAPI where to look for the token in the headers
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/login")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check if the provided password matches the stored bcrypt hash."""
    # bcrypt requires bytes, so we encode the strings to utf-8 first
    password_bytes = plain_password.encode('utf-8')
    hash_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hash_bytes)

def get_password_hash(password: str) -> str:
    """Securely hash a plain text password using raw bcrypt."""
    password_bytes = password.encode('utf-8')
    # Generate a salt and hash the password
    salt = bcrypt.gensalt()
    hashed_bytes = bcrypt.hashpw(password_bytes, salt)
    # Decode back to a string so it can be stored in PostgreSQL
    return hashed_bytes.decode('utf-8')

def create_access_token(data: dict):
    """Generate a JWT containing user identity and role."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    # Cryptographically sign the token
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str):
    """Verify and decode a JWT. Returns the payload or None if invalid."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None

# --- NEW: Route Protection Dependency ---
async def get_current_user(token: str = Depends(oauth2_scheme)):
    """
    Validates the token from the incoming request.
    If valid, returns a dictionary containing the user's email.
    If invalid or expired, throws an HTTP 401 error.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Decode the token using PyJWT
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        
        # 'sub' (subject) usually contains the user identifier (email in our case)
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
            
        return {"email": email}
        
    except jwt.PyJWTError:
        raise credentials_exception