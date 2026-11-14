"""FastAPI application creation and router registration."""

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health, phone_numbers, twilio_webhooks


app = FastAPI(title="AI House Agent Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health.router)
app.include_router(phone_numbers.router)
app.include_router(twilio_webhooks.router)
