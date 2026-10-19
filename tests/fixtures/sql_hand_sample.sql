BEGIN;
INSERT INTO queries VALUES ('QTEST', '原始词', 'medical_search');

INSERT INTO search_requests VALUES
    ('R1', 'QTEST', '2026-09-13T00:00:00+08:00', '330100', '330100-B', 'medical_search', 1),
    ('R2', 'QTEST', '2026-09-13T12:00:00+08:00', '330100', '330100-B', 'medical_search', 1),
    ('R3', 'QTEST', '2026-09-14T12:00:00+08:00', '330100', '330100-B', 'medical_search', 1),
    ('R4', 'QTEST', '2026-09-14T13:00:00+08:00', '310100', '310100-B', 'medical_search', 1),
    ('R5', 'QTEST', '2026-09-19T23:59:59+08:00', '330100', '330100-B', 'medical_search', 1),
    ('R6', 'QTEST', '2026-09-20T00:00:00+08:00', '330100', '330100-B', 'medical_search', 1),
    ('R7', 'QTEST', '2026-09-17T00:00:00+08:00', '330100', '330100-B', 'medical_search', 1),
    ('R8', 'QTEST', '2026-09-18T12:00:00+08:00', '330100', '330100-B', 'medical_search', 1);

-- R1 has one final success and a different candidate failure.  E1 is repeated.
INSERT INTO recall_events VALUES
    (1, 'E1', 'R1', '2026-09-13T00:00:00+08:00', '2026-09-13T00:01:00+08:00', 'keyword', 'C1', 'P1', 'T1', 1, NULL, 'idx-test'),
    (2, 'E1', 'R1', '2026-09-13T00:00:00+08:00', '2026-09-13T00:01:00+08:00', 'keyword', 'C1', 'P1', 'T1', 1, NULL, 'idx-test'),
    (3, 'E2', 'R1', '2026-09-13T00:00:01+08:00', '2026-09-13T00:01:01+08:00', 'supply', 'C2', 'P2', 'T1', 0, 'SUPPLY_UNAVAILABLE', 'idx-test'),
    (4, 'E3', 'R1', '2026-09-13T00:00:02+08:00', '2026-09-13T00:01:02+08:00', 'supply', 'C1', 'P1', 'T1', 1, NULL, 'idx-test'),
    (5, 'E4', 'R1', '2026-09-13T00:00:03+08:00', '2026-09-13T00:01:03+08:00', 'plan', 'C1', 'P1', 'T1', 1, NULL, 'idx-test'),
    (6, 'E5', 'R1', '2026-09-13T00:00:04+08:00', '2026-09-13T00:01:04+08:00', 'eligible', 'C1', 'P1', 'T1', 1, NULL, 'idx-test'),
    -- R2 is a keyword miss with two plans for one offer.
    (7, 'E7', 'R2', '2026-09-13T12:00:00+08:00', '2026-09-13T12:01:00+08:00', 'keyword', NULL, NULL, NULL, 0, 'NO_TERM', 'idx-test'),
    -- R5 is a keyword miss whose historical snapshots overlap.
    (8, 'E8', 'R5', '2026-09-19T23:59:59+08:00', '2026-09-19T23:59:59+08:00', 'keyword', NULL, NULL, NULL, 0, 'NO_TERM', 'idx-test'),
    -- R7 exercises valid_from == event_time.
    (9, 'E9', 'R7', '2026-09-17T00:00:00+08:00', '2026-09-17T00:01:00+08:00', 'keyword', NULL, NULL, NULL, 0, 'NO_TERM', 'idx-test'),
    -- R4 belongs to another city and must not enter the requested scope.
    (10, 'E10', 'R4', '2026-09-14T13:00:00+08:00', '2026-09-14T13:01:00+08:00', 'keyword', NULL, NULL, NULL, 0, 'NO_TERM', 'idx-test'),
    -- R8 has one candidate with a missing supply stage and another candidate
    -- explicitly failing supply; the request must remain unknown.
    (11, 'E11', 'R8', '2026-09-18T12:00:00+08:00', '2026-09-18T12:01:00+08:00', 'keyword', 'C8', 'P8', 'T1', 1, NULL, 'idx-test'),
    (12, 'E12', 'R8', '2026-09-18T12:00:01+08:00', '2026-09-18T12:01:01+08:00', 'keyword', 'C9', 'P9', 'T1', 1, NULL, 'idx-test'),
    (13, 'E13', 'R8', '2026-09-18T12:00:02+08:00', '2026-09-18T12:01:02+08:00', 'supply', 'C9', 'P9', 'T1', 0, 'SUPPLY_UNAVAILABLE', 'idx-test');

INSERT INTO historical_candidate_replay VALUES
    ('RP2A', 'R2', 'T1', 'P2A', 'O2', 'lexical_replay_unreviewed', 'idx-historical', '2026-09-20T10:00:00+08:00'),
    ('RP2B', 'R2', 'T1', 'P2B', 'O2', 'lexical_replay_unreviewed', 'idx-historical', '2026-09-20T10:00:00+08:00'),
    ('RP5', 'R5', 'T1', 'P5', 'O5', 'lexical_replay_unreviewed', 'idx-historical', '2026-09-20T10:00:00+08:00'),
    ('RP7', 'R7', 'T1', 'P7', 'O7', 'lexical_replay_unreviewed', 'idx-historical', '2026-09-20T10:00:00+08:00'),
    ('RP4', 'R4', 'T1', 'P4', 'O4', 'lexical_replay_unreviewed', 'idx-historical', '2026-09-20T10:00:00+08:00'),
    ('RP6', 'R6', 'T1', 'P6', 'O6', 'lexical_replay_unreviewed', 'idx-historical', '2026-09-20T10:00:00+08:00');

INSERT INTO historical_eligibility VALUES
    ('H2A', 'P2A', 'O2', 'S2', '330100', '["330100-B"]', '2026-09-13T00:00:00+08:00', '2026-09-20T00:00:00+08:00', 5, 1, 'valid', 'valid', 'active', 100, 'approved'),
    ('H2B', 'P2B', 'O2', 'S2', '330100', '["330100-B"]', '2026-09-13T00:00:00+08:00', '2026-09-20T00:00:00+08:00', 5, 1, 'valid', 'valid', 'active', 100, 'approved'),
    ('H5A', 'P5', 'O5', 'S5', '330100', '["330100-B"]', '2026-09-19T00:00:00+08:00', '2026-09-20T00:00:00+08:00', 5, 1, 'valid', 'valid', 'active', 100, 'approved'),
    ('H5B', 'P5', 'O5', 'S5', '330100', '["330100-B"]', '2026-09-19T12:00:00+08:00', '2026-09-20T00:00:00+08:00', 5, 1, 'valid', 'valid', 'active', 100, 'approved'),
    ('H7', 'P7', 'O7', 'S7', '330100', '["330100-B"]', '2026-09-17T00:00:00+08:00', '2026-09-20T00:00:00+08:00', 5, 1, 'valid', 'valid', 'active', 100, 'approved'),
    ('H4', 'P4', 'O4', 'S4', '310100', '["310100-B"]', '2026-09-13T00:00:00+08:00', '2026-09-20T00:00:00+08:00', 5, 1, 'valid', 'valid', 'active', 100, 'approved'),
    ('H6', 'P6', 'O6', 'S6', '330100', '["330100-B"]', '2026-09-13T00:00:00+08:00', '2026-09-20T00:00:00+08:00', 5, 1, 'valid', 'valid', 'active', 100, 'approved');

INSERT INTO load_manifest(dataset, biz_date, city_id, status, loaded_at)
SELECT dataset, biz_date, '330100', 'complete', '2026-09-20T08:00:00+08:00'
FROM (
    SELECT 'search_requests' AS dataset UNION ALL
    SELECT 'recall_events' UNION ALL
    SELECT 'historical_candidate_replay'
) AS datasets
CROSS JOIN (
    SELECT '2026-09-13' AS biz_date UNION ALL
    SELECT '2026-09-14' UNION ALL
    SELECT '2026-09-15' UNION ALL
    SELECT '2026-09-16' UNION ALL
    SELECT '2026-09-17' UNION ALL
    SELECT '2026-09-18' UNION ALL
    SELECT '2026-09-19'
) AS dates;
COMMIT;
