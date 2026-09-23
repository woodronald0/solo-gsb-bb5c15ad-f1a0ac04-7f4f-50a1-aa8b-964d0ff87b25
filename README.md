# SVG 轮廓规范化 API

基于 **FastAPI + SQLite + Docker** 的服务：接收 SVG `path` 数据与填充规则，
按轮廓之间的**包含层级**把每个子路径分类为**外环 / 孔洞（可嵌套出“岛”）**，
并重写绘制方向（外环顺时针、孔洞逆时针），同时保留轮廓层级、原始索引与自定义元数据，
对边界情况给出明确警告；另提供 **`POST /paths/{id}/metrics` 填充面积与面积质心分析**
（按外环/孔洞/岛交替计入，曲线经细分多边形近似，只读不写库）、
**`POST /paths/{id}/scanlines` 水平扫描线填充区间**
（在整体包围盒内等间距取水平扫描线，按层级交替合并外环/孔/岛，输出逐行填充区间）、
**`POST /paths/{id}/clearance` 轮廓间安全间隙校验**
（按细分线段计算任意两轮廓边界的最小欧氏距离）、
**`POST /paths/{id}/cut-plan` 闭合轮廓加工计划**
（后代先加工、每轮贪心选取距刀头最近的细分顶点入刀，输出加工顺序与逐段空走距离）
与 **`POST /paths/compare` 版本差异审计**，
按几何指纹稳定匹配两条已存路径并给出 added / removed / modified 三组差异。启动后自动建表，
可直接调用。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由、模型、启动自动初始化
│   ├── db.py          # SQLite 连接与幂等建表
│   ├── svggeo.py      # SVG path 解析 / 细分 / 包含层级 / 方向重写 / 几何指纹 / 线段距离（零三方依赖）
│   ├── metrics.py     # 填充面积与面积质心：外/孔/岛交替计入，基于细分多边形
│   ├── scanlines.py   # 水平扫描线填充区间：半开边求交、按层级交替合并，逐行输出
│   ├── clearance.py   # 轮廓间安全间隙校验：逐段最小欧氏距离、孔-直接 parent 标记
│   ├── cutplan.py     # 闭合轮廓加工计划：后代先加工 + 最近入刀点贪心、逐段空走距离
│   └── diff.py        # 版本差异审计：指纹+索引稳定匹配、三类 modified 分类
├── tests/             # pytest 单元 + API 端到端测试（157 个）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 快速开始（Docker Compose，推荐）

单条命令即可启动（构建镜像、建表、健康检查全自动，无需其他配置）：

```bash
docker compose up --build
```

服务在 <http://localhost:8000>，交互式文档在 <http://localhost:8000/docs>。
容器启动时通过 FastAPI lifespan 自动在 `/data/svgapi.db` 建表（数据存放在命名卷
`svgapi-data` 中），健康探针 `GET /healthz` 通过后即可调用。

必要配置（均有默认值，通常无需修改）：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `SVGAPI_DB_PATH` | `/data/svgapi.db`（容器内） | SQLite 数据库文件路径 |
| 端口映射 | `8000:8000` | 宿主机 8000 -> 容器 8000，API 地址 <http://localhost:8000> |

不用 Compose 时：

```bash
docker build -t svg-contour-normalizer .
docker run -p 8000:8000 -v "$PWD/data:/data" svg-contour-normalizer
```

本地裸机运行：

```bash
pip install -r requirements.txt
SVGAPI_DB_PATH=./svgapi.db uvicorn app.main:app --reload
```

## 方向约定

采用 SVG 屏幕坐标系（**y 轴向下**）：

| 角色 | 层级深度 | 目标方向 | 有符号面积（鞋带公式） |
|------|----------|----------|------------------------|
| 外环 `outer` | 偶数（0, 2, 4 …） | 顺时针 | `> 0` |
| 孔洞 `hole`  | 奇数（1, 3, 5 …） | 逆时针 | `< 0` |

- 包含层级通过“多点采样 + 射线法点在多边形内 + 边界检测”投票判定，
  选择**最紧的包含轮廓**作为父轮廓；支持孔中岛、岛中孔的任意偶数层嵌套。
- `nonzero` 下与约定相反的原始方向会被重写并产生 `NONZERO_DIR_CONFLICT` 警告；
  `evenodd` 渲染本身不依赖方向，服务仍统一为外顺内逆，结果对两种填充规则均正确。
- 贝塞尔（`C/S/Q/T`）与椭圆弧（`A`）在几何分析时细分为折线；
  方向重写时**保留原始命令形态**（曲线只交换控制点顺序，弧只翻转 `sweep-flag`）。

## API

### `POST /paths` — 写入 path 与填充规则

请求体：

| 字段 | 类型 | 说明 |
|------|------|------|
| `d` | string | SVG path 数据（必填） |
| `fill_rule` | `nonzero` \| `evenodd` | 默认 `nonzero` |
| `metadata` | object | 路径级自定义元数据，原样保留 |
| `contour_metadata` | object[] | 按**轮廓原始索引**给出的自定义元数据 |

无法解析的 `d` 或非法填充规则返回 `422`，坏数据不会落库。

```bash
curl -s -X POST localhost:8000/paths -H 'Content-Type: application/json' -d '{
  "d": "M0,0h40v40h-40Z M10,10h20v20h-20Z",
  "fill_rule": "nonzero",
  "metadata": {"part": "法兰", "material": "Q235"},
  "contour_metadata": [{"name": "外法兰"}, {"name": "螺栓孔"}]
}'
# -> {"id":1, "d":"...", "fill_rule":"nonzero", "metadata":{...}, "contour_metadata":[...]}
```

### `POST /paths/{id}/normalize` — 分类并重写方向

幂等：重复调用结果一致。返回原始 `d`、规范化结果（新 `d`、每个轮廓的层级信息、
边界警告）。

```bash
curl -s -X POST localhost:8000/paths/1/normalize
```

响应中的 `normalized.contours[]`：

| 字段 | 说明 |
|------|------|
| `original_index` | 该轮廓在原始 `d` 中的子路径序号（从 0 开始） |
| `role` | `outer` / `hole` / `open` / `degenerate` |
| `depth` | 嵌套深度，0 为最外层；开放/退化轮廓为 `null` |
| `parent_original_index` | 直接父轮廓的原始索引（顶层为 `null`） |
| `closed` | 原始子路径是否以 `Z` 闭合 |
| `fingerprint` | 轮廓几何指纹（sha256），与起点、绘制方向无关，供版本匹配 |
| `signed_area` | 细分折线的有符号面积（**原始方向**，未重写前的符号） |
| `bounds` | `minx/miny/maxx/maxy` 包围盒 |
| `reversed` | 本次是否重写了方向 |
| `metadata` | 创建时按索引提交的自定义元数据 |

### `POST /paths/{id}/metrics` — 填充总面积与面积质心（只读分析）

对已存路径**即时解析**并完成轮廓包含层级分类（无需事先调用 normalize，
**不写规范化状态、不写数据库**），再按**包含层级交替**统计填充区域：

- 外环 `outer`（深度 0、2、4 …）面积 **+** 计入；
- 孔洞 `hole`（深度 1、3、5 …）面积 **−** 扣减；
- 孔中的“岛”（深度 2）重新计入，岛中孔（深度 3）继续扣减，依此类推；
- **开放轮廓**（未以 `Z` 闭合）与**退化轮廓**（面积为 0 或顶点不足）不参与。

面积在曲线（贝塞尔/椭圆弧）**细分后的多边形**上按鞋带公式近似，与
normalize / clearance 使用同一份细分折线；质心为各环带符号面积矩加权的
几何质心（SVG 屏幕坐标，y 轴向下），与轮廓书写方向无关。无请求体。

```bash
# 外方 0..10 含内孔 2..8：填充面积 100 - 36 = 64
curl -s -X POST localhost:8000/paths/1/metrics
```

响应：

```json
{
  "id": 1,
  "fill_rule": "nonzero",
  "area": 64.0,
  "centroid": {"x": 5.0, "y": 5.0},
  "contributing_contour_count": 2,
  "contours": [
    {"original_index": 0, "role": "outer", "depth": 0,
     "area": 100.0, "signed_contribution": 100.0,
     "centroid": {"x": 5.0, "y": 5.0}},
    {"original_index": 1, "role": "hole", "depth": 1,
     "area": 36.0, "signed_contribution": -36.0,
     "centroid": {"x": 5.0, "y": 5.0}}
  ],
  "skipped_contours": []
}
```

- `area`：填充总面积（外环/岛为正贡献、孔洞为负贡献后的净面积，恒 ≥ 0）；
- `centroid`：`{"x", "y"}` 面积质心；**没有任何有效闭合区域**（空路径、
  仅开放/退化轮廓，或净面积被孔洞完全抵消）时为 `null`，同时 `area` 为 `0.0`；
- `contours[]`：逐环明细（几何面积、对总面积的带符号贡献、单环质心），
  按 `original_index` 升序；
- `skipped_contours[]`：未参与统计的轮廓及 `reason`（`open` / `degenerate`）。

孔中岛示例（`100 − 64 + 4 = 40`，质心偏向位于 `(7,7)` 的小岛）：

```bash
curl -s -X POST localhost:8000/paths -H 'Content-Type: application/json' -d '{
  "d": "M0,0h10v10h-10Z M1,1h8v8h-8Z M6,6h2v2h-2Z"
}'
curl -s -X POST localhost:8000/paths/1/metrics
# -> {"id":1, "area":40.0, "centroid":{"x":5.2,"y":5.2}, ...}
```

路径不存在返回 `404`；已存路径无法解析（历史脏数据）返回 `422`。

### `POST /paths/{id}/scanlines` — 水平扫描线填充区间（只读，供逐行加工）

对已存路径**即时解析**并完成轮廓包含层级分类（无需事先调用 normalize，
**不写数据库**），复用与 metrics / clearance 相同的曲线细分折线，在
**有效闭合轮廓（`outer` / `hole`）的整体包围盒**内等间距布置水平扫描线，
逐行求交并按包含层级交替合并，供逐行加工（如激光/打印路径规划）使用：

- 从整体包围盒最小 y 起，每隔 `spacing` 取一条线（`y = miny + k*spacing`），
  直到最大 y（**不含** `maxy`）；高度恰好是 `spacing` 整数倍时，
  线条数恰为 `高度/spacing`；`spacing` 大于高度时仍输出最小 y 处一条线；
- **开放轮廓**（未以 `Z` 闭合）与**退化轮廓**（面积为 0 或顶点不足）跳过；
  没有任何有效闭合轮廓（空路径、仅开放/退化轮廓）时返回空列表、`bounds` 为 `null`；
- 交点采用**半开边规则** `(y1 > y) != (y2 > y)`：水平边永不相交，扫描线
  穿过顶点时只被上行/下行两条邻边之一计数，避免顶点重复计数；
- 每个环的交点升序两两配对，再按**深度奇偶**做事件扫描合并：外环/岛
  （深度偶数）计入，孔洞（深度奇数）扣除；每行输出**升序、互不重叠**的
  `[x_start, x_end]` 填充区间；
- 单次最多 **1000** 条扫描线，超过返回 `422`（请增大 `spacing`）。

请求体：

| 字段 | 类型 | 说明 |
|------|------|------|
| `spacing` | number | 扫描线间距，必须 **> 0**（NaN/无穷/非数字返回 `422`） |

```bash
# 外方 0..10 含内孔 2..8（y=0,5 两条线）
curl -s -X POST localhost:8000/paths/1/scanlines \
  -H 'Content-Type: application/json' \
  -d '{"spacing": 5}'
```

响应：

```json
{
  "id": 1,
  "fill_rule": "nonzero",
  "spacing": 5.0,
  "bounds": {"miny": 0.0, "maxy": 10.0},
  "scanline_count": 2,
  "lines": [
    {"y": 0.0, "intervals": [[0.0, 10.0]]},
    {"y": 5.0, "intervals": [[0.0, 2.0], [8.0, 10.0]]}
  ],
  "skipped_contours": []
}
```

孔中岛在同一行被重新填回（`100 − 64 + 4` 的嵌套，`spacing=2`，
`y=6` 一行的 `6..8` 即岛内填充）：

```bash
curl -s -X POST localhost:8000/paths -H 'Content-Type: application/json' -d '{
  "d": "M0,0h10v10h-10Z M2,2h6v6h-6Z M6,6h2v2h-2Z"
}'
curl -s -X POST localhost:8000/paths/1/scanlines \
  -H 'Content-Type: application/json' -d '{"spacing": 2}'
# y=6 行 -> {"y":6.0, "intervals":[[0.0,2.0],[6.0,10.0]]}
```

- `bounds`：参与计算的有效闭合轮廓整体包围盒（仅 `miny` / `maxy`），
  无有效轮廓时为 `null`；
- `lines[]`：按 y 升序的扫描线结果，每条含 `y` 与升序、互不重叠的
  `intervals`（`[x_start, x_end]`）；扫描线与轮廓仅相切（如包围盒最小 y
  的底边切点）时该行 `intervals` 为空数组；
- `skipped_contours[]`：跳过的轮廓及 `reason`（`open` / `degenerate`）；
- 路径不存在返回 `404`，`spacing` 缺失/≤0/非有限数或扫描线超过
  1000 条返回 `422`，已存路径无法解析返回 `422`。

### `POST /paths/{id}/clearance` — 轮廓间安全间隙校验

对已存路径**即时规范化**（只读：无需事先调用 normalize，也不写库），把贝塞尔/弧
细分为折线后，逐段计算**任意两个参与校验的轮廓边界**之间的最小欧氏距离。
当某对轮廓的距离**严格小于** `min_distance` 时记为一条冲突；
**距离恰好等于阈值视为通过**（含 1e-9 相对容差以吸收细分浮点误差）。

请求体：

| 字段 | 类型 | 说明 |
|------|------|------|
| `min_distance` | number | 允许的最小间隙，必须 **> 0**（NaN/无穷/非数字返回 `422`） |
| `include_open` | boolean | 是否把未闭合的开放轮廓按折线纳入，默认 `false` |

参与/跳过规则：

- 闭合且非退化轮廓（`outer` / `hole`）始终参与；
- 开放轮廓默认跳过，`include_open=true` 时按**开放折线**（不含首尾闭合边）参与；
- 退化轮廓（面积为 0 或顶点不足，含只有两个点的“轮廓”）始终跳过；
- 同一轮廓不与自身比较。

响应中的冲突条目 `conflicts[]` **按 `(较小 original_index, 较大 original_index)`
升序排列**，多次调用结果确定；当一对轮廓中一侧是 `hole`、另一侧正是其
**直接 parent**（`parent_original_index`）时，该条目标记 `"hole_parent": true`。

```bash
curl -s -X POST localhost:8000/paths/1/clearance \
  -H 'Content-Type: application/json' \
  -d '{"min_distance": 8, "include_open": false}'
```

响应：

```json
{
  "id": 1,
  "min_distance": 8.0,
  "include_open": false,
  "checked_contour_count": 2,
  "skipped_contours": [],
  "conflict_count": 1,
  "conflicts": [
    {
      "a": {"original_index": 0, "role": "outer"},
      "b": {"original_index": 1, "role": "hole"},
      "distance": 5.0,
      "threshold": 8.0,
      "hole_parent": true
    }
  ]
}
```

- `a` / `b`：冲突两侧轮廓的 `original_index` 与 `role`（`a` 索引恒小于 `b`）；
- `distance`：两轮廓细分线段之间的最小欧氏距离，`threshold` 回显本次阈值；
- `skipped_contours[]`：未参与比较的轮廓，每项含 `original_index`、`role`
  与 `reason`（`open` 表示默认跳过的开放轮廓，`degenerate` 表示退化轮廓）；
- 间隙达标时 `conflicts` 为空数组；路径不存在返回 `404`，
  `min_distance` 缺失/≤0/非有限数返回 `422`，已存路径无法规范化返回 `422`。

含开放折线与同级孔的示例（`include_open=true` 时开放轮廓才参与，
同级孔之间的冲突 `hole_parent=false`）：

```bash
# 外方 0..20 与开放折线 M21,5 L22,6 L21,7 的最近距离为 1
curl -s -X POST localhost:8000/paths/1/clearance \
  -H 'Content-Type: application/json' \
  -d '{"min_distance": 2, "include_open": true}'
# -> conflict_count=1，b.role="open"，distance=1.0，hole_parent=false
```

### `POST /paths/{id}/cut-plan` — 闭合轮廓加工计划（只读）

对已存路径**即时解析**并完成轮廓包含层级分类（无需事先调用 normalize，
**不写数据库**），从给定刀头起点 `(x, y)` 出发，为全部**闭合且非退化**
轮廓（`outer` / `hole`）生成加工顺序与每步空走（rapid traverse）距离：

- **后代先加工**：一个轮廓必须等它的**全部后代**（任意深度的孔/岛，
  按 `parent_original_index` 传递）加工完后才可加工——先加工最深层
  轮廓，逐层向外；
- **最近入刀点贪心**：每轮在当前可加工（未加工且所有后代均已加工）的
  轮廓中，从各自**细分后的全部顶点**里找出距刀头当前位置最近的一个
  作为该轮廓的入刀点，再在这些候选里取最近者；距离相同（平方距离
  精确相等）时依次按**原始索引**、**顶点序号**决胜；
- **刀头停在入刀点**：闭合轮廓走刀一圈后回到入刀点，随后以该点为
  起点继续选择下一步，直至所有有效轮廓加工完毕；
- **开放轮廓**（未以 `Z` 闭合）与**退化轮廓**（面积为 0 或顶点不足）
  跳过并列入 `skipped_contours`；没有任何有效闭合轮廓（空路径、
  仅开放/退化轮廓）时返回空计划（`steps` 为空、总空走距离为 0）。

请求体：

| 字段 | 类型 | 说明 |
|------|------|------|
| `x` | number | 刀头起点 x 坐标，必须为有限数值（NaN/无穷/非数字返回 `422`） |
| `y` | number | 刀头起点 y 坐标，必须为有限数值（NaN/无穷/非数字返回 `422`） |

```bash
# 外方 0..100 含孔 10..90、孔中岛 40..60，刀头起点 (0,0)
curl -s -X POST localhost:8000/paths -H 'Content-Type: application/json' -d '{
  "d": "M0,0h100v100h-100Z M10,10h80v80h-80Z M40,40h20v20h-20Z"
}'
curl -s -X POST localhost:8000/paths/1/cut-plan \
  -H 'Content-Type: application/json' \
  -d '{"x": 0, "y": 0}'
```

响应：

```json
{
  "id": 1,
  "fill_rule": "nonzero",
  "start": {"x": 0.0, "y": 0.0},
  "total_contours": 3,
  "steps": [
    {"order": 1, "original_index": 2, "role": "outer", "depth": 2,
     "entry_vertex": 0, "entry_point": {"x": 40.0, "y": 40.0},
     "air_distance": 56.568542495, "head_after": {"x": 40.0, "y": 40.0}},
    {"order": 2, "original_index": 1, "role": "hole", "depth": 1,
     "entry_vertex": 0, "entry_point": {"x": 10.0, "y": 10.0},
     "air_distance": 42.426406871, "head_after": {"x": 10.0, "y": 10.0}},
    {"order": 3, "original_index": 0, "role": "outer", "depth": 0,
     "entry_vertex": 0, "entry_point": {"x": 0.0, "y": 0.0},
     "air_distance": 14.142135624, "head_after": {"x": 0.0, "y": 0.0}}
  ],
  "total_air_distance": 113.13708499,
  "skipped_contours": []
}
```

- `steps[]`：按加工顺序排列，每步含 `order`（从 1 开始）、轮廓
  `original_index` / `role` / `depth`、入刀点 `entry_point` 及其在
  细分顶点列表中的序号 `entry_vertex`、本步空走距离 `air_distance`
  （刀头当前位置到入刀点的欧氏距离）与加工后刀头位置 `head_after`
  （闭合轮廓恒等于入刀点）；
- `total_air_distance`：各步空走距离之和；
- `skipped_contours[]`：跳过的轮廓及 `reason`（`open` / `degenerate`）；
- 路径不存在返回 `404`，`x` / `y` 缺失或非有限数返回 `422`，
  已存路径无法解析返回 `422`。

### `GET /paths/{id}` — 查询原始路径、规范化结果与警告

未规范化时 `normalized` 为 `null`；已规范化时包含 `d`、`contours`、
`warnings` 与方向约定说明。`id` 不存在返回 `404`。

```bash
curl -s localhost:8000/paths/1
```

### `POST /paths/compare` — 两条已存路径的版本差异审计

对 `base_id`（基线）与 `candidate_id`（新版本）两条**已存**路径分别即时规范化
（审计为只读操作：无需事先调用 normalize，也不会写库），再按**轮廓几何指纹**建立
稳定匹配：

- 指纹对**绘制起点与顺/逆时针不敏感**（坐标量化到 1e-6 后做循环旋转 + 反向归一），
  且含闭合标志与绝对坐标，因此同一几何被**重排子路径顺序**、仅改变方向时仍被识别为
  同一轮廓——**相同指纹但 `original_index` 变化不算差异**；
- 同指纹轮廓先按相同 `original_index` 配对，剩余项（如多个全等重复轮廓）按索引就近
  稳定一对一配对；
- 指纹匹配不上的轮廓退而按相同 `original_index` 配对（作为几何变化），
  再配不上的计入 `added` / `removed`。

`modified` 的 `change_type`（优先级从高到低）：

| change_type | 含义 |
|-------------|------|
| `geometry` | 几何指纹不同（形状/坐标变化；开放折线闭合等也归此类） |
| `hierarchy` | 几何相同，但 `role`（outer/hole/…）或 `depth` 发生变化 |
| `orientation` | 几何与层级均相同，仅原始绕向相反（有符号面积符号翻转） |

任一路径不存在返回 `404`；参数非法（id 缺失/非正整数）或已存路径无法规范化
返回 `422`。

```bash
curl -s -X POST localhost:8000/paths/compare \
  -H 'Content-Type: application/json' \
  -d '{"base_id": 1, "candidate_id": 2}'
```

响应：

```json
{
  "base_id": 1,
  "candidate_id": 2,
  "added": [
    {
      "candidate_original_index": 2,
      "candidate_role": "hole",
      "candidate_depth": 1,
      "candidate_fingerprint": "a5d4b697…",
      "candidate_metadata": {"name": "新增螺栓孔"}
    }
  ],
  "removed": [
    {
      "base_original_index": 1,
      "base_role": "hole",
      "base_depth": 1,
      "base_fingerprint": "…",
      "base_metadata": {}
    }
  ],
  "modified": [
    {
      "change_type": "orientation",
      "matched_by": "fingerprint",
      "fingerprint": {"base": "d39a2033…", "candidate": "d39a2033…"},
      "base": {
        "original_index": 1, "role": "hole", "depth": 1,
        "parent_original_index": 0, "closed": true,
        "reversed": true, "metadata": {"name": "螺栓孔"}
      },
      "candidate": {
        "original_index": 1, "role": "hole", "depth": 1,
        "parent_original_index": 0, "closed": true,
        "reversed": false, "metadata": {"name": "螺栓孔"}
      }
    }
  ],
  "summary": {
    "base_contour_count": 2,
    "candidate_contour_count": 3,
    "added": 1,
    "removed": 1,
    "modified": 1,
    "unchanged": 1
  }
}
```

- `matched_by` 标识配对依据：`fingerprint`（同指纹，含索引变化的同一轮廓）或
  `index`（指纹不同、仅原始索引相同的几何变化）；
- `modified` 两侧均携带 `original_index`、`role`、`depth`、
  `parent_original_index`、`closed`、`reversed` 与 `metadata`；
- 仅 `metadata` 不同（几何/层级/方向均未变）不计为 `modified`，元数据仍可在配对
  快照中查看；
- `added` / `removed` 条目按对应侧 `original_index` 升序，字段以
  `candidate_` / `base_` 前缀扁平化给出。

典型调用流程（创建两个版本后审计）：

```bash
curl -s -X POST localhost:8000/paths -H 'Content-Type: application/json' -d '{
  "d": "M0,0h40v40h-40Z M10,10h20v20h-20Z"
}'                                   # -> {"id":1, ...}

curl -s -X POST localhost:8000/paths -H 'Content-Type: application/json' -d '{
  "d": "M0,0h40v40h-40Z M12,12h16v16h-16Z M30,10h5v5h-5Z"
}'                                   # -> {"id":2, ...}

curl -s -X POST localhost:8000/paths/compare \
  -H 'Content-Type: application/json' \
  -d '{"base_id": 1, "candidate_id": 2}'
# -> added: 1（新小孔）; modified: 1（原孔 geometry，matched_by=index）
```

### `GET /healthz` — 健康探针

返回 `{"status":"ok"}`，同时用于 Docker HEALTHCHECK。

## 警告码（`normalized.warnings[]`）

| code | 含义 |
|------|------|
| `EMPTY_PATH` | `d` 为空或没有子路径 |
| `OPEN_CONTOUR` | 子路径未以 `Z` 闭合，不参与层级判定，方向原样保留 |
| `DEGENERATE_CONTOUR` | 面积为 0 或顶点不足，跳过方向重写 |
| `NONZERO_DIR_CONFLICT` | `nonzero` 下原方向与外顺内逆约定相反，已重写 |
| `TOUCHING_PARENT_BOUNDARY` | 采样点落在候选父轮廓边上，包含关系可能不严格 |

每条警告带 `contour_original_index`（全局警告为 `null`）与中文 `message`。

## 支持的 path 语法

`M/m L/l H/h V/v C/c S/s Q/q T/t A/a Z/z`，包括：相对/绝对坐标、
隐式命令重复（多组坐标对）、`Z` 之后的隐式 moveto、标志位与数字紧贴
（如 `A5,5 0 00 10,0`）或逗号分隔（`0,1,1`）。弧线半径自动修正遵循
SVG 规范的端点参数化算法。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q          # 157 passed
```

覆盖：解析器边界、面积符号、外/孔/岛三层嵌套、同级双孔、曲线与弧反转几何等价性、
开放与退化轮廓、元数据保留、完整 API 生命周期、404/422、重复规范化幂等；
几何指纹的起点/方向不变性、闭合区分、量化噪声吸收；版本审计的仅方向变化、
层级变化、几何变化、子路径重排（同指纹索引变化）、全等重复轮廓就近配对、
added/removed、元数据透传、未规范化即时审计与脏数据 422；
填充面积/质心的单环、外-孔、外-孔-岛、四层交替、书写方向无关、
两种填充规则一致、曲线细分近似、开放与退化排除、空路径返回面积 0 与质心 null、
只读不落库、404/脏数据 422；
扫描线填充区间的单环/外-孔/孔-岛三层交替、间距起止与最大 y 不含、
半开边顶点不重复计数、相切行空区间、离散外环区间升序不重叠、曲线细分、
开放与退化跳过、无有效轮廓空列表、两种填充规则一致、恰好 1000 条放行与
超限 422、非法 spacing 422、确定性与只读不落库、404/脏数据 422；
安全间隙的点/线段距离原语（相交、零长度段）、孔-直接 parent 标记、
同级孔与隔层 island 不标记、**距离等于阈值通过**、开放轮廓默认跳过与
`include_open` 纳入、退化轮廓始终跳过、曲线细分、包围盒剔除、结果确定性与
只读不落库、404/非法参数 422 等；
闭合轮廓加工计划的后代先加工（外-孔、外-孔-岛三层、同级孔全部先于父轮廓）、
每轮最近细分顶点入刀与刀头停在入刀点的链式空走距离、距离相等时按原始索引、
顶点序号决胜、曲线细分顶点入刀、开放与退化跳过、无有效轮廓空计划、
两种填充规则一致、确定性与只读不落库、404/非有限起点 422 等。

## 数据存储

SQLite 单表 `paths`（WAL 模式）保存原始 `d`、填充规则、两类元数据、
规范化 `d`、规范化详情与警告 JSON。默认路径由环境变量 `SVGAPI_DB_PATH`
控制（容器内 `/data/svgapi.db`）。
