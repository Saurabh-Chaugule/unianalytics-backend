import os
import asyncpg
from dotenv import load_dotenv

# Load secrets from .env file
load_dotenv()

# THE FIX: Force FastAPI to use the EXACT SAME database URL that init_db.py used to build the tables.
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:Saukshi%403567@localhost:5432/postgres")

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Establish a connection pool to PostgreSQL."""
        try:
            print(f"Connecting API to: {DATABASE_URL}") # This will print in terminal so you know it matches!
            self.pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,   # Minimum number of connections open
                max_size=10   # Maximum number of concurrent connections
            )
            print("🟢 Database Connection Pool Established")
        except Exception as e:
            print(f"🔴 Database Connection Failed: {e}")

    async def disconnect(self):
        """Close the connection pool gracefully."""
        if self.pool:
            await self.pool.close()
            print("🔴 Database Connection Pool Closed")

    async def execute_query(self, query: str, *args):
        """Helper to execute INSERT/UPDATE/DELETE queries."""
        async with self.pool.acquire() as connection:
            return await connection.execute(query, *args)

    async def fetch_query(self, query: str, *args):
        """Helper to execute SELECT queries and return data."""
        async with self.pool.acquire() as connection:
            records = await connection.fetch(query, *args)
            return [dict(record) for record in records]

# Create a global instance to be used across the app
db = Database()