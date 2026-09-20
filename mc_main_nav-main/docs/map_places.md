# Named places on the field

The field is **1.80 m x 1.20 m**, origin at the **bottom-left corner**, X to the right,
Y up, angles in degrees counter-clockwise with **0 = +X**.

The places are **packaged in the engine**: a generated tree calls them by name and never
writes a coordinate.

```xml
<SubTree ID="GoHome" name="go_home"/>
```

```
 y
1.20 +-----------------------------------------------+
     |                                               |
0.90 |   [NW 0.45,0.90]          [NE 1.35,0.90]      |
     |        o------------o   patrol box            |
     |        | 0.60,0.80  | 1.20,0.80               |
0.60 |        |     * CENTRE 0.90,0.60               |
     |        | 0.60,0.40  | 1.20,0.40               |
     |        o------------o                         |
0.30 |  (HOME 0.30,0.30)  [SW 0.45,0.30] [SE 1.35,0.30]
     |                            (STORAGE 1.50,0.30) |
0.00 +-----------------------------------------------+ x
    0.00            0.90                          1.80
```

## Why the margins are what they are

**The robot radius is 0.15 m**, so the robot is 30 cm across and the field only holds
**1.50 m x 0.90 m of legal centre positions**. That is the number that decides everything else:

- A goal whose centre is within **0.15 m** of a wall is **lethal** and Nav2 refuses it outright.
- `inflation_radius` is **0.25 m**, so between 0.15 and 0.25 m the goal is legal but expensive:
  the planner will take it and then squirm along the wall.
- Therefore **every place keeps the centre at least 0.30 m from a wall**, which leaves 0.05 m of
  slack beyond the inflation.

Both costmaps in `ws/src/diff_nav/config/nav2_table.yaml` carry these values.

## The places

| Subtree | x | y | yaw | What it is for |
|---|---|---|---|---|
| `GoHome` | 0.30 | 0.30 | 0 | Where the robot starts and parks. Bottom-left, facing into the field so it drives away without turning first. |
| `GoToStorage` | 1.50 | 0.30 | 0 | Drop-off, diagonally opposite HOME so a loaded carry never crosses the dock. Drives `slow`. |
| `GoToCentre` | 0.90 | 0.60 | - | The middle, and the centre of the patrol box. |
| `GoToVantageNW` | 0.45 | 0.90 | - | Quadrant scan posts: four points that see the corners a patrol lap never reaches. |
| `GoToVantageNE` | 1.35 | 0.90 | - | |
| `GoToVantageSE` | 1.35 | 0.30 | - | |
| `GoToVantageSW` | 0.45 | 0.30 | - | |
| `ScanFromCentre` | - | - | - | Drive to CENTRE and turn 45 deg at a time **for ever**. Never succeeds on its own: put it under a `Timeout` or a `ReactiveFallback`. |
| `TourTheCorners` | - | - | - | One lap of the four vantage posts. Succeeds when the lap finishes. |

Yaw `-` means it does not matter, so the robot faces the way it drove.

**Fixed in the engine, not a place:** the `Patrol` rectangle is 0.60 x 0.40 m centred on CENTRE, so
its corners are (0.60, 0.80), (1.20, 0.80), (1.20, 0.40), (0.60, 0.40) - all at least 0.40 m from a
wall, comfortable even for a 30 cm robot.

## How the packaging works

`ws/src/bt_engine/places/places.xml` holds one `<BehaviorTree ID="...">` per place. The engine
registers the whole file into its factory at start-up, so:

- a submitted tree may **call** `<SubTree ID="GoHome" name="go_home"/>` without defining it;
- the places appear in `GET /nodes` as `<SubTree ID="GoHome"/>` entries, so the LLM can see them;
- **coordinates live in one file**. Moving HOME is an edit to `places.xml` and an engine restart,
  not a change to every generated tree.

### Changing a place

**Edit `ws/src/bt_engine/places/places.xml` and send the next tree. That is all.**

- **No rebuild.** `colcon build --symlink-install` symlinks the installed file back to the source, so
  the engine reads the file you edited.
- **No restart.** The engine checks the file's timestamp on every `/nodes`, `/validate` and
  `/execute`, and re-imports it when it has changed. The log says
  `places re-read after an edit: GoHome, ...`.
- **The whole set is rebuilt**, so adding a place makes it callable immediately and **deleting one
  really removes it** - a tree that still calls it then fails with `Can't find a tree with name: X`.
- **A broken file is refused, not obeyed**: the previous places stay registered and the engine logs
  `places file is broken, keeping the old one`.
- A run already in progress keeps the definitions it started with.

On the mini PC the file lives at `~/mc_main_nav/ws/src/bt_engine/places/places.xml`; `./mc deploy pc`
overwrites it with your local copy, so edit it here, not there.

Turn the whole mechanism off with `ros2 param set /bt_engine places_file ""` and a restart.

**The import always wins.** If a submitted tree also defines a `<BehaviorTree ID="GoHome">` of its
own, that definition is dropped at load time and the imported one is used, so a stale copy in an old
tree cannot silently move a place for every later run. It is reported rather than done quietly:

- `POST /validate` answers `{"ok": true, "used_imported_subtrees": ["GoHome", ...]}`;
- the run's `notes` carry `the tree redefined 'GoHome'; the engine's imported place was used instead`.

This is why `examples/fetch_cup_mission.xml` can carry readable copies of the places at the bottom
and still be guaranteed to run the engine's versions.

The places are ordinary subtrees, so their `speed` is fixed inside `places.xml`. A tree that needs a
different speed to the same point writes its own `NavigateToPoint` instead.

## Before trusting any of this

These are **geometry, not survey**. The origin is wherever the camera team's ArUco frame puts it,
and nobody has yet confirmed that (0, 0) is the bottom-left corner of the *physical* field rather
than of their calibration board. Run `GoHome` and the two far vantage posts with RViz open and check
the robot ends up where the table says before a mission depends on it.
