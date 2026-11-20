# 药品追溯装箱扫描 API

单条产线的装箱扫描服务：FastAPI + SQLite。数据员通过批量扫描确认每盒药当前
所在托盘，支持包装层级（盒 → 箱 → 箱/托盘）的转入、拆出、补传与更正。

## 包装编码规则

| 前缀 | 含义 | 规则 |
| --- | --- | --- |
| `B:` | 盒（最小包装） | 只能作为子项，只能装入 `C:` 箱 |
| `C:` | 箱 | 可装入另一 `C:` 箱或 `P:` 托盘 |
| `P:` | 托盘 | 只能作顶层父项，**不能作为子项** |

* 非空父码：子项转入该包装，并**原子替换旧归属**（任一时点不会同时归属两处）。
* 父码为 `null`：把子项从当前包装**拆出**。

## 扫描与修订语义

* 每批最多 **100 笔**，字段：`scan_no`（扫描号）、`seq`（全产线序号）、
  `revision`（修订号）、`child`（子码）、`parent`（目标父码或 null）。
* 有效扫描的 `seq` 全局唯一（批次内重复直接 400；与历史冲突在重放时 409）。
* 每个 `scan_no` 只保留**最高修订**：
  * 新修订号必须 **大于** 已存修订，否则整批拒绝（409）；
  * 同号同修订但内容（序号/子码/父码）不一致，整批拒绝（409）；
  * 同号同修订同文重提为幂等空操作。
* 所有生效扫描按 `seq` 全序**重放**，逐笔重建层级；任一笔违反角色规则、
  形成环或序号冲突，整批拒绝并返回**首个冲突序号** `conflict_seq`，
  原扫描与层级保持不变。
* 补传/更正导致后续已存扫描变非法时，同样整批拒绝（重放覆盖全量历史）。

## 一致性保证

* 校验、重放、扫描表 upsert、归属边表整体替换在**同一个 SQLite 事务**内完成，
  失败即回滚，不会只更新上层箱而漏掉内盒。
* WAL 模式 + 进程内写锁串行化并发提交，读写并发下不会出现混合修订。
* 数据落盘到 Docker 卷，重启后从已提交状态恢复。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

* API：<http://localhost:8000>
* 交互式文档（Swagger）：<http://localhost:8000/docs>
* OpenAPI：<http://localhost:8000/openapi.json>
* SQLite 数据持久化在命名卷 `trace-data`（容器内 `/data/trace.db`）。

## 本地开发（无 Docker）

```bash
pip install -r requirements.txt
pip install pytest httpx
TRACE_DB_PATH=./data/trace.db uvicorn app.main:app --reload
pytest          # 端到端测试（扫描 17 个 + 包材核算 19 个）
```

## 接口

### `POST /scans/batch` 提交一批扫描

```json
{
  "scans": [
    {"scan_no": "S001", "seq": 1, "revision": 1, "child": "B:box01", "parent": "C:case01"},
    {"scan_no": "S002", "seq": 2, "revision": 1, "child": "C:case01", "parent": "P:pal01"},
    {"scan_no": "S003", "seq": 3, "revision": 1, "child": "B:box02", "parent": null}
  ]
}
```

成功响应（仅列出**路径发生变化的盒码**，含旧/新托盘与完整路径）：

```json
{
  "accepted": true,
  "replayed_scans": 3,
  "changes": [
    {
      "box": "B:box01",
      "old_pallet": null,
      "new_pallet": "P:pal01",
      "old_path": ["B:box01"],
      "new_path": ["P:pal01", "C:case01", "B:box01"]
    }
  ]
}
```

冲突响应（HTTP 409，指出首个冲突序号，整批未生效）：

```json
{"error": "replay_conflict", "message": "装入 C:c2 会使 C:c1 的层级形成环", "conflict_seq": 11}
```

| 状态码 | 场景 |
| --- | --- |
| 200 | 整批生效 |
| 400 | 空批次、批次内扫描号/序号重复 |
| 409 | 修订冲突、序号历史冲突、角色违规、环（带 `conflict_seq`） |
| 422 | 字段类型/编码前缀/批次超 100 笔等 schema 校验失败 |

### `GET /boxes/{box}` 按盒查询当前完整包装路径

```bash
curl http://localhost:8000/boxes/B:box01
```

```json
{
  "box": "B:box01",
  "parent": "C:case01",
  "pallet": "P:pal01",
  "path": ["P:pal01", "C:case01", "B:box01"]
}
```

盒从未出现或最后一次生效操作为拆出时返回 404。

### `GET /health` 健康检查

## 包材核算：模板化导出与解码

### 判同规则

以**箱（`C:`）为根**的子树，满足以下条件即视为同形，共用一份模板编号（`T:000001`…）：

* 只看箱盒**层级**、**空箱**与各层**子项份数**；
* **忽略实物码**与**同层顺序**（盒码不同、扫描先后不同不影响判同）；
* 模板编号首次出现时落库，**跨托盘、跨次导出、进程重启后沿用同一编号**。

### `POST /packages/export` 按托盘导出交付包

请求体给 1～20 个**不同**托盘码；任一托盘当前无箱则**整次报错**（409），不写入任何模板。
导出只读取同一已提交状态的包装层级，模板结构定义内嵌在包内，交付包完全自描述。

```json
{"pallets": ["P:p1", "P:p2"]}
```

响应（节选）：

```json
{
  "format_version": "1.0",
  "package_id": "PKG:9f2c…",
  "exported_at": "2026-11-20T03:00:00.000Z",
  "templates": [
    {"template_id": "T:000001", "kind": "C",
     "children": [{"kind": "B", "count": 2, "template_id": null}]}
  ],
  "pallets": [
    {"pallet": "P:p1", "cases": [
      {"template_id": "T:000001", "slots": [
        {"path": [], "kind": "C", "code": "C:c1"},
        {"path": [0], "kind": "B", "code": "B:b1"},
        {"path": [1], "kind": "B", "code": "B:b2"}
      ]}
    ]}
  ]
}
```

* `templates`：去重后的同形模板（含嵌套箱模板），只描述角色与份数，无实物码；
* `pallets[].cases[]`：每个箱根子树的**模板引用** + 逐槽位（`path` 为相对箱根的下标路径，
  `[]` 是箱根）对应的**实物码**。

| 状态码 | 场景 |
| --- | --- |
| 200 | 导出成功 |
| 409 | 托盘无箱（`pallet_without_case`）、非托盘码/托盘码重复 |
| 422 | 托盘数为空或超过 20 等 schema 校验失败 |

### `POST /packages/decode` 仅凭包内数据解码

**不访问源库**：源库更正扫描、甚至清空后，旧交付包仍能还原原托盘、箱、盒的**直属关系**。

```bash
curl -X POST http://localhost:8000/packages/decode \
  -H 'Content-Type: application/json' -d @package.json
```

```json
{
  "package_id": "PKG:9f2c…",
  "format_version": "1.0",
  "pallets": [{"code": "P:p1", "kind": "P", "children": ["C:c1"]}],
  "cases":   [{"code": "C:c1", "kind": "C", "children": ["B:b1", "B:b2"]}],
  "boxes":   [{"code": "B:b1", "kind": "B", "children": []},
              {"code": "B:b2", "kind": "B", "children": []}],
  "physical_code_count": 3,
  "node_count": 3
}
```

整包拒绝（HTTP 422）的情形：

| error 码 | 含义 |
| --- | --- |
| `missing_template` / `missing_template_reference` | 实例引用了包内不存在的模板、箱类子项缺模板引用 |
| `template_cycle` | 模板互相引用成环 |
| `duplicate_template` / `duplicate_pallet` / `duplicate_slot` | 模板编号、托盘段、槽位路径重复 |
| `duplicate_physical_code` | 同一实物码在包内出现多次 |
| `slot_count_mismatch` | 槽位实物码数量与模板声明不符 |
| `slot_role_mismatch` | 槽位声明角色或编码前缀（`B:`/`C:`）与模板不符 |
| `node_limit_exceeded` | 解码展开的包装节点数超过包内实物码数（防结构放大） |
| `bad_pallet_code` / `bad_template_kind` / `bad_template_role` | 编码角色不合法 |

## 项目结构

```
app/
  main.py       FastAPI 路由与异常映射
  service.py    修订合并、按序号重放、环/角色/双归属校验、路径计算
  packaging.py  同形判同、模板编号落库、导出建包、自描述解码与整包校验
  db.py         SQLite 连接、WAL、写锁、建表（含 templates 表）
  schemas.py    Pydantic 请求/响应模型
tests/          端到端测试（含并发、重启恢复、整批/整包回滚）
Dockerfile
docker-compose.yml
```
