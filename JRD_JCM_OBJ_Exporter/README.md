# JRD JCM OBJ Exporter

Target: Blender 4.5.9 LTS. Blender 5.2 compatibility is not yet confirmed and requires future verification.

This standalone add-on exports sculpted JCM mesh objects as individual OBJ files for the deterministic JRD JCM workflow. It does not change or call DAZ Studio scripts.

## Install

1. Install `JRD_JCM_OBJ_Exporter_Blender_4_5.zip` through **Edit > Preferences > Add-ons > Install from Disk**.
2. Enable **JRD JCM OBJ Exporter**.
3. In the 3D Viewport, open the Sidebar with `N`, then select the **JRD JCM** tab.

## Use

Set **Export Folder** to the existing folder configured as the Import OBJ path in the DAZ JRD JCM Tool. The path is stored on the Blender Scene and therefore persists with the `.blend` file.

- **Export Active OBJ** exports only the active mesh.
- **Export Selected OBJs** exports every selected mesh separately in stable view-layer order.
- **Overwrite Existing Files** defaults off. With it off, Active cancels if its exact destination exists; Batch skips existing files and continues. It never creates numbered fallback names.

The object name is the filename authority. The add-on writes exactly:

```text
<object.name>.obj
```

It does not rename, reconstruct, sanitize, or strip duplicate suffixes. For example, `JRD_Foo_lThighBend_X_40_NEG.001` exports as `JRD_Foo_lThighBend_X_40_NEG.001.obj` and reports a warning. An invalid filesystem object name fails clearly instead of being changed.

Example:

```text
Object: TEST_VENDOR_TEST_CHARACTER_lThighBend_X_115_NEG
Output: TEST_VENDOR_TEST_CHARACTER_lThighBend_X_115_NEG.obj
```

Each export temporarily selects only the mesh being exported, then restores the original selected objects and active object in `finally`, including after an error.

## Confirmed OBJ Preset

The confirmed Blender 4.5.9 production preset is:

```text
Selection Only: ON
Scale: 100.000
Forward Axis: -Z
Up Axis: Y
UV Coordinates: ON
Normals: ON
Colors: OFF
Curves as NURBS: OFF
Triangulated Mesh: OFF
Apply Modifiers: ON
Properties: Viewport
Object Groups: ON
Material Groups: OFF
Vertex Groups: ON
Smooth Groups: OFF
Smooth Group Bitflags: OFF
Materials: ON
PBR Extensions: OFF
Path Mode: Auto
Animation: OFF
```
