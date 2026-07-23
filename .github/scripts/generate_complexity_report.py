#!/usr/bin/env python3
# ruff: noqa: E501
"""Generate the release-time source complexity HTML report.

Usage: generate_complexity_report.py [WORKTREE] [-o REPORT]
"""

import argparse
import ast
import datetime
import html
import pathlib
import subprocess

WORKSPACE = pathlib.Path.cwd()
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "worktree",
    nargs="?",
    type=pathlib.Path,
    default=WORKSPACE,
    help="worktree to analyze (default: %(default)s)",
)
parser.add_argument(
    "-o",
    "--output",
    type=pathlib.Path,
    default=WORKSPACE / "code-complexity-report.html",
    help="HTML report path (default: %(default)s)",
)
args = parser.parse_args()
ROOT = args.worktree.resolve()
SOURCE = ROOT / "src"
OUTPUT = args.output.resolve()
if not SOURCE.is_dir():
    parser.error(f"Python source directory not found: {SOURCE}")


def rank(cc):
    if cc <= 5:
        return "A", "low"
    if cc <= 10:
        return "B", "moderate"
    if cc <= 20:
        return "C", "high"
    if cc <= 30:
        return "D", "very-high"
    if cc <= 40:
        return "E", "severe"
    return "F", "critical"


class Complexity(ast.NodeVisitor):
    def __init__(self, root):
        self.root = root
        self.score = 1

    def visit_FunctionDef(self, node):
        if node is self.root:
            self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        if node is self.root:
            self.generic_visit(node)

    def visit_If(self, node):
        self.score += 1
        self.generic_visit(node)

    visit_For = visit_If
    visit_AsyncFor = visit_If
    visit_While = visit_If
    visit_IfExp = visit_If
    visit_Assert = visit_If

    def visit_Try(self, node):
        self.score += len(node.handlers)
        self.generic_visit(node)

    def visit_BoolOp(self, node):
        self.score += max(0, len(node.values) - 1)
        self.generic_visit(node)

    def visit_comprehension(self, node):
        self.score += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Match(self, node):
        self.score += sum(
            not (
                isinstance(case.pattern, ast.MatchAs)
                and case.pattern.name is None
                and case.pattern.pattern is None
            )
            for case in node.cases
        )
        self.generic_visit(node)


def complexity(node):
    visitor = Complexity(node)
    visitor.visit(node)
    return visitor.score


def nesting(node):
    branch_types = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)
    maximum = 0

    def walk(current, depth):
        nonlocal maximum
        if current is not node and isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        next_depth = depth + 1 if isinstance(current, branch_types) else depth
        maximum = max(maximum, next_depth)
        for child in ast.iter_child_nodes(current):
            walk(child, next_depth)

    walk(node, 0)
    return maximum


def params(node):
    return len(node.args.posonlyargs) + len(node.args.args) + len(node.args.kwonlyargs)


modules = []
callables = []
classes = []
for path in sorted(SOURCE.rglob("*.py")):
    relative = str(path.relative_to(ROOT))
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text)
    module_callables = []
    module_classes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            item = {
                "path": relative,
                "name": node.name,
                "kind": "function",
                "line": node.lineno,
                "end": node.end_lineno,
                "loc": node.end_lineno - node.lineno + 1,
                "cc": complexity(node),
                "nesting": nesting(node),
                "params": params(node),
                "returns": sum(isinstance(child, ast.Return) for child in ast.walk(node)),
            }
            callables.append(item)
            module_callables.append(item)
        elif isinstance(node, ast.ClassDef):
            methods = []
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    item = {
                        "path": relative,
                        "name": f"{node.name}.{method.name}",
                        "kind": "method",
                        "line": method.lineno,
                        "end": method.end_lineno,
                        "loc": method.end_lineno - method.lineno + 1,
                        "cc": complexity(method),
                        "nesting": nesting(method),
                        "params": params(method),
                        "returns": sum(isinstance(child, ast.Return) for child in ast.walk(method)),
                    }
                    methods.append(item)
                    callables.append(item)
                    module_callables.append(item)
            class_item = {
                "path": relative,
                "name": node.name,
                "line": node.lineno,
                "end": node.end_lineno,
                "loc": node.end_lineno - node.lineno + 1,
                "methods": len(methods),
                "cc_total": sum(item["cc"] for item in methods) or 1,
                "cc_max": max((item["cc"] for item in methods), default=0),
            }
            classes.append(class_item)
            module_classes.append(class_item)
    modules.append(
        {
            "path": relative,
            "loc": len(lines),
            "sloc": sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in lines),
            "functions": sum(item["kind"] == "function" for item in module_callables),
            "classes": len(module_classes),
            "cc_total": sum(item["cc"] for item in module_callables),
            "cc_avg": sum(item["cc"] for item in module_callables) / len(module_callables) if module_callables else 0,
            "cc_max": max((item["cc"] for item in module_callables), default=0),
        }
    )

hotspots = sorted((item for item in callables if item["cc"] >= 11), key=lambda item: (-item["cc"], -item["loc"]))
size_hotspots = sorted((item for item in callables if item["loc"] >= 75), key=lambda item: -item["loc"])
total_loc = sum(module["loc"] for module in modules)
total_sloc = sum(module["sloc"] for module in modules)
total_cc = sum(item["cc"] for item in callables)
avg_cc = total_cc / len(callables)
low_count = sum(item["cc"] <= 5 for item in callables)
moderate_count = sum(6 <= item["cc"] <= 10 for item in callables)
high_count = len(hotspots)
test_modules = list((ROOT / "tests").glob("test_*.py"))
test_loc = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in test_modules)
test_count = 0
for path in test_modules:
    test_count += sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    )
if not callables:
    parser.error(f"No Python functions or methods found below {SOURCE}")
commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
created = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
largest_module = max(modules, key=lambda item: item["loc"])
largest_share = largest_module["loc"] / total_loc
peak = max(callables, key=lambda item: item["cc"])
peak_label = rank(peak["cc"])[1].replace("-", " ")
class_hotspots = sum(item["cc_max"] >= 11 for item in classes)


def badge(cc):
    letter, label = rank(cc)
    return f'<span class="badge {label}">{letter} · {label.replace("-", " ")}</span>'


def module_rows():
    rows = []
    for module in sorted(modules, key=lambda item: (-item["cc_max"], -item["loc"])):
        rows.append(
            f"<tr><td><code>{html.escape(module['path'])}</code></td>"
            f"<td>{module['loc']:,}</td><td>{module['sloc']:,}</td><td>{module['functions']}</td>"
            f"<td>{module['classes']}</td><td>{module['cc_total']}</td><td>{module['cc_avg']:.1f}</td>"
            f"<td>{module['cc_max']}</td><td>{badge(module['cc_max']) if module['cc_max'] else '—'}</td></tr>"
        )
    return "".join(rows)


def callable_rows(items):
    rows = []
    for item in items:
        rows.append(
            f"<tr class={'hot-row' if item['cc'] >= 11 else ''}><td><code>{html.escape(item['name'])}</code>"
            f"<small>{item['kind']}</small></td><td><code>{html.escape(item['path'])}:{item['line']}–{item['end']}</code></td>"
            f"<td>{item['loc']}</td><td><strong>{item['cc']}</strong></td><td>{badge(item['cc'])}</td>"
            f"<td>{item['nesting']}</td><td>{item['params']}</td><td>{item['returns']}</td></tr>"
        )
    return "".join(rows)


def class_rows():
    if not classes:
        return '<tr><td colspan="7">No classes found.</td></tr>'
    return "".join(
        f"<tr><td><code>{html.escape(item['name'])}</code></td><td><code>{html.escape(item['path'])}:{item['line']}–{item['end']}</code></td>"
        f"<td>{item['loc']}</td><td>{item['methods']}</td><td>{item['cc_total']}</td><td>{item['cc_max']}</td>"
        f"<td>{badge(item['cc_max']) if item['cc_max'] else badge(1)}</td></tr>"
        for item in classes
    )


def size_rows():
    return "".join(
        f"<tr><td><code>{html.escape(item['name'])}</code></td><td><code>{html.escape(item['path'])}:{item['line']}–{item['end']}</code></td>"
        f"<td>{item['loc']}</td><td>{item['cc']}</td><td>{badge(item['cc'])}</td></tr>"
        for item in size_hotspots
    )


def review_items():
    items = hotspots[:4]
    if not items:
        return "<li>No high-complexity callable needs priority review.</li>"
    return "".join(
        f"<li><strong><code>{html.escape(item['name'])}</code></strong> — CC {item['cc']}, "
        f"{item['loc']} LOC at <code>{html.escape(item['path'])}:{item['line']}</code>. "
        "Reduce branches along its existing execution paths before introducing new abstractions.</li>"
        for item in items
    )


def linear_size_items():
    items = [item for item in size_hotspots if item["cc"] <= 5][:4]
    if not items:
        return "<li>No large, linear routine was detected.</li>"
    return "".join(
        f"<li><code>{html.escape(item['name'])}</code>: {item['loc']} LOC but CC {item['cc']}; "
        "size alone does not justify refactoring.</li>"
        for item in items
    )


report = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sbx source complexity report</title>
<link rel="icon" href="assets/sbx-icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="styles.css">
<style>
.report-art .logo-card {{ top: 72px; }}
.meta {{ color: var(--stone); font-size: 13px; }}
.report-cards {{ grid-template-columns: repeat(4, 1fr); }}
.report-card .number {{ font-size: 35px; font-weight: 900; line-height: 1; }}
.report-card p {{ margin-top: 8px; }}
.summary {{ display: grid; grid-template-columns: 1.4fr 1fr; gap: 18px; }}
.panel {{ padding: 22px; }}
.callout {{ border-left: 10px solid var(--red); }}
.good {{ border-left: 10px solid var(--green); }}
.table-wrap {{ overflow: auto; border: 4px solid var(--ink); box-shadow: 6px 6px 0 var(--shadow); background: var(--paper-2); }}
table {{ width: 100%; border-collapse: collapse; min-width: 850px; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid color-mix(in srgb, var(--stone) 28%, transparent); vertical-align: top; }}
th {{ position: sticky; top: 0; background: var(--ink); color: var(--paper-2); font-size: 12px; text-transform: uppercase; }}
td small {{ display: block; color: var(--stone); }}
tbody tr:hover {{ background: color-mix(in srgb, var(--cyan) 18%, transparent); }}
.hot-row {{ background: color-mix(in srgb, var(--red) 10%, transparent); }}
.badge {{ display: inline-block; padding: 2px 7px; border: 2px solid currentColor; font-size: 11px; font-weight: 900; text-transform: uppercase; white-space: nowrap; }}
.low {{ color: var(--green); }}
.moderate {{ color: #a56b00; }}
.high, .very-high, .severe, .critical {{ color: var(--red); }}
.bar {{ display: flex; height: 24px; border: 3px solid var(--ink); margin: 14px 0; }}
.bar span {{ display: grid; place-items: center; color: white; font-size: 10px; font-weight: 900; min-width: 30px; }}
.bar .a {{ background: #5d8f64; }}
.bar .b {{ background: #c08a28; }}
.bar .c {{ background: #bf616a; }}
ol li {{ margin: 10px 0; }}
details {{ background: var(--paper-2); border: 4px solid var(--ink); box-shadow: 6px 6px 0 var(--shadow); }}
summary {{ cursor: pointer; padding: 15px 18px; font-weight: 900; }}
details .table-wrap {{ border: 0; box-shadow: none; }}
.legend {{ display: flex; gap: 10px; flex-wrap: wrap; }}
@media (max-width: 900px) {{ .report-cards, .summary {{ grid-template-columns: 1fr 1fr; }} }}
@media (max-width: 600px) {{ .report-cards, .summary {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<input class="theme-toggle" id="theme-toggle" type="checkbox" aria-label="Use dark dungeon theme">
<header class="site-header"><nav class="nav" aria-label="Main navigation"><a class="brand" href="index.html" aria-label="sbx home"><img src="assets/sbx-icon.svg" alt="" width="40" height="40"><span>sbx</span></a><div class="nav-links"><a href="index.html">home</a><a href="#overview">overview</a><a href="#modules">modules</a><a href="#hotspots">hotspots</a><a href="#inventory">inventory</a><a href="https://github.com/nueces/sbx">github</a><label class="theme-label" for="theme-toggle"><span class="light-label">dark</span><span class="dark-label">light</span></label></div></nav></header>
<section class="hero" id="top"><div class="hero-copy pixel-frame"><span class="eyebrow">STATIC ANALYSIS · PYTHON PRODUCTION SOURCE</span><h1>Complexity report</h1><p class="subtitle">The codebase is mostly made of small, low-complexity functions, but risk is concentrated in a few execution paths. <code>{html.escape(largest_module['path'])}</code> contains {largest_share:.1%} of production LOC; the peak is <code>{html.escape(peak['name'])}</code> at CC {peak['cc']}.</p><p class="meta">Snapshot <code>{commit[:12]}</code> · generated {created} · scope <code>src/**/*.py</code></p></div><div class="hero-art report-art pixel-frame" aria-label="sbx low-fi pouch logo"><div class="logo-card"><img src="assets/sbx-logo.svg" alt="sbx low-fi pouch logo"></div><div class="dungeon" aria-hidden="true"><span class="brick b1"></span><span class="brick b2"></span><span class="brick b3"></span><span class="brick b4"></span><span class="door"></span><span class="torch"></span></div></div></section>
<main>
<section id="overview"><h2>General health</h2><div class="cards report-cards">
<div class="card report-card pixel-frame"><div class="number">{total_loc:,}</div><p>physical source lines<br><strong>{total_sloc:,}</strong> nonblank/non-comment</p></div>
<div class="card report-card pixel-frame"><div class="number">{len(callables)}</div><p>functions + methods<br><strong>{len(classes)}</strong> classes</p></div>
<div class="card report-card pixel-frame"><div class="number">{avg_cc:.1f}</div><p>mean cyclomatic complexity<br><strong>{total_cc}</strong> aggregate decisions</p></div>
<div class="card report-card pixel-frame"><div class="number">{high_count}</div><p>high-or-higher callables<br><strong>{high_count/len(callables):.1%}</strong> of callables</p></div>
</div></section>
<section><div class="summary"><article class="panel pixel-frame callout"><h3>Assessment: {peak_label} peak</h3><p>Most callables are easy to reason about, but the maximum CC of <strong>{peak['cc']}</strong> in <code>{html.escape(peak['name'])}</code> makes change risk uneven. Review high-complexity routines before adding more paths.</p><div class="bar" aria-label="Complexity distribution"><span class="a" style="width:{low_count/len(callables):.1%}">A {low_count}</span><span class="b" style="width:{moderate_count/len(callables):.1%}">B {moderate_count}</span><span class="c" style="width:{high_count/len(callables):.1%}">C+ {high_count}</span></div><p class="meta">{low_count} low (A), {moderate_count} moderate (B), {high_count} high or above (C–F).</p></article>
<article class="panel pixel-frame good"><h3>Testability signal</h3><p><strong>{test_count} test functions</strong> across {len(test_modules)} test modules and <strong>{test_loc:,} test LOC</strong> ({test_loc/total_loc:.2f}× production LOC). This is a useful safety net, but runtime coverage was not re-measured for this static report, so no coverage percentage is claimed.</p></article></div></section>
<section id="modules"><h2>Complexity by module</h2><div class="table-wrap"><table><thead><tr><th>Module</th><th>LOC</th><th>SLOC</th><th>Functions</th><th>Classes</th><th>Total CC</th><th>Mean CC</th><th>Max CC</th><th>Max rank</th></tr></thead><tbody>{module_rows()}</tbody></table></div><p class="meta">Total CC is useful for concentration, not a quality grade: larger modules naturally accumulate more paths. Max and mean CC should be read together.</p></section>
<section id="hotspots"><h2>High and superior complexity</h2><p class="subtitle">Every callable at CC ≥ 11 is listed. These are the routines most likely to require extra review and focused tests when changed.</p><div class="table-wrap"><table><thead><tr><th>Callable</th><th>Source lines</th><th>LOC</th><th>CC</th><th>Rank</th><th>Max nesting</th><th>Parameters</th><th>Returns</th></tr></thead><tbody>{callable_rows(hotspots)}</tbody></table></div></section>
<section><h2>Size hotspots</h2><p class="subtitle">Long routines are shown separately because generated strings and parser declarations can be large while having few branches.</p><div class="table-wrap"><table><thead><tr><th>Callable</th><th>Source lines</th><th>LOC</th><th>CC</th><th>Rank</th></tr></thead><tbody>{size_rows()}</tbody></table></div></section>
<section><h2>Classes</h2><div class="table-wrap"><table><thead><tr><th>Class</th><th>Source lines</th><th>LOC</th><th>Methods</th><th>Method CC total</th><th>Max method CC</th><th>Rank</th></tr></thead><tbody>{class_rows()}</tbody></table></div><p class="meta">{class_hotspots} classes have a high-complexity method.</p></section>
<section><h2>Priority actions</h2><div class="summary"><article class="panel pixel-frame"><h3>Review first</h3><ol>{review_items()}</ol></article><article class="panel pixel-frame"><h3>Do not optimize for size alone</h3><ul>{linear_size_items()}</ul></article></div></section>
<section id="inventory"><h2>Complete callable inventory</h2><details><summary>Show all {len(callables)} functions and methods</summary><div class="table-wrap"><table><thead><tr><th>Callable</th><th>Source lines</th><th>LOC</th><th>CC</th><th>Rank</th><th>Max nesting</th><th>Parameters</th><th>Returns</th></tr></thead><tbody>{callable_rows(sorted(callables, key=lambda item: (item['path'], item['line'])))}</tbody></table></div></details></section>
<section><h2>Methodology</h2><div class="panel pixel-frame"><p><strong>Cyclomatic complexity (CC)</strong> starts at 1 and adds paths for branches, loops, exception handlers, boolean branches, assertions, comprehensions, ternaries, and match cases. Ratings follow common McCabe bands:</p><div class="legend">{badge(1)} {badge(6)} {badge(11)} {badge(21)} {badge(31)} {badge(41)}</div><p><strong>LOC</strong> spans the AST definition from first to last source line. <strong>SLOC</strong> excludes blank and comment-only lines. <strong>Max nesting</strong> counts nested control structures. Parameters include positional and keyword-only arguments; returns count explicit return statements.</p><p class="meta">This is deterministic static analysis, not a runtime profile. CC estimates branching difficulty; it does not measure correctness, coupling, duplication, performance, or test coverage. Line references apply only to commit <code>{commit}</code>.</p></div></section>
<footer class="footer"><div><strong>sbx</strong><br>source complexity report</div><div><a href="https://github.com/nueces/sbx">github.com/nueces/sbx</a><br>License: Apache-2.0</div></footer>
</main>
</body></html>"""
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(report, encoding="utf-8")
print(OUTPUT)
print(f"{len(report):,} bytes; {len(hotspots)} hotspots; {len(callables)} callables")
