"""业务路由汇总：固定路径的监控路由在前，通用资源路由在后。"""
from __future__ import annotations

from app.routers import monitor, resource

# monitor 含 /api/monitor 固定前缀，必须先于 resource 的 /api/{module} 动态路径注册
ROUTERS = [monitor, resource]
