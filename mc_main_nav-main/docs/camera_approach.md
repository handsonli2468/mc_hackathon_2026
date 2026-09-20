# Camera-guided approach (`TrackObject`)

Nav2 drives to a **map position**, so it is only as accurate as the global camera and the VLM's
estimate of where the object is. `TrackObject` closes the loop on **what the onboard camera sees right
now**, so the robot lines up with the real object. It does not use Nav2: the servo drives `/cmd_vel`
directly.

Typical tree: `NavigateToPoint x y` → `SetGripper position="0"` → `TrackObject distance="contact"` → `SetGripper position="100"`
(full example: [`examples/grab_cup_with_camera.xml`](../examples/grab_cup_with_camera.xml)).

## What the vision team must publish

**Topic `/object_detections`, type `robot_interfaces/msg/ObjectDetection`, about 10 Hz, one message per
object being tracked.** A topic, not a service: a control loop needs a stream, and stale data must be
visible.

```
std_msgs/Header header   # stamp = when the frame was captured; frame_id = base_link
string  object_name      # "cup"
float32 x                # metres, forward from the robot's centre
float32 y                # metres, left of the robot's centre
bool    visible          # false when it cannot be seen right now
float32 confidence       # 0..1
```

Notes for whoever implements it:
- **Publish even when the object is not visible** (`visible: false`). Silence is indistinguishable from a
  crashed node; an explicit "I can't see it" lets the robot stop immediately.
- **x/y are from the robot's centre.** If the camera is mounted forward of the centre, add that offset
  before publishing, or tell us and we'll configure it.
- **Timestamp the capture, not the publish.** Detections older than `detection_timeout` (1 s) are ignored.
- Accuracy matters most under 20 cm. A few millimetres of error there is worth more than perfect range at 1 m.

## How the servo drives

`diff_nav`'s `TrackObject` server, at 10 Hz (`visual_servo.py` holds the maths, with no ROS in it):

1. **Turn to face it** when the bearing is worse than `align_first` (0.35 rad).
2. **Creep in**, steering to keep it centred: `v = k_linear x (gap - stop_distance)`, capped at 5 cm/s,
   which is slower than Nav2 because we are close to things.
3. **Stop** when the gap is within 1 cm of the target and the object is centred within 0.10 rad.
4. **Too close** (gap smaller than asked) → it reverses.

`distance` uses named presets: `contact` 1 cm, `near` 5 cm, `far` 10 cm, measured
from the robot's **front edge** to the object's centre.

### The last few centimetres, where the camera goes blind

A fixed forward camera with no tilt loses the object just when it matters. The servo handles this:
when the detection stops **and the last gap was under `blind_gap` (12 cm)**, it keeps driving straight,
measuring progress with the **global camera** (which still sees the robot), and stops once it has
covered the remaining gap. The result says so: *"lined up with 'cup' at 1.0 cm (last stretch blind, too
close for the camera)"*.

Lose sight further out than that and it stops and fails instead of guessing.

## Failures, and what the tree should do

| Message | Meaning | What the LLM can do |
|---|---|---|
| `never saw the object; is it in the camera's view?` | Not in view at all — the servo does **not** search | `RotateInPlace` a little, `VisualizeObject`, then retry |
| `lost sight of the object 0.25 m short of the target` | Disappeared while still far away | Retry, or drive closer with `NavigateToPoint` first |
| `camera approach timed out after 40s` | Not converging (slipping wheels, bad detections) | Back off and retry |
| `lost the robot pose (global camera)` | The global camera stopped | Nothing the tree can fix; a human must look |

## Testing without hardware

`sim_camera` fakes the pipeline: it takes object positions from the VLM's `/get_object_pose`, applies a
field of view (60°), a max range (1 m) and a **min range (8 cm, where a real fixed camera loses sight)**,
adds noise, and publishes `/object_detections`.

```bash
MOCK_DISABLE=navigate_to_object,rotate_in_place,is_at_object,navigate_to_point,track_object \
  docker compose -f docker/compose.yaml --profile sim up -d engine mocks map nav-sim
./scripts/bt_exec.py run examples/grab_cup_with_camera.xml
./scripts/test_nav_sim.py camera      # the camera tests only
```

On the robot Pi, `docker compose --profile robot up -d sim-camera` does the same thing until the real
pipeline exists.

## Verified in simulation (2026-09-19)

| Check | Result |
|---|---|
| Unit tests for the control law (23 total in `diff_nav`) | ✅ stop distances 1/5/10 cm, off-centre approach, speed limits, reversing when too close, blind finish, both give-up cases |
| Full tree: find → Nav2 → camera → grasp | ✅ final gap 2 cm for a 1 cm target, heading error 0.003 rad |
| Camera loses the object at 8 cm | ✅ finished blind, and said so |
| Object behind the robot | ✅ failed with "never saw the object", no searching |
| End-to-end suite | ✅ 16/16 |

**Not tested on hardware.** The simulated camera is ideal apart from noise: no motion blur, no false
positives, no lag beyond the publish rate.

## Settings (`ws/src/diff_nav/config/nav.yaml`, under `servo:`)

`max_linear` 0.05, `max_angular` 0.8, `k_linear` 0.8, `k_angular` 1.8, `align_first` 0.35 rad,
`yaw_tolerance` 0.10 rad, `distance_tolerance` 0.01 m, `blind_gap` 0.12 m, plus `servo_timeout` 40 s and
`detection_timeout` 1 s. All can be changed while running with `ros2 param set /diff_nav servo.<name>`.
