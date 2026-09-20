# v6.3 Typed Scene/Location RAG

## Design goal

Environment knowledge stays data-driven. Planner prompts contain no site-specific place names, coordinates, storage relations, or map facts. The same planning code must work after adding a new site, region, or object-location prior by changing RAG data only.

## Knowledge responsibilities

### Scene RAG

Scene RAG answers contextual questions such as:

- what object/category is commonly found in which semantic area;
- which area is worth searching first;
- semantic/spatial relationships between environment concepts;
- environment descriptions and SOP-like search context.

A Scene document may reference `location_ids`, but should not duplicate x/y/yaw. Example schema:

```yaml
documents:
  - id: <knowledge_id>
    enabled: true
    title: <human title>
    knowledge_type: LOCATION_PRIOR
    content: <environment relationship or search advice>
    target_terms:
      - <object_or_category_term>
    tags:
      - <retrieval_tag>
    location_ids:
      - <registered_location_id>
    confidence: 0.9
    requires_visual_verification: true
```

### Location RAG

Location RAG answers where a named spatial entity is. It is independently retrievable by registered ID/alias and can also be discovered through a Scene reference.

```yaml
locations:
  <location_id>:
    enabled: true
    type: STATIC_LOCATION       # or STATIC_REGION / SEMI_STATIC_LOCATION
    frame_id: map
    pose:                       # STATIC_LOCATION / SEMI_STATIC_LOCATION
      x: <metres>
      y: <metres>
      yaw: <radians>
    # For STATIC_REGION, entry_pose can be used instead of pose.
    map_version: <map_version>
    yaw_unit: rad
    calibrated: false
    aliases:
      - <spoken/location alias>
    description: <meaning of this place>
```

For a `STATIC_REGION`, optional polygon data can be retained:

```yaml
region:
  shape: polygon
  points:
    - [x1, y1]
    - [x2, y2]
    - [x3, y3]
```

The current `NavigateToPoint` binding uses `pose` or `entry_pose`; the polygon remains useful for future region-constrained perception/planning.

## Retrieval paths

A Location document can be reached through multiple independent paths:

1. exact Location ID/alias in raw user text;
2. exact Location ID/alias in extracted targets/location queries;
3. lexical/semantic Location retrieval when the user describes a place indirectly;
4. exact Scene `location_id` reference when Scene RAG discovers a likely search area.

An explicit named location therefore does not depend on Scene RAG ranking.

Scene retrieval is query-fanned-out: targets and scene queries are searched independently, results are fused, over-fetched, then entity/anchor filtered before the final top-K. This avoids a long mixed query diluting a discriminative object/category term.

## Search-prior execution

For an unresolved visual target:

```text
Scene RAG target/category prior
        -> referenced location_id
Location RAG exact ID lookup
        -> structured pose/entry pose
Location Grounding
        -> trust/frame/map/skill checks
navigation candidate
        -> deterministic navigation inserted before local perception
VisualizeObject(target)
```

The search location changes the robot viewpoint/region; it never proves that the movable target is actually present. Live visual grounding remains required.

## Trust modes

### integration (default in v6.3)

```env
LOCATION_KNOWLEDGE_MODE=integration
```

Configured location records are treated as executable for end-to-end pipeline testing even if `calibrated: false`, the current robot map is unknown, or the stored map version differs. Those conditions are retained as telemetry warnings.

This mode is intentionally permissive and should not be interpreted as coordinate safety certification.

### validated

After measuring the real environment:

```env
LOCATION_KNOWLEDGE_MODE=validated
ROBOT_MAP_VERSION=<actual-map-version>
LOCATION_REQUIRE_ACTIVE_MAP_VERSION=1
```

In validated mode, uncalibrated or incompatible records stay advisory and cannot produce executable coordinate navigation.

## Operator workflow

Add/update a location:

```bash
python3 docker/manage_rag_location.py <location_id> \
  --x <x_m> --y <y_m> --yaw-deg <yaw_deg> \
  --map-version <map_version> \
  --alias "<name>" \
  --description "<meaning>" \
  --enable
```

For regions:

```bash
python3 docker/manage_rag_location.py <location_id> \
  --type STATIC_REGION \
  --x <entry_x> --y <entry_y> --yaw-deg <entry_yaw> \
  --region-point <x1,y1> \
  --region-point <x2,y2> \
  --region-point <x3,y3> \
  --enable
```

Add/update Scene knowledge:

```bash
python3 docker/manage_rag_scene.py <scene_id> \
  --knowledge-type LOCATION_PRIOR \
  --content "<environment relation/search prior>" \
  --target-term "<object/category>" \
  --location-id <location_id> \
  --confidence 0.9 \
  --requires-visual-verification \
  --enable
```

Then:

```bash
python3 docker/validate_typed_rag.py
curl -s -X POST http://127.0.0.1:8000/api/rag/sync | jq .
```

No Python or planner-prompt change is required for a new location or Scene prior.

## Registered-term retrieval robustness

Location aliases and Scene `target_terms` / `object_categories` are deterministic typed-RAG identity channels. They are stored in RAG data, not in prompts or Python use-case branches.

- Latin location aliases use normalized phrase-boundary matching.
- CJK location aliases can match inside a full CJK sentence without requiring spaces.
- Exact Scene target/category terms are merged ahead of probabilistic FTS results, while broader Scene context still uses fan-out FTS retrieval.

For high-value search priors, prefer explicit `target_terms` or `object_categories` in the Scene document instead of relying only on prose/tags.
