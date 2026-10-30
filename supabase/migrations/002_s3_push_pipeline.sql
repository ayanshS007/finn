-- Segment S3 (push pipeline). Additive and safe to re-run. Apply in the Supabase SQL editor.
--
-- 1. facts.has_exceptional_items: true/false when the source reports an exceptional-items line for
--    the period (true = nonzero), null when it does not. EBITDA always excludes exceptional items.
-- 2. Column comments so nobody mistakes the llm_calls estimate for a bill: FINN runs on free tiers
--    only and the actual cost of every call is zero.
--
-- Nothing else changes: filings.tier stays a smallint (the 1-based position of the tier in
-- config/categories.yaml; the tier name is in card_json.tier_name) and filings.status stays NULL
-- while an item is pending a rate-limited LLM call.

alter table facts add column if not exists has_exceptional_items boolean;

comment on column facts.has_exceptional_items is
    'true/false when the filing reports exceptional items for the period; null when the source has no such line';
comment on column llm_calls.cost_inr is
    'PAID-EQUIVALENT estimate in INR of what these tokens would cost on a paid plan. Actual cost is zero: free tiers only.';
comment on column filings.tier is
    '1-based position of the tier in config/categories.yaml for the category; name in card_json.tier_name';
comment on column filings.status is
    'card | oneliner | drawer; NULL means pending (a rate-limited LLM call), retried on the next run';
