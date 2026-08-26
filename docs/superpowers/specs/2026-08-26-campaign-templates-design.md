# 外呼营销模板管理（Campaign Templates）设计

- **日期**：2026-08-26
- **状态**：已批准（用户逐节确认）
- **范围**：toB 使用者通过前端页面录入并管理多套外呼配置模板（公司名称、业务背景与卖点、开场白、固定话术、AI 人设），批次外呼时选择模板生效

---

## 1. 背景与目标

现状中四类配置分散且不可编辑：业务背景来自 `.env`（`OUTBOUND_BUSINESS_BACKGROUND`）+ LLM 个性化；开场白由客户姓名自动生成；固定话术硬编码 3 条（`BUILTIN_SCRIPTS`）；AI 名字与语气风格是 `persona.py` 模块常量。toB 产品需要让使用者自助录入、管理多套配置并绑定到具体批次。

**明确不做**：客户画像录入（客户级属性由 CSV 导入承载）；单通调试页选模板（本期仅批量链路）。

## 2. 数据模型与存储

SQLite 同库（`data/sessions.db`）新增两张表，`app/storage.py` 的 `_init_schema` 中创建：

```sql
CREATE TABLE IF NOT EXISTS campaign_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    company_name TEXT NOT NULL DEFAULT '',
    business_background TEXT NOT NULL DEFAULT '',
    opening_template TEXT NOT NULL DEFAULT '',
    bot_name TEXT NOT NULL DEFAULT '',
    speaking_style TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS template_scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL
        REFERENCES campaign_templates(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    triggers TEXT NOT NULL,       -- JSON 数组
    reply TEXT NOT NULL,
    end_call INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 5,
    description TEXT NOT NULL DEFAULT ''
);
```

`batches` 表加两列记录拨号时使用的模板（追溯用）：

- 迁移方式：`ALTER TABLE batches ADD COLUMN`，SQLite 不支持 `ADD COLUMN IF NOT EXISTS`，用 `PRAGMA table_info(batches)` 检测后按需执行（与既有 `_init_schema` 幂等风格一致）。

**启动种子**：`create_app` 时若 `campaign_templates` 为空，插入一条"内置默认"模板（`is_default=1`），内容取自现有 `.env` 业务背景与 `DEFAULT_OPENING_TEXT`，话术为空（内置 3 条始终另行合并），保证升级后零配置行为不变。

Repository 新增方法：`list_templates()`、`get_template(id)`（含话术）、`create_template(...)`、`update_template(...)`（整体替换：删旧话术插新话术）、`delete_template(id)`、`set_default_template(id)`（事务内清零其他）。模板话术单独存 `template_scripts` 表，不复用既有 `scripts` 表，避免与既有脚本库语义混淆。

## 3. REST API（`app/main.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/templates` | 列表（含 `is_default`，按创建时间） |
| POST | `/api/templates` | 新建 |
| PUT | `/api/templates/{id}` | 整体替换（字段 + 话术） |
| DELETE | `/api/templates/{id}` | 删除；历史批次保留快照 |
| POST | `/api/templates/{id}/default` | 设为默认（自动取消其他） |

**校验规则（不通过返回 400，附条目级错误）**：

- `name` 非空且唯一（冲突返回 409）；`company_name` 必填
- `bot_name` ≤ 20 字（豆包硬限制，见 `persona.MAX_BOT_NAME_CHARS`）
- 每条话术过现有 `validate_script`（回复 20-40 字、`end_call` 必须含"再见"、禁写死价格、优先级 0-10、结论枚举），复用 `app/outbound/script_library.py`

## 4. 前端页面 `/templates`

新建 `app/static/templates.html` + `templates.js`，沿用 `workbench.html` 静态页风格与 `styles.css` 约定；`main.py` 加页面路由，`workbench.html` 与 `index.html` 头部加链接入口。

- **列表区**：模板名、公司名、默认徽标；操作：编辑 / 删除（有确认提示）/ 设为默认 / 新建
- **编辑表单**：模板名、公司名称、业务背景与卖点（多行文本）、开场白（提示可用 `{name}`/`{title}` 占位符，留空=按客户姓名自动生成）、AI 名字（显示 20 字上限）、语气风格
- **固定话术编辑器**：行编辑表格（类别 / 触发词逗号分隔 / 回复 / 是否结束通话 / 结论下拉：感兴趣·不感兴趣·中立·不定结论 / 优先级 0-10）；保存前前端先跑一遍与后端同规则的校验并即时标红提示
- 页面固定说明：**内置 3 条合规话术（身份否认 / 投诉免打扰 / 听不清）始终生效、不可删除**

## 5. 拨号链路集成

`POST /api/batches/{batch_id}/confirm` 载荷新增可选 `template_id`：缺省取默认模板；无任何模板则完全走现有 `.env` 老行为（向后兼容）。确认时校验模板存在（否则 409），并把 `template_id`/`template_name` 快照写入批次行。

拨号时（`app/batch/runner.py` 按批次行的模板解析）：

1. **业务背景** = 公司名称 + 模板背景拼接。**取舍**：模板提供了背景时跳过 LLM 按客户列生成个性化背景（B 端所见即所得、行为确定）；模板背景为空才回退现有 personalizer 流程。
2. **开场白** = `opening_template` 中的 `{name}`/`{title}` 用客户 `raw_data` 替换（复用 `personalizer.extract_name/extract_title`）；模板开场白留空则用现有 `build_opening_text`。
3. **人设**：`bridge.dial` 载荷新增 `bot_name`/`speaking_style` → 桥接页透传进 `startSession` → 网关 `_setup` 中 payload 值优先、缺省回退 `outbound.bot_name`/`outbound.speaking_style`（沿用 `business_background` 的既有覆盖模式）。
4. **话术匹配**：`bridge.dial` 与 `startSession` 载荷新增 `template_id`；网关 `_load_scripts` 扩展为"内置话术（现有 `list_scripts('builtin')` 或 `BUILTIN_SCRIPTS` 兜底）+ 该模板的 `template_scripts`"合并，冲突由现有优先级 + 触发词长度规则裁决。

`Dial` 动作、`bridge.dial` 消息、`startSession` 载荷三处各加 `bot_name`/`speaking_style`/`template_id` 字段；旧版桥接页不传新字段时网关行为与现状完全一致。

## 6. 测试计划

- **存储层**：模板/话术 CRUD、默认互斥、批次表迁移幂等
- **API 层**：校验 400（坏话术、超长 `bot_name`）、重名 409、设默认互斥、删除后批次快照仍可读
- **链路层**：确认批次带 `template_id` → 拨号载荷（开场白占位符替换、人设字段、背景拼接）；模板背景存在时 personalizer 被跳过；话术合并后的匹配优先级；无模板回退老行为
- **页面冒烟**：模板页路由返回 200、静态资源可加载
- 基线：203 passed → 预计 218+，改动前后全量回归

## 7. 假设与边界

- 模板编辑在批次确认之后生效于**后续**拨号（不做版本快照冻结）；批次行保存的是确认时刻的模板名，内容变化不追溯
- 内置 3 条合规话术不可经页面删除（合规底线）
- 单通调试页、`/api/scenarios` 其他场景不受影响
- 模板数量无上限，但预期个位数，不做分页
