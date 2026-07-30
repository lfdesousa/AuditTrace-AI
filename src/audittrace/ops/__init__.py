"""Operational controllers that run beside the AuditTrace service.

These modules are packaged in the same image as the memory-server but run
as their own single-purpose Deployments (``python -m audittrace.ops.<name>``)
so they can be scheduled, scoped, and RBAC-limited independently of the app.
"""
