"""Aggregates all /api/v1 routers into a single APIRouter mounted by app.main."""

from fastapi import APIRouter

from app.api.v1.generate import router as generate_router

router = APIRouter(prefix="/api/v1")
router.include_router(generate_router)
