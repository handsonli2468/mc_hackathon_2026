# Target images

Put target images here (e.g. `target.png`). They are installed to
`share/object_tracker/targets/` and can be selected by file name:

```bash
ros2 launch object_tracker orb_tracker_bringup.launch.py target_image_path:=target.png
```

Capture targets from the live camera ([space] save frame, [r] crop ROI):

```bash
ros2 run object_tracker frame_capture_node --ros-args -p save_dir:=src/object_tracker/targets
```

Tips:
- Camera is a RealSense D405: capture and track at about 7-50 cm, the range where its depth is reliable.
- Use a flat, textured object (logos, printed patterns). Plain / glossy surfaces give too few ORB features.
- Crop tightly to the object; background (or your hand) in the target image creates wrong matches.
- The published point is the image center of the target, so crop symmetrically around the point you care about.
- Rebuild (`colcon build`) after adding images, or pass an absolute path to `target_image_path`.
