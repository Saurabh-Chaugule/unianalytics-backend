import os
import random
import csv
import io
import smtplib
import json
import socket
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, status, Depends, UploadFile, File, Body
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordRequestForm
import asyncpg

from api.database import db
from api.models import UserCreate, UserLogin, Token, MarkEntry, EnrollmentEntry, PasswordUpdate
from api.security import get_password_hash, verify_password, create_access_token, get_current_user
from api.dependencies import require_developer_role, require_teacher_role, require_student_role 
import socket

# =========================================================================
# THE ULTIMATE RENDER NETWORK FIX: FORCE IPv4
# Google DNS returns IPv6 addresses for smtp.gmail.com, but Render's free 
# tier Linux containers DO NOT support IPv6 outbound traffic. This causes
# the instant "[Errno 101] Network is unreachable" crash. 
# This code intercepts Python's networking and forces it to strictly use IPv4.
# =========================================================================
old_getaddrinfo = socket.getaddrinfo
def new_getaddrinfo(*args, **kwargs):
    responses = old_getaddrinfo(*args, **kwargs)
    return [res for res in responses if res[0] == socket.AF_INET]
socket.getaddrinfo = new_getaddrinfo
# =========================================================================

router = APIRouter()

# ---------------------------------------------------------
# AUTHENTICATION & ONBOARDING
# ---------------------------------------------------------

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_user(user: UserCreate):
    safe_email = str(user.email)
    
    existing_user = await db.pool.fetchrow("SELECT id FROM users WHERE LOWER(username) = LOWER($1)", user.username)
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already taken. Please choose another.")

    hashed_pwd = get_password_hash(user.password)

    insert_query = """
        INSERT INTO users (email, password_hash, role, username, dob)
        VALUES ($1, $2, 'teacher', $3, $4)
        RETURNING id;
    """
    try:
        new_user = await db.pool.fetchrow(
            insert_query, 
            safe_email, 
            hashed_pwd, 
            user.username, 
            user.dob
        )
        return {"message": "User registered successfully.", "user_id": str(new_user["id"]), "role": "teacher"}
    except asyncpg.exceptions.UniqueViolationError:
        raise HTTPException(status_code=400, detail="Email already registered.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database insert error: {str(e)}")

@router.post("/login")
async def login_user(form_data: OAuth2PasswordRequestForm = Depends()):
    try:
        user_record = await db.pool.fetchrow(
            "SELECT id, email, username, password_hash, role, dob FROM users WHERE email = $1", 
            str(form_data.username) 
        )
        
        if not user_record or not verify_password(form_data.password, user_record['password_hash']):
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        token_data = {"sub": user_record['email'], "role": user_record['role']}
        access_token = create_access_token(data=token_data)

        return {
            "access_token": access_token, 
            "token_type": "bearer", 
            "role": user_record['role'],
            "name": user_record['username'],
            "dob": str(user_record['dob']) if user_record['dob'] else "Not Provided"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Login error: {str(e)}")

# ---------------------------------------------------------
# SYSTEM ANALYTICS
# ---------------------------------------------------------

@router.get("/developer/analytics")
async def get_system_analytics(current_user: dict = Depends(require_developer_role)):
    try:
        user_counts = await db.pool.fetch("""
            SELECT role, COUNT(id) as total 
            FROM users 
            GROUP BY role;
        """)
        
        db_size = await db.pool.fetchrow("SELECT pg_size_pretty(pg_database_size(current_database()));")
        
        return {
            "status": "success",
            "developer_id": current_user.get("sub"),
            "database_size": db_size['pg_size_pretty'],
            "system_users": [dict(u) for u in user_counts]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analytics error: {str(e)}")
    
# ---------------------------------------------------------
# ACADEMIC ENDPOINTS
# ---------------------------------------------------------

@router.post("/teacher/marks", status_code=status.HTTP_201_CREATED)
async def submit_marks(entry: MarkEntry, current_user: dict = Depends(require_teacher_role)):
    insert_query = """
        INSERT INTO grades (enrollment_id, assessment_name, score, max_score)
        VALUES ($1, $2, $3, $4)
    """
    try:
        await db.pool.execute(
            insert_query, 
            entry.enrollment_id, 
            entry.exam_type, 
            entry.marks_obtained, 
            entry.max_marks
        )
        return {"message": "Marks successfully recorded."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.get("/student/report-card")
async def get_student_report(current_user: dict = Depends(require_student_role)):
    student_id = current_user.get("sub")
    
    report_query = """
        SELECT 
            s.name AS subject,
            g.assessment_name AS exam_type,
            g.score AS marks_obtained,
            g.max_score,
            RANK() OVER(PARTITION BY g.assessment_name ORDER BY g.score DESC) as class_rank
        FROM grades g
        JOIN enrollments e ON g.enrollment_id = e.id
        JOIN classes c ON e.class_id = c.id
        JOIN subjects s ON c.subject_id = s.id
        WHERE e.student_id = $1::uuid;
    """
    try:
        results = await db.pool.fetch(report_query, student_id)
        return {"student_id": student_id, "report": [dict(r) for r in results]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
@router.get("/students")
async def get_all_students():
    try:
        query = """
            SELECT id, first_name, last_name, contact_email as email, enrollment_number, 
                   obtained_marks, max_marks, sgpa
            FROM students 
            ORDER BY last_name ASC;
        """
        records = await db.pool.fetch(query)
        
        students = []
        for r in records:
            students.append({
                "id": str(r["id"]),
                "name": f"{r['first_name']} {r['last_name']}",
                "email": r["email"] or "No Email",
                "obtained_marks": float(r['obtained_marks']),
                "max_marks": float(r['max_marks']),
                "sgpa": float(r['sgpa']),
                "major": "Computer Science", 
                "status": "Excellent" if float(r['sgpa']) >= 8.0 else ("Passing" if float(r['sgpa']) >= 5.0 else "Pending")
            })
            
        return students
    except Exception as e:
        print(f"Database error: {e}")
        raise HTTPException(status_code=500, detail="Could not fetch students")
    
@router.post("/teacher/students/bulk-upload")
async def bulk_upload_students(
    file: UploadFile = File(...), 
    current_user: dict = Depends(require_teacher_role)
):
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="Only .csv files are allowed.")
    
    try:
        content = await file.read()
        decoded_content = content.decode('utf-8')
        reader = csv.DictReader(io.StringIO(decoded_content))
        
        insert_query = """
            INSERT INTO students (first_name, last_name, contact_email, enrollment_number)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (enrollment_number) DO NOTHING;
        """
        
        inserted_count = 0
        for row in reader:
            await db.pool.execute(
                insert_query,
                row.get('first_name', 'Unknown'),
                row.get('last_name', 'Unknown'),
                row.get('email', ''),
                row.get('enrollment_number', '')
            )
            inserted_count += 1
            
        return {"message": f"Successfully processed {inserted_count} student records.", "count": inserted_count}
        
    except Exception as e:
        print(f"CSV Upload Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process CSV file: {str(e)}")
    
@router.get("/teacher/students/export")
async def export_students_csv(current_user: dict = Depends(require_teacher_role)):
    try:
        query = """
            SELECT first_name, last_name, contact_email, enrollment_number, 
                   obtained_marks, max_marks, sgpa
            FROM students 
            ORDER BY last_name ASC;
        """
        records = await db.pool.fetch(query)

        output = io.StringIO()
        writer = csv.writer(output)
        
        writer.writerow(["First Name", "Last Name", "Email", "ID Number", "Score", "Max Marks", "Calculated SGPA"])
        
        for r in records:
            writer.writerow([
                r['first_name'], 
                r['last_name'], 
                r['contact_email'] or "N/A", 
                r['enrollment_number'] or "N/A", 
                float(r['obtained_marks']), 
                float(r['max_marks']), 
                float(r['sgpa'])
            ])

        output.seek(0)
        
        return Response(
            content=output.getvalue(), 
            media_type="text/csv", 
            headers={"Content-Disposition": "attachment; filename=UniAnalytics_Class_Roster.csv"}
        )
    except Exception as e:
        print(f"Export Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate CSV export.")
    
# ---------------------------------------------------------
# REAL EMAIL PASSWORD RECOVERY ENGINE
# ---------------------------------------------------------

OTP_STORE = {}

class OTPRequest(BaseModel):
    email: str

class OTPVerify(BaseModel):
    email: str
    code: str

class PasswordReset(BaseModel):
    email: str
    code: str
    new_password: str

def send_real_email(receiver_email: str, code: str):
    sender_email = os.getenv("EMAIL_SENDER")
    sender_password = os.getenv("EMAIL_PASSWORD")
    
    # Brevo Relay Settings
    smtp_host = "smtp-relay.brevo.com"
    smtp_port = 587
    
    msg = MIMEMultipart("alternative")
    msg['Subject'] = 'UniAnalytics Security: Password Reset'
    msg['From'] = f"UniAnalytics <{sender_email}>"
    msg['To'] = receiver_email

    html = f"""
    <html>
      <body style="font-family: sans-serif; padding: 20px;">
        <div style="background: #ffffff; padding: 20px; border-radius: 10px; border: 1px solid #ddd;">
          <h2>Account Recovery</h2>
          <p>Your verification code is: <strong>{code}</strong></p>
          <p>Expires in 10 minutes.</p>
        </div>
      </body>
    </html>
    """
    msg.attach(MIMEText(html, "html"))

    try:
        # THE FIX: We use a specific connection setup that forces IPv4 and STARTTLS
        # The 'source_address' forces the use of IPv4 (AF_INET) to bypass the unreachable error.
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)
        print(f"✅ Successfully sent OTP email to {receiver_email}")
        return True
    except Exception as e:
        print(f"❌ SMTP RELAY ERROR: {e}")
        return False

@router.post("/request-otp")
async def request_otp(req: OTPRequest):
    user = await db.pool.fetchrow("SELECT id FROM users WHERE email = $1", req.email)
    if not user:
        raise HTTPException(status_code=404, detail="Email not found in system.")
    
    code = str(random.randint(100000, 999999))
    OTP_STORE[req.email] = {
        "code": code,
        "expiry": datetime.now() + timedelta(minutes=10)
    }
    
    success = send_real_email(req.email, code)
    if not success:
        del OTP_STORE[req.email]
        raise HTTPException(status_code=500, detail="Mail server configuration error. SMTP dispatch failed.")
        
    return {"message": "Secure OTP processing completed."}

@router.post("/verify-otp")
async def verify_otp(req: OTPVerify):
    record = OTP_STORE.get(req.email)
    if not record:
        raise HTTPException(status_code=400, detail="No OTP requested for this email.")
    
    if datetime.now() > record["expiry"]:
        del OTP_STORE[req.email]
        raise HTTPException(status_code=400, detail="OTP has expired. Request a new one.")
        
    if record["code"] != req.code:
        raise HTTPException(status_code=400, detail="Incorrect verification code.")
        
    return {"message": "Identity verified."}

@router.post("/reset-password")
async def reset_password(req: PasswordReset):
    record = OTP_STORE.get(req.email)
    if not record or record["code"] != req.code:
        raise HTTPException(status_code=400, detail="Unauthorized password reset attempt.")
        
    hashed_pwd = get_password_hash(req.new_password)
    
    await db.pool.execute(
        "UPDATE users SET password_hash = $1 WHERE email = $2", 
        hashed_pwd, req.email
    )
    
    del OTP_STORE[req.email]
    
    return {"message": "Password successfully updated."}

@router.delete("/user")
async def delete_user(current_user: dict = Depends(require_teacher_role)):
    try:
        user_email = current_user.get("email") or current_user.get("sub")
        
        if not user_email:
            raise HTTPException(status_code=401, detail="Invalid user token context.")

        await db.pool.execute("DELETE FROM users WHERE email = $1", user_email)
        
        return {"status": "success", "message": "Account permanently wiped from database."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete account: {str(e)}")
    
@router.put("/user/password")
async def update_password(payload: PasswordUpdate, current_user: dict = Depends(require_teacher_role)):
    user_email = current_user.get("email") or current_user.get("sub")
    
    try:
        user_record = await db.pool.fetchrow("SELECT password_hash FROM users WHERE email = $1", user_email)
        if not user_record:
            raise HTTPException(status_code=404, detail="User not found.")
            
        if not verify_password(payload.old_password, user_record['password_hash']):
            raise HTTPException(status_code=400, detail="Incorrect current password.")
            
        hashed_new_pwd = get_password_hash(payload.new_password)
        await db.pool.execute(
            "UPDATE users SET password_hash = $1 WHERE email = $2",
            hashed_new_pwd, user_email
        )
        
        return {"message": "Password updated successfully."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

# ---------------------------------------------------------
# MASTER CLOUD DATA SYNC
# ---------------------------------------------------------

@router.post("/sync-master-data")
async def sync_master_data(
    data: list = Body(...), 
    current_user: dict = Depends(get_current_user)
):
    try:
        json_data = json.dumps(data)
        await db.pool.execute('''
            UPDATE users 
            SET master_data = $1 
            WHERE email = $2
        ''', json_data, current_user['email'])
        
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/get-master-data")
async def get_master_data(current_user: dict = Depends(get_current_user)):
    try:
        row = await db.pool.fetchrow('''
            SELECT master_data FROM users WHERE email = $1
        ''', current_user['email'])
        
        if row and row['master_data']:
            return json.loads(row['master_data'])
        return []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))