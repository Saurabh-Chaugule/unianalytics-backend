import asyncio
import asyncpg
import os
from dotenv import load_dotenv

# Force it to read your exact .env file so the API and this script match perfectly
load_dotenv()
# Now it will grab the proper URL you just added to .env
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:Saukshi%403567@localhost:5432/uni_analytics_db")

async def rebuild_master_database():
    print(f"Connecting to: {DATABASE_URL}")
    conn = await asyncpg.connect(DATABASE_URL)

    print("Dropping old tables...")
    await conn.execute("""
        DROP TABLE IF EXISTS grades CASCADE;
        DROP TABLE IF EXISTS enrollments CASCADE;
        DROP TABLE IF EXISTS classes CASCADE;
        DROP TABLE IF EXISTS subjects CASCADE;
        DROP TABLE IF EXISTS students CASCADE;
        DROP TABLE IF EXISTS users CASCADE;
        DROP TABLE IF EXISTS universities CASCADE;
    """)

    print("Building Teacher-Centric Schema with Username Auth...")
    
    # THE FIX: Updated users table schema to use 'username'
    await conn.execute("""
        CREATE TABLE users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email VARCHAR(255) UNIQUE NOT NULL,
            username VARCHAR(255) UNIQUE NOT NULL, 
            password_hash VARCHAR(255) NOT NULL,
            role VARCHAR(50) DEFAULT 'teacher', 
            dob DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    await conn.execute("""
        CREATE TABLE students (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            first_name VARCHAR(100) NOT NULL,
            last_name VARCHAR(100) NOT NULL,
            contact_email VARCHAR(255),
            enrollment_number VARCHAR(100) UNIQUE,
            obtained_marks DECIMAL(5,2) DEFAULT 0.00,
            max_marks DECIMAL(5,2) DEFAULT 100.00,
            sgpa DECIMAL(3,2) DEFAULT 0.00,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    print("✅ Master Database and Grading Engine Successfully Built!")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(rebuild_master_database())