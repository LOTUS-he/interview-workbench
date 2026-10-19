WITH
task_query AS (
    SELECT query_id, raw_query, scene_id
    FROM queries
    WHERE query_id = :query_id
      AND raw_query = :raw_query
      AND scene_id = :scene_id
),
scoped_requests AS (
    SELECT
        r.request_id,
        r.event_time,
        r.city_id,
        r.region_id,
        r.scene_id
    FROM search_requests AS r
    JOIN task_query AS q
      ON q.query_id = r.query_id
    WHERE r.query_id = :query_id
      AND r.city_id = :city_id
      AND r.region_id = :region_id
      AND r.scene_id = :scene_id
      AND r.is_valid = 1
      AND r.event_time >= :history_start
      AND r.event_time < :history_end_exclusive
),
dedup_recall_ranked AS (
    SELECT
        e.delivery_id,
        e.event_id,
        e.request_id,
        e.event_time,
        e.ingested_at,
        e.stage,
        e.candidate_id,
        e.ad_plan_id,
        e.term_id,
        e.passed,
        e.reason_code,
        e.index_version,
        ROW_NUMBER() OVER (
            PARTITION BY e.event_id
            ORDER BY e.ingested_at DESC, e.delivery_id DESC
        ) AS event_row_number
    FROM recall_events AS e
),
dedup_recall AS (
    SELECT
        delivery_id,
        event_id,
        request_id,
        event_time,
        ingested_at,
        stage,
        candidate_id,
        ad_plan_id,
        term_id,
        passed,
        reason_code,
        index_version
    FROM dedup_recall_ranked
    WHERE event_row_number = 1
),
candidate_progress AS (
    SELECT
        r.request_id,
        e.candidate_id,
        MAX(CASE WHEN e.event_id IS NOT NULL THEN 1 ELSE 0 END) AS has_recall,
        MAX(CASE WHEN e.stage = 'keyword' THEN 1 ELSE 0 END) AS keyword_seen,
        MAX(CASE WHEN e.stage = 'keyword' AND e.passed = 1 AND e.candidate_id IS NOT NULL THEN 1 ELSE 0 END) AS keyword_pass,
        MAX(CASE WHEN e.stage = 'supply' THEN 1 ELSE 0 END) AS supply_seen,
        MAX(CASE WHEN e.stage = 'supply' AND e.passed = 1 AND e.candidate_id IS NOT NULL THEN 1 ELSE 0 END) AS supply_pass,
        MAX(CASE WHEN e.stage = 'plan' THEN 1 ELSE 0 END) AS plan_seen,
        MAX(CASE WHEN e.stage = 'plan' AND e.passed = 1 AND e.candidate_id IS NOT NULL THEN 1 ELSE 0 END) AS plan_pass,
        MAX(CASE WHEN e.stage = 'eligible' THEN 1 ELSE 0 END) AS eligible_seen,
        MAX(CASE WHEN e.stage = 'eligible' AND e.passed = 1 AND e.candidate_id IS NOT NULL THEN 1 ELSE 0 END) AS eligible_pass
    FROM scoped_requests AS r
    LEFT JOIN dedup_recall AS e
      ON e.request_id = r.request_id
    GROUP BY r.request_id, e.candidate_id
),
request_outcome_base AS (
    SELECT
        r.request_id,
        r.event_time,
        MAX(COALESCE(c.has_recall, 0)) AS has_recall,
        MAX(COALESCE(c.keyword_seen, 0)) AS keyword_seen,
        MAX(COALESCE(c.keyword_pass, 0)) AS keyword_pass,
        MAX(COALESCE(c.supply_seen, 0)) AS supply_seen,
        MAX(COALESCE(c.supply_pass, 0)) AS supply_pass,
        MAX(COALESCE(c.plan_seen, 0)) AS plan_seen,
        MAX(COALESCE(c.plan_pass, 0)) AS plan_pass,
        MAX(COALESCE(c.eligible_seen, 0)) AS eligible_seen,
        MAX(COALESCE(c.eligible_pass, 0)) AS eligible_pass,
        COALESCE(SUM(CASE WHEN c.candidate_id IS NOT NULL AND c.keyword_seen = 1 AND c.keyword_pass = 1 AND c.supply_seen = 0 THEN 1 ELSE 0 END), 0) AS keyword_path_unknown_count,
        COALESCE(SUM(CASE WHEN c.candidate_id IS NOT NULL AND c.keyword_pass = 1 AND c.supply_pass = 1 AND c.plan_seen = 0 THEN 1 ELSE 0 END), 0) AS supply_path_unknown_count,
        COALESCE(SUM(CASE WHEN c.candidate_id IS NOT NULL AND c.keyword_pass = 1 AND c.supply_pass = 1 AND c.plan_pass = 1 AND c.eligible_seen = 0 THEN 1 ELSE 0 END), 0) AS plan_path_unknown_count,
        COALESCE(SUM(CASE WHEN c.candidate_id IS NOT NULL AND c.keyword_seen = 0 AND (c.supply_seen = 1 OR c.plan_seen = 1 OR c.eligible_seen = 1) THEN 1 ELSE 0 END), 0) AS malformed_path_count
    FROM scoped_requests AS r
    LEFT JOIN candidate_progress AS c
      ON c.request_id = r.request_id
    GROUP BY r.request_id, r.event_time
),
request_outcomes AS (
    SELECT
        request_id,
        event_time,
        keyword_pass,
        supply_pass,
        plan_pass,
        eligible_pass,
        CASE
            WHEN eligible_pass = 1 THEN 'eligible'
            WHEN has_recall = 0 THEN 'unknown'
            WHEN malformed_path_count > 0 THEN 'unknown'
            WHEN keyword_seen = 0 THEN 'unknown'
            WHEN keyword_pass = 0 THEN 'keyword_miss'
            WHEN keyword_path_unknown_count > 0 THEN 'unknown'
            WHEN supply_seen = 0 THEN 'unknown'
            WHEN supply_pass = 0 THEN 'supply_fail'
            WHEN supply_path_unknown_count > 0 THEN 'unknown'
            WHEN plan_seen = 0 THEN 'unknown'
            WHEN plan_pass = 0 THEN 'plan_fail'
            WHEN plan_path_unknown_count > 0 THEN 'unknown'
            WHEN eligible_seen = 0 THEN 'unknown'
            ELSE 'unknown'
        END AS bucket
    FROM request_outcome_base
),
replay_tuples AS (
    SELECT
        h.request_id,
        h.term_id,
        h.ad_plan_id,
        h.offer_id,
        MIN(h.source) AS replay_source,
        MIN(h.index_version) AS replay_index_version,
        COUNT(*) AS replay_row_count
    FROM historical_candidate_replay AS h
    JOIN request_outcomes AS o
      ON o.request_id = h.request_id
     AND o.bucket = 'keyword_miss'
    GROUP BY h.request_id, h.term_id, h.ad_plan_id, h.offer_id
),
eligibility_candidates AS (
    SELECT
        t.request_id,
        t.term_id,
        t.ad_plan_id,
        t.offer_id,
        t.replay_source,
        t.replay_index_version,
        t.replay_row_count,
        r.event_time,
        e.snapshot_id,
        e.sku_id,
        e.city_id AS eligibility_city_id,
        e.region_ids_json,
        e.valid_from,
        e.valid_to,
        e.stock,
        e.on_shelf,
        e.product_qualification,
        e.merchant_qualification,
        e.plan_status,
        e.remaining_budget_fen,
        e.material_status
    FROM replay_tuples AS t
    JOIN scoped_requests AS r
      ON r.request_id = t.request_id
    LEFT JOIN historical_eligibility AS e
      ON e.ad_plan_id = t.ad_plan_id
     AND e.offer_id = t.offer_id
     AND e.city_id = :city_id
     AND e.valid_from <= r.event_time
     AND r.event_time < e.valid_to
     AND json_valid(e.region_ids_json) = 1
     AND EXISTS (
         SELECT 1
         FROM json_each(e.region_ids_json) AS region_value
         WHERE region_value.value = :region_id
     )
),
ranked_verification AS (
    SELECT
        e.*,
        COUNT(e.snapshot_id) OVER (
            PARTITION BY e.request_id, e.term_id, e.ad_plan_id, e.offer_id
        ) AS snapshot_count,
        ROW_NUMBER() OVER (
            PARTITION BY e.request_id, e.term_id, e.ad_plan_id, e.offer_id
            ORDER BY e.snapshot_id
        ) AS snapshot_row_number
    FROM eligibility_candidates AS e
),
verification_rows AS (
    SELECT
        request_id,
        term_id,
        ad_plan_id,
        offer_id,
        replay_source,
        replay_index_version,
        replay_row_count,
        event_time,
        CASE WHEN snapshot_count = 1 THEN snapshot_id ELSE NULL END AS snapshot_id,
        CASE WHEN snapshot_count = 1 THEN sku_id ELSE NULL END AS sku_id,
        snapshot_count,
        CASE
            WHEN snapshot_count <> 1 THEN 'unknown'
            WHEN stock IS NULL
              OR on_shelf IS NULL
              OR product_qualification IS NULL
              OR merchant_qualification IS NULL
              OR plan_status IS NULL
              OR remaining_budget_fen IS NULL
              OR material_status IS NULL THEN 'unknown'
            WHEN stock > 0
             AND on_shelf = 1
             AND product_qualification = 'valid'
             AND merchant_qualification = 'valid'
             AND plan_status = 'active'
             AND remaining_budget_fen > 0
             AND material_status = 'approved' THEN 'verified'
            ELSE 'ineligible'
        END AS verification_status,
        CASE
            WHEN snapshot_count = 0 THEN 'no_historical_eligibility'
            WHEN snapshot_count > 1 THEN 'overlapping_historical_eligibility'
            WHEN stock IS NULL
              OR on_shelf IS NULL
              OR product_qualification IS NULL
              OR merchant_qualification IS NULL
              OR plan_status IS NULL
              OR remaining_budget_fen IS NULL
              OR material_status IS NULL THEN 'historical_eligibility_missing_field'
            WHEN stock <= 0 THEN 'stock_unavailable'
            WHEN on_shelf <> 1 THEN 'offer_off_shelf'
            WHEN product_qualification <> 'valid' THEN 'product_qualification_invalid'
            WHEN merchant_qualification <> 'valid' THEN 'merchant_qualification_invalid'
            WHEN plan_status <> 'active' THEN 'plan_unavailable'
            WHEN remaining_budget_fen <= 0 THEN 'budget_unavailable'
            WHEN material_status <> 'approved' THEN 'material_unapproved'
            ELSE NULL
        END AS verification_reason
    FROM ranked_verification
    WHERE snapshot_row_number = 1
),
summary_values AS (
    SELECT
        COUNT(*) AS request_count,
        COALESCE(SUM(CASE WHEN bucket = 'eligible' THEN 1 ELSE 0 END), 0) AS eligible_count,
        COALESCE(SUM(CASE WHEN bucket = 'keyword_miss' THEN 1 ELSE 0 END), 0) AS keyword_miss_count,
        COALESCE(SUM(CASE WHEN bucket = 'supply_fail' THEN 1 ELSE 0 END), 0) AS supply_fail_count,
        COALESCE(SUM(CASE WHEN bucket = 'plan_fail' THEN 1 ELSE 0 END), 0) AS plan_fail_count,
        COALESCE(SUM(CASE WHEN bucket = 'unknown' THEN 1 ELSE 0 END), 0) AS unknown_count,
        (
            SELECT COUNT(DISTINCT request_id)
            FROM verification_rows
            WHERE verification_status = 'verified'
        ) AS verified_gap_count,
        (
            SELECT COUNT(*) FROM verification_rows
        ) AS evidence_rows_count,
        (
            SELECT COUNT(DISTINCT request_id)
            FROM verification_rows
            WHERE verification_status = 'unknown'
        ) AS history_unknown_request_count
    FROM request_outcomes
),
request_details AS (
    SELECT
        o.request_id,
        o.event_time,
        o.bucket,
        o.keyword_pass,
        o.supply_pass,
        o.plan_pass,
        o.eligible_pass,
        COALESCE((
            SELECT COUNT(*)
            FROM verification_rows AS v
            WHERE v.request_id = o.request_id
        ), 0) AS replay_candidate_count,
        COALESCE((
            SELECT COUNT(*)
            FROM verification_rows AS v
            WHERE v.request_id = o.request_id
              AND v.verification_status = 'verified'
        ), 0) AS verified_candidate_count
    FROM request_outcomes AS o
),
output_sizes AS (
    SELECT
        1 + (SELECT COUNT(*) FROM request_details) + (SELECT COUNT(*) FROM verification_rows) AS total_rows
),
output_rows AS (
    SELECT
        'summary' AS row_kind,
        0 AS row_order,
        NULL AS request_id,
        NULL AS event_time,
        NULL AS bucket,
        NULL AS keyword_pass,
        NULL AS supply_pass,
        NULL AS plan_pass,
        NULL AS eligible_pass,
        NULL AS term_id,
        NULL AS ad_plan_id,
        NULL AS offer_id,
        NULL AS sku_id,
        NULL AS replay_index_version,
        NULL AS replay_row_count,
        NULL AS snapshot_id,
        NULL AS snapshot_count,
        NULL AS verification_status,
        NULL AS verification_reason,
        s.request_count,
        s.eligible_count,
        s.keyword_miss_count,
        s.supply_fail_count,
        s.plan_fail_count,
        s.unknown_count,
        s.verified_gap_count,
        NULL AS verified_candidate_count,
        s.history_unknown_request_count,
        (SELECT COUNT(*) FROM request_details) AS request_rows_count,
        s.evidence_rows_count,
        z.total_rows
    FROM summary_values AS s
    CROSS JOIN output_sizes AS z
    UNION ALL
    SELECT
        'request' AS row_kind,
        1 AS row_order,
        d.request_id,
        d.event_time,
        d.bucket,
        d.keyword_pass,
        d.supply_pass,
        d.plan_pass,
        d.eligible_pass,
        NULL AS term_id,
        NULL AS ad_plan_id,
        NULL AS offer_id,
        NULL AS sku_id,
        NULL AS replay_index_version,
        d.replay_candidate_count AS replay_row_count,
        NULL AS snapshot_id,
        NULL AS snapshot_count,
        NULL AS verification_status,
        NULL AS verification_reason,
        NULL AS request_count,
        NULL AS eligible_count,
        NULL AS keyword_miss_count,
        NULL AS supply_fail_count,
        NULL AS plan_fail_count,
        NULL AS unknown_count,
        NULL AS verified_gap_count,
        d.verified_candidate_count,
        NULL AS history_unknown_request_count,
        NULL AS request_rows_count,
        NULL AS evidence_rows_count,
        NULL AS total_rows
    FROM request_details AS d
    UNION ALL
    SELECT
        'evidence' AS row_kind,
        2 AS row_order,
        v.request_id,
        v.event_time,
        'keyword_miss' AS bucket,
        NULL AS keyword_pass,
        NULL AS supply_pass,
        NULL AS plan_pass,
        NULL AS eligible_pass,
        v.term_id,
        v.ad_plan_id,
        v.offer_id,
        v.sku_id,
        v.replay_index_version,
        v.replay_row_count,
        v.snapshot_id,
        v.snapshot_count,
        v.verification_status,
        v.verification_reason,
        NULL AS request_count,
        NULL AS eligible_count,
        NULL AS keyword_miss_count,
        NULL AS supply_fail_count,
        NULL AS plan_fail_count,
        NULL AS unknown_count,
        NULL AS verified_gap_count,
        NULL AS verified_candidate_count,
        NULL AS history_unknown_request_count,
        NULL AS request_rows_count,
        NULL AS evidence_rows_count,
        NULL AS total_rows
    FROM verification_rows AS v
)
SELECT *
FROM output_rows
WHERE row_kind IN ('summary', 'evidence')
ORDER BY row_order, request_id, term_id, ad_plan_id, offer_id
LIMIT :output_limit
