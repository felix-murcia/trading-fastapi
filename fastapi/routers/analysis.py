from fastapi import APIRouter, Depends, HTTPException
from models.analysis import PairsAnalysisRequest, PairsAnalysisResponse
from services import pair_analyzer
from .deps import verify_token

router = APIRouter(prefix="/v1/analysis", tags=["analysis"])


@router.post("/pairs", response_model=PairsAnalysisResponse)
async def analyze_pairs(
    req: PairsAnalysisRequest,
    _: None = Depends(verify_token),
) -> PairsAnalysisResponse:
    try:
        return await pair_analyzer.analyze(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
