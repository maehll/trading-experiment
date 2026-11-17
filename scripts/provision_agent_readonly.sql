-- Run as a PostgreSQL administrator after applying database migrations.
-- The agent has no database credentials. Only the data API's dedicated LOGIN
-- role may inherit trading_data_reader.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_data_reader') THEN
        EXECUTE 'CREATE ROLE trading_data_reader '
            'NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_agent_readonly') THEN
        EXECUTE 'CREATE ROLE trading_agent_readonly '
            'NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT';
    END IF;
END
$$;

ALTER ROLE trading_data_reader
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

-- Retire the earlier direct-to-agent reader role: evaluation records remain
-- available only through the filtered data API, never through an agent DB login.
REVOKE CONNECT ON DATABASE trading_experiment FROM trading_agent_readonly;
REVOKE USAGE ON SCHEMA public FROM trading_agent_readonly;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM trading_agent_readonly;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM trading_agent_readonly;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM trading_agent_readonly;

GRANT CONNECT ON DATABASE trading_experiment TO trading_data_reader;
GRANT USAGE ON SCHEMA public TO trading_data_reader;
REVOKE ALL PRIVILEGES ON TABLE
    agents, market_data, research_items, positions, simulator_accounts, rewards,
    portfolio_snapshots, benchmark_portfolios
    FROM trading_data_reader;

GRANT SELECT (id, is_active, scenario, variant) ON agents TO trading_data_reader;
GRANT SELECT (
    id, symbol, source, currency, price, bid, ask, as_of, fetched_at, quality, meta
) ON market_data TO trading_data_reader;
GRANT SELECT (
    id, agent_id, source, url, title, summary, content, content_hash,
    published_at, fetched_at, tags
) ON research_items TO trading_data_reader;
GRANT SELECT (
    agent_id, symbol, currency, quantity, avg_entry_price, opened_at, closed_at
) ON positions TO trading_data_reader;
GRANT SELECT (agent_id, cash_eur) ON simulator_accounts TO trading_data_reader;
GRANT SELECT (
    agent_id, period_start, period_end, reward_version, net_return,
    benchmark_return, drawdown_increase_pct, turnover_pct, reward_value, created_at
) ON rewards TO trading_data_reader;

COMMIT;
