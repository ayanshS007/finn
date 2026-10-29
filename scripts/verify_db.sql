-- Run after the migration: psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f scripts/verify_db.sql
-- Fails loudly (exception) if a table is missing or anon can write. Leaves no rows behind.
begin;

do $$
declare
    missing text;
begin
    select string_agg(t, ', ') into missing
    from unnest(array['filings', 'facts', 'claims', 'verdicts', 'qa_labels', 'prices', 'rules',
                      'alerts', 'swipes', 'llm_calls', 'cache', 'push_subscriptions']) as t
    where to_regclass('public.' || t) is null;
    if missing is not null then
        raise exception 'missing tables: %', missing;
    end if;
    raise notice 'all 12 tables exist';
end $$;

set local role anon;

do $$
declare
    r record;
    stmt text;
    t text;
begin
    -- anon must not be able to write anywhere (table, one column to update)
    for r in select * from (values
        ('filings', 'symbol'), ('facts', 'ticker'), ('claims', 'ticker'), ('verdicts', 'reason'),
        ('qa_labels', 'ticker'), ('prices', 'symbol'), ('rules', 'enabled'), ('alerts', 'symbol'),
        ('swipes', 'direction'), ('llm_calls', 'step'), ('cache', 'key'),
        ('push_subscriptions', 'endpoint')) as v(t, c)
    loop
        foreach stmt in array array['insert into %1$I default values',
                                    'update %1$I set %2$I = %2$I', 'delete from %1$I'] loop
            begin
                execute format(stmt, r.t, r.c);
                raise exception 'anon could run: %', format(stmt, r.t, r.c);
            exception
                when insufficient_privilege then null;  -- the expected outcome
            end;
        end loop;
    end loop;
    raise notice 'anon cannot insert, update or delete on any table';

    -- anon can read what the web app shows, and nothing private
    foreach t in array array['filings', 'facts', 'claims', 'verdicts', 'qa_labels', 'prices',
                             'rules', 'alerts'] loop
        execute format('select count(*) from %I', t);
    end loop;
    foreach t in array array['cache', 'llm_calls', 'push_subscriptions', 'swipes'] loop
        begin
            execute format('select count(*) from %I', t);
            raise exception 'anon can read %', t;
        exception when insufficient_privilege then null;
        end;
    end loop;
    raise notice 'anon reads the 8 public tables only';
end $$;

rollback;
