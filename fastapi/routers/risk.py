from fastapi import APIRouter, Depends, HTTPException
from models.risk import RiskEvaluateRequest, RiskEvaluateResponse
from services import risk_evaluator, mt5_client
from .deps import verify_token

router = APIRouter(prefix="/v1/risk", tags=["risk"])


@router.post("/evaluate", response_model=RiskEvaluateResponse)
async def evaluate_risk(
    req: RiskEvaluateRequest,
    _: None = Depends(verify_token),
) -> RiskEvaluateResponse:
    try:
        account = await mt5_client.get_account()
        equity = float(account.get("equity", 0))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"mt5_socket_error: {e}")

    if equity <= 0:
        raise HTTPException(status_code=422, detail="equity_zero_or_negative")

    return await risk_evaluator.evaluate(req, equity)
