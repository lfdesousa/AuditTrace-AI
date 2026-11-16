# Neuters of the silent-truncation change

Every neuter is one exact textual replacement (`neuter.py`), applied to a clean checkout of the change; the chain tests then run (128 tests); the tree is restored. Baseline and restore: 0 failed each, in every shard. A neuter that reddens 0 tests would be a vacuous guard; none does.

Shards ran at a1902e64 (the earlier S14b result with 0 reddened, on the first version of its test, was strengthened at 5ff1e70e and re-run there: 1 reddened). Counts of the original chain neuters are compared with the spec chain figures in the PR body.

| id | neuter | tests reddened | tests that reddened |
|---|---|---|---|
| M1 | a stamped document without chunk 0 falls back to one chunk | 2 | test_a_stamped_row_never_falls_back_to_one_chunk, test_stamped_document_without_chunk_0_is_refused_from_any_chunk |
| M2 | chunks are matched on the chunk COUNT only, not the fold identity | 1 | test_failed_refold_is_refused_never_read_mixed |
| M3 | the reassembled text is not hashed against the fold | 1 | test_a_chunk_whose_text_changed_under_its_stamp_is_refused |
| M4 | count only AND no hash | 2 | test_a_chunk_whose_text_changed_under_its_stamp_is_refused, test_failed_refold_is_refused_never_read_mixed |
| M5 | the tail walk ignores chunks stored at the pre-LC-9 id | 1 | test_one_refold_makes_an_old_document_exact_and_verified |
| M6 | an unstamped document is reported verified | 4 | test_a_stamp_in_a_direct_write_is_stripped_and_the_read_is_unverified, test_a_stamp_in_an_mcp_write_is_stripped, test_a_stamp_in_an_update_is_stripped, test_an_unstamped_document_is_reported_unverified |
| M7 | the stale tail is removed without authorization | 1 | test_a_tail_the_caller_may_not_remove_refuses_the_fold_before_any_write |
| M8 | the tail removal writes no audit event | 2 | test_a_fold_that_removes_a_tail_writes_one_audit_event, test_the_blank_refold_is_audited_once_with_the_removed_count |
| M9 | the first window is sized by the stamp the caller supplied | 1 | test_a_forged_stamp_costs_one_window_and_is_refused |
| M10 | a PDF is not bounded at upload | 2 | test_a_pdf_one_byte_over_its_bound_is_refused_and_not_quarantined, test_a_pdf_over_the_upload_cap_is_refused_below_the_fold_bound |
| M11 | the claimed content type alone makes an upload a PDF | 1 | test_claiming_pdf_without_the_magic_bytes_keeps_the_text_bound |
| M12 | the request-body ceiling is not installed | 4 | test_a_declared_body_over_the_ceiling_is_refused_unparsed, test_an_mcp_request_over_the_ceiling_is_refused, test_an_undeclared_streamed_body_is_cut_at_the_ceiling, test_an_upload_over_its_ceiling_is_refused_unspooled |
| M13 | only a declared Content-Length is checked, a stream is not counted | 1 | test_an_undeclared_streamed_body_is_cut_at_the_ceiling |
| M14 | a third MCP write tool is registered with no over-limit case | 2 | test_every_registered_mcp_write_tool_has_an_over_limit_case, test_mcp_write_tool_over_the_limit_is_a_tool_error_and_not_indexed |
| M15 | the upload does not record what it stored | 5 | test_200_twice_for_the_same_name_the_helpers_reseed_path, test_409_when_a_concurrent_write_wins_the_object_is_overwritten_and_audited, test_500_when_the_write_audit_cannot_be_written, test_upload_records_a_manifest_row_with_the_stored_size |
| M16 | the upload is not authorized before it writes | 1 | test_403_when_the_name_is_a_shared_corpus_document_and_nothing_is_stored |
| N1 | the read stops after the first window (the cap of the base) | 14 | test_control_last_length_under_the_cap_reads_byte_exact, test_document_past_the_cap_reads_byte_exact, test_two_byte_characters_past_the_cap_read_byte_exact |
| N2 | the fold no longer stamps its chunks | 18 | test_a_chunk_whose_text_changed_under_its_stamp_is_refused, test_a_clean_refold_after_a_failed_one_reads_whole_again, test_a_forged_stamp_costs_one_window_and_is_refused, test_a_missing_chunk_is_an_explicit_error_never_a_prefix, test_a_reupload_without_a_refold_still_reads_the_old_fold_verified, test_after_the_refold_the_stamp_equals_the_stored_object, test_failed_refold_is_refused_never_read_mixed, test_one_refold_makes_an_old_document_exact_and_verified, test_stamped_document_without_chunk_0_is_refused_from_any_chunk, test_the_document_reads_back_exactly_and_verified, test_the_stamp_and_the_verdict_are_in_the_response |
| N3 | a short read is returned instead of refused | 4 | test_a_forged_stamp_costs_one_window_and_is_refused, test_a_missing_chunk_is_an_explicit_error_never_a_prefix, test_failed_refold_is_refused_never_read_mixed |
| N4 | the size check accepts everything | 17 | test_a_413_writes_no_audit_row, test_a_pdf_one_byte_over_its_bound_is_refused_and_not_quarantined, test_a_pdf_over_the_upload_cap_is_refused_below_the_fold_bound, test_claiming_pdf_without_the_magic_bytes_keeps_the_text_bound, test_control_under_the_ceiling_the_document_bound_answers, test_file_document_one_byte_over_is_refused_and_nothing_is_stored, test_mcp_write_tool_over_the_limit_is_a_tool_error_and_not_indexed, test_semantic_document_one_byte_over_is_refused_and_not_indexed, test_session_upload_over_the_limit_is_refused_and_not_stored, test_the_limit_counts_utf8_bytes_not_characters |
| N5 | the bound counts characters, not bytes | 1 | test_the_limit_counts_utf8_bytes_not_characters |
| N6 | ONE route loses its check (PUT /memory/episodic) | 1 | test_file_document_one_byte_over_is_refused_and_nothing_is_stored |
| N7 | the upload read does not refuse (the document is stored) | 6 | test_a_pdf_one_byte_over_its_bound_is_refused_and_not_quarantined, test_a_pdf_over_the_upload_cap_is_refused_below_the_fold_bound, test_claiming_pdf_without_the_magic_bytes_keeps_the_text_bound, test_file_document_one_byte_over_is_refused_and_nothing_is_stored, test_session_upload_over_the_limit_is_refused_and_not_stored |
| N8 | the MCP write tools lose their check | 2 | test_mcp_write_tool_over_the_limit_is_a_tool_error_and_not_indexed |
| N9 | a shorter re-fold keeps its stale tail chunks | 11 | test_a_blank_refold_whose_tail_the_caller_may_not_remove_is_refused, test_a_fold_that_removes_a_tail_writes_one_audit_event, test_a_shorter_reindex_removes_the_stale_tail_chunks, test_a_tail_the_caller_may_not_remove_refuses_the_fold_before_any_write, test_every_chunk_manifest_row_is_tombstoned, test_one_refold_makes_an_old_document_exact_and_verified, test_the_blank_refold_is_audited_once_with_the_removed_count, test_the_old_text_is_not_readable_after_a_blank_refold |
| N10 | a NEW memory write route is added with no test case | 1 | test_every_memory_write_route_is_covered_or_named_exempt |
| K1 | the REST semantic writes keep a caller's stamp | 2 | test_a_stamp_in_a_direct_write_is_stripped_and_the_read_is_unverified, test_a_stamp_in_an_update_is_stripped |
| K2 | the MCP write tools keep a caller's stamp | 1 | test_a_stamp_in_an_mcp_write_is_stripped |
| K3 | the fold chunks with _chunk_text again (a whitespace window is dropped) | 10 | test_every_chunk_manifest_row_is_tombstoned, test_the_blank_refold_is_audited_once_with_the_removed_count, test_the_document_reads_back_exactly_and_verified, test_the_old_text_is_not_readable_after_a_blank_refold |
| K4 | chunk_document drops a whitespace window instead of attaching it | 7 | test_reassembly_is_the_text_for_2000_random_documents, test_the_document_reads_back_exactly_and_verified, test_the_whitespace_window_goes_to_the_chunk_before_it |
| K5 | the upload is read in two pieces and joined (a second copy) | 1 | test_an_upload_at_its_bound_is_held_in_one_buffer |
| K6 | the PDF upload cap is ignored (only the fold's bound applies) | 1 | test_a_pdf_over_the_upload_cap_is_refused_below_the_fold_bound |
| K7 | the BFF body ceiling is not installed | 3 | test_a_declared_console_upload_over_the_ceiling_is_refused_unread, test_an_undeclared_streamed_upload_is_cut_at_the_ceiling, test_any_other_route_has_the_general_ceiling |
| K8 | a write that lost the manifest race is not audited | 2 | test_409_when_a_concurrent_write_wins_the_object_is_overwritten_and_audited |
| K9 | POST /memory/episodic refuses an over-limit document only after it has stored and audited it | 3 | test_a_413_writes_no_audit_row, test_file_document_one_byte_over_is_refused_and_nothing_is_stored, test_the_limit_counts_utf8_bytes_not_characters |
| S14a | a blank fold leaves the previous fold's chunks in place | 6 | test_a_blank_refold_whose_tail_the_caller_may_not_remove_is_refused, test_every_chunk_manifest_row_is_tombstoned, test_the_blank_refold_is_audited_once_with_the_removed_count, test_the_old_text_is_not_readable_after_a_blank_refold |
| S14b | a blank fold removes the tail without authorizing it | 1 | test_a_blank_refold_whose_tail_the_caller_may_not_remove_is_refused |
| S14c | a blank fold is looked up with the old 'kept' of a non-blank fold (tail starts at 1) | 5 | test_every_chunk_manifest_row_is_tombstoned, test_the_blank_refold_is_audited_once_with_the_removed_count, test_the_old_text_is_not_readable_after_a_blank_refold |
| S15a | the BFF 413 loses the additive detail key | 3 | test_a_declared_console_upload_over_the_ceiling_is_refused_unread, test_an_undeclared_streamed_upload_is_cut_at_the_ceiling, test_any_other_route_has_the_general_ceiling |
| S15b | the BFF 413 is the old detail-only body (no error object) | 3 | test_a_declared_console_upload_over_the_ceiling_is_refused_unread, test_an_undeclared_streamed_upload_is_cut_at_the_ceiling, test_any_other_route_has_the_general_ceiling |
| S15c | the BFF 413 error object drops its numeric status | 3 | test_a_declared_console_upload_over_the_ceiling_is_refused_unread, test_an_undeclared_streamed_upload_is_cut_at_the_ceiling, test_any_other_route_has_the_general_ceiling |

Baseline/restore runs:
- neuter-shard1.json: 0 failed (128 passed in 138.10s (0:02:18))
- neuter-shard2.json: 0 failed (128 passed in 138.14s (0:02:18))
- neuter-shard3.json: 0 failed (128 passed in 137.91s (0:02:17))
- neuter-s14b2.json: 0 failed (128 passed in 136.24s (0:02:16))
- neuter-shard1.json: 0 failed (128 passed in 127.35s (0:02:07))
- neuter-shard2.json: 0 failed (128 passed in 134.25s (0:02:14))
- neuter-shard3.json: 0 failed (128 passed in 141.26s (0:02:21))
- neuter-s14b2.json: 0 failed (128 passed in 139.93s (0:02:19))
