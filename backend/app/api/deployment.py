"""Read-only endpoint serving the stakeholder deployment-readiness report."""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.api.schemas import DeploymentReport
from app.core.config import get_settings

router = APIRouter(prefix="/api", tags=["deployment"])


@router.get("/deployment-report", response_model=DeploymentReport)
def get_deployment_report() -> DeploymentReport:
    """The ticket-eval deployment report read from the configured JSON path.

    Synchronous so FastAPI runs the small blocking file read in its threadpool rather than on
    the event loop.
    """
    path = Path(get_settings().deployment_report_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"deployment report not found at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return DeploymentReport.model_validate(data)
