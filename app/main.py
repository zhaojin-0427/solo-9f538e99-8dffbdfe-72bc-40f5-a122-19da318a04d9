"""FastAPI 入口：单条产线装箱扫描 API。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db
from .packaging import ExportError, PackageError, decode_package, export_package
from .schemas import BatchIn, BatchOut, BoxPathOut, DecodeOut, ExportIn, PackageIn
from .service import BatchError, ReplayConflict, RevisionConflict, get_box_path, submit_batch


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="药品追溯装箱扫描 API",
    version="1.0.0",
    description=(
        "单条产线扫描提交与包装层级查询。每批至多 100 笔；非空父码表示转入并替换旧归属，"
        "空值表示拆出。冲突批次整体拒绝，原扫描与层级不变。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(BatchError)
def _handle_batch_error(_: Request, exc: BatchError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": exc.code, "message": exc.message, **exc.extra},
    )


@app.exception_handler(RevisionConflict)
def _handle_revision_conflict(_: Request, exc: RevisionConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "revision_conflict",
            "message": exc.reason,
            "scan_no": exc.scan_no,
            "conflict_seq": exc.seq,
        },
    )


@app.exception_handler(ReplayConflict)
def _handle_replay_conflict(_: Request, exc: ReplayConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "replay_conflict",
            "message": exc.reason,
            "conflict_seq": exc.conflict_seq,
        },
    )


@app.exception_handler(ExportError)
def _handle_export_error(_: Request, exc: ExportError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"error": exc.code, "message": exc.message, **exc.extra},
    )


@app.exception_handler(PackageError)
def _handle_package_error(_: Request, exc: PackageError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "message": exc.message, **exc.extra},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/scans/batch", response_model=BatchOut)
def submit(batch: BatchIn) -> dict:
    """提交一批扫描（≤100 笔），全部成功才生效，否则整批拒绝。"""

    return submit_batch(batch)


@app.get("/boxes/{box:path}", response_model=BoxPathOut)
def box_path(box: str) -> dict:
    """按盒码查询完整当前包装路径（自顶向底）。"""

    result = get_box_path(box)
    if result is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "box_not_found",
                "message": f"盒码 {box} 不存在或当前已拆出，无包装归属",
                "box": box,
            },
        )
    return result


@app.post("/packages/export")
def export(req: ExportIn) -> dict:
    """按 1～20 个托盘码导出自描述交付包（JSON）。

    任一托盘当前无箱则整次报错；同形箱根子树共用同一模板编号，
    跨托盘、跨次导出稳定沿用。
    """

    return export_package(req)


@app.post("/packages/decode", response_model=DecodeOut)
def decode(pkg: PackageIn) -> dict:
    """仅凭交付包内数据还原原托盘/箱/盒的直属关系（不查源库）。"""

    return decode_package(pkg)
