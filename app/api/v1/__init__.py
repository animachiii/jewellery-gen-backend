"""Aggregates all /api/v1 routers into a single APIRouter mounted by app.main."""

from fastapi import APIRouter

from app.api.v1.generate import router as generate_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.matrix import router as matrix_router

router = APIRouter(prefix="/api/v1")
router.include_router(generate_router)
router.include_router(jobs_router)
router.include_router(matrix_router)
