# Grok2API 项目文档

## 项目概览

将 Grok Web API 转换为 OpenAI 兼容 API 的服务。

| 项目 | 说明 |
|------|------|
| **当前镜像** | `ghcr.io/xinchengzi/grok2api:latest` |
| **端口** | 7862（映射到容器 8000） |
| **源码路径** | `/root/YData/github/grok2api`（custom 分支） |
| **部署路径** | `/opt/grok2api/` |
| **管理后台** | `http://localhost:7862/manage`（默认账号密码：admin/admin） |

### 目录结构

```
/root/YData/github/grok2api/
├── AGENTS.md                    # 本文档
├── main.py                      # 应用入口
├── app/
│   ├── api/
│   │   ├── admin/manage.py      # 管理后台 API
│   │   └── v1/                  # OpenAI 兼容 API
│   │       ├── chat.py          # /v1/chat/completions
│   │       ├── models.py        # /v1/models
│   │       └── images.py        # 图片接口
│   ├── core/
│   │   ├── config.py            # 配置管理
│   │   ├── proxy_pool.py        # 代理池（多代理、SSO绑定、健康检测）
│   │   ├── storage.py           # 存储抽象（file/mysql/redis）
│   │   ├── auth.py              # API Key 认证
│   │   └── logger.py            # 日志
│   ├── services/
│   │   ├── call_log.py          # 调用日志服务
│   │   ├── grok/
│   │   │   ├── client.py        # Grok HTTP 客户端
│   │   │   ├── token.py         # Token 管理、状态刷新
│   │   │   ├── cache.py         # 图片/视频缓存
│   │   │   ├── upload.py        # 图片上传
│   │   │   └── processer.py     # 响应处理
│   │   └── mcp/                 # MCP 服务
│   ├── models/                  # 数据模型
│   └── template/                # 前端页面
│       ├── admin.html           # 管理后台
│       └── login.html           # 登录页
├── data/                        # 数据目录
│   ├── tokens.json              # Token 存储
│   ├── call_logs.json           # 调用日志
│   └── proxy_state.json         # 代理状态
└── .github/workflows/build.yml  # CI/CD
```

---

## 核心功能

### 1. OpenAI 兼容 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 聊天对话（流式/非流式） |
| `/v1/models` | GET | 获取支持的模型列表 |
| `/images/{path}` | GET | 获取缓存图片 |
| `/videos/{path}` | GET | 获取缓存视频 |

### 2. 管理后台功能

| 页面 | 功能 |
|------|------|
| **Token 管理** | 增删改查、标签、备注、测试可用性 |
| **调用日志** | SSO/状态/模型筛选、时间范围、统计 |
| **代理管理** | 多代理、SSO 绑定、批量导入 |
| **Setting 配置** | 管理员账号、日志级别、缓存、代理配置 |

### 3. 支持的模型

| 模型 | 计次 | 账户类型 | 特性 |
|------|------|----------|------|
| `grok-4.1` | 1 | Basic/Super | 图像、深度思考、联网 |
| `grok-4.1-thinking` | 1 | Basic/Super | 图像、深度思考、联网 |
| `grok-4-fast` | 1 | Basic/Super | 图像、深度思考、联网 |
| `grok-4-heavy` | 1 | Super | 图像、深度思考、联网 |
| `grok-3-fast` | 1 | Basic/Super | 图像、联网 |
| `grok-imagine-0.9` | - | Basic/Super | 视频生成 |

---

## 自定义修改（相对上游）

从 `grok2api-pro` 移植的功能：

| 功能 | 文件 | 说明 |
|------|------|------|
| **调用日志** | `app/services/call_log.py` | 记录每次 API 调用的 SSO、模型、成功率、响应时间 |
| **代理池增强** | `app/core/proxy_pool.py` | 多代理支持、SSO 绑定、健康检测、状态持久化、并发安全 |
| **Token 状态刷新** | `app/services/grok/token.py` | 定时刷新过期 Token、视频配额追踪 |
| **TLS 错误重试** | `app/services/grok/client.py` | TLS 握手瞬断自动重试（渐进延迟） |
| **新管理界面** | `app/template/admin.html` | 调用日志页面、代理管理页面 |

### 安全增强

- **SSO 脱敏**：日志只存储 `前6位****后4位`，不可逆
- **并发安全**：ProxyPool 关键操作加锁（`_state_lock`）
- **日志队列**：`queue_call()` 同步追加，后台批量写盘

---

## 重试与延迟机制

### TLS 重试

网络不稳定导致 HTTPS 握手失败时，渐进延迟重试：

```python
# client.py:437
await asyncio.sleep(0.4 * tls_retry_count)  # 第1次0.4s，第2次0.8s...
```

### 外层重试

遇到 429（限流）、401（未授权）等 HTTP 错误时：

```python
# client.py:396-398
delay = (outer_retry + 1) * 0.1  # 第1次0.1s，第2次0.2s，第3次0.3s
await asyncio.sleep(delay)
```

### 调用日志

- **写入方式**：`queue_call()` 追加到内存队列（不阻塞）
- **持久化**：后台任务每 2 秒批量写入 `data/call_logs.json`
- **容量限制**：默认最多 10000 条，超限自动删除最旧的
- **关闭保护**：服务关闭时处理剩余队列并保存

---

## 部署与运维

### Docker Compose 配置

```yaml
services:
  grok2api:
    image: ghcr.io/xinchengzi/grok2api:latest
    container_name: grok2api
    restart: unless-stopped
    ports:
      - "7862:8000"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - STORAGE_MODE=file
```

### 常用命令

```bash
# 查看日志
docker logs -f grok2api

# 重启服务
cd /opt/grok2api && docker compose restart

# 更新镜像
cd /opt/grok2api && docker compose pull && docker compose up -d

# 触发构建
gh workflow run build.yml --repo xinchengzi/grok2api --ref custom

# 查看构建状态
gh run list --repo xinchengzi/grok2api --limit 3
```

### 数据文件

| 文件 | 说明 |
|------|------|
| `data/tokens.json` | Token 存储（SSO/SuperSSO） |
| `data/call_logs.json` | 调用日志（最多 10000 条） |
| `data/proxy_state.json` | 代理绑定状态 |
| `data/temp/image/` | 图片缓存 |
| `data/temp/video/` | 视频缓存 |

---

## CI/CD

GitHub Actions：`.github/workflows/build.yml`

```
custom 分支 → 手动触发 → 构建镜像 → 推送到 GHCR
```

### 触发构建

```bash
# 方式一：gh CLI
gh workflow run build.yml --repo xinchengzi/grok2api --ref custom

# 方式二：GitHub 页面
# 访问 https://github.com/xinchengzi/grok2api/actions
# 点击 "Build Custom Image" → "Run workflow"
```

---

## API 接口

### 管理接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/login` | GET | 登录页面 |
| `/manage` | GET | 管理后台页面 |
| `/api/login` | POST | 管理员登录 |
| `/api/logout` | POST | 管理员登出 |
| `/api/tokens` | GET | 获取 Token 列表 |
| `/api/tokens/add` | POST | 批量添加 Token |
| `/api/tokens/delete` | POST | 批量删除 Token |
| `/api/tokens/tags` | POST | 更新 Token 标签 |
| `/api/tokens/note` | POST | 更新 Token 备注 |
| `/api/tokens/test` | POST | 测试 Token 可用性 |
| `/api/settings` | GET/POST | 获取/更新配置 |
| `/api/stats` | GET | 获取统计信息 |
| `/api/cache/size` | GET | 获取缓存大小 |
| `/api/cache/clear` | POST | 清理缓存 |

### 调用日志接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/logs` | GET | 获取日志列表（分页、筛选） |
| `/api/logs/models` | GET | 获取日志中的模型列表 |
| `/api/logs/stats` | GET | 获取日志统计 |
| `/api/logs` | DELETE | 清空/清理日志 |

---

## 提交记录（自定义分支）

| 提交 | 说明 |
|------|------|
| `1be1190` | feat: 添加调用日志 API 端点 /api/logs |
| `e20545e` | fix: Oracle 审查修复 - 并发安全和日志任务管理 |
| `5ce303c` | feat: 移植 grok2api-pro 前端界面 |
| `0dcdc94` | feat: 移植 grok2api-pro 优秀功能 |
| `9896b42` | fix: 修复流式响应超时检测失效的问题 |
| `2710252` | fix: 只返回一张生成图片 |
| `95ff438` | feat: 支持图片连续对话修改 |
| `6e46c98` | fix: 修复多轮对话上下文重复问题 |

---

## 已知问题

| 问题 | 状态 | 说明 |
|------|------|------|
| 图片模型多轮对话提示语泄露 | 保留 | 返回 `[注意：请根据以上对话历史...]` 提示语 |

---

## 参考资源

| 资源 | 链接 |
|------|------|
| 上游仓库 | `https://github.com/chenyme/grok2api` |
| Fork 仓库 | `git@github.com:xinchengzi/grok2api.git` |
| grok2api-pro | `/root/YData/github/grok2api-pro`（功能参考） |
