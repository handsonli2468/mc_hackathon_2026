# Ground before navigation
NavigateToDetectedObject consumes the latest continuously published camera pose and has no object-name port. Select the exact target with VisualizeObject, then complete the bounded ReactiveFallback(IsObjectFound, Patrol) monitor before navigation. A relational clue such as "person beside the table" is a visual query, not a known map coordinate.

Before object-relative navigation, establish the exact target with perception and verify it is found. Scene/location priors may guide where to search first, but do not themselves prove that a dynamic target is currently there.
