-- FINN initial schema (segment S2).
-- Writes go through the service key (bypasses RLS). The web app reads with the anon key, so
-- anon gets SELECT policies on the tables it shows and nothing else.

create extension if not exists pgcrypto;

-- filings: one row per exchange announcement, with its classification and ranking.
create table if not exists filings (
    id             uuid primary key default gen_random_uuid(),
    symbol         text not null,
    exchange       text not null check (exchange in ('NSE', 'BSE')),
    source_id      text not null unique,
    filed_at       timestamptz,
    category_raw   text,
    subject        text,
    attachment_url text,
    text_excerpt   text,
    category       text,
    tier           smallint,
    label_source   text check (label_source in ('rules', 'llm')),
    quote          text,
    quote_match    real check (quote_match between 0 and 1),
    grader_score   smallint check (grader_score between 1 and 5),
    materiality    real,
    status         text check (status in ('card', 'oneliner', 'drawer')),
    one_liner      text,
    why_line       text,
    card_json      jsonb,
    created_at     timestamptz not null default now()
);
create index if not exists filings_symbol_filed_idx on filings (symbol, filed_at desc);
create index if not exists filings_status_idx on filings (status, materiality desc);

-- facts: every figure the app shows. Mirrors finn.db.models.Fact.
-- period labels: FY27Q2, FY27H1, FY27H2, FY27-9M, FY27 (H2 is derived: FY - H1).
create table if not exists facts (
    id            uuid primary key default gen_random_uuid(),
    ticker        text not null,
    metric        text not null check (metric in (
                      'revenue', 'ebitda', 'pbt', 'pat', 'tax', 'finance_costs', 'd_and_a',
                      'other_income', 'nii', 'ppop', 'capex', 'cfo', 'fcf', 'debt', 'equity',
                      'ebitda_margin', 'pat_margin', 'segment_revenue')),
    segment       text,
    value_cr      double precision,
    unit_original text not null check (unit_original in ('lakh', 'crore', 'million', 'rupee')),
    period        text not null check (period ~ '^FY[0-9]{2}(Q[1-4]|H[12]|-9M)?$'),
    period_kind   text not null check (period_kind in ('Q', 'H1', 'H2', '9M', 'FY')),
    basis         text not null check (basis in ('consolidated', 'standalone')),
    derived       boolean not null default false,
    formula       text,
    source_url    text not null,
    page          integer,
    quote         text not null,
    quote_match   real not null check (quote_match between 0 and 1),
    created_at    timestamptz not null default now(),
    -- one value per metric, period and basis; segment is optional so NULLs compare equal
    constraint facts_natural_key unique nulls not distinct
        (ticker, metric, segment, period, period_kind, basis)
);
create index if not exists facts_ticker_period_idx on facts (ticker, period);

-- claims: what management said on an earnings call. Mirrors finn.db.models.Claim.
create table if not exists claims (
    id          uuid primary key default gen_random_uuid(),
    ticker      text not null,
    said_in     text not null,
    speaker     text,
    metric      text not null check (metric in (
                    'revenue', 'ebitda', 'pbt', 'pat', 'tax', 'finance_costs', 'd_and_a',
                    'other_income', 'nii', 'ppop', 'capex', 'cfo', 'fcf', 'debt', 'equity',
                    'ebitda_margin', 'pat_margin', 'segment_revenue', 'text')),
    target_low  double precision,
    target_high double precision,
    target_unit text check (target_unit in ('%', 'cr', 'units', 'text')),
    by_period   text,
    quote       text not null,
    page        integer,
    quote_match real not null check (quote_match between 0 and 1),
    created_at  timestamptz not null default now()
);
create index if not exists claims_ticker_idx on claims (ticker, said_in);

-- verdicts: judgement of a claim against a fact. actual_fact_id is null only when abstaining.
create table if not exists verdicts (
    id              uuid primary key default gen_random_uuid(),
    claim_id        uuid not null references claims (id) on delete cascade,
    actual_fact_id  uuid references facts (id) on delete set null,
    verdict         text not null check (verdict in (
                        'delivered', 'missed', 'moved_goalposts', 'went_quiet',
                        'not_enough_evidence')),
    reason          text not null,
    created_at      timestamptz not null default now(),
    constraint verdicts_fact_unless_abstaining
        check (actual_fact_id is not null or verdict in ('not_enough_evidence', 'went_quiet'))
);
create index if not exists verdicts_claim_idx on verdicts (claim_id);

create table if not exists qa_labels (
    id          uuid primary key default gen_random_uuid(),
    ticker      text not null,
    call_period text not null,
    question    text not null,
    answer      text,
    label       text not null,
    quote       text,
    page        integer,
    created_at  timestamptz not null default now()
);
create index if not exists qa_labels_ticker_idx on qa_labels (ticker, call_period);

create table if not exists prices (
    date   date not null,
    symbol text not null,
    close  double precision not null,
    volume bigint,
    primary key (symbol, date)
);

create table if not exists rules (
    id      uuid primary key default gen_random_uuid(),
    kind    text not null check (kind in ('price_move', 'volume_spike', 'category')),
    params  jsonb not null default '{}'::jsonb,
    enabled boolean not null default true
);

create table if not exists alerts (
    id         uuid primary key default gen_random_uuid(),
    rule_id    uuid references rules (id) on delete set null,
    symbol     text not null,
    filing_id  uuid references filings (id) on delete set null,
    message    text not null,
    created_at timestamptz not null default now(),
    read       boolean not null default false
);
create index if not exists alerts_created_idx on alerts (created_at desc);

create table if not exists swipes (
    id         uuid primary key default gen_random_uuid(),
    filing_id  uuid not null references filings (id) on delete cascade,
    direction  text not null,
    created_at timestamptz not null default now()
);

create table if not exists llm_calls (
    id          bigint generated always as identity primary key,
    step        text not null,
    model       text not null,
    tokens_in   integer,
    tokens_out  integer,
    latency_ms  integer,
    cost_inr    numeric(12, 4),
    cache_hit   boolean not null default false,
    created_at  timestamptz not null default now()
);

create table if not exists cache (
    key        text primary key,
    value      jsonb not null,
    created_at timestamptz not null default now()
);

create table if not exists push_subscriptions (
    id         uuid primary key default gen_random_uuid(),
    endpoint   text not null unique,
    p256dh     text not null,
    auth       text not null,
    created_at timestamptz not null default now()
);

-- Row level security: on everywhere. Anon may only read what the web app shows.
alter table filings            enable row level security;
alter table facts              enable row level security;
alter table claims             enable row level security;
alter table verdicts           enable row level security;
alter table qa_labels          enable row level security;
alter table prices             enable row level security;
alter table rules              enable row level security;
alter table alerts             enable row level security;
alter table swipes             enable row level security;
alter table llm_calls          enable row level security;
alter table cache              enable row level security;
alter table push_subscriptions enable row level security;

do $$
declare t text;
begin
    foreach t in array array['filings', 'facts', 'claims', 'verdicts', 'qa_labels', 'prices',
                             'rules', 'alerts']
    loop
        execute format('drop policy if exists anon_read on %I', t);
        execute format('create policy anon_read on %I for select to anon using (true)', t);
    end loop;
end $$;

-- Defence in depth: Supabase grants anon/authenticated everything on new tables by default.
-- Take it all away, then give anon back SELECT on the tables with a read policy.
revoke all on all tables in schema public from anon, authenticated;
grant select on filings, facts, claims, verdicts, qa_labels, prices, rules, alerts to anon;
