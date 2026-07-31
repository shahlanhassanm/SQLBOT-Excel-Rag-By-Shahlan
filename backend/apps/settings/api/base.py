import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from apps.swagger.i18n import PLACEHOLDER_PREFIX
from common.core.config import settings
from common.core.file import FileRequest
from common.utils.paths import PathEscapeError, safe_join

router = APIRouter(tags=["System"], prefix="/system")

path = settings.EXCEL_PATH

ERROR_FILE_SUFFIX = '_error.xlsx'


@router.post("/download-fail-info", summary=f"{PLACEHOLDER_PREFIX}download-fail-info")
async def download_excel(req: FileRequest) -> FileResponse:
    """
    根据文件路径下载 Excel 文件
    """
    # Confine the caller-supplied name to the upload directory and check the
    # suffix BEFORE touching the filesystem, so a rejected request cannot be
    # used to probe whether an arbitrary path exists (AUDIT D-07).
    try:
        file_path = safe_join(path, req.file, (ERROR_FILE_SUFFIX,))
    except PathEscapeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File Not Exists")

    # 获取文件名
    filename = os.path.basename(file_path)

    # 返回文件
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
