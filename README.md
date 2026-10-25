# Upgrade Check Lite

**Find what breaks before you move an Unreal Engine project to a newer version.** Free, MIT, plain Python 3.9+, no editor, no dependencies.

It reads the headers and plugins of the engine you are moving **to**, compares them with your current engine, and lists:

- **C++ that will not compile**: calls, types, fields and enum values your current engine deprecated and the new one deleted, with the engine's own replacement hint.
- **Headers that are gone**: `#include`s the new engine no longer has, or marks with `UE_DEPRECATED_HEADER`.
- **New deprecation warnings**: APIs deprecated after your version.
- **Plugins**: enabled plugins the new engine no longer ships, plugins that became part of the engine, Fab plugins installed to your current engine only, project plugins without source, and plugins the new engine marks deprecated, beta or experimental.

Your `Source/` and every project plugin's source are scanned. Comments and strings are ignored, and `UFoo::Bar` is matched to the right type. Nothing in the project is changed.

## Use

```
git clone https://github.com/mikey92/ue-upgrade-check-lite
python3 ue-upgrade-check-lite/check_upgrade.py --project MyGame.uproject --to 5.8
```

- `--to` and `--from` take an engine version installed with the Epic Games Launcher (`5.8`) or an engine folder (a source build). `--from` defaults to the project's `EngineAssociation`.
- The engine you are moving to must be installed on the same machine.
- The first run reads both engines' headers (10 to 30 seconds each) and caches them in `Saved/UpgradeCheckLite`.
- `--json report.json` also writes the findings as JSON. `--include-existing` lists APIs your current engine already warns about.
- Exit code: 0 ready, 1 work needed (`--fail-on blocking|warnings|never`), 2 could not run. Usable as a CI gate as is.

## Example

A UE 5.6 project (the ThirdPerson template with some C++ and plugins) checked against UE 5.8:

```
Upgrade Check Lite: ShooterGame  UE 5.6 -> UE 5.8
  5 C++ lines will not compile, 3 plugins to fix first, 2 new deprecation warnings, 2 lines to check by hand

Plugins
  NOT IN TARGET   AnimNext: UE 5.8 does not include it
  NOT IN TARGET   LegacyTools: depends on AnimNext, which UE 5.8 does not have
  NOW IN ENGINE   MassEntity: no longer a plugin in UE 5.8: it is part of the engine (Source/Runtime/MassEntity). Remove it from the .uproject Plugins list
  DEPRECATED      HoldoutComposite: Deprecated plugin now redirected to CompositeCore
  BETA            ModelingToolsEditorMode: beta in UE 5.8

C++
  REMOVED      Plugins/LegacyTools/Source/LegacyTools/Private/LegacyHooks.cpp:2  FCoreDelegates::OnAsyncLoadPackage
               This delegate is not thread-safe, please use GetOnAsyncLoadPackage().  [deprecated in 5.3]
  REMOVED      Source/ShooterSample/ShooterCharacter.cpp:10  FNetworkVersion::GetEngineNetworkProtocolVersion
               Please use GetNetworkProtocolVersion instead.  [deprecated in 5.2]
  REMOVED      Source/ShooterSample/ShooterCharacter.cpp:11  GTrue
               GTrue has been deprecated in favor of FCoreTexts::Get().True.  [deprecated in 4.23]
  REMOVED      Source/ShooterSample/ShooterTests.cpp:6  FAutomationTestBase::AddErrorS
               Please use AddError instead.  [deprecated in 5.4]
  HEADER       Source/ShooterSample/ShooterCharacter.cpp:3  AreTypesEqual.h
               AreTypesEqual.h is not in UE 5.8  [deprecated in 5.6]
  NEW WARNING  Source/ShooterSample/ShooterCharacter.cpp:17  APawn::GetMovementBase  (check)
               GetMovementBase is deprecated. Use GetMovementBaseObject or GetMovementBaseInterfaceData instead.  [deprecated in 5.8]
  NEW WARNING  Source/ShooterSample/ShooterCharacter.cpp:19  UCharacterMovementComponent::SetBase  (check)
               Movement functions using UPrimitiveComponents have been deprecated. Use functions using the FMovementBaseInterfaceData struct instead.  [deprecated in 5.8]
```

**(check)** means several engine types declare that name, so confirm the type before changing the line.

## Limits

- Names are matched, not compiled types. APIs that changed signature without a deprecation are not detected; build in the new engine to confirm.
- Blueprints are not checked here (they need the editor).

## Upgrade Check for Fab

The full plugin adds what needs the editor and a team:

| | Lite | [Upgrade Check](https://mikey92.github.io/upgrade-check/) |
|---|---|---|
| C++, headers, plugins | yes | yes |
| Blueprint nodes that call removed or deprecated functions and variables | | yes |
| Tools > Upgrade Check menu, one entry per newer installed engine | | yes |
| HTML report | | yes |
| `-run=UpgradeCheck` commandlet with JUnit for CI | | yes |
| UE 5.6, 5.7, 5.8 plugin builds | | yes |

## Tests

```
python3 -m unittest discover -s tests
```

Runs every check against two small fake engines and a fake project.

## License

MIT. By BCD.
