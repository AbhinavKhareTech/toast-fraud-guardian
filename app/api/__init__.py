from fastapi import APIRouter

from app.api.admin import router as admin_router
from app.api.disputes import router as disputes_router
from app.api.scoring import router as scoring_router

api_router = APIRouter()
api_router.include_router(scoring_router)
api_router.include_router(disputes_router)
api_router.include_router(admin_router)

__all__ = ["api_router"]
