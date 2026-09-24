"""AST-validated, restricted-eval evaluator for user-authored row expressions
(agents/general/mcp_tools.py's modify_data): allow-lists safe node types, builtins,
and attributes, denying dunder access to close sandbox-escape paths.
"""

import ast
import math
import numpy as np
from typing import Any, Dict, Callable


class ExpressionEvaluator:
    ALLOWED_NODES = {
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp,
        ast.Name, ast.Constant, ast.Subscript, ast.Index, ast.Slice,
        ast.Tuple, ast.List, ast.Dict, ast.Set,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.USub, ast.UAdd, ast.Not, ast.Invert,
        ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
        ast.Is, ast.IsNot, ast.In, ast.NotIn,
        ast.And, ast.Or,
        ast.Call, ast.Attribute,
        ast.IfExp,
        ast.Load, ast.Store, ast.Del,
    }
    
    ALLOWED_BUILTINS = {
        'int': int,
        'float': float,
        'str': str,
        'bool': bool,
        'list': list,
        'dict': dict,
        'tuple': tuple,
        'set': set,
        'len': len,
        'round': round,
        'abs': abs,
        'min': min,
        'max': max,
        'sum': sum,
        'sorted': sorted,
        'math.sqrt': math.sqrt,
        'math.pow': math.pow,
        'math.exp': math.exp,
        'math.log': math.log,
        'math.sin': math.sin,
        'math.cos': math.cos,
        'math.tan': math.tan,
        'np.where': np.where,
    }

    ALLOWED_ATTRIBUTES = {
        'get', 'keys', 'values', 'items',
        'lower', 'upper', 'strip', 'replace', 'split', 'startswith', 'endswith',
        'str', 'contains', 'where',
    }
    
    def __init__(self, expression: str, use_ast_validation: bool = True):
        self.expression = expression.strip()
        self.use_ast_validation = use_ast_validation
        self._compiled = None
        
        if use_ast_validation:
            self._validate_expression()
        
    @staticmethod
    def _is_dunder(name: str) -> bool:
        return isinstance(name, str) and name.startswith("__")

    def _validate_expression(self) -> None:
        try:
            tree = ast.parse(self.expression, mode='eval')
        except SyntaxError as e:
            raise ValueError(f"Invalid expression syntax: {e}")
        
        for node in ast.walk(tree):
            node_type = type(node)
            if node_type not in self.ALLOWED_NODES:
                raise ValueError(
                    f"Unsafe operation detected: {node_type.__name__} "
                    f"at line {node.lineno if hasattr(node, 'lineno') else '?'}"
                )

            if isinstance(node, ast.Attribute) and self._is_dunder(node.attr):
                raise ValueError(f"Disallowed attribute: {node.attr}")
            if isinstance(node, ast.Name) and self._is_dunder(node.id):
                raise ValueError(f"Disallowed name: {node.id}")

            if isinstance(node, ast.Call):
                # Else d['eval'](...) bypasses the allowlist below
                if not isinstance(node.func, (ast.Name, ast.Attribute)):
                    raise ValueError(
                        "Disallowed call target: only named functions and "
                        "attribute methods may be called")
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                    if func_name not in self.ALLOWED_BUILTINS:
                        if not (func_name.startswith('math.') and func_name in self.ALLOWED_BUILTINS):
                            raise ValueError(f"Disallowed function: {func_name}")
                elif isinstance(node.func, ast.Attribute):
                    attr_name = node.func.attr
                    if attr_name not in self.ALLOWED_ATTRIBUTES:
                        if attr_name not in ['contains', 'str', 'where']:
                            raise ValueError(f"Disallowed attribute: {attr_name}")
    
    def compile(self) -> Callable:
        if self._compiled is not None:
            return self._compiled
        
        safe_globals = {
            '__builtins__': {
                'int': int,
                'float': float,
                'str': str,
                'bool': bool,
                'list': list,
                'dict': dict,
                'tuple': tuple,
                'set': set,
                'len': len,
                'round': round,
                'abs': abs,
                'min': min,
                'max': max,
                'sum': sum,
                'sorted': sorted,
                'math': math,
                'np': np,
            },
            'math': math,
            'np': np,
            'row': None,
        }
        
        for name, func in self.ALLOWED_BUILTINS.items():
            if name.startswith('math.') or name.startswith('np.'):
                continue
            safe_globals[name] = func
        
        try:
            code = compile(self.expression, '<string>', 'eval')
            
            def evaluator(row: Dict[str, Any]) -> Any:
                safe_globals['row'] = row
                try:
                    return eval(code, safe_globals, {})
                except Exception as e:
                    raise ValueError(
                        f"Error evaluating expression '{self.expression}': {e}"
                    ) from e
            
            self._compiled = evaluator
            return evaluator
        except SyntaxError as e:
            raise ValueError(f"Expression compilation failed: {e}")
    
    def evaluate(self, row: Dict[str, Any]) -> Any:
        if self._compiled is None:
            self.compile()
        return self._compiled(row)
    
    @classmethod
    def evaluate_batch(
        cls,
        expression: str,
        rows: list[Dict[str, Any]],
        default: Any = None,
        use_ast_validation: bool = True
    ) -> list[Any]:
        evaluator = cls(expression, use_ast_validation)
        evaluator.compile()
        
        results = []
        for row in rows:
            try:
                results.append(evaluator.evaluate(row))
            except Exception:
                results.append(default)
        return results


def safe_eval(expression: str, row: Dict[str, Any], default: Any = None) -> Any:
    try:
        evaluator = ExpressionEvaluator(expression)
        return evaluator.evaluate(row)
    except Exception:
        return default


def validate_expression(expression: str) -> bool:
    try:
        ExpressionEvaluator(expression)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    test_row = {"age": 25, "salary": 50000, "name": "Alice", "active": True}
    
    expr1 = "row['age'] * 2 + 5"
    evaluator1 = ExpressionEvaluator(expr1)
    print(f"{expr1} = {evaluator1.evaluate(test_row)}")
    
    expr2 = "'Adult' if row['age'] >= 18 else 'Minor'"
    evaluator2 = ExpressionEvaluator(expr2)
    print(f"{expr2} = {evaluator2.evaluate(test_row)}")
    
    expr3 = "row['name'] + ' is ' + str(row['age'])"
    evaluator3 = ExpressionEvaluator(expr3)
    print(f"{expr3} = {evaluator3.evaluate(test_row)}")
    
    rows = [
        {"age": 15, "salary": 20000},
        {"age": 30, "salary": 60000},
        {"age": 45, "salary": 80000},
    ]
    results = ExpressionEvaluator.evaluate_batch(
        "row['salary'] * 0.1 if row['age'] > 20 else 0",
        rows
    )
    print(f"Batch results: {results}")
