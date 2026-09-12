#!/usr/bin/env python3
"""
Runtime Parameters Parser for OpenTrons Protocol Files

Parses Python protocol files to extract runtime parameter definitions
from the add_parameters() function using AST parsing.

Usage:
    python runtime_parameters_parser.py <protocol_file.py> [output.json]
    cat protocol.py | python runtime_parameters_parser.py -
"""

import ast
import json
import sys
import os
from typing import Any, Optional, Dict


def parse_protocol_file(file_path: str) -> list[dict[str, Any]]:
    """Parse a protocol file and extract runtime parameter definitions."""
    
    # Handle stdin case
    if file_path == '-':
        source = sys.stdin.read()
    else:
        with open(file_path, 'r', encoding='utf-8') as f:
            source = f.read()
    
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [{
            'error': f'Syntax error in protocol file: {e}',
            'line': e.lineno,
            'offset': e.offset
        }]
    
    parameters = []
    
    # Find the add_parameters function and detect the parameter object name
    param_object_name = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'add_parameters':
            # The first parameter of add_parameters() is the parameter object
            if node.args.args:
                param_object_name = node.args.args[0].arg
            break
    
    # Find parameter definitions using the detected object name,
    # unrolling for/if/with/try blocks so loop-generated params show in UI
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'add_parameters':
            parameters.extend(
                collect_parameter_statements(
                    node.body, param_object_name, {}
                )
            )
    
    return parameters


def collect_parameter_statements(
    stmts: list[ast.stmt],
    param_object_name: Optional[str],
    env: Dict[str, Any],
) -> list[dict[str, Any]]:
    """Walk statements, unrolling loops/branches with a small static env."""
    found: list[dict[str, Any]] = []
    local_env: Dict[str, Any] = dict(env)
    for stmt in stmts:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                value = extract_value(stmt.value, local_env)
                if value is not None:
                    local_env[target.id] = value
            continue
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.value is not None:
                value = extract_value(stmt.value, local_env)
                if value is not None:
                    local_env[stmt.target.id] = value
            continue
        if isinstance(stmt, ast.For):
            for child_env in iter_loop_envs(stmt, local_env):
                found.extend(
                    collect_parameter_statements(
                        stmt.body, param_object_name, child_env
                    )
                )
            if stmt.orelse:
                found.extend(
                    collect_parameter_statements(
                        stmt.orelse, param_object_name, dict(local_env)
                    )
                )
            continue
        if isinstance(stmt, ast.If):
            test = extract_value(stmt.test, local_env)
            if test is True:
                found.extend(
                    collect_parameter_statements(
                        stmt.body, param_object_name, dict(local_env)
                    )
                )
            elif test is False:
                found.extend(
                    collect_parameter_statements(
                        stmt.orelse, param_object_name, dict(local_env)
                    )
                )
            else:
                found.extend(
                    collect_parameter_statements(
                        stmt.body, param_object_name, dict(local_env)
                    )
                )
                found.extend(
                    collect_parameter_statements(
                        stmt.orelse, param_object_name, dict(local_env)
                    )
                )
            continue
        if isinstance(stmt, ast.With):
            found.extend(
                collect_parameter_statements(
                    stmt.body, param_object_name, dict(local_env)
                )
            )
            continue
        if isinstance(stmt, ast.Try):
            for block in (stmt.body, stmt.orelse, stmt.finalbody):
                found.extend(
                    collect_parameter_statements(
                        block, param_object_name, dict(local_env)
                    )
                )
            for handler in stmt.handlers:
                found.extend(
                    collect_parameter_statements(
                        handler.body, param_object_name, dict(local_env)
                    )
                )
            continue
        param_def = parse_parameter_statement(stmt, param_object_name, local_env)
        if param_def:
            found.append(param_def)
    return found


def iter_loop_envs(node: ast.For, env: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Bind loop target(s) for each iteration of a statically known iterable."""
    values = extract_iter_values(node.iter, env)
    if values is None:
        return [dict(env)]
    envs: list[Dict[str, Any]] = []
    for value in values:
        child = dict(env)
        if isinstance(node.target, ast.Name):
            child[node.target.id] = value
        elif isinstance(node.target, (ast.Tuple, ast.List)):
            names = [e.id for e in node.target.elts if isinstance(e, ast.Name)]
            if len(names) > 1 and isinstance(value, (list, tuple)):
                for name, item in zip(names, value):
                    child[name] = item
            elif len(names) == 1:
                child[names[0]] = value
            else:
                continue
        else:
            continue
        envs.append(child)
    return envs or [dict(env)]


def extract_iter_values(node: ast.AST, env: Dict[str, Any]) -> list[Any] | None:
    """Statically evaluate the iterable of a for loop."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == 'range' and not node.keywords:
            try:
                args = [extract_value(a, env) for a in node.args]
            except Exception:
                return None
            if any(not isinstance(a, int) or isinstance(a, bool) for a in args):
                return None
            try:
                if len(args) == 1:
                    return list(range(args[0]))
                if len(args) == 2:
                    return list(range(args[0], args[1]))
                if len(args) == 3:
                    return list(range(args[0], args[1], args[2]))
            except Exception:
                return None
            return None
        return None
    if isinstance(node, (ast.List, ast.Tuple)):
        values = []
        for item in node.elts:
            value = extract_value(item, env)
            if value is None:
                return None
            values.append(value)
        return values
    resolved = extract_value(node, env)
    if isinstance(resolved, (list, tuple, range)):
        return list(resolved)
    return None


def parse_parameter_statement(stmt: ast.stmt, param_object_name: Optional[str], env: Optional[Dict[str, Any]] = None) -> dict[str, Any] | None:
    """Parse a parameter definition statement (p.add_int, parameters.add_str, etc.)."""
    
    if not isinstance(stmt, ast.Expr):
        return None
    
    call = stmt.value
    
    if not isinstance(call, ast.Call):
        return None
    
    # Check if it's a method call on the parameter object
    if not isinstance(call.func, ast.Attribute):
        return None
    
    attr = call.func
    if not isinstance(attr.value, ast.Name) or attr.value.id != param_object_name:
        return None
    
    method_name = attr.attr
    
    # Only process known parameter methods
    if method_name not in ('add_int', 'add_float', 'add_str', 'add_bool', 'add_csv_file'):
        return None
    
    # Parse the method call to extract parameter metadata
    param_def = {
        'type': method_name.replace('add_', ''),
        'variable_name': None,
        'display_name': None,
        'default': None,
        'description': None,
        'choices': None,
        'minimum': None,
        'maximum': None,
        'unit': None
    }
    
    # Extract keyword arguments
    env = env or {}
    for kw in call.keywords:
        if kw.arg == 'variable_name':
            param_def['variable_name'] = extract_value(kw.value, env)
        elif kw.arg == 'display_name':
            param_def['display_name'] = extract_value(kw.value, env)
        elif kw.arg == 'default':
            param_def['default'] = extract_value(kw.value, env)
        elif kw.arg == 'description':
            param_def['description'] = extract_value(kw.value, env)
        elif kw.arg == 'choices':
            param_def['choices'] = extract_choices(kw.value, env)
        elif kw.arg == 'minimum':
            param_def['minimum'] = extract_value(kw.value, env)
        elif kw.arg == 'maximum':
            param_def['maximum'] = extract_value(kw.value, env)
        elif kw.arg == 'unit':
            param_def['unit'] = extract_value(kw.value, env)
    
    # Validate required fields
    if not param_def['variable_name'] or not param_def['display_name']:
        return None
    
    return param_def


def extract_value(node: ast.AST, env: Optional[Dict[str, Any]] = None) -> Any:
    """Extract a value from an AST node, resolving loop vars and f-strings."""
    env = env or {}
    
    if isinstance(node, ast.Constant):
        return node.value
    elif isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in ('True', 'False', 'None'):
            return {'True': True, 'False': False, 'None': None}[node.id]
        return None
    elif isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                inner = extract_value(value.value, env)
                if inner is None:
                    return None
                if value.format_spec is not None:
                    spec = extract_value(value.format_spec, env)
                    if spec is None:
                        return None
                    try:
                        parts.append(format(inner, str(spec)))
                    except Exception:
                        return None
                else:
                    parts.append(str(inner))
            else:
                return None
        return ''.join(parts)
    elif isinstance(node, ast.BinOp):
        left = extract_value(node.left, env)
        right = extract_value(node.right, env)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Mod):
                return left % right
        except Exception:
            return None
        return None
    elif isinstance(node, ast.UnaryOp):
        operand = extract_value(node.operand, env)
        if operand is None:
            return None
        try:
            if isinstance(node.op, ast.UAdd):
                return +operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.Not):
                return not operand
        except Exception:
            return None
        return None
    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and not node.keywords:
            try:
                args = [extract_value(a, env) for a in node.args]
            except Exception:
                return None
            if any(v is None for v in args):
                return None
            try:
                if node.func.id == 'str' and len(args) == 1:
                    return str(args[0])
                if node.func.id == 'int' and len(args) == 1:
                    return int(args[0])
                if node.func.id == 'float' and len(args) == 1:
                    return float(args[0])
                if node.func.id == 'bool' and len(args) == 1:
                    return bool(args[0])
            except Exception:
                return None
        return None
    elif isinstance(node, ast.List):
        values = []
        for item in node.elts:
            value = extract_value(item, env)
            if value is None and not isinstance(item, ast.Starred):
                return None
            values.append(value)
        return values
    elif isinstance(node, ast.Tuple):
        values = []
        for item in node.elts:
            value = extract_value(item, env)
            if value is None:
                return None
            values.append(value)
        return tuple(values)
    elif isinstance(node, ast.Dict):
        result = {}
        for key, value in zip(node.keys, node.values):
            if key is None:
                return None
            k = extract_value(key, env)
            v = extract_value(value, env)
            if k is None or v is None:
                return None
            result[k] = v
        return result
    
    return None


def extract_choices(node: ast.AST, env: Optional[Dict[str, Any]] = None) -> list[dict[str, Any]] | None:
    """Extract choices list from AST node."""
    env = env or {}
    
    if isinstance(node, ast.Name) and node.id in env:
        resolved = env[node.id]
        if isinstance(resolved, list):
            node = ast.parse(repr(resolved)).body[0].value
        else:
            return None
    if isinstance(node, ast.List):
        choices = []
        for item in node.elts:
            if isinstance(item, ast.Dict):
                choice = {}
                for key, value in zip(item.keys, item.values):
                    k = extract_value(key, env)
                    v = extract_value(value, env)
                    if k is None or v is None:
                        continue
                    if k and v is not None:
                        choice[k] = v
                if choice:
                    choices.append(choice)
        return choices if choices else None
    
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python runtime_parameters_parser.py <protocol_file.py> [output.json]", file=sys.stderr)
        sys.exit(1)
    
    input_file = sys.argv[1]
    
    # Handle stdin case
    if input_file == '-':
        # Read from stdin, write to output file if specified
        parameters = parse_protocol_file('-')
        
        output_json = json.dumps(parameters, indent=2, ensure_ascii=False)
        
        if len(sys.argv) >= 3:
            output_file = sys.argv[2]
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(output_json)
            print(f"Parameters written to: {output_file}")
        else:
            print(output_json)
    else:
        if not os.path.exists(input_file):
            print(f"Error: File not found: {input_file}", file=sys.stderr)
            sys.exit(1)
        
        parameters = parse_protocol_file(input_file)
        
        # Output to stdout or file
        output_json = json.dumps(parameters, indent=2, ensure_ascii=False)
        
        if len(sys.argv) >= 3:
            output_file = sys.argv[2]
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(output_json)
            print(f"Parameters written to: {output_file}")
        else:
            print(output_json)
    
    # Return exit code based on results
    if any('error' in p for p in parameters):
        sys.exit(1)
    
    sys.exit(0)


if __name__ == '__main__':
    main()
