"""Stage 4: human-approved live (real-money) execution.

The single most important invariant in this package: a real order can
only ever be attempted through LiveExecutionService.execute_authorized(),
and only for an ExecutionAuthorization that a human explicitly approved,
that is unexpired, and that has not already been used. Every provider's
actual mutation entry point (`_submit_authorized_order`) is a private,
underscore-prefixed method that no other file may call -- enforced by
tests/test_live_architecture.py.

place_order/cancel_order on PredictionMarketProvider are UNCHANGED from
Stage 1 -- they still unconditionally raise NotImplementedError. Nothing
in this package touches them.
"""
