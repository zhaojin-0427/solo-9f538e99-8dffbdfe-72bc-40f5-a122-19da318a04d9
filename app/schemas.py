"""请求/响应模型。

编码约定：
* ``B:`` 开头 —— 盒（最小包装，只能作为子项）
* ``C:`` 开头 —— 箱（可装入另一箱或托盘）
* ``P:`` 开头 —— 托盘（只能作为顶层父项，不能作为子项）
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

CODE_PATTERN = r"^[BCP]:\S+$"
MAX_BATCH_SIZE = 100


class ScanIn(BaseModel):
    """单条装箱扫描记录。"""

    scan_no: str = Field(..., min_length=1, description="扫描号，全局唯一")
    seq: int = Field(..., ge=1, description="全产线递增序号，有效扫描不得重复")
    revision: int = Field(..., ge=1, description="递增修订号，同扫描号只能增大")
    child: str = Field(..., pattern=CODE_PATTERN, description="子码（B: 盒 / C: 箱 / P: 托盘）")
    parent: Optional[str] = Field(
        ...,
        pattern=CODE_PATTERN,
        description="目标父码：非空=转入该包装并替换旧归属；null=拆出",
    )


class BatchIn(BaseModel):
    """一批扫描提交，至多 100 笔。"""

    scans: List[ScanIn] = Field(..., max_length=MAX_BATCH_SIZE)


class ChangeOut(BaseModel):
    """单个盒码的托盘归属变化。"""

    box: str
    old_pallet: Optional[str]
    new_pallet: Optional[str]
    old_path: List[str] = Field(..., description="变更前完整包装路径，自顶（托盘）向底（盒）")
    new_path: List[str] = Field(..., description="变更后完整包装路径，自顶（托盘）向底（盒）")


class BatchOut(BaseModel):
    accepted: bool
    replayed_scans: int = Field(..., description="重放后当前生效的扫描总数")
    changes: List[ChangeOut]


class BoxPathOut(BaseModel):
    box: str
    parent: Optional[str]
    pallet: Optional[str]
    path: List[str] = Field(..., description="完整当前包装路径，自顶（托盘）向底（盒）")


# --------------------------------------------------------------------------- #
# 包材核算：模板化导出 / 解码
# --------------------------------------------------------------------------- #
MAX_EXPORT_PALLETS = 20
PACKAGE_FORMAT_VERSION = "1.0"


class ExportIn(BaseModel):
    """按托盘导出交付包：1～20 个不同托盘码。"""

    pallets: List[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_EXPORT_PALLETS,
        description="待导出托盘码（P: 前缀），至少 1 个、至多 20 个且不得重复",
    )


class TemplateChildIn(BaseModel):
    """模板子项定义：角色 + 份数；份数必须为正整数。"""

    kind: str = Field(..., pattern=r"^[BC]$", description="子项角色：B 盒 / C 箱")
    count: int = Field(..., ge=1, description="该角色子项的份数")
    template_id: Optional[str] = Field(
        default=None,
        pattern=r"^T:\d+$",
        description="kind=C 时引用的同形箱模板；kind=B 时必须为 null",
    )


class TemplateDef(BaseModel):
    """以箱为根的同形模板：只描述层级、空箱与份数，不含任何实物码。"""

    template_id: str = Field(..., pattern=r"^T:\d+$")
    kind: str = Field("C", pattern=r"^C$", description="模板根固定为箱")
    children: List[TemplateChildIn] = Field(
        default_factory=list, description="同角色子项按 (kind, template_id) 聚合的份数"
    )


class SlotIn(BaseModel):
    """模板槽位与实物码的对应：path 为相对箱根的槽位路径，[] 表示箱根本身。"""

    path: List[int] = Field(..., description="槽位路径，整数下标逐层定位")
    kind: str = Field(..., pattern=r"^[BC]$", description="该槽位角色，必须与模板一致")
    code: str = Field(..., pattern=CODE_PATTERN, description="落在该槽位上的实物码")


class CaseInstanceIn(BaseModel):
    """一个箱根子树实例：模板引用 + 逐槽位实物码。"""

    template_id: str = Field(..., pattern=r"^T:\d+$")
    slots: List[SlotIn] = Field(..., min_length=1)


class PalletPackageIn(BaseModel):
    """交付包中的单个托盘段。"""

    pallet: str = Field(..., pattern=CODE_PATTERN, description="托盘码（P: 前缀由解码业务校验）")
    cases: List[CaseInstanceIn] = Field(..., min_length=1)


class PackageIn(BaseModel):
    """自描述交付包：模板定义内嵌，解码只依赖包内数据。"""

    format_version: str = Field(default=PACKAGE_FORMAT_VERSION)
    package_id: Optional[str] = Field(default=None, min_length=1)
    exported_at: Optional[str] = None
    templates: List[TemplateDef] = Field(
        default_factory=list, description="模板定义；为空或引用缺失由解码业务校验拒绝"
    )
    pallets: List[PalletPackageIn] = Field(..., min_length=1, max_length=MAX_EXPORT_PALLETS)


class DirectChildren(BaseModel):
    """一个包装节点的直属关系。"""

    code: str
    kind: str = Field(..., pattern=r"^[PCB]$")
    children: List[str] = Field(default_factory=list, description="直属子项实物码（盒/箱）")


class DecodeOut(BaseModel):
    """解码结果：原托盘、箱、盒的直属关系，全部可由包内数据独立还原。"""

    package_id: Optional[str]
    format_version: str
    pallets: List[DirectChildren] = Field(..., description="托盘 -> 直属箱")
    cases: List[DirectChildren] = Field(..., description="箱 -> 直属箱/盒")
    boxes: List[DirectChildren] = Field(..., description="盒（恒无直属子项）")
    physical_code_count: int = Field(..., description="包内实物码总数（含托盘码）")
    node_count: int = Field(..., description="解码展开的包装节点数，不得超过实物码数")
