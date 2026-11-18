"""FastAPI application creation and router registration."""

import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health, notifications, phone_numbers, property_images, twilio_webhooks
from app.services.notifications import run_notification_worker


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop_event = asyncio.Event()
    worker = asyncio.create_task(run_notification_worker(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        await worker


app = FastAPI(title="AI House Agent Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080","https://preview--simple-supabase-studio.lovable.app","https://brixprint.com",
    "https://www.brixprint.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health.router)
app.include_router(notifications.router)
app.include_router(phone_numbers.router)
app.include_router(property_images.router)
app.include_router(twilio_webhooks.router)
