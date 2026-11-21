from datetime import datetime, timezone

import numpy as np
from sklearn.cluster import DBSCAN
from sqlalchemy import select

from ..models import AnalysisJob, RepairZone, SurveyBatch, SurveyPoint, WorkNotice
from ..constants.enums import LOCKING_NOTICE_STATUSES, PENDING_ZONE_STATUSES
from .audit import record
from .geometry import bounds, overlaps

EPS_METERS = 26.0
MIN_SAMPLES = 3

# 新片区做道路边界冲突复核时视为在建的施工单。
CONFLICT_NOTICE_STATUSES = ("submitted", "acknowledged", "active")


class PublishError(Exception):
    """发布失败。调用方回滚事务，旧版待派工片区保持不变。"""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def build_input(db, batch):
    points = db.execute(select(SurveyPoint).where(SurveyPoint.batch_id == batch.id).order_by(SurveyPoint.id)).scalars().all()
    return [
        {
            "id": p.id,
            "point_code": p.point_code,
            "x": p.x,
            "y": p.y,
            "settlement_mm": p.settlement_mm,
            "coordinate_reference": p.coordinate_reference,
            "source": p.source,
        }
        for p in points
    ]


def validate_datum(batch, points):
    return {batch.coordinate_reference, *(p["coordinate_reference"] for p in points)}


def _split_free_and_locked(zones, notices):
    """返回（锁定片区, 锁定片区点 id 集合）。只要片区开过单，范围就冻结。"""
    noticed = {n.zone_id: n for n in notices if n.status in LOCKING_NOTICE_STATUSES and n.zone_id is not None}
    locked_zones = [z for z in zones if z.id in noticed]
    locked_point_ids = {pid for z in locked_zones for pid in (z.point_ids or [])}
    return locked_zones, locked_point_ids


def _dbscan_clusters(free_points):
    if not free_points:
        return [], 0
    matrix = np.array([[p["x"], p["y"]] for p in free_points], dtype=float)
    labels = DBSCAN(eps=EPS_METERS, min_samples=MIN_SAMPLES).fit_predict(matrix)
    clusters = []
    noise_count = 0
    for label in sorted({int(x) for x in labels}):
        members = [p for p, current in zip(free_points, labels) if int(current) == label]
        if label == -1:
            noise_count += len(members)
            continue
        clusters.append((label, members))
    return clusters, noise_count


def _publish(db, batch, mode):
    """在持有批次行锁的事务内重算并发布待派工热区。

    任一步骤抛出异常，调用方 rollback，旧版待派工片区原样保留。
    """
    now = datetime.now(timezone.utc)
    zones = db.execute(select(RepairZone).where(RepairZone.batch_id == batch.id)).scalars().all()
    zone_ids = [z.id for z in zones]
    notices = (
        db.execute(select(WorkNotice).where(WorkNotice.zone_id.in_(zone_ids))).scalars().all()
        if zone_ids
        else []
    )
    locked_zones, locked_point_ids = _split_free_and_locked(zones, notices)

    points = db.execute(select(SurveyPoint).where(SurveyPoint.batch_id == batch.id).order_by(SurveyPoint.id)).scalars().all()
    free_points = [
        {"id": p.id, "point_code": p.point_code, "x": p.x, "y": p.y, "settlement_mm": p.settlement_mm,
         "coordinate_reference": p.coordinate_reference, "source": p.source}
        for p in points if p.id not in locked_point_ids
    ]

    clusters, noise_count = _dbscan_clusters(free_points)

    revision = (batch.zone_revision or 0) + 1
    short_code = batch.batch_code.replace("SURV-2026-", "")

    # 旧版待派工片区整批停用，开单入口随即失效；已开单片区不受影响。
    # notice_sent 但施工单全部作废的片区也属于待重新核算。
    retiring = [
        z for z in zones
        if z.id not in {lz.id for lz in locked_zones}
        and (z.status in PENDING_ZONE_STATUSES or z.status == "notice_sent")
    ]
    for zone in retiring:
        zone.status = "superseded"
        zone.superseded_at = now

    conflicting_notices = list(
        db.execute(select(WorkNotice).where(WorkNotice.status.in_(CONFLICT_NOTICE_STATUSES))).scalars().all()
    )

    created = []
    for index, (label, members) in enumerate(clusters, 1):
        b = bounds(members)
        center_x = sum(p["x"] for p in members) / len(members)
        center_y = sum(p["y"] for p in members) / len(members)
        avg_settlement = sum(p["settlement_mm"] for p in members) / len(members)
        max_settlement = max(p["settlement_mm"] for p in members)
        area = max(1, (b["max_x"] - b["min_x"]) * (b["max_y"] - b["min_y"]))
        density = round(len(members) / area * 10000, 3)
        zone = RepairZone(
            zone_code=f"RZ-{short_code}-R{revision}-{index:02d}",
            batch_id=batch.id,
            cluster_label=label,
            name=f"沉降热区 {index}",
            status="proposed",
            point_ids=[p["id"] for p in members],
            center_x=round(center_x, 2),
            center_y=round(center_y, 2),
            average_settlement_mm=round(avg_settlement, 2),
            max_settlement_mm=round(max_settlement, 2),
            local_density=density,
            boundary=b,
            revision=revision,
            original_cluster_evidence={
                "algorithm": "DBSCAN",
                "eps_m": EPS_METERS,
                "min_samples": MIN_SAMPLES,
                "point_codes": [p["point_code"] for p in members],
                "label": label,
                "revision": revision,
                "mode": mode,
                "excluded_locked_zone_codes": [z.zone_code for z in locked_zones],
            },
        )
        db.add(zone)
        created.append(zone)

    db.flush()

    conflicts = 0
    for zone in created:
        for notice in conflicting_notices:
            if overlaps(zone.boundary, notice.boundary):
                zone.status = "needs_split"
                zone.conflict_notice_id = notice.id
                zone.split_reason = f"与 {notice.road_name} 在建边界重叠，需按施工边界拆分"
                conflicts += 1
                break

    point_models = {p.id: p for p in points}
    for p in points:
        model_point = point_models[p.id]
        if p.id in locked_point_ids:
            # 锁定片区的测点保留原聚类归属，不参与邻近点聚类。
            continue
        zone = next((z for z in created if p.id in (z.point_ids or [])), None)
        model_point.cluster_label = zone.cluster_label if zone else None
        model_point.local_density = zone.local_density if zone else None
        model_point.quality_status = "clustered"
        if model_point.source == "supplement":
            # 记录补传测点是否已被本次发布覆盖，供页面判断是否仍需重算。
            model_point.published_revision = revision

    batch.zone_revision = revision

    result = {
        "algorithm": "DBSCAN + local density score",
        "mode": mode,
        "eps_m": EPS_METERS,
        "min_samples": MIN_SAMPLES,
        "cluster_count": len(created),
        "noise_count": noise_count,
        "conflicted_zones": conflicts,
        "point_count": len(points),
        "clustered_point_count": len(free_points),
        "revision": revision,
        "published_zone_codes": [z.zone_code for z in created],
        "retained_zone_codes": [z.zone_code for z in locked_zones],
        "superseded_zone_codes": [z.zone_code for z in retiring],
        "excluded_point_codes": [p.point_code for p in points if p.id in locked_point_ids],
        "formula": "local_density = cluster_points / bbox_area × 10000",
    }
    record(
        db,
        None,
        "publish_pending_zones",
        "SurveyBatch",
        batch.batch_code,
        {
            "mode": mode,
            "revision": revision,
            "published": result["published_zone_codes"],
            "retained": result["retained_zone_codes"],
            "superseded": result["superseded_zone_codes"],
        },
    )
    return result


def run_cluster(db, job, mode):
    """在批次行锁内完成重算与发布，和开单操作串行化。"""
    batch = db.execute(select(SurveyBatch).where(SurveyBatch.id == job.batch_id).with_for_update()).scalar_one_or_none()
    if not batch:
        raise PublishError("BATCH_MISSING", "测量批次不存在")
    points = build_input(db, batch)
    refs = validate_datum(batch, points)
    if len(refs) != 1:
        conflict_codes = [p["point_code"] for p in points if p["coordinate_reference"] != batch.coordinate_reference]
        raise PublishError(
            "DATUM_CONFLICT",
            "坐标基准不一致，发布已回滚，旧版待派工片区保持不变",
            point_codes=conflict_codes,
            references=sorted(refs),
        )
    return _publish(db, batch, mode)


def cluster_batch(db, job_id):
    job = db.get(AnalysisJob, job_id)
    if not job:
        return
    mode = (job.input_summary or {}).get("mode", "initial")
    previous_status = (job.input_summary or {}).get("batch_status_before")
    try:
        job.status = "running"
        db.commit()
        result = run_cluster(db, job, mode)
        job = db.get(AnalysisJob, job_id)
        batch = db.get(SurveyBatch, job.batch_id)
        job.result = result
        job.status = "succeeded"
        job.finished_at = datetime.now(timezone.utc)
        batch.status = "supplemented" if mode == "supplement" else "clustered"
        db.commit()
    except PublishError as exc:
        db.rollback()
        job = db.get(AnalysisJob, job_id)
        batch = db.get(SurveyBatch, job.batch_id)
        job.status = "failed"
        job.error = exc.message
        job.result = {"mode": mode, "publish": "failed", "code": exc.code, **exc.extra}
        job.finished_at = datetime.now(timezone.utc)
        if batch:
            if mode == "initial":
                batch.status = "blocked"
            else:
                batch.status = previous_status or "supplemented"
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        job = db.get(AnalysisJob, job_id)
        batch = db.get(SurveyBatch, job.batch_id)
        job.status = "failed"
        job.error = str(exc)
        job.result = {"mode": mode, "publish": "failed"}
        job.finished_at = datetime.now(timezone.utc)
        if batch:
            if mode == "initial":
                batch.status = "blocked"
            else:
                batch.status = previous_status or "supplemented"
        db.commit()
