import os
import random
import csv
import io
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Ensure .env is loaded for email credentials
load_dotenv()

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, status, Depends, UploadFile, File, Body
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordRequestForm
import asyncpg

from api.database import db
from api.models import UserCreate, UserLogin, Token, MarkEntry, EnrollmentEntry, PasswordUpdate
from api.security import get_password_hash, verify_password, create_access_token, get_current_user
from api.dependencies import require_developer_role, require_teacher_role, require_student_role

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
# REAL EMAIL PASSWORD RECOVERY ENGINE (HTTP API BYPASS)
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
    api_key = os.getenv("EMAIL_API_KEY") or os.getenv("EMAIL_PASSWORD")  # Check both env var names
    
    # Debug logging to identify missing credentials on Render
    print(f"📧 EMAIL DEBUG: sender_email={'SET' if sender_email else 'MISSING'}, api_key={'SET (length={})'.format(len(api_key)) if api_key else 'MISSING'}")
    
    if not sender_email or not api_key:
        print("❌ CREDENTIALS MISSING! Set EMAIL_SENDER and EMAIL_API_KEY in Render Environment variables.")
        return False
        
    url = "https://api.brevo.com/v3/smtp/email"
    
    # Formulate the payload for Brevo API
    payload = {
        "sender": {"email": sender_email, "name": "UniAnalytics Security"},
        "to": [{"email": receiver_email}],
        "subject": "UniAnalytics Security: Password Reset Verification Code",
        "htmlContent": f"""
        <html>
          <body style="font-family: 'Segoe UI', Arial, sans-serif; background-color: #f4f4f9; padding: 20px; margin: 0;">
            <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; padding: 30px 20px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); border-top: 5px solid #4F46E5;">
              <h2 style="color: #1e293b; margin-top: 0; font-size: 20px;">Account Recovery</h2>
              <p style="color: #475569; font-size: 15px; line-height: 1.5;">Enter the following password reset code to verify your identity:</p>
              
              <div style="margin: 30px 0; text-align: center;">
                <span style="display: inline-block; font-size: 26px; font-weight: 800; letter-spacing: 4px; color: #4F46E5; background-color: #e0e7ff; padding: 12px 20px; border-radius: 8px;">{code}</span>
              </div>
              
              <p style="color: #475569; font-size: 13px; line-height: 1.5;">This code will securely expire in <strong>10 minutes</strong>. If you did not request this code, you can safely ignore this email.</p>
              <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 25px 0;" />
              <p style="color: #94a3b8; font-size: 11px; text-align: center;">UniAnalytics Security Systems &copy; {datetime.now().year}</p>
            </div>
          </body>
        </html>
        """
    }
    
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json"
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as response:
            response_body = response.read().decode("utf-8")
            print(f"✅ Successfully sent OTP email via Brevo API to {receiver_email}")
            print(f"   Brevo response: {response_body}")
            return True
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else "No response body"
        print(f"❌ BREVO API HTTP ERROR {e.code}: {e.reason}")
        print(f"   Response body: {error_body}")
        print(f"   This usually means the API key is invalid. Get your key from: https://app.brevo.com/settings/keys/api")
        return False
    except Exception as e:
        print(f"❌ CRITICAL EMAIL ERROR: {type(e).__name__}: {e}")
        return False

@router.post("/request-otp")
async def request_otp(req: OTPRequest):
    user = await db.pool.fetchrow("SELECT id FROM users WHERE email = $1", req.email)
    if not user:
        raise HTTPException(status_code=404, detail="Email not found in system.")
    
    # Enforcing the 24-hour exact timeline block
    existing_record = OTP_STORE.get(req.email)
    if existing_record and "requested_at" in existing_record:
        seconds_passed = (datetime.now() - existing_record["requested_at"]).total_seconds()
        if seconds_passed < 86400: # Exactly 24 hours
            raise HTTPException(
                status_code=429, 
                detail="Too many requests , Try again after 24hrs"
            )

    code = str(random.randint(100000, 999999))
    OTP_STORE[req.email] = {
        "code": code,
        "expiry": datetime.now() + timedelta(minutes=10),
        "requested_at": datetime.now()
    }
    
    success = send_real_email(req.email, code)
    if not success:
        del OTP_STORE[req.email]
        raise HTTPException(status_code=500, detail="Mail server configuration error. API dispatch failed.")
        
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

        # Wipe execution logic locks the database transaction to prevent race conditions.
        async with db.pool.acquire() as connection:
            async with connection.transaction():
                # Eradicating the user row inherently destroys the JSON master_data 
                # resolving your persistent data wipe concern
                await connection.execute("DELETE FROM users WHERE email = $1", user_email)

        return {"status": "success", "message": "Account and all associated cloud data permanently wiped."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete account data: {str(e)}")
    
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