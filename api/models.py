from pydantic import BaseModel, EmailStr
from datetime import date
from typing import Optional

class UserCreate(BaseModel):
    username: str  # <-- Replaced first_name, last_name, gender
    email: EmailStr
    password: str
    dob: date

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    role: str
    name: Optional[str] = None  # <-- Added to pass username to frontend
    dob: Optional[date] = None  # <-- Added to pass DOB to frontend

class MarkEntry(BaseModel):
    enrollment_id: str
    exam_type: str
    marks_obtained: float
    max_marks: float = 100.0

class EnrollmentEntry(BaseModel):
    class_id: str

class PasswordUpdate(BaseModel):
    old_password: str
    new_password: str