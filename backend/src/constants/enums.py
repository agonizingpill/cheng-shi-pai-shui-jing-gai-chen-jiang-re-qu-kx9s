ROLE_OPERATOR = "operator"
ROLE_SUPERVISOR = "supervisor"
ROLE_VIEWER = "viewer"
BATCH_STATUSES = ["open", "blocked", "ready", "queued", "clustered", "recomputing", "supplemented"]
ZONE_STATUSES = ["proposed", "needs_split", "ready_for_notice", "notice_sent", "repairing", "awaiting_resurvey", "closed", "split", "superseded"]
NOTICE_STATUSES = ["draft", "submitted", "acknowledged", "active", "completed", "cancelled"]
# 仍可派工的待派工片区状态
PENDING_ZONE_STATUSES = ["proposed", "needs_split", "ready_for_notice"]
# 有开单按钮的片区状态
DISPATCHABLE_ZONE_STATUSES = ["proposed", "ready_for_notice"]
# 已开施工联动单、范围冻结的片区状态
LOCKED_ZONE_STATUSES = ["notice_sent", "repairing", "awaiting_resurvey", "closed"]
# 视为已开单（范围冻结）的施工联动单状态
LOCKING_NOTICE_STATUSES = ["draft", "submitted", "acknowledged", "active", "completed"]
POINT_SOURCES = ["initial", "supplement"]
JOB_MODES = ["initial", "supplement"]
