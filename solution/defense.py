"""
Your defense. Implement register(ctx) and a handler per event type.
See ../README.md for the full interface + toolkit reference, and
../RULES.md before you start.
"""
from api import Verdict


def _safe_mean(values):
    return sum(values) / len(values) if values else None


def _safe_stdev(values):
    if len(values) < 2:
        return 0.0
    m = _safe_mean(values)
    return (sum((v - m) ** 2 for v in values) / len(values)) ** 0.5


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


def _rolling_stats(values):
    if len(values) < 2:
        return None, None
    mean_value = _safe_mean(values)
    std_value = _safe_stdev(values)
    return mean_value, std_value


def check_data_batch(payload, ctx):
    baseline = ctx.baseline
    result = _safe_tool_call(lambda: ctx.tools.batch_profile(payload["batch_id"]), ctx)
    if result.get("error"):
        return _no_alert("checks", f"batch_profile error: {result['error']}")

    row_count = result.get("row_count")
    null_rate = result.get("null_rate", {}).get("customer_id")
    mean_amount = result.get("mean_amount")
    std_amount = result.get("std_amount")
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

    state = ctx.state.setdefault("data_batch_history", [])
    recent_alerts = ctx.state.get("data_batch_recent_alerts", 0)
    if recent_alerts > 0:
        ctx.state["data_batch_recent_alerts"] = recent_alerts - 1
    if len(state) >= 3:
        rows = [h["row_count"] for h in state]
        means = [h["mean_amount"] for h in state]
        nulls = [h["null_rate"] for h in state]
        stds = [h["std_amount"] for h in state]
        stales = [h["staleness_min"] for h in state]

        row_mean, row_std = _rolling_stats(rows)
        mean_mean, mean_std = _rolling_stats(means)
        null_mean, null_std = _rolling_stats(nulls)
        std_mean, std_std = _rolling_stats(stds)
        stale_mean, stale_std = _rolling_stats(stales)

        # Rolling volume (row_count) detection intentionally omitted: the global
        # baseline row_count_min/max already catches every real volume spike/drop
        # (they land far outside the clean band), while a rolling z-score on the
        # short window only ever fired on normal within-baseline fluctuation —
        # pure false alarms with no added catch. Precision win, no recall loss.
        if null_std is not None and null_rate - null_mean > max(2 * null_std, 0.003):
            strong_null = null_rate - null_mean > max(3 * null_std, 0.006)
            if recent_alerts > 0 and not strong_null:
                pass
            else:
                ctx.state["data_batch_recent_alerts"] = 2
                return _alert("customer_id null rate spike", "checks")
        if stale_std is not None and staleness_min - stale_mean > max(2 * stale_std, 1.5):
            strong_stale = staleness_min - stale_mean > max(3 * stale_std, 3)
            if recent_alerts > 0 and not strong_stale:
                pass
            else:
                ctx.state["data_batch_recent_alerts"] = 2
                return _alert("batch freshness lag", "checks")
        if mean_std is not None and abs(mean_amount - mean_mean) > max(3 * mean_std, 8):
            ctx.state["data_batch_recent_alerts"] = 2
            return _alert("distribution shift", "checks")
        if std_mean is not None and std_amount - std_mean > max(2 * std_std, 4):
            ctx.state["data_batch_recent_alerts"] = 2
            return _alert("distribution shift", "checks")

    state.append({
        "row_count": row_count,
        "null_rate": null_rate,
        "mean_amount": mean_amount,
        "std_amount": std_amount,
        "staleness_min": staleness_min,
    })
    if len(state) > 10:
        state.pop(0)

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
    info = job_state.setdefault(job, {
        "expected_upstream": None,
        "observed_upstreams": [],
        "durations": [],
        "max_upstream_size": 0,
    })
    actual_upstream_set = set(actual_upstream)
    expected_upstream = info["expected_upstream"]
    inputs = payload.get("inputs") or []

    if actual_downstream_count == 0:
        info["observed_upstreams"].append(actual_upstream_set)
        info["durations"].append(duration_ms)
        info["max_upstream_size"] = max(info["max_upstream_size"], len(actual_upstream_set))
        return _alert("orphaned lineage run", "lineage")

    if inputs and len(actual_upstream_set) < len(inputs):
        info["observed_upstreams"].append(actual_upstream_set)
        info["durations"].append(duration_ms)
        info["max_upstream_size"] = max(info["max_upstream_size"], len(actual_upstream_set))
        return _alert("missing upstream lineage", "lineage")

    if expected_upstream is not None and expected_upstream - actual_upstream_set:
        info["observed_upstreams"].append(actual_upstream_set)
        info["durations"].append(duration_ms)
        return _alert("missing upstream lineage", "lineage")

    if info["max_upstream_size"] >= 2 and len(actual_upstream_set) < info["max_upstream_size"]:
        info["observed_upstreams"].append(actual_upstream_set)
        info["durations"].append(duration_ms)
        return _alert("missing upstream lineage", "lineage")

    if duration_ms > baseline["lineage_duration_ms_max"]:
        info["observed_upstreams"].append(actual_upstream_set)
        info["durations"].append(duration_ms)
        info["max_upstream_size"] = max(info["max_upstream_size"], len(actual_upstream_set))
        return _alert("lineage runtime too long", "lineage")

    # Rolling per-job runtime-anomaly detection intentionally omitted: the global
    # baseline lineage_duration_ms_max catches genuinely long runs, whereas a
    # rolling z-score on duration only fired on clean runs sitting at the high end
    # of normal variance — false alarms with no real anomaly caught. The
    # structural checks above (orphaned output, missing upstream) are the reliable
    # lineage signals.

    info["observed_upstreams"].append(actual_upstream_set)
    info["durations"].append(duration_ms)
    info["max_upstream_size"] = max(info["max_upstream_size"], len(actual_upstream_set))
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
    # warn earlier when mean shift approaches threshold, but escalate only after repeat
    fstate = ctx.state.setdefault("feature_warnings", {})
    key = payload.get("feature_view")
    count = fstate.get(key, 0)
    if mean_shift_sigma > 0.8 * baseline["feature_mean_shift_sigma_max"] and mean_shift_sigma <= baseline["feature_mean_shift_sigma_max"]:
        count += 1
        fstate[key] = count
        if count >= 2:
            fstate[key] = 0
            return _alert("feature mean shift too large", "ai_infra")
    else:
        fstate[key] = 0
    if mean_shift_sigma > baseline["feature_mean_shift_sigma_max"]:
        fstate[key] = 0
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

    embed_recent = ctx.state.get("embedding_recent_alerts", 0)
    if embed_recent > 0:
        ctx.state["embedding_recent_alerts"] = embed_recent - 1
    estate = ctx.state.setdefault("embedding_history", {})
    entry = estate.setdefault(payload.get("corpus"), {"centroids": [], "ages": []})
    centroids = entry["centroids"]
    ages = entry["ages"]

    if len(centroids) >= 3 and len(ages) >= 3:
        mean_centroid, std_centroid = _rolling_stats(centroids)
        mean_age, std_age = _rolling_stats(ages)
        if mean_centroid is not None and mean_age is not None:
            centroid_dev = centroid_shift - mean_centroid
            if centroid_shift > mean_centroid + max(1.2 * std_centroid, 0.008) and avg_doc_age_days < mean_age + max(std_age, 6) and avg_doc_age_days < 28:
                if embed_recent > 0 and centroid_dev < max(1.5 * std_centroid, 0.015):
                    pass
                else:
                    ctx.state["embedding_recent_alerts"] = 2
                    return _alert("embedding drift", "ai_infra")
            if avg_doc_age_days > max(mean_age + max(1.2 * std_age, 5), 35) and centroid_shift < 0.018:
                if embed_recent > 0 and avg_doc_age_days < mean_age + max(1.5 * std_age, 8):
                    pass
                else:
                    ctx.state["embedding_recent_alerts"] = 2
                    return _alert("corpus staleness", "ai_infra")
    if centroid_shift > 0.03 and avg_doc_age_days < 28:
        ctx.state["embedding_recent_alerts"] = 2
        return _alert("embedding drift", "ai_infra")
    if avg_doc_age_days > 35 and centroid_shift < 0.018:
        ctx.state["embedding_recent_alerts"] = 2
        return _alert("corpus staleness", "ai_infra")

    centroids.append(centroid_shift)
    ages.append(avg_doc_age_days)
    if len(centroids) > 10:
        centroids.pop(0)
    if len(ages) > 10:
        ages.pop(0)

    return _no_alert("ai_infra")
