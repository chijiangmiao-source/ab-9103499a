# 高空探测器标定包发布系统

操作员在控制台输入**发布标识**和不超过 64 KiB 的 **Base64 工件**，控制服务将同一候选
字节切换到两座离线镜像仓；可按标识查看进度、当前摘要以及准备/激活证据。

## 组件

| 组件 | 说明 |
| --- | --- |
| `controller` | 控制服务（Python 标准库，零三方依赖）。先持久化 SHA-256 与不可变发布意图，再用以发布标识派生的仓端操作键驱动两仓 prepare/activate；提供控制台页面、健康页与发布 API。 |
| `registry-a` / `registry-b` | 两座离线镜像仓 stub。各自持久化状态，支持首次回执回放、同键异摘要拒绝、操作键校验，以及 `drop`（先落盘再断响应）和 `foreign`（回报外摘要、不动指针）故障注入。 |
| `verify` | 一次性验收服务：执行代码测试、构建检查与 HTTP 冒烟/断连重启场景，以退出码报告结果后退出。 |

## 启动与验收

```bash
# 默认宿主机端口 8000；可通过环境变量配置
CONTROL_HTTP_PORT=9000 docker compose up verify

# 或仅启动长期运行的服务
docker compose up -d controller
# 浏览器打开 http://localhost:9000/
```

`verify` 服务结束后可通过退出码判断：

```bash
docker compose up verify; echo "exit=$?"   # 0 = 验收通过
docker compose logs verify                 # 查看逐项检查结果
```

健康检查：`GET /health`。

## 关键语义

- **意图先行持久化**：提交后先把 `release_id -> SHA-256` 不可变意图与意图日志落盘，
  再联系任一镜像仓。
- **派生操作键**：`HMAC-SHA256(主密钥, release_id)`；同标识恒得同键，不同标识不同键，
  错键返回 403。
- **幂等回放**：同键同摘要回放**首次** prepare/activate 回执（同一 nonce），
  重复提交绝不产生第二次激活（每仓 `activation_count` 恒为 1）。
- **同键异摘要拒绝**：仓端返回 409；控制服务对“已用标识 + 不同工件”返回 409 并保留
  既有成功发布的真实状态。
- **双仓同摘要完成门**：只有两仓都激活相同 SHA-256 才置 `COMPLETED` 并改写活动指针。
- **断连重启收敛**：若某仓在**持久化激活之后**断开响应，发布停留 `ACTIVATING`；
  控制服务重启时（或按标识查询时）依据仓端回执对账，收敛为 `COMPLETED`，不会二次激活。
- **外摘要锁定**：任一仓回报不属于该发布的摘要时，发布终态锁定 `REJECTED`，
  活动指针绝不被改写，后续查询也不会翻案。
- **输入反馈**：非法 Base64（400）、超限工件（400，64 KiB）、已用标识不同工件（409）
  均有明确中文反馈，且不影响既有发布。

## HTTP API

- `POST /api/releases` — body `{"release_id": "...", "artifact": "<base64>"}`
  - `200` 完成（含 `replay:true` 表示重复提交回放）
  - `202` 已持久化、推进中（如等待断连仓的回执收敛）
  - `400` 非法 Base64 / 超限 / 标识非法
  - `409` 同标识异摘要或外摘要锁定
- `GET /api/releases/<id>` — 进度、当前摘要、prepare/activate 证据（并触发一次对账）
- `GET /api/active` — 当前活动摘要与对应发布
- `GET /health` — 健康响应
- `POST /admin/restart`（需 `X-Admin-Token`）— 验收场景下触发控制服务重启，
  由 Compose 重启策略拉起

## 本地开发

```bash
cd app
PYTHONPATH=lib python3 -m unittest discover -s tests -v
```
