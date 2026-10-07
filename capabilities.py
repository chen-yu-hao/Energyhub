"""Inspect installed PySCF capabilities without importing its numerical stack."""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
from pathlib import Path
from typing import Any

METHOD_NAMES = ("CCSD", "CCSD(T)", "CCSDT", "CCSDT(Q)")


def _tree(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None


def _implemented_method(path: Path, class_name: str, method: str) -> bool:
    """Return whether a source class contains a non-placeholder method.

    PySCF 2.13.1 exposes ``ccsdt_q`` on both CCSDT classes, but the body is
    solely ``raise NotImplementedError``. Attribute detection is insufficient.
    """

    tree = _tree(path)
    if tree is None:
        return False
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method:
                    body = list(item.body)
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                        body.pop(0)
                    if not body or isinstance(body[0], ast.Pass):
                        return False
                    if isinstance(body[0], ast.Raise):
                        return False
                    return True
    return False


def get_capabilities() -> dict[str, Any]:
    """Return environment capabilities with an explicit reason per method.

    Only the top-level module spec and Python source are inspected. This
    keeps ``/methods`` cheap and avoids initializing BLAS in a web process.
    Source-unavailable and unknown implementations fail closed.
    """

    try:
        spec = importlib.util.find_spec("pyscf")
    except (ImportError, ValueError):
        spec = None
    root = Path(spec.origin).parent if spec is not None and spec.origin else None
    try:
        version = importlib.metadata.version("pyscf") if root else None
    except importlib.metadata.PackageNotFoundError:
        version = None
    exports = _tree(root / "cc" / "__init__.py") if root else None
    names = {node.name for node in exports.body if isinstance(node, ast.FunctionDef)} if exports else set()
    methods = []
    for name in METHOD_NAMES:
        closed = opened = False
        if root is None:
            reason = "PySCF is not installed in this interpreter."
        elif name in {"CCSD", "CCSD(T)"}:
            closed = opened = "CCSD" in names
            reason = None if closed else "Installed PySCF does not expose CCSD."
        elif name == "CCSDT":
            closed = "CCSDT" in names and (root / "cc" / "rccsdt.py").is_file()
            opened = "CCSDT" in names and (root / "cc" / "uccsdt.py").is_file()
            reason = None if closed else "Installed PySCF does not expose CCSDT; PySCF 2.13 or newer is required."
        else:
            closed = "CCSDT" in names and _implemented_method(root / "cc" / "rccsdt.py", "RCCSDT", "ccsdt_q")
            opened = "CCSDT" in names and _implemented_method(root / "cc" / "uccsdt.py", "UCCSDT", "ccsdt_q")
            reason = None if closed or opened else (
                "Installed PySCF has no implemented perturbative quadruples CCSDT(Q) correction. "
                "Its ccsdt_q method is absent or raises NotImplementedError; full CCSDTQ is a different method."
            )
        methods.append({"name": name, "available": closed or opened, "closed_shell": closed, "open_shell": opened, "reason": reason})
    return {
        "pyscf_version": version,
        "methods": methods,
        "basis": ["3zeta", "4zeta", "5zeta", "CBS"],
        "basis_families": ["cc", "aug"],
        "cbs_pairs": ["34", "45"],
        "local_methods": [
            {"name": "canonical", "available": True, "reason": None},
            {"name": "LNO", "available": False, "reason": "No validated LNO coupled-cluster backend is configured."},
            {"name": "PNO", "available": False, "reason": "No validated PNO coupled-cluster backend is configured."},
        ],
    }


__all__ = ["get_capabilities"]
