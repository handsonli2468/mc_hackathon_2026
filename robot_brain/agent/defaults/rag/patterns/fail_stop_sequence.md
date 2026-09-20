# Fail-stop mission sequence
Use a top-level Sequence for dependent phases. Each downstream phase may execute only after the prior phase succeeds. If bounded search, navigation, or manipulation exhausts and returns FAILURE, later phases must remain unticked.
