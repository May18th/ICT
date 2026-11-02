# 对外数字自检报告

由 `python -m decision_poc.evidence_check --report <path>` 生成，**不要手改**。

- 期望指纹：`3349335a244f6918`（期望文件被改动即换指纹）
- 检查项：106 项，通过 106，失败 0
- 结论：全部一致

| 检查项 | 期望 | 实际 | 结果 |
| --- | --- | --- | --- |
| `p6_deduction.event_id` | `"WO-20261029-007"` | `"WO-20261029-007"` | 通过 |
| `p6_deduction.six_hops` | `["observed", "affected", "owner", "conflict", "resource", "action"]` | `["observed", "affected", "owner", "conflict", "resource", "action"]` | 通过 |
| `p6_deduction.conflict_ids` | `["C1", "C2", "C3"]` | `["C1", "C2", "C3"]` | 通过 |
| `p6_deduction.conflict_kinds` | `["contention", "gap", "overlap"]` | `["contention", "gap", "overlap"]` | 通过 |
| `p6_deduction.resource_gap` | `2` | `2` | 通过 |
| `p6_deduction.action_count` | `10` | `10` | 通过 |
| `p6_deduction.isolate_before_drain` | `true` | `true` | 通过 |
| `p6_deduction.handoff_required` | `true` | `true` | 通过 |
| `p6_deduction.snapshot_hash_len` | `64` | `64` | 通过 |
| `mcp.tools_count` | `13` | `13` | 通过 |
| `mcp.has_allocation_plan` | `true` | `true` | 通过 |
| `p8_eval.cases_total` | `22` | `22` | 通过 |
| `p8_eval.cases_passed` | `22` | `22` | 通过 |
| `p8_eval.cases_failed` | `0` | `0` | 通过 |
| `p8_eval.assets_parsed` | `16` | `16` | 通过 |
| `p8_eval.assets_total` | `16` | `16` | 通过 |
| `p8_eval.unmapped_columns` | `0` | `0` | 通过 |
| `p8_eval.locator_hit_rate` | `1.0` | `1.0` | 通过 |
| `p8_eval.six_hop_rate` | `1.0` | `1.0` | 通过 |
| `p8_eval.citation_completeness` | `1.0` | `1.0` | 通过 |
| `p8_eval.unsupported_claims` | `0` | `0` | 通过 |
| `p8_eval.safety_by_kind.conflict` | `"3/3"` | `"3/3"` | 通过 |
| `p8_eval.safety_by_kind.expired` | `"4/4"` | `"4/4"` | 通过 |
| `p8_eval.safety_by_kind.insufficient` | `"6/6"` | `"6/6"` | 通过 |
| `p8_eval.safety_by_kind.normal` | `"7/7"` | `"7/7"` | 通过 |
| `p8_eval.safety_by_kind.permission` | `"2/2"` | `"2/2"` | 通过 |
| `p9_evolution.cases_digest` | `"c2c7666eb0bd1bf8725a3af9c31b7be5c6c71ad553cd0cbbe477d71885df2a18"` | `"c2c7666eb0bd1bf8725a3af9c31b7be5c6c71ad553cd0cbbe477d71885df2a18"` | 通过 |
| `p9_evolution.feedback_verified` | `true` | `true` | 通过 |
| `p9_evolution.deterministic` | `true` | `true` | 通过 |
| `p9_evolution.train.initial_deviation` | `0.226428` | `0.226428` | 通过 |
| `p9_evolution.train.final_deviation` | `0.0` | `0.0` | 通过 |
| `p9_evolution.train.initial_l1_units` | `13` | `13` | 通过 |
| `p9_evolution.train.final_l1_units` | `0` | `0` | 通过 |
| `p9_evolution.train.initial_critical_unmet_units` | `15` | `15` | 通过 |
| `p9_evolution.train.final_critical_unmet_units` | `6` | `6` | 通过 |
| `p9_evolution.train.initial_missed_transfers` | `4` | `4` | 通过 |
| `p9_evolution.train.final_missed_transfers` | `0` | `0` | 通过 |
| `p9_evolution.train.initial_order_hits` | `7` | `7` | 通过 |
| `p9_evolution.train.final_order_hits` | `8` | `8` | 通过 |
| `p9_evolution.holdout.initial_deviation` | `0.218155` | `0.218155` | 通过 |
| `p9_evolution.holdout.final_deviation` | `0.0` | `0.0` | 通过 |
| `p9_evolution.rounds_actions` | `["promoted", "promoted", "rejected"]` | `["promoted", "promoted", "rejected"]` | 通过 |
| `p9_evolution.releases` | `["flood-response-v1.1", "flood-response-v1.2"]` | `["flood-response-v1.1", "flood-response-v1.2"]` | 通过 |
| `p9_evolution.final_label` | `"flood-response-v1.2"` | `"flood-response-v1.2"` | 通过 |
| `p9_evolution.review.review_id` | `"REV-001"` | `"REV-001"` | 通过 |
| `p9_evolution.review.deviation_before` | `0.117143` | `0.117143` | 通过 |
| `p9_evolution.review.deviation_after` | `0.0` | `0.0` | 通过 |
| `p9_evolution.review.expected_allocation.A-03` | `5` | `5` | 通过 |
| `p9_evolution.review.expected_allocation.B-01` | `1` | `1` | 通过 |
| `p9_evolution.warm_start.entry_id` | `"M-3979c3901de5"` | `"M-3979c3901de5"` | 通过 |
| `p9_evolution.warm_start.label` | `"flood-response-v1.2"` | `"flood-response-v1.2"` | 通过 |
| `p9_evolution.warm_start.similarity` | `1.0` | `1.0` | 通过 |
| `p9_evolution.warm_start.second_initial_deviation` | `0.0` | `0.0` | 通过 |
| `p9_evolution.warm_start.second_rounds` | `1` | `1` | 通过 |
| `p9_evolution.warm_start.second_promoted_rounds` | `0` | `0` | 通过 |
| `p9_evolution.memory_entries` | `1` | `1` | 通过 |
| `p9_evolution.real_event.baseline_allocation.A-03` | `4` | `4` | 通过 |
| `p9_evolution.real_event.baseline_allocation.B-01` | `2` | `2` | 通过 |
| `p9_evolution.real_event.evolved_allocation.A-03` | `6` | `6` | 通过 |
| `p9_evolution.real_event.evolved_allocation.B-01` | `0` | `0` | 通过 |
| `p9_evolution.real_event.evolved_unmet.A-03` | `0` | `0` | 通过 |
| `p9_evolution.real_event.evolved_unmet.B-01` | `2` | `2` | 通过 |
| `p9_evolution.real_event.evolved_transfer_count` | `1` | `1` | 通过 |
| `test_suite.docs_declared` | `[739]` | `[739]` | 通过 |
| `p11_ablation.configs` | `["rules_only", "semantic_only", "single_skill", "multi_skill", "full_audited"]` | `["rules_only", "semantic_only", "single_skill", "multi_skill", "full_audited"]` | 通过 |
| `p11_ablation.metrics.rules_only.six_hop_rate` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.rules_only.safety_rate` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.rules_only.impact_resource_rate` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.rules_only.citation_completeness` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.rules_only.auditable_rate` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.semantic_only.six_hop_rate` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.semantic_only.safety_rate` | `0.125` | `0.125` | 通过 |
| `p11_ablation.metrics.semantic_only.impact_resource_rate` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.semantic_only.citation_completeness` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.semantic_only.auditable_rate` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.single_skill.six_hop_rate` | `0.6364` | `0.6364` | 通过 |
| `p11_ablation.metrics.single_skill.safety_rate` | `1.0` | `1.0` | 通过 |
| `p11_ablation.metrics.single_skill.impact_resource_rate` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.single_skill.citation_completeness` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.single_skill.auditable_rate` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.multi_skill.six_hop_rate` | `0.6364` | `0.6364` | 通过 |
| `p11_ablation.metrics.multi_skill.safety_rate` | `1.0` | `1.0` | 通过 |
| `p11_ablation.metrics.multi_skill.impact_resource_rate` | `0.6364` | `0.6364` | 通过 |
| `p11_ablation.metrics.multi_skill.citation_completeness` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.multi_skill.auditable_rate` | `0.0` | `0.0` | 通过 |
| `p11_ablation.metrics.full_audited.six_hop_rate` | `0.6364` | `0.6364` | 通过 |
| `p11_ablation.metrics.full_audited.safety_rate` | `1.0` | `1.0` | 通过 |
| `p11_ablation.metrics.full_audited.impact_resource_rate` | `0.6364` | `0.6364` | 通过 |
| `p11_ablation.metrics.full_audited.citation_completeness` | `0.6364` | `0.6364` | 通过 |
| `p11_ablation.metrics.full_audited.auditable_rate` | `0.6364` | `0.6364` | 通过 |
| `p11_ablation.pairwise_pairs` | `20` | `20` | 通过 |
| `p11_ablation.deterministic` | `true` | `true` | 通过 |
| `p12_anchoring.anchors` | `1` | `1` | 通过 |
| `p12_anchoring.verify_ok` | `true` | `true` | 通过 |
| `p12_anchoring.offchain_ok` | `true` | `true` | 通过 |
| `p12_anchoring.tamper_detected` | `true` | `true` | 通过 |
| `p12_anchoring.deterministic` | `true` | `true` | 通过 |
| `platform_skills.captured` | `true` | `true` | 通过 |
| `platform_skills.skill_count` | `2` | `2` | 通过 |
| `platform_skills.names` | `["scarcity-allocation-evolution", "urban-six-hop-deduction"]` | `["scarcity-allocation-evolution", "urban-six-hop-deduction"]` | 通过 |
| `platform_skills.local_file_hashes_match` | `true` | `true` | 通过 |
| `platform_skills.platform_content_matches` | `true` | `true` | 通过 |
| `platform_ui.captured` | `true` | `true` | 通过 |
| `platform_ui.page_count` | `3` | `3` | 通过 |
| `platform_ui.all_expectations_met` | `true` | `true` | 通过 |
| `platform_ui.screenshot_hashes_ok` | `true` | `true` | 通过 |
