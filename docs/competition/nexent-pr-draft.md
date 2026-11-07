# 上游 PR 草稿：agent list 未授权返回 401 而非 500

状态：**已提交 → https://github.com/ModelEngine-Group/nexent/pull/4308**（2026-11-07，base: develop，head: May18th:fix/agent-list-401，fork: May18th/nexent）。跟踪 review 结果即可。

## 分支

- `Nexent` 仓库 `fix/agent-list-401`（基于上游 v2.7.0 `519b9b17c`，不含本地定制）
- 提交：`062694ed1` fix(agent): return 401 instead of 500 for unauthorized agent list（+3 行）

## 复现（真机日志留档）

```
2026-11-07 11:32:01 ERROR agent_app    Agent list error: No authorization header provided
2026-11-07 11:32:01 ERROR app_factory  HTTPException: Agent list error.
2026-11-07 11:32:01 INFO  access       "GET /api/agent/list?page=1&page_size=1 HTTP/1.1" 500
```

无 Authorization 头请求 `GET /api/agent/list` 得到 **500**，客户端会把认证问题误判为服务端故障并触发重试。

## 根因

`backend/apps/agent_app.py` 的 `list_agent_config_api` 用裸 `except Exception` 包住
`get_current_user_info()`；`utils/auth_utils.py` 在缺头/过期时抛
`TokenExpiredError`（`UnauthorizedError` 子类，`consts/exceptions.py:120`），被兜底成 500。

## 修复

在通用 `except Exception` 之前捕获 `UnauthorizedError` → 401（该异常类已在文件 import 列表中，零新增依赖）。

## PR 描述（英文，可直接贴）

**Title**: fix(agent): return 401 instead of 500 for unauthorized agent list

**Body**:

`GET /agent/list` wraps `get_current_user_info()` in a bare `except Exception`, so a
missing/expired `Authorization` header (`TokenExpiredError`, subclass of
`UnauthorizedError` raised in `utils/auth_utils.py`) surfaces as
`500 "Agent list error."` instead of `401`. Clients cannot distinguish auth
problems from server faults, and may retry pointlessly.

Reproduction (from a live deployment log):

```
ERROR agent_app    Agent list error: No authorization header provided
INFO  access      "GET /api/agent/list?page=1&page_size=1 HTTP/1.1" 500
```

Fix: catch `UnauthorizedError` before the generic handler and map it to `401`.
`UnauthorizedError` is already imported in this module, so no new dependency.
The same bare-except pattern exists in other endpoints of this file; this PR
keeps the change minimal as a reference fix, happy to follow up with a batch
cleanup if maintainers prefer.

## 流程备忘

1. GitHub fork `ModelEngine-Group/nexent`（用户尚未 fork 过）
2. `git remote add fork <fork_url>` → `git push fork fix/agent-list-401`
3. 向上游开 PR（base: main），贴上面 Body
4. 提 PR 属外部动作，**先征得用户同意**
