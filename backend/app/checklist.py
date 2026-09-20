"""收尾协议检查（completion preconditions）。

实验在 CompleteRun 之前必须满足三项协议：
1. 已有至少一条指标；
2. 已挂至少一件产物；
3. 数据集哈希与代码提交号均已填写。

检查结果同时供详情页逐项展示与审计员只读查看；complete_run 以此作为
服务端强制守卫，前端按钮禁用只是体验优化，不能替代服务端校验。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import RunProjection


class PreconditionError(Exception):
    """协议检查未通过，status_code=422。"""

    def __init__(self, checks: list[dict[str, Any]]):
        self.checks = checks
        missing = [c["label"] for c in checks if not c["passed"]]
        self.message = "收尾协议检查未通过，无法完成实验，缺口：" + "；".join(missing)
        self.status_code = 422
        super().__init__(self.message)


def evaluate_preconditions(proj: RunProjection) -> list[dict[str, Any]]:
    metric_count = len(proj.metrics_json or [])
    artifact_count = len(proj.artifacts_json or [])

    dataset_ok = bool((proj.dataset_content_sha256 or "").strip())
    commit_ok = bool((proj.code_commit_sha or "").strip())
    provenance_ok = dataset_ok and commit_ok

    if provenance_ok:
        provenance_detail = "数据集哈希与代码提交号均已填写"
    elif not dataset_ok and not commit_ok:
        provenance_detail = "缺少数据集哈希与代码提交号"
    elif not dataset_ok:
        provenance_detail = "缺少数据集哈希 dataset_content_sha256"
    else:
        provenance_detail = "缺少代码提交号 code_commit_sha"

    return [
        {
            "key": "metric",
            "label": "已有至少一条指标",
            "passed": metric_count >= 1,
            "detail": f"已记录 {metric_count} 条指标"
            if metric_count >= 1
            else "当前 0 条指标，至少需要记录 1 条",
        },
        {
            "key": "artifact",
            "label": "已挂至少一件产物",
            "passed": artifact_count >= 1,
            "detail": f"已挂载 {artifact_count} 件产物"
            if artifact_count >= 1
            else "当前 0 件产物，至少需要挂载 1 件",
        },
        {
            "key": "provenance",
            "label": "数据集哈希与代码提交号均已填写",
            "passed": provenance_ok,
            "detail": provenance_detail,
        },
    ]


def preconditions_satisfied(proj: RunProjection) -> bool:
    return all(c["passed"] for c in evaluate_preconditions(proj))


def require_preconditions(proj: RunProjection) -> list[dict[str, Any]]:
    """complete_run 守卫：未通过则抛 PreconditionError（HTTP 422）。"""
    checks = evaluate_preconditions(proj)
    if not all(c["passed"] for c in checks):
        raise PreconditionError(checks)
    return checks
