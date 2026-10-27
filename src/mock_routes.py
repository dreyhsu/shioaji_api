"""Dashboard endpoints for the explicitly enabled offline replay only."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StrictBool

router = APIRouter(prefix="/api/mock")


def runtime_for(request):
    runtime = getattr(request.app.state, "mock_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=404, detail="Mock API 未啟用，請使用 --mock 啟動")
    return runtime


@router.get("")
def status(request: Request):
    runtime = getattr(request.app.state, "mock_runtime", None)
    return {"enabled": False} if runtime is None else runtime.snapshot()


class ReplayControl(BaseModel):
    action: Literal["play", "pause", "step", "reset"]
    steps: int = Field(default=1, strict=True, ge=1, le=600)
    confirmed: StrictBool = False


@router.post("/control")
def control(body: ReplayControl, request: Request):
    runtime = runtime_for(request)
    with runtime.lock:
        if body.action == "reset":
            if not body.confirmed:
                raise HTTPException(status_code=422, detail="重設會清除本次 Mock 委託與曲線，請先確認")
            return runtime.reset()
        if body.action == "step":
            runtime.playing = False
            return runtime.advance(body.steps)
        runtime.playing = body.action == "play" and runtime.cursor < len(runtime.bars)
        return runtime.snapshot()


class CurveEntryIn(BaseModel):
    action: Literal["Buy", "Sell"] = "Buy"
    curve_id: int = Field(gt=0)
    contract_code: str = Field(min_length=1)
    quantity: int = Field(default=1, strict=True, ge=1, le=100)
    confirmed: StrictBool = False


@router.post("/curve-entry")
def arm_curve(body: CurveEntryIn, request: Request):
    runtime = runtime_for(request)
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="啟用曲線自動進場前必須確認")
    try:
        return runtime.arm_entry(body.curve_id, body.contract_code, body.quantity, action=body.action)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.delete("/curve-entry")
def cancel_curve(request: Request):
    return runtime_for(request).cancel_entry()
