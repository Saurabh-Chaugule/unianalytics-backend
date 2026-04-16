import os
import random
import csv
import io
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, status, Depends, UploadFile, File
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordRequestForm
import asyncpg

from api.database import db
from api.models import UserCreate, UserLogin, Token, MarkEntry, EnrollmentEntry, PasswordUpdate
from api.security import get_password_hash, verify_password, create_access_token
from api.dependencies import require_developer_role, require_teacher_role, require_student_role
from fastapi import APIRouter, Depends, HTTPException, Body
from typing import List, Dict, Any
from . import database, security
from fastapi.security import OAuth2PasswordRequestForm

router = APIRouter()

# ---------------------------------------------------------
# AUTHENTICATION & ONBOARDING
# ---------------------------------------------------------

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_user(user: UserCreate):
    safe_email = str(user.email)
    
    # 1. Check if Username is already taken
    existing_user = await db.pool.fetchrow("SELECT id FROM users WHERE username = $1", user.username)
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already taken. Please choose another.")

    hashed_pwd = get_password_hash(user.password)

    # 2. Insert into database using the new username column
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
        # Fetch the user by email, grabbing ALL necessary columns
        user_record = await db.pool.fetchrow(
            "SELECT id, email, username, password_hash, role, dob FROM users WHERE email = $1", 
            str(form_data.username)  # OAuth2 maps the email field to 'username'
        )
        
        # Verify user exists and password is correct
        if not user_record or not security.verify_password(form_data.password, user_record['password_hash']):
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        # CRITICAL: Create the token using the user's EMAIL as the 'sub'
        # This matches exactly what get_current_user in security.py expects for Cloud Syncing!
        token_data = {"sub": user_record['email'], "role": user_record['role']}
        access_token = security.create_access_token(data=token_data)

        # Return the token AND the user's profile details back to the React frontend
        return {
            "access_token": access_token, 
            "token_type": "bearer", 
            "role": user_record['role'],
            "name": user_record['username'], # Sends 'username' to React as 'name'
            "dob": str(user_record['dob']) if user_record['dob'] else "Not Provided"
        }
        
    except HTTPException:
        raise  # Pass standard HTTP exceptions through cleanly
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

# Temporary in-memory store for OTPs
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
    
    if not sender_email or not sender_password:
        print("❌ EMAIL CREDENTIALS MISSING IN .ENV FILE")
        return False
        
    msg = MIMEMultipart("alternative")
    msg['Subject'] = 'UniAnalytics Security: Password Reset Verification Code'
    msg['From'] = f"UniAnalytics Security <{sender_email}>"
    msg['To'] = receiver_email

    # THE FIX: Adjusted font-size (24px), padding, and added 'display: inline-block' for perfect mobile rendering
    html = f"""
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
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender_email, sender_password)
            server.send_message(msg)
        print(f"✅ Successfully sent OTP email to {receiver_email}")
        return True
    except Exception as e:
        print(f"❌ CRITICAL EMAIL ERROR: {e}")
        return False

@router.post("/request-otp")
async def request_otp(req: OTPRequest):
    # 1. Verify the email actually exists in the database
    user = await db.pool.fetchrow("SELECT id FROM users WHERE email = $1", req.email)
    if not user:
        raise HTTPException(status_code=404, detail="Email not found in system.")
    
    # 2. Generate a secure 6-digit code
    code = str(random.randint(100000, 999999))
    OTP_STORE[req.email] = {
        "code": code,
        "expiry": datetime.now() + timedelta(minutes=10)
    }
    
    # 3. Fire the email
    success = send_real_email(req.email, code)
    if not success:
        # Fallback terminal print in case your .env isn't loaded properly
        print(f"--- FALLBACK: OTP FOR {req.email} IS: {code} ---")
        
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
        
    # Hash the new password securely
    hashed_pwd = get_password_hash(req.new_password)
    
    # Update the database
    await db.pool.execute(
        "UPDATE users SET password_hash = $1 WHERE email = $2", 
        hashed_pwd, req.email
    )
    
    # Destroy the OTP so it can't be used again
    del OTP_STORE[req.email]
    
    return {"message": "Password successfully updated."}

@router.delete("/user")
async def delete_user(current_user: dict = Depends(require_teacher_role)):
    try:
        user_id = current_user.get("sub")
        # 1. Physically delete the user from the PostgreSQL database using their unique ID
        await db.pool.execute("DELETE FROM users WHERE id = $1::uuid", user_id)
        return {"message": "Account permanently wiped from database."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete account: {str(e)}")
    
@router.put("/user/password")
async def update_password(payload: PasswordUpdate, current_user: dict = Depends(require_teacher_role)):
    user_id = current_user.get("sub")
    
    try:
        # 1. Fetch the user's current hashed password from the database
        user_record = await db.pool.fetchrow("SELECT password_hash FROM users WHERE id = $1::uuid", user_id)
        if not user_record:
            raise HTTPException(status_code=404, detail="User not found.")
            
        # 2. Verify the old password matches what they typed
        if not verify_password(payload.old_password, user_record['password_hash']):
            raise HTTPException(status_code=400, detail="Incorrect current password.")
            
        # 3. Hash the new password and update the database
        hashed_new_pwd = get_password_hash(payload.new_password)
        await db.pool.execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2::uuid",
            hashed_new_pwd, user_id
        )
        
        return {"message": "Password updated successfully."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.post("/sync-master-data")
async def sync_master_data(
    data: list = Body(...), 
    current_user: dict = Depends(security.get_current_user)
):
    """Saves massive JSON to Neon DB securely"""
    conn = await database.get_db_connection()
    try:
        import json
        json_data = json.dumps(data)
        
        # Updated to search by email instead of ID
        await conn.execute('''
            UPDATE users 
            SET master_data = $1 
            WHERE email = $2
        ''', json_data, current_user['email'])
        
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()

@router.get("/get-master-data")
async def get_master_data(current_user: dict = Depends(security.get_current_user)):
    """Pulls data from Neon DB on login"""
    conn = await database.get_db_connection()
    try:
        # Updated to search by email instead of ID
        row = await conn.fetchrow('''
            SELECT master_data FROM users WHERE email = $1
        ''', current_user['email'])
        
        if row and row['master_data']:
            import json
            return json.loads(row['master_data'])
        return []
    finally:
        await conn.close()