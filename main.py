from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api import routes
from api.database import db  

app = FastAPI(
    title="University Analytics API",
    description="Backend engine for Student Marks Analysis Dashboard",
    version="1.0.0"
)

# --- WAKE UP THE DATABASE ---
@app.on_event("startup")
async def startup():
    await db.connect()

@app.on_event("shutdown")
async def shutdown():
    await db.disconnect()

# --- CORS SECURITY CONFIGURATION ---
# backend/main.py (Find this section and update allow_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # <--- THE FIX: Allows your future Vercel site to connect
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include the routes we built earlier
app.include_router(routes.router, prefix="/api/v1")

@app.get("/")
def read_root():
    return {"message": "System Online"}