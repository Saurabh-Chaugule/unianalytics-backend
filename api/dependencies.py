from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from api.security import decode_access_token

security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Extracts and validates the JWT from the request header."""
    token = credentials.credentials
    payload = decode_access_token(token)
    
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload

def require_developer_role(current_user: dict = Depends(get_current_user)):
    """Ensures the user has the 'developer' role."""
    if current_user.get("role") != "developer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Developer privileges required."
        )
    return current_user

def require_teacher_role(current_user: dict = Depends(get_current_user)):
    """Ensures the user has the 'teacher' role."""
    if current_user.get("role") != "teacher":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Teacher privileges required."
        )
    return current_user

def require_student_role(current_user: dict = Depends(get_current_user)):
    """Ensures the user has the 'student' role."""
    if current_user.get("role") != "student":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Student privileges required."
        )
    return current_user