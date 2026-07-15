"""Small AST predicates shared by the parser, the scanner and the checks."""
from __future__ import annotations

import ast


def const_str(node: ast.AST | None) -> str | None:
    """The literal string value of a node, or None."""
    return node.value if isinstance(node, ast.Constant) and isinstance(
        node.value, str) else None


def is_model_base(base: ast.expr) -> bool:
    """Is this class base models.Model / TransientModel / AbstractModel?"""
    if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
        return (base.value.id == "models"
                and base.attr in ("Model", "TransientModel", "AbstractModel"))
    if isinstance(base, ast.Name):
        return base.id in ("Model", "TransientModel", "AbstractModel")
    return False


def is_cursor(e: ast.expr) -> bool:
    """Does this expression look like a database cursor (cr, self.env.cr)?"""
    return ((isinstance(e, ast.Attribute) and e.attr in ("cr", "_cr"))
            or (isinstance(e, ast.Name) and e.id in ("cr", "cursor")))


def is_env(e: ast.expr) -> bool:
    """Does this expression look like an Odoo environment (env, self.env)?"""
    return ((isinstance(e, ast.Attribute) and e.attr == "env")
            or (isinstance(e, ast.Name) and e.id == "env"))
