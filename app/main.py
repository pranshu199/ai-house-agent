"""FastAPI application creation and router registration."""

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI

from app.api import health, phone_numbers, twilio_webhooks


app = FastAPI(title="AI House Agent Backend")
app.include_router(health.router)
app.include_router(phone_numbers.router)
app.include_router(twilio_webhooks.router)
