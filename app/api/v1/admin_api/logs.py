"""Admin API - Call Logs (custom)."""

from fastapi import APIRouter, Depends, Query

from app.core.auth import verify_app_key
from app.services.call_log import call_log_service

router = APIRouter(dependencies=[Depends(verify_app_key)])


@router.get("/logs")
async def get_call_logs_api(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    model: str = Query(""),
    success: str = Query(""),
):
    """获取调用日志。"""
    await call_log_service.start()
    success_flag = None
    if success.lower() in {"true", "1", "yes"}:
        success_flag = True
    elif success.lower() in {"false", "0", "no"}:
        success_flag = False
    logs, total = await call_log_service.query(
        page=page, page_size=page_size, model=model, success=success_flag
    )
    return {"logs": logs, "total": total}


@router.delete("/logs")
async def clear_call_logs_api(max_count: int = Query(0, ge=0)):
    """清理调用日志：max_count=0 表示清空，否则保留最后 max_count 条。"""
    await call_log_service.start()
    if max_count <= 0:
        deleted = await call_log_service.clear(None)
    else:
        deleted = await call_log_service.clear(max_count)
    return {"status": "success", "deleted": deleted}
