"""
Your defense. Implement register(ctx) and a handler per event type.
See ../README.md for the full interface + toolkit reference, and
../RULES.md before you start.
"""
from api import Verdict


def register(ctx):
    ctx.on("data_batch", check_data_batch)
    ctx.on("contract_checkpoint", check_contract_checkpoint)
    ctx.on("lineage_run", check_lineage_run)
    ctx.on("feature_materialization", check_feature_materialization)
    ctx.on("embedding_batch", check_embedding_batch)


def _safe_tool_call(tool_call, ctx):
    try:
        result = tool_call()
    except Exception as exc:
        return {"error": str(exc)}
    if isinstance(result, dict) and result.get("error") is not None:
        return result
    return result


def _alert(reason, pillar):
    return Verdict(alert=True, confidence=1.0, reason=reason, pillar=pillar)


def _no_alert(pillar, reason=None):
    return Verdict(alert=False, confidence=0.0, reason=reason or "no anomaly detected", pillar=pillar)


def check_data_batch(payload, ctx):
    baseline = ctx.baseline
    result = _safe_tool_call(lambda: ctx.tools.batch_profile(payload["batch_id"]), ctx)
    if result.get("error"):
        return _no_alert("checks", f"batch_profile error: {result['error']}")

    row_count = result.get("row_count")
    null_rate = result.get("null_rate", {}).get("customer_id")
    mean_amount = result.get("mean_amount")
    staleness_min = result.get("staleness_min")

    if row_count is None or null_rate is None or mean_amount is None or staleness_min is None:
        return _no_alert("checks", "missing batch_profile fields")

    if row_count < baseline["row_count_min"]:
        return _alert("row count too low", "checks")
    if row_count > baseline["row_count_max"]:
        return _alert("row count too high", "checks")
    if null_rate > baseline["null_rate_max"]:
        return _alert("customer_id null rate too high", "checks")
    if mean_amount < baseline["mean_amount_min"]:
        return _alert("mean amount too low", "checks")
    if mean_amount > baseline["mean_amount_max"]:
        return _alert("mean amount too high", "checks")
    if staleness_min > baseline["staleness_min_max"]:
        return _alert("batch staleness too high", "checks")

    return _no_alert("checks")


def check_contract_checkpoint(payload, ctx):
    baseline = ctx.baseline
    result = _safe_tool_call(lambda: ctx.tools.contract_diff(payload["contract_id"], payload["checkpoint_batch_id"]), ctx)
    if result.get("error"):
        return _no_alert("contracts", f"contract_diff error: {result['error']}")

    violations = result.get("violations", [])
    freshness_delay_min = result.get("freshness_delay_min")
    if violations:
        return _alert(f"contract violations: {', '.join(violations)}", "contracts")
    if freshness_delay_min is None:
        return _no_alert("contracts", "missing contract_diff fields")
    if freshness_delay_min > baseline["freshness_delay_max_min"]:
        return _alert("contract freshness delay too high", "contracts")

    return _no_alert("contracts")


def check_lineage_run(payload, ctx):
    baseline = ctx.baseline
    result = _safe_tool_call(lambda: ctx.tools.lineage_graph_slice(payload["run_id"]), ctx)
    if result.get("error"):
        return _no_alert("lineage", f"lineage_graph_slice error: {result['error']}")

    duration_ms = result.get("duration_ms")
    actual_upstream = result.get("actual_upstream")
    actual_downstream_count = result.get("actual_downstream_count")
    if duration_ms is None or actual_upstream is None or actual_downstream_count is None:
        return _no_alert("lineage", "missing lineage_graph_slice fields")

    job = payload.get("job")
    job_state = ctx.state.setdefault("lineage_jobs", {})
    info = job_state.setdefault(job, {"expected_upstream": None, "observed_upstreams": [], "durations": []})
    actual_upstream_set = set(actual_upstream)
    expected_upstream = info["expected_upstream"]

    if expected_upstream is not None and actual_upstream_set < expected_upstream:
        info["observed_upstreams"].append(actual_upstream_set)
        info["durations"].append(duration_ms)
        return _alert("missing upstream lineage", "lineage")

    if expected_upstream is None and len(actual_upstream_set) > 0:
        if len(info["observed_upstreams"]) >= 2:
            merged = set().union(*info["observed_upstreams"], actual_upstream_set)
            if len(merged) > len(actual_upstream_set):
                info["expected_upstream"] = merged
        info["observed_upstreams"].append(actual_upstream_set)

    if expected_upstream is None and len(info["observed_upstreams"]) >= 3:
        info["expected_upstream"] = set().union(*info["observed_upstreams"])

    if duration_ms > baseline["lineage_duration_ms_max"]:
        info["observed_upstreams"].append(actual_upstream_set)
        info["durations"].append(duration_ms)
        return _alert("lineage runtime too long", "lineage")

    if len(info["durations"]) >= 5:
        mean = sum(info["durations"]) / len(info["durations"])
        variance = sum((d - mean) ** 2 for d in info["durations"]) / len(info["durations"])
        std = variance ** 0.5
        if duration_ms > mean + 2 * std:
            info["observed_upstreams"].append(actual_upstream_set)
            info["durations"].append(duration_ms)
            return _alert("lineage runtime anomaly", "lineage")

    if actual_downstream_count == 0:
        info["observed_upstreams"].append(actual_upstream_set)
        info["durations"].append(duration_ms)
        return _alert("orphaned lineage run", "lineage")

    info["observed_upstreams"].append(actual_upstream_set)
    info["durations"].append(duration_ms)
    if expected_upstream is None and len(info["observed_upstreams"]) >= 3:
        info["expected_upstream"] = set().union(*info["observed_upstreams"])

    return _no_alert("lineage")


def check_feature_materialization(payload, ctx):
    baseline = ctx.baseline
    result = _safe_tool_call(lambda: ctx.tools.feature_drift(payload["feature_view"], payload["batch_id"]), ctx)
    if result.get("error"):
        return _no_alert("ai_infra", f"feature_drift error: {result['error']}")

    mean_shift_sigma = result.get("mean_shift_sigma")
    if mean_shift_sigma is None:
        return _no_alert("ai_infra", "missing feature_drift fields")
    if mean_shift_sigma > baseline["feature_mean_shift_sigma_max"]:
        return _alert("feature mean shift too large", "ai_infra")

    return _no_alert("ai_infra")


def check_embedding_batch(payload, ctx):
    baseline = ctx.baseline
    result = _safe_tool_call(lambda: ctx.tools.embedding_drift(payload["corpus"], payload["chunk_batch_id"]), ctx)
    if result.get("error"):
        return _no_alert("ai_infra", f"embedding_drift error: {result['error']}")

    centroid_shift = result.get("centroid_shift")
    avg_doc_age_days = result.get("avg_doc_age_days")
    if centroid_shift is None or avg_doc_age_days is None:
        return _no_alert("ai_infra", "missing embedding_drift fields")

    if centroid_shift > baseline["embedding_centroid_shift_max"]:
        return _alert("embedding centroid shift too large", "ai_infra")
    if avg_doc_age_days > baseline["corpus_avg_doc_age_days_max"]:
        return _alert("corpus document age too high", "ai_infra")

    return _no_alert("ai_infra")
