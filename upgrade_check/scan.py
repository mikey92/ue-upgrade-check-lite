"""Engine upgrade checks that need no editor: C++ APIs deprecated or removed in the target engine,
headers that are gone, and plugins the target engine does not have.

Everything here is plain Python (no `unreal` import), so Tools/upgrade_check.py runs it from a
terminal or CI and the editor menu runs the same code.
"""

import gzip
import json
import os
import re
import sys

CACHE_FORMAT = 2
ENGINE_DIRS = ("Source", "Plugins")
SKIP_DIRS = {"ThirdParty", "Intermediate", "Binaries", "Content", "Resources", "Saved", "DerivedDataCache", ".git"}
PROJECT_SOURCE_EXT = (".h", ".hpp", ".cpp", ".inl", ".cc")

DEPRECATED = re.compile(r'\bUE_DEPRECATED(?:_FORGAME)?\s*\(\s*([0-9]+\.[0-9]+)\s*,\s*"((?:[^"\\]|\\.)*)"[^)]*\)')
DEPRECATED_HEADER = re.compile(r'\bUE_DEPRECATED_HEADER\s*\(\s*([0-9]+\.[0-9]+)\s*,\s*"((?:[^"\\]|\\.)*)"')
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
IDENT_BYTES = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{2,}")
TYPE_DECL = re.compile(r"^\s*(?:enum\s+)?(?:class|struct)\s+(?:alignas\s*\([^)]*\)\s*)?(?:UE_DEPRECATED\s*\([^)]*\)\s*)?(?:[A-Z0-9_]+_API\s+)?([A-Za-z_]\w*)\b[^;]*$")
INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^">]+)[">]')
# `Type Name =`, `Type* Name;`, `Type& Name)`: a variable or parameter the file declares itself.
LOCAL_DECL = re.compile(r"\b(?:auto|bool|int8|int16|int32|int64|uint8|uint16|uint32|uint64|float|double|[A-Z]\w*(?:<[^;()]*?>)?)\s*[*&]*\s+([A-Za-z_]\w*)\s*(?=[=;,):\[{])")
SELF = "UpgradeCheck"
STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')
SKIP_DECL_LINE = re.compile(r"^\s*(?:U(?:PROPERTY|FUNCTION|ENUM|STRUCT|CLASS|META|INTERFACE|PARAM)\b|UE_|template\s*<|\[\[|//|/\*|\*|#|PRAGMA_|GENERATED_|$)")
TYPE_KEYWORDS = ("class", "struct", "enum", "union", "namespace")
NOT_SYMBOLS = {
    "const", "constexpr", "consteval", "static", "virtual", "inline", "FORCEINLINE", "FORCENOINLINE", "explicit",
    "override", "final", "void", "bool", "int", "float", "double", "char", "auto", "unsigned", "signed", "long",
    "short", "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64", "return", "typename",
    "template", "operator", "public", "private", "protected", "friend", "mutable", "volatile", "extern",
    "noexcept", "default", "delete", "using", "typedef", "class", "struct", "enum", "union", "namespace",
    "TEXT", "UMETA", "true", "false", "nullptr", "this", "decltype", "sizeof", "FString", "FName", "FText",
}


# ---------------------------------------------------------------------------------------------
# Engines


def engine_version(engine_dir):
    """'5.8' for an engine folder (the one that contains Engine/), or None."""
    path = os.path.join(engine_dir, "Engine", "Build", "Build.version")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return f"{data.get('MajorVersion')}.{data.get('MinorVersion')}"


def engine_changelist(engine_dir):
    try:
        with open(os.path.join(engine_dir, "Engine", "Build", "Build.version"), encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    return int(data.get("Changelist") or 0)


def installed_engines():
    """Launcher-installed engines as {'5.8': '/Users/Shared/Epic Games/UE_5.8', ...}."""
    candidates = []
    if sys.platform == "darwin":
        candidates.append(os.path.expanduser("~/Library/Application Support/Epic/UnrealEngineLauncher/LauncherInstalled.dat"))
    elif os.name == "nt":
        candidates.append(os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat"))
    else:
        candidates.append(os.path.expanduser("~/.config/Epic/UnrealEngineLauncher/LauncherInstalled.dat"))
    found = {}
    for path in candidates:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                items = json.load(fh).get("InstallationList", [])
        except (OSError, ValueError):
            continue
        for item in items:
            if not str(item.get("AppName", "")).startswith("UE_"):
                continue
            location = item.get("InstallLocation", "")
            version = engine_version(location)
            if version:
                found[version] = location
    return dict(sorted(found.items(), key=lambda kv: version_key(kv[0])))


def version_key(version):
    try:
        return tuple(int(p) for p in str(version).split("."))
    except ValueError:
        return (0,)


# ---------------------------------------------------------------------------------------------
# Engine index: every UE_DEPRECATED declaration, deprecated headers, header names, and how many
# headers mention each identifier (to tell a removed API from one that only moved).


def _walk_headers(engine_dir):
    for sub in ENGINE_DIRS:
        base = os.path.join(engine_dir, "Engine", sub)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                if name.endswith(".h"):
                    yield os.path.join(dirpath, name)


def _owners(lines):
    """For every line, the innermost class or struct whose body holds it ('' outside any type).

    A forward pass over braces with comments, strings and character literals removed; a type
    declaration line marks the next '{' as that type's body.
    """
    owners = []
    stack = []  # one entry per open brace: the type name, or None for any other block
    pending = None
    in_block = False
    for raw in lines:
        owners.append(next((name for name in reversed(stack) if name), ""))
        line = raw
        if in_block:
            end = line.find("*/")
            if end < 0:
                continue
            line = line[end + 2:]
            in_block = False
        line = re.sub(r"/\*.*?\*/", "", line)
        if "/*" in line:
            line = line[: line.find("/*")]
            in_block = True
        line = line.split("//", 1)[0]
        line = re.sub(r"'(?:[^'\\]|\\.)'", "", STRING.sub("", line))
        if line.lstrip().startswith("#"):
            continue
        m = TYPE_DECL.match(line)
        if m and m.group(1) not in NOT_SYMBOLS and not m.group(1).endswith("_API"):
            pending = m.group(1)
        for ch in line:
            if ch == "{":
                stack.append(pending)
                pending = None
            elif ch == "}":
                if stack:
                    stack.pop()
            elif ch == ";" and pending and not stack[-1:] == [pending]:
                pending = None  # forward declaration
    return owners


def _strip_defaults(text):
    """Drop default arguments and initializers so the last identifier is the declared name."""
    depth = 0
    out = []
    for ch in text:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth = max(0, depth - 1)
        if ch == "=" and depth == 0:
            break
        out.append(ch)
    return "".join(out)


def declared_symbol(before, after, following):
    """Name declared by the UE_DEPRECATED on one line.

    `before` is the text before the macro on its line, `after` the text after it, `following` the
    next lines. Returns (symbol, kind) with kind in function, type, field, enum value, alias; or
    (None, None) when the declaration cannot be read (operators, macros).
    """
    before_tokens = IDENT.findall(before)
    stripped_before = before.strip()
    # `namespace UE_DEPRECATED(...) Old {}`: too generic to match usages reliably
    if before_tokens and before_tokens[-1] == "namespace":
        return None, None
    # `class UE_DEPRECATED(...) FOld`, `enum class UE_DEPRECATED(...) EOld`
    if before_tokens and before_tokens[-1] in TYPE_KEYWORDS:
        for tok in IDENT.findall(after):
            if tok.endswith("_API") or tok in NOT_SYMBOLS:
                continue
            return tok, "type"
        return None, None
    # `using FOld UE_DEPRECATED(5.5, "...") = FNew;`
    if len(before_tokens) == 2 and before_tokens[0] == "using":
        return before_tokens[1], "alias"
    # Enum value: `OldValue UE_DEPRECATED(5.6, "...") = 1,` (also inside a one-line enum body)
    last = re.split(r"[{,]", stripped_before)[-1].strip()
    if re.fullmatch(r"[A-Za-z_]\w*", last) and (last == stripped_before or "{" in stripped_before or "," in stripped_before):
        return last, "enum value"
    # Declaration on the same line or the next lines.
    parts = [after]
    for line in following:
        if not after.strip() and len(parts) == 1 and SKIP_DECL_LINE.match(line):
            continue
        if len(parts) > 1 and SKIP_DECL_LINE.match(line) and not line.strip().startswith("*"):
            if line.strip().startswith(("UPROPERTY", "UFUNCTION", "UE_")):
                break
        parts.append(line)
        joined = " ".join(parts)
        if ";" in joined or "{" in joined:
            break
        if len(parts) > 8:
            break
    decl = " ".join(p.strip() for p in parts)
    decl = re.split(r"[;{]", decl, maxsplit=1)[0]
    if not decl.strip():
        return None, None
    m = re.match(r"\s*(DECLARE_\w*DELEGATE\w*|DECLARE_\w*EVENT\w*)\s*\(\s*([A-Za-z_]\w*)", decl)
    if m:
        return m.group(2), "type"
    m = re.match(r"\s*(?:class|struct|enum(?:\s+class)?)\s+(.*)$", decl)
    if m and "*" not in decl and "&" not in decl:
        # A type declaration (`struct FOld : public FBase`), not an elaborated field (`class UFoo* Bar;`).
        names = [t for t in IDENT.findall(re.split(r"(?<!:):(?!:)", m.group(1), maxsplit=1)[0])
                 if not t.endswith("_API") and t not in NOT_SYMBOLS and t != "final"]
        if len(names) == 1:
            return names[0], "type"
    m = re.match(r"\s*using\s+([A-Za-z_]\w*)\s*=", decl)
    if m:
        return m.group(1), "alias"
    m = re.match(r"\s*typedef\s+.*?\b([A-Za-z_]\w*)\s*$", decl)
    if m:
        return m.group(1), "alias"
    if "operator" in IDENT.findall(decl):
        return None, None
    paren = _first_call_paren(decl)
    if paren is not None:
        tokens = IDENT.findall(decl[:paren])
        while tokens and tokens[-1] in NOT_SYMBOLS:
            tokens.pop()
        if not tokens:
            return None, None
        name = tokens[-1]
        return (name, "function") if not name.isupper() else (None, None)
    head = _strip_defaults(decl)
    head = re.split(r"\[|(?<!:):(?!:)", head, maxsplit=1)[0]
    tokens = [t for t in IDENT.findall(head) if t not in NOT_SYMBOLS]
    if not tokens:
        return None, None
    return tokens[-1], "field"


def _first_call_paren(decl):
    """Index of the '(' that opens a parameter list, skipping template arguments."""
    depth = 0
    for i, ch in enumerate(decl):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif ch == "(" and depth == 0:
            prefix = decl[:i].rstrip()
            if prefix and (prefix[-1].isalnum() or prefix[-1] == "_"):
                return i
        elif ch == "=" and depth == 0:
            return None
    return None


def parse_header(text, rel_path):
    """UE_DEPRECATED declarations in one header as dicts."""
    out = []
    if "UE_DEPRECATED" not in text:
        return out, None
    lines = text.splitlines()
    owners = None
    header = None
    for index, line in enumerate(lines):
        if "UE_DEPRECATED" not in line:
            continue
        hm = DEPRECATED_HEADER.search(line)
        if hm and not line.lstrip().startswith(("#define", "//")):
            header = {"version": hm.group(1), "message": _unescape(hm.group(2))}
            continue
        if line.lstrip().startswith(("#define", "//", "*", "/*")):
            continue
        for m in DEPRECATED.finditer(line):
            symbol, kind = declared_symbol(line[: m.start()], line[m.end():], lines[index + 1: index + 12])
            if not symbol or len(symbol) < 3:
                continue
            if owners is None:
                owners = _owners(lines)
            if kind == "function" and symbol == owners[index]:
                continue  # a deprecated constructor: the type itself is still fine
            out.append({
                "symbol": symbol,
                "kind": kind,
                "owner": owners[index] if kind in ("function", "field", "enum value") else "",
                "version": m.group(1),
                "message": _unescape(m.group(2)),
                "header": rel_path,
                "line": index + 1,
            })
    return out, header


def _unescape(message):
    return message.replace('\\"', '"').replace("\\\\", "\\")


def build_index(engine_dir, progress=None):
    """Scan an engine's headers. Takes 10-30 s; use load_index to cache it."""
    deprecations = []
    headers = {}
    deprecated_headers = {}
    counts = {}
    engine_root = os.path.join(engine_dir, "Engine")
    for n, path in enumerate(_walk_headers(engine_dir)):
        if progress and n % 5000 == 0:
            progress(n)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        rel = os.path.relpath(path, engine_root).replace(os.sep, "/")
        headers.setdefault(os.path.basename(path), rel)
        for tok in set(IDENT_BYTES.findall(data)):
            counts[tok] = counts.get(tok, 0) + 1
        if b"UE_DEPRECATED" in data:
            found, header = parse_header(data.decode("utf-8", "replace"), rel)
            deprecations.extend(found)
            if header:
                deprecated_headers[os.path.basename(path)] = dict(header, header=rel)
    return {
        "format": CACHE_FORMAT,
        "engine": engine_version(engine_dir),
        "changelist": engine_changelist(engine_dir),
        "deprecations": deprecations,
        "deprecated_headers": deprecated_headers,
        "headers": headers,
        "counts": {k.decode("ascii", "replace"): v for k, v in counts.items()},
    }


def load_index(engine_dir, cache_dir=None, progress=None):
    """build_index, cached as cache_dir/engine-<version>-<changelist>.json.gz."""
    version = engine_version(engine_dir)
    if not version:
        raise ValueError(f"not an Unreal Engine folder (no Engine/Build/Build.version): {engine_dir}")
    cache_path = None
    if cache_dir:
        cache_path = os.path.join(cache_dir, f"engine-{version}-{engine_changelist(engine_dir)}.json.gz")
        try:
            with gzip.open(cache_path, "rt", encoding="utf-8") as fh:
                index = json.load(fh)
            if index.get("format") == CACHE_FORMAT:
                return index
        except (OSError, ValueError, EOFError):
            pass
    index = build_index(engine_dir, progress)
    if cache_path:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = cache_path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
            json.dump(index, fh, separators=(",", ":"))
        os.replace(tmp, cache_path)
    return index


# ---------------------------------------------------------------------------------------------
# Project


def project_source_files(project_dir):
    """C++ files of the project and of its project plugins."""
    roots = [os.path.join(project_dir, "Source")]
    plugins = os.path.join(project_dir, "Plugins")
    for dirpath, dirnames, filenames in os.walk(plugins):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if f"{SELF}.uplugin" in filenames:
            dirnames[:] = []
            continue
        if "Source" in dirnames and any(f.endswith(".uplugin") for f in filenames):
            roots.append(os.path.join(dirpath, "Source"))
            dirnames.remove("Source")
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in sorted(filenames):
                if name.endswith(PROJECT_SOURCE_EXT):
                    yield os.path.join(dirpath, name)


def _code_lines(text):
    """(line number, code with comments and string literals removed, raw line)."""
    in_block = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw
        out = []
        i = 0
        while i < len(line):
            if in_block:
                end = line.find("*/", i)
                if end < 0:
                    i = len(line)
                    break
                in_block = False
                i = end + 2
                continue
            start_block = line.find("/*", i)
            start_line = line.find("//", i)
            cut = min([p for p in (start_block, start_line) if p >= 0], default=-1)
            if cut < 0:
                out.append(line[i:])
                break
            out.append(line[i:cut])
            if cut == start_line:
                break
            in_block = True
            i = cut + 2
        yield number, STRING.sub('""', "".join(out)), raw


def _usage_owner_hint(code, symbol):
    """`UFoo::Symbol` or `FFoo::Symbol` written in the code, else ''."""
    m = re.search(r"\b([A-Za-z_]\w*)\s*::\s*" + re.escape(symbol) + r"\b", code)
    return m.group(1) if m else ""


def check_cpp(project_dir, source_index, target_index, include_existing=False):
    """Findings for the project's C++ against the target engine.

    severity:
      removed      deprecated in the current engine and no longer declared in the target: will not compile
      deprecated   newly deprecated in the target (after the current engine version): a warning after upgrading
      existing     deprecated in both (only with include_existing): a warning today already
      header       an #include the target engine no longer has, or a deprecated header
    """
    source_version = source_index.get("engine") or "0.0"
    target_deps = {}
    for d in target_index["deprecations"]:
        target_deps.setdefault(d["symbol"], []).append(d)
    source_deps = {}
    for d in source_index["deprecations"]:
        source_deps.setdefault(d["symbol"], []).append(d)
    target_counts = target_index["counts"]
    source_counts = source_index["counts"]
    target_headers = target_index["headers"]
    source_headers = source_index["headers"]
    target_deprecated_headers = target_index.get("deprecated_headers", {})

    findings = []
    seen = set()
    for path in project_source_files(project_dir):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        rel = os.path.relpath(path, project_dir).replace(os.sep, "/")
        lines = list(_code_lines(text))
        declared = set(LOCAL_DECL.findall("\n".join(code for _, code, _ in lines)))
        for number, code, raw in lines:
            inc = INCLUDE.match(raw)
            if inc:
                name = os.path.basename(inc.group(1))
                if name in source_headers and name not in target_headers:
                    findings.append(_finding("header", rel, number, raw, name, "", source_index["engine"], f"{name} is not in UE {target_index['engine']}", source_headers[name], "high"))
                elif name in target_deprecated_headers:
                    dh = target_deprecated_headers[name]
                    findings.append(_finding("header", rel, number, raw, name, "", dh["version"], dh["message"], dh["header"], "high"))
                continue
            for tok in set(IDENT.findall(code)):
                if tok not in target_deps and tok not in source_deps:
                    continue
                if tok in NOT_SYMBOLS or (tok, rel, number) in seen:
                    continue
                hint = _usage_owner_hint(code, tok)
                result = _classify(tok, hint, source_deps.get(tok, []), target_deps.get(tok, []), source_version,
                                   target_counts, source_counts, include_existing)
                if not result:
                    continue
                severity, dep, confidence = result
                if tok in declared and dep.get("kind") in ("field", "enum value") and not hint:
                    continue  # the file's own variable that happens to share the engine field's name
                seen.add((tok, rel, number))
                findings.append(_finding(severity, rel, number, raw, tok, dep.get("owner", ""), dep["version"],
                                         dep["message"], f"{dep['header']}:{dep['line']}", confidence))
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["confidence"] != "high", f["file"], f["line"]))
    return findings


SEVERITY_ORDER = {"removed": 0, "header": 1, "deprecated": 2, "existing": 3}
# A name declared in more engine headers than this (Initialize, Parameters, Padding...) is only
# reported when the code names its type (`UFoo::Name`); otherwise it is mostly noise.
GENERIC_NAME_HEADERS = 40


def _pick(deps, hint):
    if hint:
        for d in deps:
            if d.get("owner") == hint:
                return d, True
    return (deps[0], False) if deps else (None, False)


def _classify(symbol, hint, source_deps, target_deps, source_version, target_counts, source_counts, include_existing):
    """(severity, deprecation, confidence) for one identifier the project uses, or None."""
    if target_deps:
        dep, owner_matched = _pick(target_deps, hint)
        if hint and not owner_matched and not any(d.get("owner") == hint for d in source_deps):
            return None  # `FOther::Symbol` where the deprecated one belongs to a different type
        new = version_key(dep["version"]) > version_key(source_version)
        if not new and not include_existing:
            return None
        if not owner_matched and target_counts.get(symbol, 0) > GENERIC_NAME_HEADERS:
            return None
        confidence = "high" if owner_matched or target_counts.get(symbol, 0) <= 3 else "check"
        return ("deprecated" if new else "existing"), dep, confidence
    if source_deps:
        dep, owner_matched = _pick(source_deps, hint)
        if hint and not owner_matched:
            return None
        if target_counts.get(symbol, 0) > 0:
            return None  # still declared somewhere in the target (moved, or the deprecation was lifted)
        if not owner_matched and source_counts.get(symbol, 0) > GENERIC_NAME_HEADERS:
            return None
        confidence = "high" if owner_matched or source_counts.get(symbol, 0) <= 3 else "check"
        return "removed", dep, confidence
    return None


def _finding(severity, rel, line, raw, symbol, owner, version, message, where, confidence):
    return {
        "severity": severity,
        "file": rel,
        "line": line,
        "code": raw.strip()[:200],
        "symbol": f"{owner}::{symbol}" if owner else symbol,
        "deprecated_in": version,
        "message": message,
        "engine_declaration": where,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------------------------
# Plugins


def _uplugins(base):
    found = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") and d != "Source"]
        for name in filenames:
            if name.endswith(".uplugin"):
                found.setdefault(name[:-8], os.path.join(dirpath, name))
    return found


def _engine_module(engine_dir, name):
    """'Source/Runtime/MassEntity' when the engine has a module folder with that name, else ''."""
    for group in ("Runtime", "Editor", "Developer"):
        rel = os.path.join("Source", group, name)
        if os.path.isdir(os.path.join(engine_dir, "Engine", rel)):
            return rel.replace(os.sep, "/")
    return ""


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def check_plugins(project_file, source_engine, target_engine):
    """Every plugin the .uproject enables, and every project plugin, against the target engine.

    status: ok, removed (not in the target engine), moved (now an engine module, not a plugin),
    fab (installed to the current engine from Fab,
    not installed to the target), rebuild (project plugin with C++ but no source to rebuild, or built
    for another engine version), deprecated (the target marks it deprecated), beta/experimental.
    """
    project_dir = os.path.dirname(project_file)
    uproject = _read_json(project_file)
    project_plugins = _uplugins(os.path.join(project_dir, "Plugins"))
    source_plugins = _uplugins(os.path.join(source_engine, "Engine", "Plugins"))
    target_plugins = _uplugins(os.path.join(target_engine, "Engine", "Plugins"))
    target_version = engine_version(target_engine)
    rows = []
    enabled = [p for p in uproject.get("Plugins", []) if p.get("Enabled")]
    names = [p["Name"] for p in enabled if p.get("Name")]
    names += sorted(n for n in project_plugins if n not in names)
    for name in names:
        if name == SELF:
            continue  # this tool; it is not needed in the target engine
        row = {"plugin": name, "enabled": name in [p.get("Name") for p in enabled], "where": "", "status": "ok", "note": ""}
        if name in project_plugins:
            row["where"] = "project"
            desc = _read_json(project_plugins[name])
            folder = os.path.dirname(project_plugins[name])
            has_modules = bool(desc.get("Modules"))
            has_source = os.path.isdir(os.path.join(folder, "Source"))
            built_for = ".".join(str(desc.get("EngineVersion", "")).split(".")[:2])
            if has_modules and not has_source:
                row.update(status="rebuild", note="has C++ modules but no Source folder: you need a build for UE " + target_version + " from its author")
            elif has_modules and built_for and built_for != target_version:
                row.update(status="recompile", note=f"built for UE {desc.get('EngineVersion')}; it recompiles from source in UE {target_version}. Check the C++ findings for its files")
            for dep in desc.get("Plugins", []):
                if dep.get("Enabled") and not dep.get("Optional") and dep.get("Name") not in target_plugins and dep.get("Name") not in project_plugins:
                    row.update(status="removed", note=f"depends on {dep.get('Name')}, which UE {target_version} does not have")
            rows.append(row)
            continue
        if name in target_plugins:
            desc = _read_json(target_plugins[name])
            row["where"] = "engine"
            text = str(desc.get("Description", ""))
            if re.match(r"\s*deprecated\b", text, re.I) or desc.get("IsDeprecated"):
                row.update(status="deprecated", note=text.strip()[:200])
            elif desc.get("IsExperimentalVersion"):
                row.update(status="experimental", note=f"experimental in UE {target_version}")
            elif desc.get("IsBetaVersion"):
                row.update(status="beta", note=f"beta in UE {target_version}")
            if name in source_plugins and "/Marketplace/" in source_plugins[name].replace(os.sep, "/"):
                row["where"] = "fab"
            rows.append(row)
            continue
        if name in source_plugins:
            fab = "/Marketplace/" in source_plugins[name].replace(os.sep, "/")
            row["where"] = "fab" if fab else "engine"
            module = _engine_module(target_engine, name)
            if fab:
                row.update(status="fab", note=f"installed from Fab to this engine only: install the UE {target_version} version from your Fab library (if the seller offers one)")
            elif module:
                row.update(status="moved", note=f"no longer a plugin in UE {target_version}: it is part of the engine ({module}). Remove it from the .uproject Plugins list")
            else:
                row.update(status="removed", note=f"UE {target_version} does not include it")
        else:
            row.update(where="missing", status="removed", note="not found in the project or either engine")
        rows.append(row)
    order = {"removed": 0, "fab": 1, "moved": 2, "rebuild": 3, "deprecated": 4, "recompile": 5, "experimental": 6, "beta": 7, "ok": 8}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["plugin"].lower()))
    return rows


# ---------------------------------------------------------------------------------------------
# Whole check


def run(project_file, source_engine, target_engine, cache_dir=None, include_existing=False, progress=None, blueprint_findings=None):
    """The full report as a dict (Blueprint findings come from the editor and are passed in)."""
    project_file = os.path.abspath(project_file)
    project_dir = os.path.dirname(project_file)
    if cache_dir is None:
        cache_dir = os.path.join(project_dir, "Saved", "UpgradeCheck", "cache")
    source_index = load_index(source_engine, cache_dir, progress)
    target_index = load_index(target_engine, cache_dir, progress)
    cpp = check_cpp(project_dir, source_index, target_index, include_existing)
    plugins = check_plugins(project_file, source_engine, target_engine)
    blueprints = blueprint_findings or []
    report = {
        "project": os.path.splitext(os.path.basename(project_file))[0],
        "from": source_index["engine"],
        "to": target_index["engine"],
        "source_engine": source_engine,
        "target_engine": target_engine,
        "cpp": cpp,
        "plugins": plugins,
        "blueprints": blueprints,
        "blueprints_checked": blueprint_findings is not None,
    }
    report["summary"] = summarize(report)
    return report


BLOCKING_PLUGIN_STATUS = ("removed", "fab", "moved", "rebuild")


def summarize(report):
    cpp = report["cpp"]
    plugins = report["plugins"]
    bps = report["blueprints"]
    return {
        "will_not_compile": sum(1 for f in cpp if f["severity"] in ("removed", "header") and f["confidence"] == "high"),
        "to_check": sum(1 for f in cpp if f["confidence"] != "high"),
        "new_warnings": sum(1 for f in cpp if f["severity"] == "deprecated"),
        "plugins_blocking": sum(1 for p in plugins if p["status"] in BLOCKING_PLUGIN_STATUS),
        "plugins_flagged": sum(1 for p in plugins if p["status"] in ("deprecated", "recompile", "experimental", "beta")),
        "blueprint_calls": len(bps),
        "blueprint_removed": sum(1 for b in bps if b.get("severity") == "removed"),
    }


def target_for(source_engine, target):
    """A target given as '5.8' or as a folder."""
    if target and os.path.isdir(target):
        return target
    engines = installed_engines()
    if target in engines:
        return engines[target]
    raise ValueError(f"UE {target} is not installed (installed: {', '.join(engines) or 'none found'}); pass the engine folder instead")
