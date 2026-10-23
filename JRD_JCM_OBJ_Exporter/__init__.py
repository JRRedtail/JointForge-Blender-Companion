bl_info = {
    "name": "JointForge OBJ Exporter",
    "author": "JRD",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > JRD JCM",
    "description": "Exports selected JCM mesh objects as individual OBJ files using exact object names",
    "category": "Import-Export",
}

import os
import re
import uuid
import hashlib
import json
import shutil

import bpy
from mathutils import Vector
from bpy_extras.io_utils import ImportHelper
from bpy.props import BoolProperty, CollectionProperty, StringProperty
from bpy.types import Operator, OperatorFileListElement, Panel


JRD_JCM_SOURCE_STEM_KEY = "jrd_jcm_source_stem"
JRD_JCM_REFERENCE_ID_KEY = "jrd_jcm_reference_id"
JRD_JCM_IS_REFERENCE_KEY = "jrd_jcm_is_reference"
JRD_JCM_REFERENCE_COLLECTION_NAME = "JRD JCM References"
_VERTEX_TOLERANCE = 1.0e-6
MIRROR_MAP_TOLERANCE = 2.0e-5
MIRROR_MAP_FORMAT_VERSION = 1
MIRROR_EXCLUSION_MASK_FORMAT_VERSION = 1
_BUNDLED_MIRROR_DATA = (
    ("jrd_jcm_mirror_map_4a14e5b5bf4ab102af1ed52eab43e8a7cacb317e90b2759ffc962550a520af14.json", "map", "4a14e5b5bf4ab102af1ed52eab43e8a7cacb317e90b2759ffc962550a520af14", 16556),
    ("jrd_jcm_mirror_exclusion_4a14e5b5bf4ab102af1ed52eab43e8a7cacb317e90b2759ffc962550a520af14.json", "exclusion", "4a14e5b5bf4ab102af1ed52eab43e8a7cacb317e90b2759ffc962550a520af14", 16556),
    ("jrd_jcm_mirror_map_8131aaf1a3c7ba3e18146d44c7e6e139652603f43f819aabc671ebc5a0118ec2.json", "map", "8131aaf1a3c7ba3e18146d44c7e6e139652603f43f819aabc671ebc5a0118ec2", 16384),
    ("jrd_jcm_mirror_exclusion_8131aaf1a3c7ba3e18146d44c7e6e139652603f43f819aabc671ebc5a0118ec2.json", "exclusion", "8131aaf1a3c7ba3e18146d44c7e6e139652603f43f819aabc671ebc5a0118ec2", 16384),
)
_DUPLICATE_SUFFIX = re.compile(r"\.\d{3}$")
_DRIVER_CLAUSE = r"[A-Za-z0-9]+_[XYZ]_[0-9]+(?:\.[0-9]+)?_(?:POS|NEG)"
_DRIVER_SUFFIX = re.compile(r"(" + _DRIVER_CLAUSE + r"(?:__" + _DRIVER_CLAUSE + r")*)$")
_WINDOWS_INVALID_COMPONENT_CHARS = set('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def _filename_stem_for_object(obj):
    """Return the preserved JCM source stem, or the legacy object-name fallback."""
    stored_stem = obj.get(JRD_JCM_SOURCE_STEM_KEY)
    if isinstance(stored_stem, str) and stored_stem.strip():
        return stored_stem.strip()
    return obj.name


def _is_reference_object(obj):
    return bool(obj.get(JRD_JCM_IS_REFERENCE_KEY, False))


def _reference_collection(scene):
    collection = bpy.data.collections.get(JRD_JCM_REFERENCE_COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(JRD_JCM_REFERENCE_COLLECTION_NAME)
        scene.collection.children.link(collection)
    collection.hide_viewport = True
    collection.hide_render = True
    return collection


def _create_reference_copy(scene, visible_obj):
    """Create the independent, hidden import-time geometry reference for visible_obj."""
    reference_id = uuid.uuid4().hex
    reference_obj = visible_obj.copy()
    reference_obj.data = visible_obj.data.copy()
    reference_obj.name = "JRD_REF_" + visible_obj.name
    reference_obj[JRD_JCM_REFERENCE_ID_KEY] = reference_id
    reference_obj[JRD_JCM_IS_REFERENCE_KEY] = True
    reference_obj[JRD_JCM_SOURCE_STEM_KEY] = visible_obj[JRD_JCM_SOURCE_STEM_KEY]
    _reference_collection(scene).objects.link(reference_obj)
    reference_obj.hide_render = True
    reference_obj.hide_set(True)
    reference_obj.select_set(False)
    visible_obj[JRD_JCM_REFERENCE_ID_KEY] = reference_id


def _resolve_reference(visible_obj):
    reference_id = visible_obj.get(JRD_JCM_REFERENCE_ID_KEY)
    if not isinstance(reference_id, str) or not reference_id:
        return None
    matches = [
        obj for obj in bpy.data.objects
        if obj.type == 'MESH'
        and _is_reference_object(obj)
        and obj.get(JRD_JCM_REFERENCE_ID_KEY) == reference_id
    ]
    return matches[0] if len(matches) == 1 else None


def _mesh_topology_signature(obj):
    mesh = obj.data
    return (len(mesh.vertices), len(mesh.edges), len(mesh.polygons))


def _same_transform(*objects):
    baseline = tuple(value for row in objects[0].matrix_world for value in row)
    for obj in objects[1:]:
        candidate = tuple(value for row in obj.matrix_world for value in row)
        if any(abs(left - right) > _VERTEX_TOLERANCE for left, right in zip(baseline, candidate)):
            return False
    return True


def _mesh_matches_reference(obj, reference):
    if _mesh_topology_signature(obj) != _mesh_topology_signature(reference):
        return False
    return all(
        (vertex.co - reference_vertex.co).length <= _VERTEX_TOLERANCE
        for vertex, reference_vertex in zip(obj.data.vertices, reference.data.vertices)
    )


def _coordinate_delta_stats(vertices, reference_vertices):
    """Return changed-vertex count and largest local-coordinate delta."""
    changed_vertex_count = 0
    max_delta = 0.0
    for vertex, reference_vertex in zip(vertices, reference_vertices):
        delta = (vertex.co - reference_vertex.co).length
        if delta > _VERTEX_TOLERANCE:
            changed_vertex_count += 1
        max_delta = max(max_delta, delta)
    return changed_vertex_count, max_delta


def _topology_signature(obj):
    """Hash only stable mesh topology and vertex indexing, never coordinates."""
    mesh = obj.data
    edges = sorted(tuple(sorted(tuple(edge.vertices))) for edge in mesh.edges)
    polygons = [tuple(polygon.vertices) for polygon in mesh.polygons]
    payload = {"vertexCount": len(mesh.vertices), "edgeCount": len(mesh.edges),
               "polygonCount": len(mesh.polygons), "edges": edges, "polygons": polygons}
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def _mirror_map_path(signature):
    folder = bpy.utils.user_resource('CONFIG', path="jrd_jcm_obj_exporter", create=True)
    return os.path.join(folder, "jrd_jcm_mirror_map_%s.json" % signature)


def _mirror_exclusion_mask_path(signature):
    folder = bpy.utils.user_resource('CONFIG', path="jrd_jcm_obj_exporter", create=True)
    return os.path.join(folder, "jrd_jcm_mirror_exclusion_%s.json" % signature)


def _bundled_mirror_data_directory():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "MirrorData")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_bundled_mirror_data(path, filename, kind, signature, vertex_count):
    if os.path.basename(path) != filename:
        raise ValueError("unexpected bundled filename")
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("JSON root is not an object")
    if data.get("topologySignature") != signature or data.get("vertexCount") != vertex_count:
        raise ValueError("signature or vertex count does not match filename")
    if kind == "map":
        mirror_map = data.get("mirrorMap")
        valid = (data.get("formatVersion") == MIRROR_MAP_FORMAT_VERSION and data.get("axis") == "X"
                 and isinstance(mirror_map, list) and len(mirror_map) == vertex_count
                 and all(isinstance(index, int) and 0 <= index < vertex_count for index in mirror_map)
                 and len(set(mirror_map)) == vertex_count
                 and all(mirror_map[mirror_map[index]] == index for index in range(vertex_count)))
    elif kind == "exclusion":
        indices = data.get("excludedVertices")
        valid = (data.get("formatVersion") == MIRROR_EXCLUSION_MASK_FORMAT_VERSION
                 and isinstance(indices, list) and data.get("excludedCount") == len(indices)
                 and all(type(index) is int and 0 <= index < vertex_count for index in indices)
                 and indices == sorted(indices) and len(set(indices)) == len(indices))
    else:
        valid = False
    if not valid:
        raise ValueError("schema validation failed")


def _install_bundled_mirror_data():
    """Install only missing validated bundled data; never replace user files."""
    try:
        destination_folder = bpy.utils.user_resource('CONFIG', path="jrd_jcm_obj_exporter", create=True)
    except Exception as exc:
        print("JRD MIRROR DATA INSTALL: ERROR resolving user configuration folder: %s" % exc)
        return
    if not destination_folder:
        print("JRD MIRROR DATA INSTALL: ERROR user configuration folder is unavailable")
        return
    bundled_folder = _bundled_mirror_data_directory()
    for filename, kind, signature, vertex_count in _BUNDLED_MIRROR_DATA:
        bundled_path = os.path.join(bundled_folder, filename)
        destination_path = os.path.join(destination_folder, filename)
        try:
            if not os.path.isfile(bundled_path):
                raise OSError("bundled file is missing")
            _validate_bundled_mirror_data(bundled_path, filename, kind, signature, vertex_count)
            bundled_hash = _sha256_file(bundled_path)
            if os.path.exists(destination_path):
                if not os.path.isfile(destination_path):
                    print("JRD MIRROR DATA INSTALL: CONFLICT %s exists but is not a file" % destination_path)
                elif _sha256_file(destination_path) == bundled_hash:
                    print("JRD MIRROR DATA INSTALL: EXISTING MATCH %s" % filename)
                else:
                    print("JRD MIRROR DATA INSTALL: CONFLICT preserved differing user file %s" % destination_path)
                continue
            try:
                with open(bundled_path, "rb") as source_handle:
                    with open(destination_path, "xb") as destination_handle:
                        shutil.copyfileobj(source_handle, destination_handle)
            except FileExistsError:
                if os.path.isfile(destination_path) and _sha256_file(destination_path) == bundled_hash:
                    print("JRD MIRROR DATA INSTALL: EXISTING MATCH %s" % filename)
                else:
                    print("JRD MIRROR DATA INSTALL: CONFLICT preserved concurrently created file %s" % destination_path)
                continue
            print("JRD MIRROR DATA INSTALL: INSTALLED %s" % filename)
        except (OSError, ValueError, TypeError) as exc:
            print("JRD MIRROR DATA INSTALL: ERROR %s: %s" % (filename, exc))


def _quantized_coordinate(coordinate):
    return tuple(int(round(value / MIRROR_MAP_TOLERANCE)) for value in coordinate)


def _print_strict_unpaired_diagnostics(mesh, unpaired):
    """Diagnostic-only nearest evidence; never participates in map construction."""
    degrees = [0] * len(mesh.vertices)
    for edge in mesh.edges:
        degrees[edge.vertices[0]] += 1
        degrees[edge.vertices[1]] += 1
    unpaired_set = set(unpaired)
    for source_index in unpaired:
        coordinate = mesh.vertices[source_index].co
        desired = Vector((-coordinate.x, coordinate.y, coordinate.z))
        candidate_index, nearest_distance = min(
            ((vertex.index, (vertex.co - desired).length) for vertex in mesh.vertices),
            key=lambda item: item[1],
        )
        candidate_coordinate = mesh.vertices[candidate_index].co
        reciprocal_desired = Vector((-candidate_coordinate.x, candidate_coordinate.y, candidate_coordinate.z))
        reciprocal_index, _reciprocal_distance = min(
            ((vertex.index, (vertex.co - reciprocal_desired).length) for vertex in mesh.vertices),
            key=lambda item: item[1],
        )
        self_mirror_distance = (coordinate - desired).length
        print("UNPAIRED VERTEX: %d" % source_index)
        print("COORD: %.9g, %.9g, %.9g" % tuple(coordinate))
        print("DESIRED MIRROR: %.9g, %.9g, %.9g" % tuple(desired))
        print("NEAREST MIRROR CANDIDATE: %d" % candidate_index)
        print("NEAREST DISTANCE: %.12g" % nearest_distance)
        print("CANDIDATE COORD: %.9g, %.9g, %.9g" % tuple(candidate_coordinate))
        print("CANDIDATE ALSO UNPAIRED: %s" % ("true" if candidate_index in unpaired_set else "false"))
        print("RECIPROCAL NEAREST: %d" % reciprocal_index)
        print("RECIPROCAL: %s" % ("PASS" if reciprocal_index == source_index else "FAIL"))
        print("SOURCE EDGE DEGREE: %d" % degrees[source_index])
        print("CANDIDATE EDGE DEGREE: %d" % degrees[candidate_index])
        print("EDGE DEGREE MATCH: %s" % ("true" if degrees[source_index] == degrees[candidate_index] else "false"))
        print("ABS X: %.12g" % abs(coordinate.x))
        print("SELF MIRROR DISTANCE: %.12g" % self_mirror_distance)


def _build_mirror_map(mesh):
    """Build a strict local-X symmetry map; ambiguous pairing is always invalid."""
    buckets = {}
    for vertex in mesh.vertices:
        buckets.setdefault(_quantized_coordinate(vertex.co), []).append(vertex.index)
    mirror_map = [-1] * len(mesh.vertices)
    unpaired = []
    ambiguous = []
    centerline = 0
    paired_non_center = 0
    for vertex in mesh.vertices:
        coordinate = vertex.co
        if abs(coordinate.x) <= MIRROR_MAP_TOLERANCE:
            candidates = [vertex.index]
            centerline += 1
        else:
            desired = Vector((-coordinate.x, coordinate.y, coordinate.z))
            bucket = _quantized_coordinate(desired)
            candidates = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for candidate_index in buckets.get((bucket[0] + dx, bucket[1] + dy, bucket[2] + dz), []):
                            if (mesh.vertices[candidate_index].co - desired).length <= MIRROR_MAP_TOLERANCE:
                                candidates.append(candidate_index)
            candidates = sorted(set(candidates))
            paired_non_center += 1
        if len(candidates) == 0:
            unpaired.append(vertex.index)
        elif len(candidates) != 1:
            ambiguous.append(vertex.index)
        else:
            mirror_map[vertex.index] = candidates[0]
    if unpaired or ambiguous:
        error = "UNPAIRED: %d %s | AMBIGUOUS: %d %s" % (len(unpaired), unpaired[:10], len(ambiguous), ambiguous[:10])
        print(error)
        if unpaired:
            _print_strict_unpaired_diagnostics(mesh, unpaired)
        return None, error, None
    if len(set(mirror_map)) != len(mirror_map) or any(mirror_map[mirror_map[index]] != index for index in range(len(mirror_map))):
        return None, "Mirror map is not a bijective involution", None
    edge_set = {tuple(sorted(tuple(edge.vertices))) for edge in mesh.edges}
    failed_edges = [(a, b) for a, b in edge_set if tuple(sorted((mirror_map[a], mirror_map[b]))) not in edge_set]
    if failed_edges:
        return None, "MIRRORED EDGE VALIDATION: FAIL | FAILED EDGES: %d %s" % (len(failed_edges), failed_edges[:10]), None
    return mirror_map, None, {"centerline": centerline, "pairedNonCenter": paired_non_center}


def _load_mirror_map(signature, vertex_count):
    path = _mirror_map_path(signature)
    if not os.path.isfile(path):
        return None, path
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        mirror_map = data["mirrorMap"]
        valid = (data.get("formatVersion") == MIRROR_MAP_FORMAT_VERSION and data.get("axis") == "X"
                 and data.get("topologySignature") == signature and data.get("vertexCount") == vertex_count
                 and isinstance(mirror_map, list) and len(mirror_map) == vertex_count
                 and all(isinstance(index, int) and 0 <= index < vertex_count for index in mirror_map)
                 and len(set(mirror_map)) == vertex_count
                 and all(mirror_map[mirror_map[index]] == index for index in range(vertex_count)))
        return (mirror_map if valid else None), path
    except (OSError, ValueError, KeyError, TypeError):
        return None, path


def _load_mirror_exclusion_mask(signature, vertex_count):
    """Return no mask, or a fully validated deterministic target-index mask."""
    path = _mirror_exclusion_mask_path(signature)
    if not os.path.isfile(path):
        return None, path, None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        return None, path, "cannot read JSON (%s)" % exc

    indices = data.get("excludedVertices") if isinstance(data, dict) else None
    if not (
        isinstance(data, dict)
        and data.get("formatVersion") == MIRROR_EXCLUSION_MASK_FORMAT_VERSION
        and data.get("topologySignature") == signature
        and data.get("vertexCount") == vertex_count
        and isinstance(indices, list)
        and data.get("excludedCount") == len(indices)
        and all(type(index) is int and 0 <= index < vertex_count for index in indices)
        and indices == sorted(indices)
        and len(set(indices)) == len(indices)
    ):
        return None, path, "invalid format, signature, count, ordering, duplicate, or vertex index"
    return set(indices), path, None


def _exact_mirror_validation(source, target, mirror_map, excluded_vertices, clean_target_coordinates):
    """Measure mirrored vertices and retained excluded target vertices separately."""
    mirrored_errors = []
    excluded_changes = []
    worst_pair = None
    self_mapped_vertices = 0
    for source_index, target_index in enumerate(mirror_map):
        if source_index == target_index:
            self_mapped_vertices += 1
        if target_index in excluded_vertices:
            excluded_changes.append((target.data.vertices[target_index].co - clean_target_coordinates[target_index]).length)
            continue
        source_coordinate = source.data.vertices[source_index].co
        expected = Vector((-source_coordinate.x, source_coordinate.y, source_coordinate.z))
        error = (target.data.vertices[target_index].co - expected).length
        mirrored_errors.append(error)
        if worst_pair is None or error > worst_pair[0]:
            worst_pair = (error, source_index, target_index)

    def rms(errors):
        return (sum(error * error for error in errors) / len(errors)) ** 0.5 if errors else 0.0

    return {
        "mappedVertices": len(mirror_map),
        "selfMappedVertices": self_mapped_vertices,
        "mirroredVertices": len(mirrored_errors),
        "excludedVertices": len(excluded_changes),
        "rmsError": rms(mirrored_errors),
        "maxError": max(mirrored_errors) if mirrored_errors else 0.0,
        "worstPair": worst_pair,
        "excludedRmsChange": rms(excluded_changes),
        "excludedMaxChange": max(excluded_changes) if excluded_changes else 0.0,
    }


def _target_has_shape_key_correction(target, target_reference):
    shape_keys = target.data.shape_keys
    return bool(shape_keys and any(_coordinate_delta_stats(key.data, target_reference.data.vertices)[0] > 0 for key in shape_keys.key_blocks[1:]))


def _print_source_geometry_diagnostic(context, source, source_reference):
    """Print non-mutating raw, shape-key, and evaluated source geometry evidence."""
    print("JRD JCM SOURCE GEOMETRY DIAGNOSTIC")
    print("SOURCE OBJECT: %s" % source.name)
    print("SOURCE MESH: %s" % source.data.name)
    print("REFERENCE OBJECT: %s" % source_reference.name)
    print("REFERENCE MESH: %s" % source_reference.data.name)
    print("SOURCE VERTICES: %d" % len(source.data.vertices))
    print("REFERENCE VERTICES: %d" % len(source_reference.data.vertices))

    raw_changed, raw_max_delta = _coordinate_delta_stats(
        source.data.vertices, source_reference.data.vertices
    )
    print("RAW MESH CHANGED VERTICES: %d" % raw_changed)
    print("RAW MESH MAX DELTA: %.9g" % raw_max_delta)

    shape_keys = source.data.shape_keys
    print("SOURCE HAS SHAPE KEYS: %s" % ("true" if shape_keys else "false"))
    shape_key_has_delta = False
    if shape_keys:
        key_blocks = shape_keys.key_blocks
        print("KEY BLOCK COUNT: %d" % len(key_blocks))
        for key_block in key_blocks:
            print("KEY: %s" % key_block.name)
            print("VALUE: %.9g" % key_block.value)
        print("ACTIVE SHAPE KEY INDEX: %d" % source.active_shape_key_index)
        for key_block in key_blocks[1:]:
            changed, max_delta = _coordinate_delta_stats(
                key_block.data, source_reference.data.vertices
            )
            print("SHAPE KEY: %s" % key_block.name)
            print("CHANGED VERTICES: %d" % changed)
            print("MAX DELTA: %.9g" % max_delta)
            shape_key_has_delta = shape_key_has_delta or changed > 0

    evaluated_mesh = None
    try:
        depsgraph = context.evaluated_depsgraph_get()
        evaluated_obj = source.evaluated_get(depsgraph)
        evaluated_mesh = evaluated_obj.to_mesh()
        evaluated_changed, evaluated_max_delta = _coordinate_delta_stats(
            evaluated_mesh.vertices, source_reference.data.vertices
        )
        print("EVALUATED CHANGED VERTICES: %d" % evaluated_changed)
        print("EVALUATED MAX DELTA: %.9g" % evaluated_max_delta)
    finally:
        if evaluated_mesh is not None:
            evaluated_obj.to_mesh_clear()

    if raw_changed > 0:
        diagnosis = "CASE A: RAW mesh has delta"
    elif shape_key_has_delta:
        diagnosis = "CASE B: RAW mesh has zero delta, SHAPE KEY has delta"
    elif evaluated_changed > 0:
        diagnosis = "CASE C: RAW mesh has zero delta, evaluated mesh has delta"
    else:
        diagnosis = "CASE D: raw mesh, shape keys, and evaluated mesh all show zero delta"
    print(diagnosis)
    return raw_changed


def _display_name_from_source_filename(source_filename):
    """Return the canonical driver suffix for Blender display, preserving a safe fallback."""
    source_name = os.path.basename(source_filename)
    source_stem, _extension = os.path.splitext(source_name)
    match = _DRIVER_SUFFIX.search(source_stem)
    if match:
        return match.group(1) + ".obj"
    return source_name


def _filename_for_object(obj):
    """Return the exact authoritative OBJ filename, or a readable validation error."""
    name = _filename_stem_for_object(obj)
    if not name or name in {".", ".."}:
        return None, "Object name is empty or not a valid filename component"
    if "\x00" in name or "/" in name or "\\" in name:
        return None, "Object name contains a path separator or null character: %s" % name
    if os.name == "nt":
        if any(character in _WINDOWS_INVALID_COMPONENT_CHARS for character in name):
            return None, "Object name contains a Windows-invalid filename character: %s" % name
        if name.endswith((".", " ")):
            return None, "Object name ends with a Windows-invalid dot or space: %s" % name
        if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            return None, "Object name uses a Windows-reserved filename: %s" % name
    return name + ".obj", None


def _print_export_diagnostic(obj, filename, error):
    """Temporary console diagnostics for canonical JCM filename resolution."""
    stored_stem = obj.get(JRD_JCM_SOURCE_STEM_KEY)
    has_source_stem = isinstance(stored_stem, str) and bool(stored_stem.strip())
    source_stem = stored_stem if has_source_stem else "<missing>"
    resolved_filename = filename if filename is not None else "<invalid: %s>" % error
    print("JRD JCM EXPORT DIAGNOSTIC")
    print("OBJECT NAME: %s" % obj.name)
    print("HAS SOURCE STEM: %s" % ("true" if has_source_stem else "false"))
    print("SOURCE STEM VALUE: %s" % source_stem)
    print("SOURCE STEM LENGTH: %d" % (len(source_stem) if has_source_stem else 0))
    print("RESOLVED EXPORT FILENAME: %s" % resolved_filename)


def _duplicate_suffix_warning(obj):
    stem = _filename_stem_for_object(obj)
    if _DUPLICATE_SUFFIX.search(stem):
        return (
            "Warning: %s looks like a Blender duplicate name; exported filename remains %s.obj"
            % (stem, stem)
        )
    return None


def _export_active_object_to_path(context, obj, filepath):
    """Export exactly obj, retaining the caller's selection restoration responsibility."""
    for view_object in context.view_layer.objects:
        view_object.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj

    # Blender 4.5 OBJ export: proven JRD JCM workflow preset.
    bpy.ops.wm.obj_export(
        filepath=filepath,
        # Retained so this add-on's explicit overwrite policy handles existing files.
        check_existing=False,
        export_selected_objects=True,
        global_scale=100.0,
        forward_axis='NEGATIVE_Z',
        up_axis='Y',
        export_uv=True,
        export_normals=True,
        export_colors=False,
        export_triangulated_mesh=False,
        export_curves_as_nurbs=False,
        apply_modifiers=True,
        export_eval_mode='DAG_EVAL_VIEWPORT',
        export_object_groups=True,
        export_material_groups=False,
        export_vertex_groups=True,
        export_smooth_groups=False,
        smooth_group_bitflags=False,
        export_materials=True,
        export_pbr_extensions=False,
        path_mode='AUTO',
        export_animation=False,
    )


def _is_live_view_layer_object(context, obj):
    if obj is None:
        return False
    try:
        return bpy.data.objects.get(obj.name) == obj and obj.name in context.view_layer.objects
    except (ReferenceError, TypeError):
        return False


def _restore_selection(context, selected_objects, active_object):
    """Restore selection/active state even after a failed export."""
    for view_object in context.view_layer.objects:
        if view_object is not None and _is_live_view_layer_object(context, view_object):
            view_object.select_set(False)
    for obj in selected_objects:
        if _is_live_view_layer_object(context, obj):
            obj.select_set(True)
    if _is_live_view_layer_object(context, active_object):
        context.view_layer.objects.active = active_object
    else:
        context.view_layer.objects.active = None


def _validate_export_folder(scene):
    folder = bpy.path.abspath(scene.jrd_jcm_export_folder)
    if not folder:
        return None, "Choose an Export Folder first"
    if not os.path.isdir(folder):
        return None, "Export Folder does not exist or is not a directory: %s" % folder
    return folder, None


class JRDJCM_OT_import_jcm_obj(Operator, ImportHelper):
    bl_idname = "jrd_jcm.import_jcm_obj"
    bl_label = "Import JCM OBJs"
    bl_description = "Import OBJ files and preserve each complete source filename for JCM export"

    filename_ext = ".obj"
    filter_glob: StringProperty(default="*.obj", options={'HIDDEN'})
    directory: StringProperty(subtype='DIR_PATH')
    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        directory = self.directory or os.path.dirname(self.filepath)
        filenames = [file_element.name for file_element in self.files if file_element.name]
        if not filenames and self.filepath:
            filenames = [os.path.basename(self.filepath)]
        if not directory or not filenames:
            self.report({'ERROR'}, "Choose one or more OBJ files to import")
            return {'CANCELLED'}

        imported_file_count = 0
        tagged_mesh_count = 0
        for filename in filenames:
            filepath = os.path.join(directory, filename)
            full_stem = os.path.splitext(os.path.basename(filepath))[0]
            if os.path.splitext(filepath)[1].lower() != ".obj" or not full_stem:
                self.report({'ERROR'}, "Selected file must be an OBJ with a usable filename stem: %s" % filepath)
                return {'CANCELLED'}

            before_object_pointers = {obj.as_pointer() for obj in context.scene.objects}
            try:
                result = bpy.ops.wm.obj_import(filepath=filepath, global_scale=0.01)
            except Exception as exc:
                self.report({'ERROR'}, "Failed to import OBJ: %s | %s" % (filepath, exc))
                return {'CANCELLED'}
            if 'FINISHED' not in result:
                self.report({'ERROR'}, "Native OBJ import did not finish: %s" % filepath)
                return {'CANCELLED'}

            imported_meshes = [obj for obj in context.scene.objects if obj.as_pointer() not in before_object_pointers and obj.type == 'MESH']
            if not imported_meshes:
                self.report({'ERROR'}, "OBJ import created no new mesh object to tag: %s" % filepath)
                return {'CANCELLED'}
            for obj in imported_meshes:
                obj[JRD_JCM_SOURCE_STEM_KEY] = full_stem
                obj.name = _display_name_from_source_filename(filename)
                _create_reference_copy(context.scene, obj)
            imported_file_count += 1
            tagged_mesh_count += len(imported_meshes)
            print("JRD JCM IMPORT: %s" % filepath)
            print("SOURCE STEM: %s" % full_stem)
            print("TAGGED MESHES: %d" % len(imported_meshes))

        print("JRD JCM BATCH IMPORT COMPLETE")
        print("FILES: %d" % imported_file_count)
        print("MESHES TAGGED: %d" % tagged_mesh_count)
        self.report({'INFO'}, "Imported %d OBJ file(s); tagged %d JCM mesh object(s)" % (imported_file_count, tagged_mesh_count))
        return {'FINISHED'}


class JRDJCM_OT_export_active_obj(Operator):
    bl_idname = "jrd_jcm.export_active_obj"
    bl_label = "Export Active OBJ"
    bl_description = "Export the active mesh as an OBJ named exactly after the object"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Active object must be a mesh")
            return {'CANCELLED'}
        if _is_reference_object(obj):
            self.report({'ERROR'}, "Hidden JCM reference objects cannot be exported")
            return {'CANCELLED'}
        folder, error = _validate_export_folder(context.scene)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        filename, error = _filename_for_object(obj)
        _print_export_diagnostic(obj, filename, error)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        destination = os.path.join(folder, filename)
        if os.path.exists(destination) and not context.scene.jrd_jcm_overwrite_existing:
            self.report({'WARNING'}, "File exists; overwrite is off: %s" % filename)
            return {'CANCELLED'}

        selected_objects = list(context.selected_objects)
        active_object = context.view_layer.objects.active
        try:
            _export_active_object_to_path(context, obj, destination)
        except Exception as exc:
            self.report({'ERROR'}, "Failed to export %s: %s" % (obj.name, exc))
            return {'CANCELLED'}
        finally:
            _restore_selection(context, selected_objects, active_object)

        warning = _duplicate_suffix_warning(obj)
        if warning:
            self.report({'WARNING'}, warning)
        self.report({'INFO'}, "Exported: %s" % filename)
        return {'FINISHED'}


class JRDJCM_OT_export_selected_objs(Operator):
    bl_idname = "jrd_jcm.export_selected_objs"
    bl_label = "Export Selected OBJs"
    bl_description = "Export each selected mesh as an individual exact-name OBJ"

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        folder, error = _validate_export_folder(context.scene)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        # View-layer order is deliberate stable ordering; never iterate a set.
        mesh_objects = [
            obj for obj in context.view_layer.objects
            if obj.select_get() and obj.type == 'MESH' and not _is_reference_object(obj)
        ]
        if not mesh_objects:
            self.report({'WARNING'}, "No selected mesh objects")
            return {'CANCELLED'}

        selected_objects = list(context.selected_objects)
        active_object = context.view_layer.objects.active
        exported = 0
        skipped_existing = 0
        failed = []
        warnings = []
        try:
            for obj in mesh_objects:
                filename, name_error = _filename_for_object(obj)
                _print_export_diagnostic(obj, filename, name_error)
                if name_error:
                    failed.append("%s: %s" % (obj.name, name_error))
                    continue
                destination = os.path.join(folder, filename)
                if os.path.exists(destination) and not context.scene.jrd_jcm_overwrite_existing:
                    skipped_existing += 1
                    continue
                try:
                    _export_active_object_to_path(context, obj, destination)
                    exported += 1
                    warning = _duplicate_suffix_warning(obj)
                    if warning:
                        warnings.append(warning)
                except Exception as exc:
                    failed.append("%s: %s" % (obj.name, exc))
        finally:
            _restore_selection(context, selected_objects, active_object)

        for warning in warnings:
            self.report({'WARNING'}, warning)
        for failure in failed:
            self.report({'ERROR'}, failure)
        self.report(
            {'INFO'},
            "Exported: %d | Skipped existing: %d | Failed: %d"
            % (exported, skipped_existing, len(failed)),
        )
        return {'FINISHED'} if not failed else {'CANCELLED'}


class JRDJCM_OT_build_mirror_map(Operator, ImportHelper):
    bl_idname = "jrd_jcm.build_mirror_map"
    bl_label = "Build Genesis 8 Mirror Map..."
    bl_description = "Build a strict local-X vertex correspondence map from one neutral Genesis 8 OBJ"

    filename_ext = ".obj"
    filter_glob: StringProperty(default="*.obj", options={'HIDDEN'})

    def execute(self, context):
        if not self.filepath or os.path.splitext(self.filepath)[1].lower() != ".obj":
            self.report({'ERROR'}, "Choose one neutral Genesis 8 calibration OBJ")
            return {'CANCELLED'}
        before_pointers = {obj.as_pointer() for obj in context.scene.objects}
        saved_selection = list(context.selected_objects)
        saved_active = context.view_layer.objects.active
        created_objects = []
        try:
            result = bpy.ops.wm.obj_import(filepath=self.filepath, global_scale=0.01)
            if 'FINISHED' not in result:
                self.report({'ERROR'}, "Calibration OBJ import did not finish")
                return {'CANCELLED'}
            created_objects = [obj for obj in context.scene.objects if obj.as_pointer() not in before_pointers]
            meshes = [obj for obj in created_objects if obj.type == 'MESH']
            if len(meshes) != 1:
                self.report({'ERROR'}, "Calibration OBJ must import exactly one mesh; found %d" % len(meshes))
                return {'CANCELLED'}
            calibration = meshes[0]
            mirror_map, error, stats = _build_mirror_map(calibration.data)
            if error:
                self.report({'ERROR'}, error)
                return {'CANCELLED'}
            signature = _topology_signature(calibration)
            path = _mirror_map_path(signature)
            payload = {"formatVersion": MIRROR_MAP_FORMAT_VERSION, "axis": "X",
                       "vertexCount": len(calibration.data.vertices), "edgeCount": len(calibration.data.edges),
                       "polygonCount": len(calibration.data.polygons), "topologySignature": signature,
                       "buildTolerance": MIRROR_MAP_TOLERANCE, "mirrorMap": mirror_map}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
            print("VERTICES: %d" % payload["vertexCount"])
            print("PAIRED NON-CENTER: %d" % stats["pairedNonCenter"])
            print("CENTERLINE: %d" % stats["centerline"])
            print("UNPAIRED: 0")
            print("AMBIGUOUS: 0")
            print("INVOLUTION: PASS")
            print("MIRRORED EDGE VALIDATION: PASS")
            self.report({'INFO'}, "Mirror map saved: %s" % path)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, "Failed to build mirror map: %s" % exc)
            return {'CANCELLED'}
        finally:
            for obj in created_objects:
                mesh = obj.data if obj.type == 'MESH' else None
                bpy.data.objects.remove(obj, do_unlink=True)
                if mesh is not None and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            _restore_selection(context, saved_selection, saved_active)


class JRDJCM_OT_create_mirrored_jcm(Operator):
    bl_idname = "jrd_jcm.create_mirrored_jcm"
    bl_label = "Create Mirrored JCM"
    bl_description = "Mirror the active finished JCM geometry exactly onto the other selected untouched JCM"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        selected = list(context.selected_objects)
        if len(selected) != 2:
            self.report({'ERROR'}, "Select exactly two visible JCM mesh objects")
            return {'CANCELLED'}
        source = context.active_object
        if source not in selected:
            self.report({'ERROR'}, "The active selected JCM must be the source")
            return {'CANCELLED'}
        target = selected[0] if selected[1] == source else selected[1]
        for obj in (source, target):
            if obj.type != 'MESH' or _is_reference_object(obj):
                self.report({'ERROR'}, "Both selected objects must be visible JCM mesh objects")
                return {'CANCELLED'}
            source_stem = obj.get(JRD_JCM_SOURCE_STEM_KEY)
            if not isinstance(source_stem, str) or not source_stem.strip():
                self.report({'ERROR'}, "Both selected objects must have a JCM source stem")
                return {'CANCELLED'}

        target_reference = _resolve_reference(target)
        if target_reference is None:
            self.report({'ERROR'}, "Target JCM reference geometry is missing. Re-import the target JCM with the current addon before using Create Mirrored JCM.")
            return {'CANCELLED'}

        all_objects = (source, target, target_reference)
        signatures = [_mesh_topology_signature(obj) for obj in all_objects]
        if any(signature != signatures[0] for signature in signatures[1:]):
            self.report({'ERROR'}, "JCM topology differs; vertex, edge, and polygon counts must all match")
            return {'CANCELLED'}
        if not _same_transform(*all_objects):
            self.report({'ERROR'}, "JCM object transforms differ; object-space delta transfer is ambiguous")
            return {'CANCELLED'}
        if not _mesh_matches_reference(target, target_reference) or _target_has_shape_key_correction(target, target_reference):
            self.report({'ERROR'}, "Target JCM has already been modified. Use an untouched imported target JCM for mirror creation.")
            return {'CANCELLED'}
        topology_signatures = [_topology_signature(obj) for obj in all_objects]
        if any(signature != topology_signatures[0] for signature in topology_signatures[1:]):
            self.report({'ERROR'}, "JCM topology signatures differ; a compatible mirror map cannot be used")
            return {'CANCELLED'}
        topology_signature = topology_signatures[0]
        mirror_map, map_path = _load_mirror_map(topology_signature, signatures[0][0])
        if mirror_map is None:
            self.report({'ERROR'}, "No compatible JCM mirror map exists for this topology. Run Build Genesis 8 Mirror Map first.")
            return {'CANCELLED'}
        excluded_vertices, exclusion_mask_path, exclusion_mask_error = _load_mirror_exclusion_mask(
            topology_signature, signatures[0][0]
        )
        if exclusion_mask_error is not None:
            self.report({'ERROR'}, "Mirror exclusion mask is invalid: %s" % exclusion_mask_error)
            return {'CANCELLED'}
        has_exclusion_mask = excluded_vertices is not None
        if excluded_vertices is None:
            excluded_vertices = set()

        print("JRD JCM MIRROR START")
        print("SOURCE: %s" % source.name)
        print("SOURCE STEM: %s" % source[JRD_JCM_SOURCE_STEM_KEY])
        print("TARGET: %s" % target.name)
        print("TARGET STEM: %s" % target[JRD_JCM_SOURCE_STEM_KEY])
        print("TOPOLOGY SIGNATURE: %s" % topology_signature)
        print("MIRROR MAP: %s" % map_path)
        print("VERTICES: %d" % signatures[0][0])
        print("TARGET VERIFIED UNTOUCHED: true")
        if has_exclusion_mask:
            print("MIRROR EXCLUSION MASK: %s" % exclusion_mask_path)
            print("MIRROR EXCLUSION COUNT: %d" % len(excluded_vertices))
        else:
            print("MIRROR EXCLUSION MASK: none")
        clean_target_coordinates = [vertex.co.copy() for vertex in target.data.vertices]
        for source_index, source_vertex in enumerate(source.data.vertices):
            target_index = mirror_map[source_index]
            if target_index in excluded_vertices:
                continue
            target_vertex = target.data.vertices[target_index]
            target_vertex.co = Vector((-source_vertex.co.x, source_vertex.co.y, source_vertex.co.z))
        target.data.update()
        validation = _exact_mirror_validation(
            source, target, mirror_map, excluded_vertices, clean_target_coordinates
        )
        print("=== JRD EXACT MIRROR VALIDATION ===")
        print("MAPPED VERTICES: %d" % validation["mappedVertices"])
        print("SELF-MAPPED VERTICES: %d" % validation["selfMappedVertices"])
        print("MIRRORED VERTICES: %d" % validation["mirroredVertices"])
        print("EXACT MIRROR RMS ERROR: %.12g" % validation["rmsError"])
        print("EXACT MIRROR MAX ERROR: %.12g" % validation["maxError"])
        if validation["worstPair"] is None:
            print("WORST PAIR: <none; all mapped targets excluded>")
        else:
            print("WORST PAIR: source %d -> target %d (error %.12g)" % (
                validation["worstPair"][1], validation["worstPair"][2], validation["worstPair"][0]
            ))
        print("=== JRD EXACT MIRROR VALIDATION COMPLETE ===")
        print("=== JRD MIRROR EXCLUSION VALIDATION ===")
        print("EXCLUSION MASK: %s" % (exclusion_mask_path if has_exclusion_mask else "none"))
        print("EXCLUDED VERTICES: %d" % validation["excludedVertices"])
        print("MIRRORED VERTICES: %d" % validation["mirroredVertices"])
        print("MIRRORED REGION RMS ERROR: %.12g" % validation["rmsError"])
        print("MIRRORED REGION MAX ERROR: %.12g" % validation["maxError"])
        print("EXCLUDED REGION RMS CHANGE FROM CLEAN TARGET: %.12g" % validation["excludedRmsChange"])
        print("EXCLUDED REGION MAX CHANGE FROM CLEAN TARGET: %.12g" % validation["excludedMaxChange"])
        print("=== JRD MIRROR EXCLUSION VALIDATION COMPLETE ===")
        print("JRD JCM MIRROR COMPLETE")
        self.report({'INFO'}, "Mirrored source correction onto target JCM")
        return {'FINISHED'}


class JRDJCM_PT_obj_export(Panel):
    bl_label = "JointForge OBJ Export"
    bl_idname = "JRDJCM_PT_obj_export"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'JointForge'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, "jrd_jcm_export_folder")
        selected_mesh_count = sum(1 for obj in context.selected_objects if obj.type == 'MESH')
        layout.label(text="Selected mesh count: %d" % selected_mesh_count)
        layout.operator(JRDJCM_OT_import_jcm_obj.bl_idname)
        layout.operator(JRDJCM_OT_create_mirrored_jcm.bl_idname)
        layout.operator(JRDJCM_OT_export_active_obj.bl_idname)
        layout.operator(JRDJCM_OT_export_selected_objs.bl_idname)
        layout.prop(scene, "jrd_jcm_overwrite_existing")
        active = context.active_object
        if active is not None:
            warning = _duplicate_suffix_warning(active)
            if warning:
                box = layout.box()
                box.label(text="Warning: active name ends in .###", icon='ERROR')
                box.label(text="Filename will include the suffix.")


_CLASSES = (
    JRDJCM_OT_import_jcm_obj,
    JRDJCM_OT_build_mirror_map,
    JRDJCM_OT_create_mirrored_jcm,
    JRDJCM_OT_export_active_obj,
    JRDJCM_OT_export_selected_objs,
    JRDJCM_PT_obj_export,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.jrd_jcm_export_folder = StringProperty(
        name="Export Folder",
        description="Existing folder for exact object-name OBJ files",
        subtype='DIR_PATH',
        default="",
    )
    bpy.types.Scene.jrd_jcm_overwrite_existing = BoolProperty(
        name="Overwrite Existing Files",
        description="Allow replacing an OBJ whose exact object-name filename already exists",
        default=False,
    )
    _install_bundled_mirror_data()


def unregister():
    del bpy.types.Scene.jrd_jcm_overwrite_existing
    del bpy.types.Scene.jrd_jcm_export_folder
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
