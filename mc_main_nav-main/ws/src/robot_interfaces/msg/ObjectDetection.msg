# Published by the onboard-camera pipeline (VLM/SAM2) at ~10 Hz, one message per
# detected object. Topic: /object_detections
#
# header.stamp    when the frame was captured (NOT when it was published)
# header.frame_id the frame x/y are measured in, normally base_link
std_msgs/Header header
string object_name
# Where the object is relative to the robot, in metres:
#   x forward, y left, from the robot's centre (base_link origin)
float32 x
float32 y
# false = the pipeline is looking for this object but cannot see it right now
# (out of view, occluded, or too close to the camera)
bool visible
float32 confidence   # 0..1
