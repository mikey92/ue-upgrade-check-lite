"""Unit tests for the editor-free checks, on two small fake engines and a fake project.

    python3 -m unittest discover -s tests
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from upgrade_check import scan  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def make_engine(root, minor, headers, plugins=None):
    write(os.path.join(root, "Engine", "Build", "Build.version"),
          json.dumps({"MajorVersion": 5, "MinorVersion": minor, "PatchVersion": 0, "Changelist": 100 + minor}))
    for rel, text in headers.items():
        write(os.path.join(root, "Engine", rel), text)
    for rel, desc in (plugins or {}).items():
        write(os.path.join(root, "Engine", "Plugins", rel), json.dumps(desc))


OLD_ACTOR = '''#pragma once
class ENGINE_API AActor : public UObject
{
public:
	UE_DEPRECATED(5.5, "Use GetComponentsNew instead")
	void GetComponentsOld(TArray<UActorComponent*>& Out) const;

	UE_DEPRECATED(5.4, "Use Tick instead")
	virtual void TickOld(float DeltaSeconds);

	void Tick(float DeltaSeconds);
	void GetComponentsNew(TArray<UActorComponent*>& Out) const;
	void SetFlags(int32 Flags);
};
UE_DEPRECATED(5.2, "Use FCoreTexts::Get().True")
extern CORE_API const FText GTrue;
'''

NEW_ACTOR = '''#pragma once
class ENGINE_API AActor : public UObject
{
public:
	void Tick(float DeltaSeconds);
	void GetComponentsNew(TArray<UActorComponent*>& Out) const;

	UE_DEPRECATED(5.8, "Use SetFlagsNew instead")
	void SetFlags(int32 Flags);
	void SetFlagsNew(int32 Flags);
};
'''

MOVED = '''#pragma once
UE_DEPRECATED_HEADER(5.8, "Include Engine/NewPlace.h instead")
'''


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="upgrade-check-")
        self.old = os.path.join(self.tmp, "UE_5.6")
        self.new = os.path.join(self.tmp, "UE_5.8")
        make_engine(self.old, 6, {
            "Source/Runtime/Engine/Classes/GameFramework/Actor.h": OLD_ACTOR,
            "Source/Runtime/Engine/Public/GoneHeader.h": "#pragma once\nstruct FGone {};\n",
            "Source/Runtime/Engine/Public/Moving.h": "#pragma once\n",
        }, {
            "Marketplace/CoolTool/CoolTool.uplugin": {"FriendlyName": "Cool Tool", "Modules": [{"Name": "CoolTool"}]},
            "Experimental/OldThing/OldThing.uplugin": {"FriendlyName": "Old Thing"},
            "Runtime/Keeper/Keeper.uplugin": {"FriendlyName": "Keeper"},
            "Runtime/Fading/Fading.uplugin": {"FriendlyName": "Fading"},
            "Runtime/Absorbed/Absorbed.uplugin": {"FriendlyName": "Absorbed"},
        })
        generic = {f"Source/Runtime/Gen/Public/Gen{i}.h": "struct FGen%d { void Initialize(int Parameters); };\n" % i for i in range(45)}
        generic["Source/Runtime/Gen/Public/GenOld.h"] = 'struct FGenOld {\n\tUE_DEPRECATED(5.8, "x")\n\tvoid Initialize(int Parameters);\n\tUE_DEPRECATED(5.8, "x")\n\tint Parameters;\n\tUE_DEPRECATED(5.8, "x")\n\tint Stamp;\n};\n'
        make_engine(self.new, 8, dict(generic, **{
            "Source/Runtime/Absorbed/Public/Absorbed.h": "#pragma once\n",
            "Source/Runtime/Engine/Classes/GameFramework/Actor.h": NEW_ACTOR,
            "Source/Runtime/Engine/Public/Moving.h": MOVED,
        }), {
            "Runtime/Keeper/Keeper.uplugin": {"FriendlyName": "Keeper"},
            "Runtime/Fading/Fading.uplugin": {"FriendlyName": "Fading", "Description": "Deprecated plugin. Use Keeper."},
        })
        self.project = os.path.join(self.tmp, "Game")
        write(os.path.join(self.project, "Game.uproject"), json.dumps({
            "EngineAssociation": "5.6",
            "Plugins": [
                {"Name": "CoolTool", "Enabled": True},
                {"Name": "OldThing", "Enabled": True},
                {"Name": "Keeper", "Enabled": True},
                {"Name": "Fading", "Enabled": True},
                {"Name": "Absorbed", "Enabled": True},
                {"Name": "Disabled", "Enabled": False},
            ],
        }))
        write(os.path.join(self.project, "Source", "Game", "MyActor.cpp"), '''#include "MyActor.h"
#include "GoneHeader.h"
#include "Moving.h"

void AMyActor::Go()
{
	TArray<UActorComponent*> C;
	GetComponentsOld(C);            // removed in 5.8
	TickOld(0.f);                   // removed in 5.8
	SetFlags(3);                    // new deprecation in 5.8
	FText T = GTrue;                // removed
	// GetComponentsOld(C); in a comment is ignored
	UE_LOG(LogTemp, Log, TEXT("GetComponentsOld in a string is ignored"));
	FOther::SetFlags(1);            // another type's SetFlags
	Tick(1.f);
	Initialize(Parameters);         // generic names deprecated somewhere in the engine
	const bool Stamp = true; if (Stamp) {}  // a local that shares a deprecated field's name
}
''')
        write(os.path.join(self.project, "Plugins", "MyPlugin", "MyPlugin.uplugin"),
              json.dumps({"EngineVersion": "5.6.0", "Modules": [{"Name": "MyPlugin"}], "Plugins": [{"Name": "OldThing", "Enabled": True}]}))
        write(os.path.join(self.project, "Plugins", "MyPlugin", "Source", "MyPlugin", "Private", "P.cpp"), "void F() { AActor::TickOld(1.f); }\n")
        write(os.path.join(self.project, "Plugins", "Prebuilt", "Prebuilt.uplugin"), json.dumps({"Modules": [{"Name": "Prebuilt"}]}))
        write(os.path.join(self.project, "Plugins", "UpgradeCheck", "UpgradeCheck.uplugin"), json.dumps({"EngineVersion": "5.6.0", "Modules": [{"Name": "UpgradeCheck"}]}))
        write(os.path.join(self.project, "Plugins", "UpgradeCheck", "Source", "UpgradeCheck", "X.cpp"), "void F() { GetComponentsOld(C); }\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index(self):
        index = scan.build_index(self.old)
        self.assertEqual(index["engine"], "5.6")
        symbols = {(d["owner"], d["symbol"], d["kind"]) for d in index["deprecations"]}
        self.assertIn(("AActor", "GetComponentsOld", "function"), symbols)
        self.assertIn(("AActor", "TickOld", "function"), symbols)
        self.assertIn(("", "GTrue", "field"), symbols)
        new = scan.build_index(self.new)
        self.assertIn("Moving.h", new["deprecated_headers"])
        self.assertNotIn("GoneHeader.h", new["headers"])

    def test_cpp_findings(self):
        cache = os.path.join(self.tmp, "cache")
        findings = scan.check_cpp(self.project, scan.load_index(self.old, cache), scan.load_index(self.new, cache))
        got = {(f["severity"], f["symbol"].split("::")[-1], f["file"].split("/")[-1], f["line"]) for f in findings}
        self.assertIn(("removed", "GetComponentsOld", "MyActor.cpp", 8), got)
        self.assertIn(("removed", "TickOld", "MyActor.cpp", 9), got)
        self.assertIn(("deprecated", "SetFlags", "MyActor.cpp", 10), got)
        self.assertIn(("removed", "GTrue", "MyActor.cpp", 11), got)
        self.assertIn(("header", "GoneHeader.h", "MyActor.cpp", 2), got)
        self.assertIn(("header", "Moving.h", "MyActor.cpp", 3), got)
        self.assertIn(("removed", "TickOld", "P.cpp", 1), got, "project plugin sources are scanned")
        self.assertFalse([f for f in findings if "UpgradeCheck/" in f["file"]], "the tool's own source is skipped")
        lines = {f["line"] for f in findings if f["file"].endswith("MyActor.cpp")}
        self.assertFalse(lines & {12, 13}, "comments and strings are ignored")
        self.assertNotIn(14, lines, "FOther::SetFlags is a different type's function")
        self.assertNotIn(15, lines)
        self.assertNotIn(16, lines, "generic names need the type written out")
        self.assertNotIn(17, lines, "the file's own variables are not engine fields")
        self.assertEqual(findings[0]["severity"], "removed", "most severe first")
        self.assertTrue(os.listdir(cache), "indexes are cached")

    def test_plugins(self):
        rows = {r["plugin"]: r for r in scan.check_plugins(os.path.join(self.project, "Game.uproject"), self.old, self.new)}
        self.assertEqual(rows["CoolTool"]["status"], "fab")
        self.assertEqual(rows["OldThing"]["status"], "removed")
        self.assertEqual(rows["Keeper"]["status"], "ok")
        self.assertEqual(rows["Fading"]["status"], "deprecated")
        self.assertEqual(rows["MyPlugin"]["status"], "removed", "depends on OldThing")
        self.assertNotIn("UpgradeCheck", rows, "the tool does not report itself")
        self.assertEqual(rows["Prebuilt"]["status"], "rebuild", "C++ modules without Source")
        self.assertEqual(rows["Absorbed"]["status"], "moved", "became an engine module")
        self.assertNotIn("Disabled", rows)

    def test_run_summary(self):
        report = scan.run(os.path.join(self.project, "Game.uproject"), self.old, self.new)
        self.assertEqual((report["from"], report["to"]), ("5.6", "5.8"))
        self.assertGreaterEqual(report["summary"]["will_not_compile"], 5)
        self.assertEqual(report["summary"]["plugins_blocking"], 5)
        self.assertFalse(report["blueprints_checked"])

    def test_cli(self):
        import io
        import check_upgrade as cli
        out = os.path.join(self.tmp, "report.json")
        buf = io.StringIO()
        real = sys.stdout
        sys.stdout = buf
        try:
            code = cli.main(["--project", os.path.join(self.project, "Game.uproject"), "--from", self.old, "--to", self.new, "--json", out])
            never = cli.main(["--project", os.path.join(self.project, "Game.uproject"), "--from", self.old, "--to", self.new, "--fail-on", "never"])
        finally:
            sys.stdout = real
        self.assertEqual(code, 1, "blocking findings fail by default")
        self.assertEqual(never, 0)
        text = buf.getvalue()
        self.assertIn("GetComponentsOld", text)
        self.assertIn("FAB: REINSTALL", text)
        self.assertIn("NOW IN ENGINE", text)
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["to"], "5.8")
        self.assertEqual(cli.main(["--project", os.path.join(self.project, "Game.uproject"), "--to", "9.9"]), 2,
                         "an engine that is not installed is a usage error")

    def test_declared_symbol_forms(self):
        cases = {
            '\tUE_DEPRECATED(5.8, "x")\n\tclass UPrimitiveComponent* MovementBase;\n': ("field", "MovementBase"),
            '\tUE_DEPRECATED(5.7, "x")\n\tstd::atomic<uint32> Stamp = 0;\n': ("field", "Stamp"),
            '\tUE_DEPRECATED(5.8, "x")\n\tDECLARE_MULTICAST_DELEGATE_OneParam(FOnOld, bool);\n': ("type", "FOnOld"),
            '\tUE_DEPRECATED(5.8, "x")\n\tuint32 bFlag : 1;\n': ("field", "bFlag"),
            'struct UE_DEPRECATED(5.8, "x") FOldThing;\n': ("type", "FOldThing"),
            'enum class EMode { Old UE_DEPRECATED(5.6, "x") = 1, New };\n': ("enum value", "Old"),
            '\tUE_DEPRECATED(5.8, "x")\n\t/** doc */\n\tUPROPERTY(EditAnywhere)\n\tFMargin Padding;\n': ("field", "Padding"),
            '\tUE_DEPRECATED(5.8, "x")\n\tTArray<int32> GetThings(const TCHAR* N = TEXT("a(b")) const;\n': ("function", "GetThings"),
            'using FOldAlias UE_DEPRECATED(5.5, "x") = int32;\n': ("alias", "FOldAlias"),
        }
        for text, expected in cases.items():
            found = scan.parse_header(text, "X.h")[0]
            self.assertEqual([(d["kind"], d["symbol"]) for d in found], [expected], text)
        self.assertEqual(scan.parse_header('namespace UE_DEPRECATED(5.5, "x") Old {}\n', "X.h")[0], [])
        ctor = 'struct FOSC {\n\tUE_DEPRECATED(5.5, "x")\n\texplicit FOSC(int V);\n};\n'
        self.assertEqual(scan.parse_header(ctor, "X.h")[0], [], "deprecated constructors do not flag the type")


if __name__ == "__main__":
    unittest.main()
