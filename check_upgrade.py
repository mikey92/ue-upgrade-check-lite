"""Upgrade Check Lite: what breaks when an Unreal project moves to a newer engine.

    python3 check_upgrade.py --project MyGame.uproject --to 5.8 [--from 5.6] [--json report.json]
                             [--include-existing] [--fail-on blocking|warnings|never]

--from and --to are installed engine versions (Epic Games Launcher) or engine folders. --from
defaults to the project's EngineAssociation. Prints the findings; --json also writes them as JSON.
Exit code: 0 ready, 1 work needed, 2 could not run.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from upgrade_check import scan  # noqa: E402

PRO = "https://mikey92.github.io/upgrade-check/"
SEVERITY = {"removed": "REMOVED", "header": "HEADER", "deprecated": "NEW WARNING", "existing": "DEPRECATED"}
PLUGIN = {"removed": "NOT IN TARGET", "fab": "FAB: REINSTALL", "moved": "NOW IN ENGINE", "rebuild": "NEEDS BUILD",
          "deprecated": "DEPRECATED", "recompile": "RECOMPILES", "experimental": "EXPERIMENTAL", "beta": "BETA"}


def print_report(report, out=None):
    out = out or sys.stdout
    s = report["summary"]
    print(f"Upgrade Check Lite: {report['project']}  UE {report['from']} -> UE {report['to']}", file=out)
    print(f"  {s['will_not_compile']} C++ lines will not compile, {s['plugins_blocking']} plugins to fix first, "
          f"{s['new_warnings']} new deprecation warnings, {s['to_check']} lines to check by hand", file=out)
    plugins = [p for p in report["plugins"] if p["status"] != "ok"]
    if plugins:
        print("\nPlugins", file=out)
        for p in plugins:
            print(f"  {PLUGIN.get(p['status'], p['status']):15} {p['plugin']}: {p['note']}", file=out)
    if report["cpp"]:
        print("\nC++", file=out)
        for f in report["cpp"]:
            check = "  (check)" if f["confidence"] != "high" else ""
            print(f"  {SEVERITY.get(f['severity'], f['severity']):12} {f['file']}:{f['line']}  {f['symbol']}{check}", file=out)
            print(f"  {'':12} {f['message']}  [deprecated in {f['deprecated_in']}]", file=out)
    if not plugins and not report["cpp"]:
        print("\nNothing found in C++, headers or plugins.", file=out)
    print(f"\nBlueprints, an HTML report, CI with JUnit and an editor menu are in Upgrade Check for Fab: {PRO}", file=out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Find what breaks before moving an Unreal project to a newer engine.")
    parser.add_argument("--project", required=True, help="path to the .uproject")
    parser.add_argument("--to", required=True, help="target engine: installed version (5.8) or engine folder")
    parser.add_argument("--from", dest="source", help="current engine: installed version or folder (default: EngineAssociation)")
    parser.add_argument("--json", help="also write the findings to this JSON file")
    parser.add_argument("--cache", help="folder for the engine header indexes (default: <project>/Saved/UpgradeCheckLite)")
    parser.add_argument("--include-existing", action="store_true", help="also list APIs already deprecated in the current engine")
    parser.add_argument("--fail-on", default="blocking", choices=("blocking", "warnings", "never"))
    args = parser.parse_args(argv)

    project = os.path.abspath(args.project)
    try:
        with open(project, encoding="utf-8-sig") as fh:
            association = json.load(fh).get("EngineAssociation", "")
        source = args.source or association
        if not source:
            raise ValueError("the .uproject has no EngineAssociation; pass --from")
        source_dir = scan.target_for(None, source)
        target_dir = scan.target_for(None, args.to)
        cache = args.cache or os.path.join(os.path.dirname(project), "Saved", "UpgradeCheckLite")

        def progress(n):
            if n == 0:
                print("Reading engine headers (first run only, then cached)...", file=sys.stderr)

        report = scan.run(project, source_dir, target_dir, cache, args.include_existing, progress)
    except (OSError, ValueError) as exc:
        print(f"check_upgrade: {exc}", file=sys.stderr)
        return 2
    print_report(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
    s = report["summary"]
    blocking = s["will_not_compile"] or s["plugins_blocking"]
    if args.fail_on == "never":
        return 0
    if args.fail_on == "warnings":
        return 1 if blocking or s["new_warnings"] else 0
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
