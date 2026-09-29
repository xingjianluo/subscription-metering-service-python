# Subscription Metering Service（订阅计量与结算，本地存储模拟）

FastAPI 实现的订阅计费链路：**套餐 → 用量上报 → 时间窗口聚合 → 按周期出账 → 结算扣款**。
存储与支付均为进程内模拟，不接任何真实支付渠道。

## 1. 运行

```bash
uv sync --group dev
uv run pytest -q                       # 全部单测（含并发/重放，日志打印事件、账期、判定依据）
uv run python main.py                  # 启动 HTTP 服务
uv run uvicorn main:app --reload       # 开发模式
```

## 2. HTTP 接口

所有写接口都要求带 `event_id`（幂等键）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/plans` | 定义套餐（基础费、内含额度、超量单价） |
| POST | `/subscriptions` | 创建订阅（锚定起始时刻，初始套餐） |
| POST | `/usage` | 上报用量（`subscription_id/ts/quantity`） |
| POST | `/subscriptions/{id}/plan-change` | 套餐变更（`plan_id/effective_from`） |
| POST | `/subscriptions/{id}/cancel` | 中途取消（`canceled_at`） |
| POST | `/subscriptions/{id}/invoices` | 按账期出账（`period_index`，可选 `as_of`） |
| POST | `/invoices/{id}/settle` | 结算（`request_id` 为支付请求幂等键） |
| GET  | `/subscriptions/{id}`、`/invoices/{id}`、`/events` | 查询 |
| POST | `/admin/recover` | 崩溃自愈：重做所有 PENDING 事件 |
| POST | `/admin/replay` | 事件重放：以事件流重建全部状态 |

业务错误统一返回 `{"error": <稳定错误码>, "message": ..., "details": ...}`；
输入类错误 HTTP 422，状态/冲突类 409。

## 3. 计量规则

### 3.1 账期与时间窗口

- 账期锚定**订阅起始时刻**，按 1 个月切分，区间左闭右开 `[start, end)`；
  锚点日超过月末时收口（如 1/31 + 1 月 = 2/28）。
- 用量按事件时间戳 `ts` 计算账期序号并分桶存储，**乱序、补报得到相同聚合结果**；
  账期之间物理隔离，**跨周期不可能串账**。
- 同一账期内的聚合为窗口内求和；重复事件由事件流的 `event_id` 去重，
  因此“同一周期重复上报或乱序到达”最终用量恒定。

### 3.2 上报拒绝原因（错误码可区分）

| 错误码 | 触发条件 |
| --- | --- |
| `missing_field` | 缺少 `subscription_id/ts/quantity/event_id` 等关键字段 |
| `invalid_field` | 字段类型不合法（数量非数字、时间非 ISO-8601、价格为负） |
| `usage_negative_quantity` | `quantity < 0` |
| `usage_out_of_subscription_range` | `ts` 早于订阅起点，或晚于/等于取消生效时刻 |
| `unknown_subscription` / `unknown_plan` | 主数据不存在 |
| `event_id_conflict` | 同一 `event_id` 但事件类型或内容指纹不同（明确冲突，**不覆盖**） |

被拒绝的事件会以 `rejected` 状态留痕，拒绝原因 code 保存在事件结果中，可审计、可区分。

## 4. 结算规则

### 4.1 账期关闭

账期结束后才允许出账；默认以当前时间判断，也可显式传 `as_of`（便于测试）。
未关闭时返回 `period_not_closed`，属于**临时性失败**，事件保持 `pending`，关闭后用同一
`event_id` 重试即可。

### 4.2 按比例费用（套餐变更）

套餐变更把账期切成若干左闭右开小段。对每个小段：

- 基础费 = 套餐整期基础费 × 小段天数 / 账期天数
- 内含额度 = 套餐整期额度 × 小段天数 / 账期天数
- 超量费 = max(0, 小段用量 − 按比例内含额度) × 超量单价

天数按秒级精度计算，比例保留 8 位小数；每个小段只可能向“变更后”的用量收费，
不会重复收取基础费。

### 4.3 中途取消抵扣

取消所在账期：

- 取消前按套餐正常计量；取消时刻起（含边界）的用量被拒绝；
- 账单先列**整期基础费**，再追加一条负向 `credit` 明细：
  `credit = −整期基础费 × 取消后未使用天数 / 账期天数`。

这样净额等于按比例收费，但抵扣金额在账单上显式可见、稳定可解释
（例如 31 天账期在第 10 天取消：基础费 100、抵扣 −70.97）。

### 4.4 金额口径

所有金额统一 `ROUND_HALF_UP` 舍入到分（0.01），账单总额 = 各明细行之和，结果可复现。

### 4.5 结算与重试

- 账单 ID 由 `(subscription_id, period_index)` 确定性生成，重复出账返回同一张账单。
- 成功扣款台账（invoice_id 唯一）是“是否扣过钱”的唯一判据：
  - 同一 `request_id` 重试：返回首次结论，**不再扣款**；
  - 账单已结算但换了 `request_id`：返回 `invoice_already_settled`，不扣款；
  - 钱包余额不足：返回 `invoice_settle_failed`，不记账、账单保持 `pending`、事件保持
    `pending`，充值后可重试。
- 模拟钱包对幂等键 `invoice_id:request_id` 只扣一次，覆盖“渠道扣款成功、本地记账前
  崩溃”的窗口：恢复/重试不会二次扣款。

## 5. 幂等边界与事件重放

### 5.1 事件生命周期

所有状态变更都走事件流：

1. 事件先落盘为 `pending`（已分配单调递增 `seq`）；
2. 提交业务效果（幂等处理程序）；
3. 成功置 `applied` 并缓存结果；业务拒绝置 `rejected` 并保存错误码；
   临时性失败（账期未关闭、支付失败）保持 `pending`。

### 5.2 幂等键与冲突判定

- `event_id` 是调用方幂等键：内容（kind + payload）经 SHA-256 指纹比对。
- 同 id 同内容 → 重复投递，直接返回首次结果（并发投递由单进程 RLock 串行化，只生效一次）。
- 同 id 不同内容 → `event_id_conflict`，**绝不以后来的请求覆盖**。

### 5.3 中断恢复与重放

- `POST /admin/recover`：按 `seq` 重做所有 `pending` 事件，效果处理程序本身幂等
  （用量按 event_id 去重、账单按 key 去重、结算靠台账+钱包幂等键）。
- `POST /admin/replay`：以事件流为唯一事实来源，清空投影后按 `seq` 重建；
  `rejected` 事件保留拒绝结论，已结算账单用空钱包重建并标记台账 `recovered`
  （不触碰真实余额），未完成事件随后执行一次崩溃自愈。
  重建后的账单 ID、金额、订阅状态与逐条处理完全一致。

> 边界：本实现的并发安全范围是**单进程多线程**（RLock + 线程安全钱包）；
> 本地存储为内存态，未做跨进程/多实例与持久化，重启后状态不保留。

## 6. 验证方式

```bash
uv run pytest -q
```

| 测试文件 | 覆盖场景 |
| --- | --- |
| `tests/test_usage_aggregation.py` | 乱序/重复上报聚合、跨周期隔离、各类拒绝原因、event_id 冲突 |
| `tests/test_billing.py` | 超量费、套餐变更按比例、中途取消 credit、出账幂等 |
| `tests/test_settlement_idempotency.py` | 重复结算、8 线程并发结算、支付失败充值重试、扣款成功后崩溃恢复 |
| `tests/test_concurrency_and_replay.py` | 10 线程并发重复投递、中途中断恢复、完整历史重放、崩溃后重放收敛 |
| `tests/test_api.py` | HTTP 端到端、状态码与错误码区分 |

测试开启 pytest 实时日志（`log_cli`），`metering.service` 与测试日志会打印
**事件 ID、事件类型、账期编号/窗口、判定结论（accepted/rejected/conflict/recovered）及依据**。
